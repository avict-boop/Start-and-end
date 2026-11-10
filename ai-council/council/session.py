"""Оркестратор заседания совета: этапы 0–6, кворум, бюджет, стенограмма."""
from __future__ import annotations

import asyncio
import random
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel

from . import prompts
from .client import Completion, LLMClient, ProviderError
from .config import CouncilConfig, MemberConfig, Mode
from .attachments import Attachment, documents_block, image_parts, images_note
from .schemas import Ballot, CritiqueSet, Framing, ImageNotes, Proposal, Revision, Verdict
from .voting import (
    TallyRow,
    assign_labels,
    extract_json,
    normalize_critique,
    normalize_label,
    normalize_ranking,
    tally,
    tally_text,
)

T = TypeVar("T", bound=BaseModel)
EventHandler = Callable[..., None]


class CouncilError(RuntimeError):
    pass


class BudgetExceeded(CouncilError):
    pass


class InvalidResponse(CouncilError):
    pass


@dataclass
class MemberStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    failures: list[str] = field(default_factory=list)


@dataclass
class SessionResult:
    question: str
    refined_question: str
    criteria: list[str]
    mode: Mode
    authors: dict[str, str | None]  # label -> имя участника (None для вариантов пользователя)
    initial_proposals: dict[str, Proposal]
    proposals: dict[str, Proposal]  # финальные версии
    critiques: dict[str, CritiqueSet]  # последний раунд: рецензент -> отзывы
    ballots: dict[str, Ballot]
    tally: list[TallyRow]
    verdict: Verdict
    stats: dict[str, MemberStats]
    transcript: list[dict[str, Any]]
    started_at: str
    duration_s: float
    attachments: list[dict[str, Any]] = field(default_factory=list)  # описания вложений (без содержимого)
    image_notes: dict[str, str] = field(default_factory=dict)         # картинка -> текстовое описание
    image_notes_by: str = ""

    @property
    def total_cost(self) -> float:
        return sum(s.cost for s in self.stats.values())

    def to_dict(self) -> dict[str, Any]:
        dump = lambda d: {k: v.model_dump() for k, v in d.items()}  # noqa: E731
        return {
            "question": self.question,
            "refined_question": self.refined_question,
            "criteria": self.criteria,
            "mode": self.mode,
            "authors": self.authors,
            "initial_proposals": dump(self.initial_proposals),
            "proposals": dump(self.proposals),
            "critiques": dump(self.critiques),
            "ballots": dump(self.ballots),
            "tally": [r.__dict__ for r in self.tally],
            "verdict": self.verdict.model_dump(),
            "stats": {k: v.__dict__ for k, v in self.stats.items()},
            "total_cost_usd": self.total_cost,
            "started_at": self.started_at,
            "duration_s": self.duration_s,
            "attachments": self.attachments,
            "image_notes": self.image_notes,
            "image_notes_by": self.image_notes_by,
            "transcript": self.transcript,
        }


class Council:
    def __init__(
        self,
        config: CouncilConfig,
        client: LLMClient,
        *,
        on_event: EventHandler | None = None,
        seed: int | None = None,
        vision: dict[str, bool | None] | None = None,
    ) -> None:
        """vision: модель -> умеет ли принимать картинки (из каталога OpenRouter); неизвестно — пробуем."""
        self.cfg = config
        self.client = client
        self.on_event = on_event or (lambda *a, **k: None)
        self.rng = random.Random(seed)
        self._sem = asyncio.Semaphore(config.max_concurrency)
        self.stats: dict[str, MemberStats] = {m.name: MemberStats() for m in config.active_members}
        self.transcript: list[dict[str, Any]] = []
        self._last_call_emit: dict[tuple[str, str], tuple[str, float]] = {}
        self.vision = vision or {}
        self.attachments: list[Attachment] = []
        self.image_notes: dict[str, str] = {}
        self.image_notes_by = ""

    # ------------------------------------------------------------------ helpers

    @property
    def total_cost(self) -> float:
        return sum(s.cost for s in self.stats.values())

    def _emit(self, event: str, **data: Any) -> None:
        try:
            self.on_event(event, **data)
        except Exception:  # обработчик интерфейса не должен ронять заседание
            pass

    def _call(self, member: str, stage: str, phase: str, started: float, **data: Any) -> None:
        """Событие о ходе одного обращения к модели (для живого индикатора в интерфейсах).

        Фазы: queued → sent → thinking → writing → checking → done | retry | failed.
        Частые обновления «думает/пишет» прореживаются, смена фазы отправляется сразу.
        """
        now = time.monotonic()
        key = (member, stage)
        last = self._last_call_emit.get(key)
        if last and last[0] == phase and phase in ("thinking", "writing") and now - last[1] < 0.8:
            return
        self._last_call_emit[key] = (phase, now)
        self._emit("call", member=member, stage=stage, phase=phase, elapsed=round(now - started, 1), **data)

    def _account(self, member: MemberConfig, comp: Completion) -> None:
        s = self.stats[member.name]
        s.calls += 1
        s.prompt_tokens += comp.prompt_tokens
        s.completion_tokens += comp.completion_tokens
        s.cost += comp.cost
        self._emit("cost", member=member.name, total=self.total_cost)

    async def _ask(
        self,
        member: MemberConfig,
        stage: str,
        user_prompt: str,
        schema: type[T],
        *,
        check: Callable[[T], T] | None = None,
        meta: dict[str, Any] | None = None,
        content: list[dict[str, Any]] | None = None,
    ) -> T:
        system = prompts.system(
            member.name,
            self.cfg.language,
            devil=member.name == self.cfg.devil_advocate and stage in {"proposal", "critique", "revision"},
            chair=member.name == self.cfg.chair and stage in {"framing", "verdict"},
        )
        base_messages = [{"role": "system", "content": system},
                         {"role": "user", "content": content or self._with_attachments(member, stage, user_prompt)}]
        messages = list(base_messages)
        meta = {"stage": stage, "member": member.name, **(meta or {})}
        max_tokens = member.max_tokens or self.cfg.max_tokens
        effort = member.reasoning_effort or self.cfg.reasoning_effort
        error = ""
        started = time.monotonic()
        name = member.name

        def progress(ev: dict[str, Any]) -> None:
            ev = dict(ev)
            phase = ev.pop("phase", "")
            if phase == "sent" and ev.get("attempt", 1) > 1:
                ev["detail"] = f"попытка {ev['attempt']}"
            self._call(name, stage, phase, started, **ev)

        for attempt in range(self.cfg.json_retries + 1):
            if self.total_cost >= self.cfg.max_cost_usd:
                raise BudgetExceeded(
                    f"достигнут лимит бюджета ${self.cfg.max_cost_usd:.2f} (потрачено ${self.total_cost:.4f})"
                )
            if self._sem.locked():
                self._call(name, stage, "queued", started, detail="ждёт свободного слота")
            try:
                async with self._sem:
                    comp = await self.client.complete(
                        member.model,
                        messages,
                        temperature=member.temperature if member.temperature is not None else self.cfg.temperature,
                        max_tokens=max_tokens,
                        reasoning_effort=None if effort == "none" else effort,
                        meta=meta,
                        on_progress=progress,
                    )
            except ProviderError as e:
                if e.spent is not None:  # неудачные попытки тоже стоят денег
                    self._account(member, e.spent)
                self._call(name, stage, "failed", started, detail=str(e)[:300])
                raise
            self._account(member, comp)
            self._call(name, stage, "checking", started, tokens=comp.completion_tokens)
            max_tokens = max(max_tokens, comp.max_tokens or 0)  # клиент мог уже увеличить лимит
            entry = {
                "stage": stage,
                "member": member.name,
                "model": comp.model or member.model,
                "attempt": attempt + 1,
                "raw": comp.text,
                "cost": comp.cost,
                "tokens": [comp.prompt_tokens, comp.completion_tokens],
            }
            try:
                obj = schema.model_validate(extract_json(comp.text))
                if check:
                    obj = check(obj)
            except ValueError as e:  # pydantic.ValidationError тоже ValueError
                error = str(e).splitlines()[0][:500] if str(e) else repr(e)
                if comp.finish_reason in ("length", "max_tokens", "MAX_TOKENS"):
                    # Ответ обрезан по лимиту: повторяем тот же запрос с вдвое большим лимитом,
                    # не пересылая обрывок (он только тратит токены).
                    error = f"ответ обрезан на лимите {max_tokens} токенов ({error})"
                    max_tokens = min(max_tokens * 2, self.cfg.max_tokens_cap)
                    messages = list(base_messages)
                    hint = f"ответ обрезан — повтор с лимитом {max_tokens} токенов"
                else:
                    messages += [
                        {"role": "assistant", "content": comp.text},
                        {"role": "user", "content": prompts.fix_json(error)},
                    ]
                    hint = "ответ не в нужном формате — прошу модель исправить"
                if attempt < self.cfg.json_retries:
                    self._call(name, stage, "retry", started, detail=hint)
                entry["error"] = error
                entry["finish_reason"] = comp.finish_reason
                self.transcript.append(entry)
                continue
            entry["parsed"] = obj.model_dump()
            self.transcript.append(entry)
            self._call(name, stage, "done", started, cost=round(self.stats[name].cost, 6))
            return obj
        self._call(name, stage, "failed", started, detail=f"некорректный ответ: {error}"[:300])
        raise InvalidResponse(f"{member.name}: некорректный ответ после {self.cfg.json_retries + 1} попыток: {error}")

    ATTACHMENT_STAGES = {"framing", "proposal", "critique", "revision", "verdict"}

    def can_see(self, member: MemberConfig) -> bool:
        return self.vision.get(member.model) is not False

    def _with_attachments(self, member: MemberConfig, stage: str, prompt: str) -> str | list[dict[str, Any]]:
        """Добавляет к запросу вложения: текст документов и картинки (или их описание для «незрячих»)."""
        if not self.attachments or stage not in self.ATTACHMENT_STAGES:
            return prompt
        sees = self.can_see(member)
        extra = [x for x in (documents_block(self.attachments),
                             images_note(self.attachments, self.image_notes, sees)) if x]
        text = "\n\n".join(extra + [prompt])
        parts = image_parts(self.attachments) if sees else []
        if not parts:
            return text
        return [{"type": "text", "text": text}, *parts]

    async def _describe_images(self, members: list[MemberConfig], question: str) -> None:
        """Если среди участников есть модели без зрения — одна из «зрячих» описывает картинки текстом."""
        images = [a for a in self.attachments if a.kind == "image"]
        if not images or all(self.can_see(m) for m in members):
            return
        seers = [m for m in members if self.can_see(m)]
        chair = next((m for m in seers if m.name == self.cfg.chair), None)
        describer = chair or (seers[0] if seers else None)
        if describer is None:
            self._emit("member_failed", stage="describe", member="—",
                       error="ни одна модель совета не умеет смотреть на изображения — картинки будут пропущены")
            return
        self._emit("stage", stage="describe", title="Описание изображений для моделей без зрения")
        names = [a.name for a in images]
        try:
            # картинки передаём напрямую: этап describe не входит в ATTACHMENT_STAGES
            notes = await self._ask(describer, "describe", "", ImageNotes,
                                    meta={"images": names}, content=[{"type": "text", "text": prompts.describe_images(question, names)},
                                                                     *image_parts(images)])
        except (ProviderError, InvalidResponse) as e:
            self._emit("member_failed", stage="describe", member=describer.name, error=str(e))
            return
        by_name = {n.name: n.description for n in notes.images}
        for i, a in enumerate(images):  # модель могла переименовать — сопоставляем по порядку
            self.image_notes[a.name] = by_name.get(a.name) or (notes.images[i].description if i < len(notes.images) else "")
        self.image_notes_by = describer.name
        self._emit("image_notes", by=describer.name, notes=self.image_notes)

    async def _parallel(self, stage: str, jobs: dict[str, Awaitable[T]]) -> dict[str, T]:
        """Запускает запросы параллельно; упавшие участники выбывают из этапа, бюджет — останавливает всё."""
        names = list(jobs)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        ok: dict[str, T] = {}
        for name, res in zip(names, results):
            if isinstance(res, BudgetExceeded):
                raise res
            if isinstance(res, (ProviderError, InvalidResponse)):
                self.stats[name].failures.append(f"{stage}: {res}")
                self._emit("member_failed", stage=stage, member=name, error=str(res))
            elif isinstance(res, BaseException):
                self.stats[name].failures.append(f"{stage}: {res!r}")
                self._emit("member_failed", stage=stage, member=name, error=repr(res))
            else:
                ok[name] = res
                self._emit("member_done", stage=stage, member=name, result=res)
        return ok

    # ------------------------------------------------------------------ main flow

    async def run(
        self,
        question: str,
        *,
        criteria: list[str] | None = None,
        options: list[str] | None = None,
        context: str | None = None,
        mode: Mode | None = None,
        rounds: int | None = None,
        attachments: list[Attachment] | None = None,
    ) -> SessionResult:
        mode = "evaluate" if options else (mode or self.cfg.mode)
        if mode == "evaluate" and not options:
            raise CouncilError("для режима evaluate нужны варианты (--option или --options-file)")
        rounds = self.cfg.debate_rounds if rounds is None else rounds
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        t0 = time.monotonic()
        members = self.cfg.active_members
        chair = self.cfg.member(self.cfg.chair)

        self.attachments = list(attachments or [])
        if self.attachments:
            self._emit("attachments", items=[a.meta() for a in self.attachments],
                       blind=[m.name for m in members if not self.can_see(m)])
            await self._describe_images(members, question)

        # 0. Постановка задачи
        refined = question
        if criteria:
            criteria = [c.strip() for c in criteria if c.strip()]
        else:
            self._emit("stage", stage="framing", title="Постановка задачи и критерии")
            try:
                framing = await self._ask(chair, "framing", prompts.framing(question, context), Framing)
                refined = framing.refined_question.strip() or question
                criteria = [c.strip() for c in framing.criteria if c.strip()]
            except (ProviderError, InvalidResponse) as e:
                self._emit("member_failed", stage="framing", member=chair.name, error=str(e))
                criteria = []
            criteria = criteria or ["Эффективность", "Реалистичность", "Риски"]
        self._emit("framing", refined_question=refined, criteria=criteria)

        # 1. Предложения (вслепую) + 2. анонимизация
        if options:
            options = [o for o in options if o.strip()]
            if not 2 <= len(options) <= 26:
                raise CouncilError("для оценки нужно от 2 до 26 вариантов")
            authors: dict[str, str | None] = {string.ascii_uppercase[i]: None for i in range(len(options))}
            proposals: dict[str, Proposal] = {
                l: Proposal(title=opt.strip().splitlines()[0][:80], summary=opt.strip())
                for l, opt in zip(authors, options)
            }
        else:
            self._emit("stage", stage="proposal", title="Независимые предложения")
            got = await self._parallel(
                "proposal",
                {m.name: self._ask(m, "proposal", prompts.proposal(refined, criteria, context), Proposal) for m in members},
            )
            if len(got) < self.cfg.min_quorum:
                raise CouncilError(f"нет кворума: ответили {len(got)} из {len(members)}, нужно {self.cfg.min_quorum}")
            labels = assign_labels(list(got), self.rng)
            authors = dict(labels)
            proposals = {l: got[name] for l, name in labels.items()}
        initial = dict(proposals)
        label_of = {name: l for l, name in authors.items() if name}
        self._emit("proposals", proposals=proposals, authors=authors)

        def others(name: str) -> dict[str, Proposal]:
            return {l: p for l, p in proposals.items() if authors[l] != name}

        # 3–4. Критика и доработка
        critique_rounds = 0 if mode == "quick" else (1 if mode == "evaluate" else rounds)
        critiques: dict[str, CritiqueSet] = {}
        for r in range(critique_rounds):
            self._emit("stage", stage="critique", title=f"Критика, раунд {r + 1}/{critique_rounds}")
            jobs = {}
            for m in members:
                target = others(m.name)
                if not target:
                    continue
                expected = list(target)
                jobs[m.name] = self._ask(
                    m,
                    "critique",
                    prompts.critique(refined, criteria, target),
                    CritiqueSet,
                    check=lambda cs, e=expected: normalize_critique(cs, e, criteria),
                    meta={"labels": expected, "criteria": criteria},
                )
            critiques = await self._parallel("critique", jobs)

            if mode != "debate":
                continue
            self._emit("stage", stage="revision", title=f"Доработка, раунд {r + 1}/{critique_rounds}")
            jobs = {}
            for m in members:
                own = label_of.get(m.name)
                if own is None:
                    continue
                feedback = [
                    {"scores": rv.scores, "strengths": rv.strengths, "weaknesses": rv.weaknesses}
                    for reviewer, cs in critiques.items()
                    if reviewer != m.name
                    for rv in cs.reviews
                    if rv.label == own
                ]
                jobs[m.name] = self._ask(
                    m,
                    "revision",
                    prompts.revision(refined, criteria, own, proposals[own], feedback, others(m.name)),
                    Revision,
                    meta={"label": own},
                )
            revised = await self._parallel("revision", jobs)
            for name, rev in revised.items():
                proposals[label_of[name]] = rev
            self._emit("revisions", revised={label_of[n]: p for n, p in revised.items()})

        # 5. Голосование (за свой вариант голосовать нельзя)
        self._emit("stage", stage="vote", title="Голосование")
        jobs = {}
        for m in members:
            target = others(m.name)
            if not target:
                continue
            expected = list(target)

            def check_ballot(b: Ballot, e: list[str] = expected) -> Ballot:
                return b.model_copy(update={"ranking": normalize_ranking(b.ranking, e)})

            jobs[m.name] = self._ask(
                m,
                "vote",
                prompts.vote(refined, criteria, target),
                Ballot,
                check=check_ballot,
                meta={"labels": expected},
            )
        ballots = await self._parallel("vote", jobs)
        rows = tally(list(proposals), {n: b.ranking for n, b in ballots.items()}, critiques)
        self._emit("tally", rows=rows, authors=authors)

        # 6. Синтез решения председателем
        self._emit("stage", stage="verdict", title="Итоговое решение председателя")
        notes = _critique_notes(critiques)
        valid = list(proposals)

        def check_verdict(v: Verdict) -> Verdict:
            w = normalize_label(v.winner)
            if w not in valid:
                raise ValueError(f"winner должен быть одной из меток {valid}, получено '{v.winner}'")
            return v.model_copy(update={"winner": w})

        try:
            verdict = await self._ask(
                chair,
                "verdict",
                prompts.verdict(refined, criteria, proposals, tally_text(rows), notes),
                Verdict,
                check=check_verdict,
                meta={"labels": valid, "leader": rows[0].label},
            )
        except (ProviderError, InvalidResponse) as e:
            # Председатель недоступен — итог по результатам голосования без синтеза.
            self._emit("member_failed", stage="verdict", member=chair.name, error=str(e))
            top = proposals[rows[0].label]
            verdict = Verdict(
                winner=rows[0].label,
                final_answer=top.summary,
                why="Председатель не ответил; победитель определён по голосованию.",
                risks=top.risks,
            )

        self._emit("verdict", verdict=verdict)
        return SessionResult(
            question=question,
            refined_question=refined,
            criteria=criteria,
            mode=mode,
            authors=authors,
            initial_proposals=initial,
            proposals=proposals,
            critiques=critiques,
            ballots=ballots,
            tally=rows,
            verdict=verdict,
            stats=self.stats,
            transcript=self.transcript,
            started_at=started,
            duration_s=round(time.monotonic() - t0, 1),
            attachments=[a.meta() for a in self.attachments],
            image_notes=dict(self.image_notes),
            image_notes_by=self.image_notes_by,
        )


def _critique_notes(critiques: dict[str, CritiqueSet], per_label: int = 6) -> str:
    by_label: dict[str, list[str]] = {}
    for cs in critiques.values():
        for r in cs.reviews:
            items = by_label.setdefault(r.label, [])
            items += [f"+ {s}" for s in r.strengths[:2]] + [f"- {w}" for w in r.weaknesses[:2]]
    return "\n".join(
        f"{label}:\n  " + "\n  ".join(items[: per_label * 2]) for label, items in sorted(by_label.items())
    )
