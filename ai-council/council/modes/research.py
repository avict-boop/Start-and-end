"""Режим «Исследование»: постановка (рамки, подвопросы, углы зрения) → слепые варианты с проверяемыми
утверждениями → критика и спорные утверждения → проверка утверждений поиском (арбитраж доказательств, проверка
ссылок и цитат) → доработка по реестру → проверка новых утверждений → голосование, в том числе по подвопросам →
синтез «известно / спорно / неизвестно» → теневая сверка смысла.

Запросы к моделям, тексты которых — в council/research.py (там же схемы ответов и проверки программой).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from .. import prompts
from ..client import OutOfCredits, ProviderError
from ..config import MemberConfig
from ..schemas import CritiqueSet, Proposal
from ..session import (
    FACT_CHECKER,
    BudgetExceeded,
    CouncilError,
    InvalidResponse,
    MemberStats,
    SessionResult,
    _critique_notes,
    _is_placeholder,
    _letters,
    _normalize_revision,
    check_substance,
    collect_ideas,
    normalize_ideas,
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

log = logging.getLogger("council.session")


async def run(sess: Council, question: str, *, criteria: list[str] | None, context: str | None,
                        followup_of: str, started: str, t0: float) -> SessionResult:
    from .. import research as R

    rc = sess.cfg.research
    members = sess.cfg.active_members
    chair = sess.cfg.member(sess.cfg.chair)
    rmeta = {"research": True}

    # 0. Постановка: рамки, подвопросы, линзы
    sess._emit("stage", stage="framing", title="Постановка исследования: рамки и подвопросы")
    fr: R.ResearchFraming | None = None
    try:
        fr = await sess._ask(chair, "framing", R.framing(question, context, len(members), sess.cfg.max_requirements),
                             R.ResearchFraming, meta=rmeta)
        sess._input_gaps(fr.input_gaps, chair)
    except (ProviderError, InvalidResponse) as e:
        if isinstance(e, OutOfCredits):
            raise
        sess.stats[chair.name].failures.append(f"framing: {e}")
        sess._emit("member_failed", stage="framing", member=chair.name, error=str(e))
    refined = (fr.refined_question.strip() if fr else "") or question
    if criteria:
        criteria = [c.strip() for c in criteria if c.strip()]
    else:
        criteria = [c.strip() for c in (fr.criteria if fr else []) if c.strip()] or list(R.DEFAULT_CRITERIA)
    sess.requirements = [r.strip() for r in (fr.requirements if fr else []) if r.strip()][:sess.cfg.max_requirements]
    subq = (fr.subquestions if fr else [])[:5] or [refined]
    scope, as_of = (fr.scope if fr else ""), (fr.as_of if fr else "")
    lenses = R.fill_lenses(fr.lenses if fr else [], sess._n_proposers()) if rc.lenses else []
    sess._emit("framing", refined_question=refined, criteria=criteria, requirements=sess.requirements,
               scope=scope, as_of=as_of, subquestions=subq, lenses=lenses)
    req = sess.requirements or None
    objects = (fr.objects if fr else [])[:150]
    if objects and rc.catalog:  # «Каталог»: проверок и утверждений — по числу объектов
        want = len(objects) * rc.facts_per_object
        rc = rc.model_copy(update={"arbitration_claims": max(rc.arbitration_claims, min(100, want)),
                                   "max_claims": max(rc.max_claims, min(80, want + 3))})
    if objects:
        log.info("%sобъектов в задании: %d (охват %s, фактов на объект %d, проверок %d, утверждений у автора %d)",
                 sess._tag, len(objects), "вкл" if rc.object_coverage else "выкл", rc.facts_per_object,
                 rc.arbitration_claims, rc.max_claims)
        sess._emit("objects", count=len(objects), objects=objects, coverage=rc.object_coverage,
                   facts=rc.facts_per_object, checks=rc.arbitration_claims, max_claims=rc.max_claims)
    cover_objects = objects if rc.object_coverage else []
    rhead = R.research_head(refined, question, req, scope, as_of, subq)
    sess.head = rhead  # одинаковое начало всех запросов — его кэширует провайдер

    # 1. Слепые предложения с утверждениями; у каждого автора своя линза
    proposers = [m for m in members if m.name != chair.name] if sess.chair_judge else members
    pool = list(lenses)
    sess.rng.shuffle(pool)
    lens_of = {m.name: (pool[i % len(pool)] if pool else "") for i, m in enumerate(proposers)}
    if pool:
        sess._emit("lenses", by=lens_of)
    scout_pool = [m.name for m in proposers if m.name != sess.cfg.devil_advocate]
    sess.rng.shuffle(scout_pool)
    scouts = set(scout_pool[:rc.fresh_scouts]) | {m.name for m in proposers if m.web_search}  # и у кого включён поиск
    sess._emit("stage", stage="proposal", title="Независимые предложения (без поиска)" if not scouts else
               f"Независимые предложения (с поиском: {', '.join(sorted(scouts))})", chair_proposes=not sess.chair_judge)
    tries: dict[tuple[str, str], int] = {}

    def fit(p: Proposal, stage: str, name: str) -> Proposal:
        """Слишком длинный вариант — на сокращение (если включено), но один раз: второй ответ принимается как
        есть (12.11 gemini на доработке дважды сокращал 3888 → 2704 слов: +$0,4 и 7 минут ожидания всего совета).
        Уже оплаченный ответ из журнала не переспрашивается. Охват объектов задания — тоже один раз."""
        k = (stage, name)
        tries[k] = tries.get(k, 0) + 1
        if not sess._replaying and tries[k] <= min(1, sess.cfg.json_retries):
            if cover_objects:
                R.check_objects(p, cover_objects, rc.facts_per_object, rc.max_claims)
            if rc.shorten_long:
                return R.check_length(p, rc.max_words, rc.long_factor)
        return p

    got = await sess._parallel("proposal", {
        m.name: sess._ask(m, "proposal", R.proposal(rhead, criteria, context, lens_of[m.name], rc.max_words,
                                                    rc.max_claims, web=m.name in scouts),
                          R.ResearchProposal, check=lambda p, n=m.name: fit(check_substance(p), "proposal", n),
                          meta=rmeta, max_tokens=sess.author_tokens(m),
                          web_search=m.name in scouts)
        for m in proposers})
    need = sess._quorum(len(proposers), "research")
    if len(got) < need:
        raise CouncilError(f"нет кворума: ответили {len(got)} из {len(proposers)}, нужно {need}")
    authors: dict[str, str | None] = dict(sess._labels(got))
    proposals: dict[str, Proposal] = {
        l: R.number_claims(l, got[n] if n in scouts else R.mark_memory_urls(got[n], set()))
        for l, n in authors.items() if n}
    initial = dict(proposals)
    label_of = {n: l for l, n in authors.items() if n}
    sess._emit("proposals", proposals=proposals, authors=authors)

    def others(name: str) -> dict[str, Proposal]:
        return {l: p for l, p in proposals.items() if authors[l] != name}

    # 2. Критика: оценки, спорные утверждения, пропущенные подвопросы
    sess._emit("stage", stage="critique", title="Критика и спорные утверждения")
    jobs = {}
    for m in members:
        if sess._sits_out(m, "critique", members):
            continue
        target = sess._shuffled(others(m.name))
        if not target:
            continue
        expected = list(target)
        jobs[m.name] = sess._ask(m, "critique", R.critique(rhead, criteria, target, req), CritiqueSet,
                                 check=lambda cs, e=expected: normalize_critique(cs, e, criteria),
                                 meta={"labels": expected, "criteria": criteria,
                                       "claims": [c.id for l in expected for c in getattr(target[l], "claims", [])],
                                       **rmeta})
    critiques: dict[str, CritiqueSet] = await sess._parallel("critique", jobs)

    # 3. Арбитраж доказательств: поиск по утверждениям всех вариантов, два противоположных задания
    ledger: dict[str, Any] = {}
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    results: list[Any] = []
    if rc.arbitration:
        selected, deferred = R.prepare_claims(  # type: ignore[arg-type]
            proposals, critiques, rc.arbitration_claims, n_subq=len(subq), dedupe=rc.dedupe_claims,
            defer_private=rc.defer_private, coverage_first=rc.coverage_first,
            check_decisive_private=rc.check_decisive_private, objects=objects, facts_per_object=rc.facts_per_object)
        if selected:
            results = await search(sess, R, refined, as_of, scope, selected, rc, "arbitration")
            ledger = make_ledger(sess, R, selected, results, rc, deferred)
            unclear = [c for c in selected for x in ledger["claims"]
                       if x["id"] == c["id"] and x["status"] == "unclear"][:rc.recheck_unclear]
            if unclear:  # второй поиск — другими запросами
                results += await search(sess, R, refined, as_of, scope, unclear, rc, "arbitration", recheck=True)
                ledger = make_ledger(sess, R, selected, results, rc, deferred)
            sess._emit("ledger", ledger=ledger)
        elif deferred:
            ledger = make_ledger(sess, R, [], [], rc, deferred)
    await check_links(sess, R, ledger, rc)
    if sess.inherited:  # сводное заседание серии: проверенное в частях — в общий реестр (ссылки уже проверены там)
        ledger = R.merge_inherited(ledger, sess.inherited)
        sess._emit("ledger", ledger=ledger)
    ledger_txt = R.ledger_text(ledger)

    # 4. Доработка по реестру и выжимкам чужих вариантов
    sess._emit("stage", stage="revision", title="Доработка по реестру доказательств")
    jobs = {}
    # на доработке — по реестру проверенных источников; поиск (дорогой на большом контексте) — только если включён
    rev_scouts = scouts if rc.revision_search else set()
    for m in members:
        own = label_of.get(m.name)
        if own is None:
            continue
        feedback = [{"scores": rv.scores, "strengths": rv.strengths, "weaknesses": rv.weaknesses,
                     "disputed_claims": rv.disputed_claims, "missing_subquestions": rv.missing_subquestions}
                    for reviewer, cs in critiques.items() if reviewer != m.name for rv in cs.reviews if rv.label == own]
        jobs[m.name] = sess._ask(
            m, "revision",
            R.revision(rhead, criteria, own, proposals[own], feedback, others(m.name), ledger_txt,
                       lens_of.get(m.name, ""), rc.max_words, rc.max_claims, full_others=not rc.revision_digest,
                       table_rows=rc.digest_table_rows) + (prompts.WEB_NOTE if m.name in rev_scouts else ""),
            R.ResearchRevision,
            # сокращение слишком длинного варианта до заданной длины — не «потеря содержания»
            check=lambda r, prev=(None if rc.shorten_long and len(proposals[own].summary.split())
                                  > rc.max_words * rc.long_factor and rc.max_words else proposals[own]), n=m.name: fit(
                check_substance(_normalize_revision(r), previous=prev), "revision", n),
            meta={"label": own, **rmeta}, max_tokens=sess.author_tokens(m), web_search=m.name in rev_scouts)
    revised = await sess._parallel("revision", jobs)
    known_urls = {e["url"] for e in (ledger or {}).get("evidence", [])}
    for name, rev in revised.items():
        rev = rev if name in scouts else R.mark_memory_urls(rev, known_urls)
        proposals[label_of[name]] = R.number_claims(label_of[name], rev)
    sess._emit("revisions", revised={label_of[n]: proposals[label_of[n]] for n in revised})
    if rc.arbitration and rc.post_revision_claims and ledger:
        old = {R._claim_key(c["text"])[0] for c in selected}
        fresh = {l: p.model_copy(update={"claims": [c for c in p.claims if R._claim_key(c.text)[0] not in old]})
                 for l, p in proposals.items()}
        new_sel, new_def = R.prepare_claims(fresh, {}, rc.post_revision_claims, n_subq=len(subq),  # type: ignore[arg-type]
                                            dedupe=rc.dedupe_claims, defer_private=rc.defer_private,
                                            coverage_first=rc.coverage_first,
                                            check_decisive_private=rc.check_decisive_private)
        if new_sel:
            sess._emit("stage", stage="recheck", title=f"Проверка новых утверждений после доработки: {len(new_sel)}")
            results += await search(sess, R, refined, as_of, scope, new_sel, rc, "recheck", missions=1)
            selected = selected + new_sel
            deferred = deferred + new_def
            ledger = make_ledger(sess, R, selected, results, rc, deferred)
            await check_links(sess, R, ledger, rc)
            if sess.inherited:
                ledger = R.merge_inherited(ledger, sess.inherited)
            sess._emit("ledger", ledger=ledger)
            ledger_txt = R.ledger_text(ledger)

    # 5. Голосование: общий порядок и лучший вариант по каждому подвопросу
    sess._emit("stage", stage="vote", title="Голосование: каркас по подвопросам")
    jobs = {}
    for m in members:
        if sess._sits_out(m, "vote", members):
            continue
        target = sess._shuffled(others(m.name))
        if not target:
            continue
        expected = list(target)

        def check_ballot(b: R.ResearchBallot, e: list[str] = expected) -> R.ResearchBallot:
            best = {q: lab for q, lab in b.best_by_subquestion.items() if lab in e}
            return b.model_copy(update={"ranking": normalize_ranking(b.ranking, e), "best_by_subquestion": best})

        jobs[m.name] = sess._ask(m, "vote", R.vote(rhead, criteria, target, subq, req, rc.section_vote,
                                                    R.ledger_digest(ledger) if rc.vote_ledger else ""),
                                 R.ResearchBallot, check=check_ballot,
                                 meta={"labels": expected, "subquestions": len(subq), **rmeta})
    ballots = await sess._parallel("vote", jobs)
    rankings = {n: b.ranking for n, b in ballots.items()}
    rows = tally(list(proposals), rankings, critiques)
    agree = agreement(rankings)
    flags = ballot_flags(rows, rankings)
    robust = leader_without(list(proposals), rankings, critiques, set(flags)) if flags else None
    tied = co_leaders(rows)
    decisive = decisive_ballots(list(proposals), rankings, critiques)
    cover = coverage(list(proposals), rankings)
    sections = R.section_tally(subq, ballots, [r.label for r in rows]) if rc.section_vote else {}
    sess._emit("tally", rows=rows, authors=authors, agreement=agree, ballot_flags=flags,
               leader_without_flagged=robust, co_leaders=tied if len(tied) > 1 else [], decisive=decisive,
               coverage=cover, sections=sections)
    ballot_notes = ""
    if flags:
        ballot_notes = "ballots strongly diverging from the others: " + ", ".join(
            f"{n} ({'; '.join(FLAG_NAME[f] for f in fl['flags'])})" for n, fl in flags.items())

    # охват объектов задания проверкой по источникам — в реестр (отчёт и сайт покажут, кого не проверили)
    if objects and ledger is not None:
        ledger["objects"] = R.object_coverage(objects, ledger)
        oc = ledger["objects"]
        log.info("%sохват объектов: %d из %d проверены, подтверждены %d; не проверены: %s", sess._tag, oc["checked"],
                 oc["total"], oc["confirmed"], "; ".join(oc["missing"][:20]) or "—")

    # 6. Синтез: «известно / спорно / неизвестно»
    sess._emit("stage", stage="verdict", title="Синтез исследования")
    leader = rows[0].label if rows else next(iter(proposals))
    valid = list(proposals)
    ideas = collect_ideas(critiques, exclude={leader})
    ideas_text = "\n".join(f"{i['id']} (from {i['label']}): {i['idea']}" for i in ideas)
    idea_ids = [i["id"] for i in ideas]
    checks = [0]
    follow = sess.cfg.chair_follows_vote

    def check_verdict(v: R.ResearchVerdict) -> R.ResearchVerdict:
        # В исследовании «winner» — лишь общий каркас для диагностики (итог собирается по подвопросам),
        # поэтому за него ответ не отклоняется: неверная метка — лидер голосования, иной выбор — с пометкой.
        # Итог исследования длинный и дорогой (часто собран из нескольких продолжений): за поправимое —
        # «известно» без подтверждённого источника, пропущенный подвопрос или идея — его не выбрасываем,
        # а исправляем сами (неподкреплённое уходит в «неизвестно»). Заново спрашиваем только пустой итог.
        checks[0] += 1
        strict = False
        w = normalize_label(v.winner) if v.winner else leader
        if w not in valid:
            w = leader
        deviation = v.deviation.strip()
        if w == leader or (len(tied) > 1 and w in tied):
            deviation = ""
        elif follow and not re.match(r"^\W*(fact|risk|process)\b", deviation, re.I):
            deviation = f"process: председатель взял за общий каркас вариант {w}, а не лидера голосования {leader}" + (
                f" ({deviation})" if deviation else "")
        if _letters(v.final_answer) < 3 or _is_placeholder(v.final_answer):
            raise ValueError('"final_answer" is empty or a placeholder ("...") — write the research synthesis')
        moved = len(v.known)
        v = R.check_verdict(v, ledger=ledger, n_subq=len(subq), strict=strict, mark=rc.mark_unconfirmed,
                            require_claims=rc.require_claim_ids, numbers_check=rc.known_numbers_check,
                            docs_text=sess._docs_text())
        moved -= len(v.known)
        if moved:
            sess._emit("warning", stage="verdict", member=chair.name,
                       text=f"{moved} утв. перенесено из «известно» в «неизвестно»: нет опоры на проверенное "
                            "утверждение или цитату документа (причины — в «неизвестно»)")
        decided = normalize_ideas(v.ideas, idea_ids, strict=strict)
        return v.model_copy(update={"winner": w, "deviation": deviation, "ideas": decided})

    proposals_txt = "\n\n".join(
        R.proposal_block(l, p) + (f"\nChanges after critique: {p.changes}" if getattr(p, "changes", "") else "")
        + (f"\nAdded after the ledger (cannot be known): {'; '.join(p.new_after_ledger)}"
           if getattr(p, "new_after_ledger", None) else "")
        for l, p in proposals.items())
    try:
        verdict = await sess._ask(
            chair, "verdict",
            R.verdict(rhead, criteria, proposals_txt, ledger_txt, R.section_text(subq, sections), tally_text(rows),
                      _critique_notes(critiques), ideas_text, leader, valid,
                      (f"{agree['level']} (mean Kendall tau = {agree['tau']:.2f})") if agree else "", ballot_notes,
                      req, tied if len(tied) > 1 else None, subq, card=rc.decision_card,
                      brief=sess.cfg.answer_size == "brief")
            # проверка спорного при синтезе; найденное — не «известно» (его нет в реестре), новые утверждения итога
            # проверяет «Проверка новых утверждений»
            + (prompts.CHAIR_WEB_NOTE + " Facts you found yourself are not in the ledger: never put them into "
               "\"known\" — keep them in the text, contested or unknown, with the URL." if chair.web_search else ""),
            R.ResearchVerdict, check=check_verdict, max_tokens=sess.verdict_tokens(chair), web_search=chair.web_search,
            meta={"labels": valid, "leader": leader, "ideas": idea_ids, "subquestions": len(subq),
                  "evidence_ok": sorted(R.confirmed_ids(ledger)),
                  "claims_ok": {e: c["id"] for c in reversed((ledger or {}).get("claims", []))  # первое утверждение источника
                                if c.get("status") == "confirmed" for e in c.get("evidence") or []}, **rmeta})
    except (ProviderError, InvalidResponse) as e:
        if isinstance(e, OutOfCredits):
            raise
        sess.stats[chair.name].failures.append(f"verdict: {e}")
        sess._emit("member_failed", stage="verdict", member=chair.name, error=str(e))
        verdict = R.fallback_verdict(leader, proposals, ledger, subq)
    by_id = {d.id: d for d in verdict.ideas}
    for i in ideas:
        d = by_id.get(i["id"])
        i["decision"], i["reason"] = (d.decision, d.reason) if d else ("not_addressed", "")
    sess._emit("verdict", verdict=verdict, ideas=ideas)
    semantic = await semantic_audit(sess, R, chair, verdict, ledger, rc, rmeta)
    return SessionResult(
        question=question, refined_question=refined, criteria=criteria, mode="research", authors=authors,
        initial_proposals=initial, proposals=proposals, critiques=critiques, ballots=ballots, tally=rows,
        verdict=verdict, stats=sess.stats, transcript=sess.transcript, started_at=started,
        duration_s=round(time.monotonic() - t0, 1), attachments=[a.meta() for a in sess.attachments],
        image_notes=dict(sess.image_notes), image_notes_by=sess.image_notes_by, followup_of=followup_of,
        thread=sess.history, agreement=agree, chair_proposed=not sess.chair_judge, ballot_flags=flags,
        leader_without_flagged=robust, requirements=sess.requirements, co_leaders=tied if len(tied) > 1 else [],
        decisive=decisive, coverage=cover, fact_check={}, fact_checker=ledger.get("model", "") if ledger else "",
        ideas=ideas,
        research={"scope": scope, "as_of": as_of, "subquestions": subq, "lenses": lenses,
                  "lens_of": {label_of[n]: lens_of.get(n, "") for n in label_of},
                  "scouts": sorted(label_of[n] for n in scouts if n in label_of),
                  "ledger": ledger, "sections": sections, "settings": rc.model_dump(),
                  **({"semantic_audit": semantic} if semantic else {})},
    )


async def semantic_audit(sess: Council, R: Any, chair: MemberConfig, verdict: Any, ledger: dict[str, Any] | None,
                          rc: Any, rmeta: dict[str, Any]) -> dict[str, Any] | None:
    """Теневая сверка смысла «известно»: следует ли каждый пункт из своей опоры. Только записывается —
    статусы не меняются (пилот: сравнить с ручной проверкой, потом решать, делать ли её фильтром)."""
    known = [k for k in verdict.known if k.item_id]
    if not (rc.semantic_audit and known):
        return None
    member = chair.model_copy(update={"model": rc.semantic_audit_model or chair.model,
                                      "max_tokens": max(4000, sess.cfg.max_tokens or 0),
                                      "reasoning_effort": "none"})
    try:
        got = await sess._ask(member, "semantic", R.semantic_audit(R.semantic_items(known, ledger)), R.SemanticAudit,
                              meta={"items": [k.item_id for k in known], **rmeta})
    except (ProviderError, InvalidResponse) as e:
        if isinstance(e, OutOfCredits):
            raise
        log.warning("смысловая сверка не удалась: %s", e)
        return None
    by = {i.item_id: i.model_dump() for i in got.items if i.item_id in {k.item_id for k in known}}
    flagged = [i for i in by.values() if i["status"] != "supported"]
    if flagged:
        sess._emit("warning", stage="verdict", member=member.name,
                   text=f"смысловая сверка: {len(flagged)} из {len(known)} пунктов «известно» не следуют из опоры полностью "
                        "(только для сведения — статус не менялся)")
    return {"model": member.model, "items": by, "flagged": len(flagged), "checked": len(known)}


def make_ledger(sess: Council, R: Any, selected: list[dict[str, Any]], results: list[Any], rc: Any,
            deferred: list[dict[str, Any]]) -> dict[str, Any]:
    fc = sess.cfg.fact_check
    if not results:
        return {"model": fc.model, "missions": 0, "evidence": [], "gaps": [], "deferred": deferred,
                "claims": [{**c, "status": "search_unavailable" if selected else "not_checked", "notes": [],
                            "evidence": []} for c in selected]}
    return R.build_ledger(selected, results, fc.model, snippet_rule=rc.snippet_rule, deferred=deferred)


async def check_links(sess: Council, R: Any, ledger: dict[str, Any], rc: Any) -> None:
    """Открыть каждую ссылку реестра: мёртвая не подтверждает (бесплатно, модели не нужны)."""
    if not (ledger and ledger.get("evidence") and rc.link_check and sess.link_checker):
        return
    todo = [e["url"] for e in ledger["evidence"] if e.get("url") and not e.get("link")]
    if todo:  # без моделей, но может занять до пары минут — видно на сайте и в журнале
        sess._emit("links_check", count=len(todo))
    quotes = {e["url"]: list(e["quotes"].values()) for e in ledger["evidence"]
              if e.get("url") in todo and e.get("quotes")} if rc.quote_check else {}
    try:
        if not todo:
            found = {}
        elif quotes:
            try:
                found = await sess.link_checker(todo, quotes=quotes)
            except TypeError:  # проверка ссылок без поиска цитат (старые и тестовые реализации)
                found = await sess.link_checker(todo)
        else:
            found = await sess.link_checker(todo)
    except Exception as e:  # noqa: BLE001 — проверка ссылок не должна ронять исследование
        log.warning("проверка ссылок не удалась: %s", e)
        found = {}
    R.apply_links(ledger, found, quote_check=rc.quote_check)
    if ledger.get("links"):
        sess._emit("links", **ledger["links"])


async def read_leads(sess: Council, R: Any, selected: list[dict[str, Any]]) -> dict[str, list[tuple[str, str]]]:
    """Страницы, на которые сослались авторы: программа скачивает их сама (HTML и PDF) и отдаёт модели проверки
    отрывки по каждому утверждению — её поиск часто не находит сайты компаний на местных языках."""
    fc = sess.cfg.fact_check
    if not (fc.read_leads and fc.lead_pages and sess.link_checker):
        return {}
    want = {c["id"]: R.claim_urls(c, fc.lead_pages) for c in selected}
    urls = list(dict.fromkeys(u for us in want.values() for u in us))
    if not urls:
        return {}
    cache = sess.page_cache
    todo = [u for u in urls if u not in cache]
    if todo:
        sess._emit("links_check", count=len(todo))
        try:
            got = await sess.link_checker(todo, texts=True)
        except TypeError:  # проверка ссылок без чтения текста (старый вариант, тесты)
            got = {}
        except Exception as e:  # noqa: BLE001 — не прочитали страницы: проверка идёт как раньше, поиском
            log.warning("%sстраницы авторов не скачались: %s", sess._tag, e)
            got = {}
        for u, r in (got or {}).items():
            cache[u] = (r or {}).get("text") or ""
    pages: dict[str, list[tuple[str, str]]] = {}
    for c in selected:
        for u in want[c["id"]]:
            ex = R.page_excerpt(cache.get(u, ""), c["text"], c.get("entity") or "")
            if ex:
                pages.setdefault(c["id"], []).append((u, ex))
    read = sum(1 for u in urls if cache.get(u))
    log.info("%sссылки авторов: открыто %d из %d, отрывки для %d утверждений", sess._tag, read, len(urls), len(pages))
    sess._emit("leads_read", urls=len(urls), read=read, claims=len(pages))
    return pages


async def search(sess: Council, R: Any, question: str, as_of: str, scope: str, selected: list[dict[str, Any]], rc: Any,
                  stage: str, *, recheck: bool = False, missions: int | None = None) -> list[Any]:
    """Поисковые вызовы по пачкам утверждений: подтвердить и опровергнуть (второй поиск — другими запросами).
    Сбой поиска не прерывает исследование."""
    fc = sess.cfg.fact_check
    checker = MemberConfig(name=FACT_CHECKER, model=fc.model, max_tokens=max(fc.max_tokens, sess.cfg.max_tokens or 0),
                           temperature=0.1, reasoning_effort="none")
    sess.stats.setdefault(FACT_CHECKER, MemberStats())
    if stage == "arbitration" and not recheck:
        sess._emit("stage", stage="arbitration",
                   title=f"Арбитраж доказательств: {len(selected)} утверждений, поиск {fc.model}",
                   claims=[c["id"] for c in selected], model=fc.model)
    size = R.ARBITRATION_BATCH
    chunks = [selected[k:k + size] for k in range(0, len(selected), size)]
    per_chunk = max(5, rc.max_evidence // max(1, len(chunks)))
    n_missions = 1 if recheck else (missions or rc.missions)
    plan = [(i, chunk) for chunk in chunks for i in range(n_missions)]
    snippet = rc.snippet_rule != "off"
    pages = await read_leads(sess, R, selected)
    jobs = [sess._ask(checker, stage, R.arbitration(question, as_of, scope, R.MISSIONS[i], R.claims_batch(chunk, pages),
                                                    per_chunk, snippet=snippet, recheck=recheck,
                                                    fetch=fc.fetch_pages),
                      R.EvidenceResult, meta={"mission": i + 1, "claims": [c["id"] for c in chunk], "research": True,
                                              "recheck": recheck},
                      web_search=True, web_fetch=fc.fetch_pages)
            for i, chunk in plan]
    found = await asyncio.gather(*jobs, return_exceptions=True)
    ok = []
    for i, res in enumerate(found, 1):
        if isinstance(res, (BudgetExceeded, OutOfCredits)):
            raise res
        if isinstance(res, BaseException):
            sess.stats[FACT_CHECKER].failures.append(f"{stage}: задание {i}: {res}")
            sess._emit("member_failed", stage=stage, member=f"{FACT_CHECKER} (задание {i})", error=str(res))
            continue
        ok.append(res)
    return ok
