"""Общие этапы «классических» режимов — подробного обсуждения, быстрого ответа и выбора из вариантов.

Каждый этап — функция, которой передаётся заседание (Council): через него идут запросы к моделям, учёт денег,
события для сайта и бота, журнал для паузы. Порядок этапов задаёт файл режима (debate.py, quick.py,
evaluate.py); исследование устроено иначе — research.py.
"""
from __future__ import annotations

import asyncio
import re
import string
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .. import prompts
from ..client import OutOfCredits, ProviderError
from ..config import MemberConfig
from ..schemas import (
    Ballot,
    CritiqueSet,
    FactCheck,
    Framing,
    Proposal,
    Revision,
    Verdict,
)
from ..session import (
    FACT_CHECKER,
    BudgetExceeded,
    CouncilError,
    InvalidResponse,
    SessionResult,
    _critique_notes,
    _is_placeholder,
    _letters,
    _normalize_revision,
    check_substance,
    normalize_ideas,
    normalize_status,
)
from ..voting import (
    FLAG_NAME,
    agreement,
    ballot_flags,
    co_leaders,
    coverage,
    decisive_ballots,
    leader_without,
    normalize_critique,
    normalize_label,
    normalize_ranking,
    tally,
    tally_text,
)

if TYPE_CHECKING:
    from ..session import Council


@dataclass
class Board:
    """Что известно о заседании по ходу этапов: вопрос, варианты, авторы, критика, голоса."""

    question: str
    refined: str
    criteria: list[str]
    req: list[str] | None
    context: str | None
    mode: str
    authors: dict[str, str | None] = field(default_factory=dict)
    proposals: dict[str, Proposal] = field(default_factory=dict)
    initial: dict[str, Proposal] = field(default_factory=dict)
    critiques: dict[str, CritiqueSet] = field(default_factory=dict)
    ballots: dict[str, Ballot] = field(default_factory=dict)

    @property
    def label_of(self) -> dict[str, str]:
        return {name: l for l, name in self.authors.items() if name}

    def others(self, name: str) -> dict[str, Proposal]:
        """Чужие варианты: свой участник не видит в списке на оценку."""
        return {l: p for l, p in self.proposals.items() if self.authors[l] != name}


async def frame(c: Council, question: str, criteria: list[str] | None, context: str | None,
                mode: str) -> Board:
    """0. Постановка: уточнённый вопрос, критерии, обязательные требования (если критерии не заданы)."""
    chair = c.cfg.member(c.cfg.chair)
    refined = question
    if criteria:
        criteria = [x.strip() for x in criteria if x.strip()]
    else:
        c._emit("stage", stage="framing", title="Постановка задачи и критерии")
        try:
            framing = await c._ask(chair, "framing", prompts.framing(question, context, c.cfg.max_requirements), Framing)
            refined = framing.refined_question.strip() or question
            criteria = [x.strip() for x in framing.criteria if x.strip()]
            c.requirements = [r.strip() for r in framing.requirements if r.strip()][:c.cfg.max_requirements]
            c._input_gaps(framing.input_gaps, chair)
        except (ProviderError, InvalidResponse) as e:
            if isinstance(e, OutOfCredits):
                raise
            c.stats[chair.name].failures.append(f"framing: {e}")
            c._emit("member_failed", stage="framing", member=chair.name, error=str(e))
            criteria = []
        criteria = criteria or ["Эффективность", "Реалистичность", "Риски"]
    c._emit("framing", refined_question=refined, criteria=criteria, requirements=c.requirements)
    c.head = prompts.head(refined, question, c.requirements)
    return Board(question=question, refined=refined, criteria=criteria, req=c.requirements or None,
                 context=context, mode=mode)


async def propose(c: Council, b: Board) -> None:
    """1–2. Независимые предложения вслепую и анонимные метки A, B, C…"""
    members = c.cfg.active_members
    chair = c.cfg.member(c.cfg.chair)
    # Председатель по умолчанию не предлагает свой вариант (нет конфликта «автор и судья»),
    # если без него хватает участников для кворума.
    proposers = [m for m in members if m.name != chair.name] if c.chair_judge else members
    c._emit("stage", stage="proposal", title="Независимые предложения", chair_proposes=not c.chair_judge)
    got = await c._parallel(
        "proposal",
        {m.name: c._ask(m, "proposal", prompts.proposal(b.refined, b.criteria, b.context, b.question, c.compact, req=b.req)
                        + (prompts.WEB_NOTE if m.web_search else ""), Proposal,
                        check=lambda p: check_substance(p, compact=bool(c.compact)),
                        web_search=m.web_search)
         for m in proposers},
    )
    need = c._quorum(len(proposers), b.mode)
    if len(got) < need:
        raise CouncilError(f"нет кворума: ответили {len(got)} из {len(proposers)}, нужно {need}")
    labels = c._labels(got)
    b.authors = dict(labels)
    b.proposals = {l: got[name] for l, name in labels.items()}
    b.initial = dict(b.proposals)
    c._emit("proposals", proposals=b.proposals, authors=b.authors)


def user_options(c: Council, b: Board, options: list[str]) -> None:
    """Варианты пользователя («Выбор из вариантов») вместо предложений моделей."""
    options = [o for o in options if o.strip()]
    if not 2 <= len(options) <= 26:
        raise CouncilError("для оценки нужно от 2 до 26 вариантов")
    b.authors = {string.ascii_uppercase[i]: None for i in range(len(options))}
    b.proposals = {l: Proposal(title=opt.strip().splitlines()[0][:80], summary=opt.strip())
                   for l, opt in zip(b.authors, options)}
    b.initial = dict(b.proposals)
    c._emit("proposals", proposals=b.proposals, authors=b.authors)


async def critique(c: Council, b: Board, title: str) -> None:
    """3. Анонимная критика чужих вариантов (у каждого судьи свой порядок — гасит эффект позиции)."""
    members = c.cfg.active_members
    c._emit("stage", stage="critique", title=title)
    jobs = {}
    for m in members:
        if c._sits_out(m, "critique", members):
            continue
        target = c._shuffled(b.others(m.name))
        if not target:
            continue
        expected = list(target)
        jobs[m.name] = c._ask(
            m, "critique",
            prompts.critique(b.refined, b.criteria, target, b.question, bool(c.compact), req=b.req),
            CritiqueSet,
            check=lambda cs, e=expected: normalize_critique(cs, e, b.criteria),
            meta={"labels": expected, "criteria": b.criteria},
        )
    b.critiques = await c._parallel("critique", jobs)


async def revise(c: Council, b: Board, title: str) -> None:
    """4. Доработка: каждый автор улучшает свой вариант по критике и чужим вариантам."""
    members = c.cfg.active_members
    c._emit("stage", stage="revision", title=title)
    label_of = b.label_of
    jobs = {}
    for m in members:
        own = label_of.get(m.name)
        if own is None:
            continue
        feedback = [{"scores": rv.scores, "strengths": rv.strengths, "weaknesses": rv.weaknesses}
                    for reviewer, cs in b.critiques.items() if reviewer != m.name
                    for rv in cs.reviews if rv.label == own]
        jobs[m.name] = c._ask(
            m, "revision",
            prompts.revision(b.refined, b.criteria, own, b.proposals[own], feedback, b.others(m.name), b.question,
                             c.compact, req=b.req) + (prompts.WEB_NOTE if m.web_search else ""),
            Revision,
            web_search=m.web_search,
            check=lambda r, prev=b.proposals[own]: check_substance(_normalize_revision(r), previous=prev,
                                                                   compact=bool(c.compact)),
            meta={"label": own},
        )
    revised = await c._parallel("revision", jobs)
    for name, rev in revised.items():
        b.proposals[label_of[name]] = rev
    c._emit("revisions", revised={label_of[n]: p for n, p in revised.items()})


@dataclass
class Count:
    """Итоги голосования: таблица, согласие, спорные бюллетени, ничья лидеров, решающие голоса."""

    rows: list[Any]
    agree: dict[str, Any] | None
    flags: dict[str, Any]
    robust: str | None
    tied: list[str]
    decisive: dict[str, str]
    cover: dict[str, Any]
    notes: str = ""

    @property
    def leader(self) -> str:
        return self.rows[0].label


def count(c: Council, b: Board, rankings: dict[str, list[str]], *, decisive_notes: bool = True,
          **extra: Any) -> Count:
    """Подсчёт голосов и диагностика бюллетеней; событие «tally» для сайта и бота."""
    labels = list(b.proposals)
    rows = tally(labels, rankings, b.critiques)
    flags = ballot_flags(rows, rankings)
    k = Count(rows=rows, agree=agreement(rankings), flags=flags,
              robust=leader_without(labels, rankings, b.critiques, set(flags)) if flags else None,
              tied=co_leaders(rows), decisive=decisive_ballots(labels, rankings, b.critiques),
              cover=coverage(labels, rankings))
    c._emit("tally", rows=rows, authors=b.authors, agreement=k.agree, ballot_flags=flags,
            leader_without_flagged=k.robust, co_leaders=k.tied if len(k.tied) > 1 else [], decisive=k.decisive,
            coverage=k.cover, **extra)
    if decisive_notes and k.decisive:
        k.notes += ("decisive ballots (the leader changes without one of them): "
                    + ", ".join(f"without {v} the leader would be {alt}" for v, alt in k.decisive.items()) + ". ")
    if flags:
        parts = [f"{n} ({'; '.join(FLAG_NAME[f] for f in fl['flags'])})" for n, fl in flags.items()]
        if decisive_notes:
            k.notes += ("ballots strongly diverging from the others' consensus: " + ", ".join(parts)
                        + (f". Without them the leader would be {k.robust}" if k.robust and k.robust != rows[0].label
                           else ". Without them the leader stays the same"))
        else:
            k.notes = "ballots strongly diverging from the others: " + ", ".join(parts)
    return k


async def vote(c: Council, b: Board, *, keep: bool = False) -> Count:
    """5. Голосование: каждый ранжирует чужие варианты (за свой голосовать нельзя)."""
    members = c.cfg.active_members
    c._emit("stage", stage="vote", title="Голосование")
    jobs = {}
    for m in members:
        if c._sits_out(m, "vote", members):
            continue
        target = c._shuffled(b.others(m.name))
        if not target:
            continue
        expected = list(target)

        def check_ballot(bl: Ballot, e: list[str] = expected) -> Ballot:
            return bl.model_copy(update={"ranking": normalize_ranking(bl.ranking, e)})

        jobs[m.name] = c._ask(m, "vote",
                              prompts.vote(b.refined, b.criteria, target, b.question, bool(c.compact), req=b.req,
                                           keep=keep),
                              Ballot, check=check_ballot, meta={"labels": expected})
    b.ballots = await c._parallel("vote", jobs)
    return count(c, b, {n: bl.ranking for n, bl in b.ballots.items()})


async def fact_check(c: Council, question: str, proposals: dict[str, Proposal], rows: list[Any]) -> dict[str, dict]:
    """5½. Проверка ключевых фактов лидеров голосования моделью с поиском (если включена). Сбой не прерывает."""
    fc = c.cfg.fact_check
    if not fc.enabled or not rows:
        return {}
    targets = [r.label for r in rows[:fc.top]]
    checker = MemberConfig(name=FACT_CHECKER, model=fc.model, max_tokens=fc.max_tokens, temperature=0.1,
                           reasoning_effort="none")
    c._emit("stage", stage="factcheck", title=f"Проверка фактов: варианты {', '.join(targets)}",
            targets=targets, model=fc.model)

    def check(r: FactCheck) -> FactCheck:
        return r.model_copy(update={"claims": [x.model_copy(update={"status": normalize_status(x.status)})
                                               for x in r.claims if x.claim.strip()][:fc.max_claims]})

    jobs = [c._ask(checker, "factcheck", prompts.factcheck(question, l, proposals[l], fc.max_claims), FactCheck,
                   check=check, meta={"label": l}) for l in targets]
    results = await asyncio.gather(*jobs, return_exceptions=True)
    out: dict[str, dict] = {}
    for label, res in zip(targets, results):
        if isinstance(res, (BudgetExceeded, OutOfCredits)):
            raise res
        if isinstance(res, BaseException):
            c.stats[FACT_CHECKER].failures.append(f"factcheck: {label}: {res}")
            c._emit("member_failed", stage="factcheck", member=f"{FACT_CHECKER} ({label})", error=str(res))
            continue
        out[label] = res.model_dump()
    c._emit("factcheck", results=out, model=fc.model)
    return out


async def verdict(c: Council, b: Board, k: Count, facts: dict[str, dict], ideas: list[dict[str, Any]], *,
                  allow_none: bool = False, from_voters: bool = False) -> Verdict:
    """6. Итог председателя: победитель (по голосованию, иной — только с причиной) и полный ответ."""
    chair = c.cfg.member(c.cfg.chair)
    c._emit("stage", stage="verdict", title="Итоговое решение председателя")
    valid = list(b.proposals)
    leader = k.leader
    follow = c.cfg.chair_follows_vote
    ideas_text = "\n".join(f"{i['id']} (from {i['label']}): {i['idea']}" for i in ideas)
    idea_ids = [i["id"] for i in ideas]
    checks = [0]

    def check_verdict(v: Verdict) -> Verdict:
        w = normalize_label(v.winner)
        if w not in valid:
            raise ValueError(f"winner must be one of the labels {valid}, got '{v.winner}'")
        deviation = v.deviation.strip()
        if w == leader or (len(k.tied) > 1 and w in k.tied):
            deviation = ""  # лидер или один из разделивших первое место — это не отступление
        elif follow and not re.match(r"^\W*(fact|risk|process)\b", deviation, re.I):
            raise ValueError(
                f"the winner must be the voting leader {leader}; choosing {w} requires 'deviation' that starts with "
                f"fact, risk or process and explains the specific reason, otherwise set winner to {leader}")
        if _letters(v.final_answer) < 3 or _is_placeholder(v.final_answer):
            raise ValueError('"final_answer" is empty or a placeholder ("...") — write the complete final answer')
        checks[0] += 1
        decided = normalize_ideas(v.ideas, idea_ids, strict=checks[0] <= c.cfg.json_retries)
        return v.model_copy(update={"winner": w, "deviation": deviation, "ideas": decided})

    try:
        v = await c._ask(
            chair, "verdict",
            prompts.verdict(b.refined, b.criteria, b.proposals, tally_text(k.rows), _critique_notes(b.critiques),
                            b.question,
                            (f"{k.agree['level']} (mean Kendall tau between ballots = {k.agree['tau']:.2f}; "
                             "1 = identical rankings, 0 = unrelated)") if k.agree else "",
                            prompts.factcheck_block(facts), k.notes, leader=leader, follow_vote=follow,
                            compact=bool(c.compact), req=b.req, co_leaders=k.tied if len(k.tied) > 1 else None,
                            ideas=ideas_text, allow_none=allow_none, from_voters=from_voters,
                            brief=c.cfg.answer_size == "brief"),
            Verdict, check=check_verdict, max_tokens=c.verdict_tokens(chair),
            meta={"labels": valid, "leader": leader, "ideas": idea_ids},
        )
    except (ProviderError, InvalidResponse) as e:
        if isinstance(e, OutOfCredits):
            raise
        # Председатель недоступен — итог по результатам голосования без синтеза.
        c.stats[chair.name].failures.append(f"verdict: {e}")
        c._emit("member_failed", stage="verdict", member=chair.name, error=str(e))
        top = b.proposals[leader]
        v = Verdict(winner=leader, final_answer=top.summary,
                    why="Председатель не ответил; победитель определён по голосованию.", risks=top.risks)
    decide_ideas(c, v, ideas)
    return v


def decide_ideas(c: Council, v: Any, ideas: list[dict[str, Any]]) -> None:
    """Решение председателя по каждой идее «стоит сохранить» — в список идей; событие «verdict»."""
    by_id = {d.id: d for d in v.ideas}
    for i in ideas:
        d = by_id.get(i["id"])
        i["decision"], i["reason"] = (d.decision, d.reason) if d else ("not_addressed", "")
    c._emit("verdict", verdict=v, ideas=ideas)


def result(c: Council, b: Board, k: Count, v: Any, ideas: list[dict[str, Any]], *, facts: dict[str, dict],
           started: str, t0: float, followup_of: str, **extra: Any) -> SessionResult:
    """Итог заседания для отчётов, архива и статистики."""
    return SessionResult(
        question=b.question, refined_question=b.refined, criteria=b.criteria, mode=b.mode, authors=b.authors,
        initial_proposals=b.initial, proposals=b.proposals, critiques=b.critiques, ballots=b.ballots, tally=k.rows,
        verdict=v, stats=c.stats, transcript=c.transcript, started_at=started,
        duration_s=round(time.monotonic() - t0, 1), attachments=[a.meta() for a in c.attachments],
        image_notes=dict(c.image_notes), image_notes_by=c.image_notes_by, followup_of=followup_of,
        thread=c.history, agreement=k.agree, chair_proposed=not c.chair_judge and b.mode != "evaluate",
        ballot_flags=k.flags, leader_without_flagged=k.robust, requirements=c.requirements,
        co_leaders=k.tied if len(k.tied) > 1 else [], decisive=k.decisive, coverage=k.cover,
        fact_check=facts, fact_checker=c.cfg.fact_check.model if facts else "", ideas=ideas, **extra,
    )
