"""Оркестратор заседания совета: этапы 0–6, кворум, бюджет, стенограмма."""
from __future__ import annotations

import asyncio
import random
import re
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
from .filerefs import FIX_MESSAGE, refs_in
from .schemas import Ballot, CritiqueSet, FactCheck, Framing, ImageNotes, Proposal, Revision, Verdict
from .voting import (
    FLAG_NAME,
    TallyRow,
    agreement,
    assign_labels,
    ballot_flags,
    co_leaders,
    coverage,
    decisive_ballots,
    leader_without,
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


class FileRefError(ValueError):
    """Ответ ссылается на файл, которого не существует, вместо самого содержимого."""


@dataclass
class MemberStats:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    failures: list[str] = field(default_factory=list)
    cached_tokens: int = 0  # входные токены, прочитанные из кэша провайдера


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
    followup_of: str = ""                                             # id заседания, к которому это уточнение
    request: dict[str, Any] = field(default_factory=dict)             # исходные настройки запуска (для «Повторить»)
    thread: list[dict[str, Any]] = field(default_factory=list)        # предыдущие вопросы и решения цепочки
    agreement: dict[str, Any] | None = None                           # согласие совета (средний τ Кендалла)
    chair_proposed: bool = True                                       # был ли у председателя свой вариант
    ballot_flags: dict[str, dict[str, Any]] = field(default_factory=dict)  # подозрительные бюллетени
    leader_without_flagged: str | None = None                         # лидер без них (если пометки есть)
    fact_check: dict[str, dict[str, Any]] = field(default_factory=dict)    # метка -> итоги проверки фактов
    fact_checker: str = ""                                            # модель, проверявшая факты
    requirements: list[str] = field(default_factory=list)             # жёсткие требования запроса
    co_leaders: list[str] = field(default_factory=list)               # ничья за 1-е место (если была)
    decisive: dict[str, str] = field(default_factory=dict)            # голосующий -> лидер без его бюллетеня
    coverage: dict[str, int] | None = None                            # сколько раз сравнивались пары

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
            "followup_of": self.followup_of,
            "request": self.request,
            "thread": self.thread,
            "agreement": self.agreement,
            "chair_proposed": self.chair_proposed,
            "ballot_flags": self.ballot_flags,
            "leader_without_flagged": self.leader_without_flagged,
            "fact_check": self.fact_check,
            "fact_checker": self.fact_checker,
            "requirements": self.requirements,
            "co_leaders": self.co_leaders,
            "decisive": self.decisive,
            "coverage": self.coverage,
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
        if config.fact_check.enabled:
            self.stats[FACT_CHECKER] = MemberStats()
        self.transcript: list[dict[str, Any]] = []
        self._last_call_emit: dict[tuple[str, str], tuple[str, float]] = {}
        self.vision = vision or {}
        self.attachments: list[Attachment] = []
        self.image_notes: dict[str, str] = {}
        self.image_notes_by = ""
        self.history: list[dict[str, Any]] = []
        self.chair_judge = False  # председатель без своего варианта (только судья и автор итога)
        self.head = ""            # общее начало запросов этапов 1–6 (вопрос) — для кэша провайдера
        self.requirements: list[str] = []  # жёсткие требования запроса (из постановки задачи)
        # «Компактные варианты»: число слов для участников (0 — обычный режим, полный формат у всех)
        self.compact = config.compact_proposals and config.compact_words or 0

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
        s.cached_tokens += getattr(comp, "cached_tokens", 0) or 0
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
        if stage == "factcheck":
            system = prompts.FACTCHECK_SYSTEM.format(language=self.cfg.language)
        else:
            # Одна и та же для участника на всех этапах: провайдеры кэшируют одинаковое начало запроса
            # (системная инструкция + вложения + вопрос), и повторное чтение стоит дешевле.
            system = prompts.system(
                member.name,
                self.cfg.language,
                devil=member.name == self.cfg.devil_advocate,
                chair=member.name == self.cfg.chair,
                judge_only=self.chair_judge,
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

        attempt = 0          # попытки получить корректный ответ (исправление формата, ссылки на файлы)
        continuations = 0    # продолжения длинного ответа, упёршегося в лимит (попытками не считаются)
        acc = ""             # уже полученная часть ответа, который продолжается
        while attempt <= self.cfg.json_retries:
            if self.total_cost >= self.cfg.max_cost_usd:
                raise BudgetExceeded(
                    f"достигнут лимит бюджета ${self.cfg.max_cost_usd:g} (потрачено ${self.total_cost:.4f})"
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
            text = acc + comp.text if acc else comp.text
            entry = {
                "stage": stage,
                "member": member.name,
                "model": comp.model or member.model,
                "attempt": attempt + 1,
                "raw": comp.text,
                "cost": comp.cost,
                "tokens": [comp.prompt_tokens, comp.completion_tokens],
                "cached": getattr(comp, "cached_tokens", 0) or 0,
            }
            if acc:
                entry["continuation"] = continuations
            try:
                obj = self._parse(schema, text, comp.text if acc else None)
                if check:
                    obj = check(obj)
                refs = refs_in(obj.model_dump()) if stage in self.FILE_STAGES else []
                if refs and attempt < self.cfg.json_retries:
                    # модель «положила» результат в несуществующий файл — просим вставить содержимое в ответ
                    raise FileRefError(FIX_MESSAGE.format(refs="; ".join(refs)))
                if refs:  # попытки кончились — принимаем, но отмечаем: часть содержимого может отсутствовать
                    self.stats[name].failures.append(
                        f"{stage}: ответ ссылается на файл, которого нет (содержимое могло потеряться): {'; '.join(refs)}")
                    self._emit("warning", stage=stage, member=name,
                               text=f"ответ ссылается на несуществующий файл: {'; '.join(refs)}")
            except FileRefError as e:
                error = str(e)
                messages = list(base_messages) + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": prompts.fix_json(error)},
                ]
                acc = ""
                self._call(name, stage, "retry", started,
                           detail="ответ ссылается на файл вместо содержимого — прошу вставить всё в ответ")
                entry["error"] = error
                entry["finish_reason"] = comp.finish_reason
                self.transcript.append(entry)
                attempt += 1
                continue
            except ValueError as e:  # pydantic.ValidationError тоже ValueError
                error = readable_error(e)
                truncated = comp.finish_reason in ("length", "max_tokens", "MAX_TOKENS")
                entry["error"] = error
                entry["finish_reason"] = comp.finish_reason
                self.transcript.append(entry)
                if truncated and continuations < self.MAX_CONTINUATIONS and comp.text.strip():
                    # Длинный ответ упёрся в лимит: просим продолжить с места остановки, а не писать заново —
                    # так работает и у моделей с жёстким потолком длины ответа (например, Perplexity).
                    continuations += 1
                    acc = text
                    messages = list(base_messages) + [
                        {"role": "assistant", "content": acc},
                        {"role": "user", "content": prompts.continue_answer()},
                    ]
                    self._call(name, stage, "retry", started,
                               detail=f"ответ длинный, упёрся в лимит {max_tokens} токенов — прошу продолжить "
                                      f"с места остановки (часть {continuations + 1})")
                    continue
                acc = ""
                if truncated:
                    error = f"ответ обрезан на лимите {max_tokens} токенов даже после продолжений ({error})"
                    max_tokens = min(max_tokens * 2, self.cfg.max_tokens_cap)
                    messages = list(base_messages) + [{"role": "user", "content": prompts.shorter_answer()}]
                    hint = "ответ слишком длинный даже после продолжений — прошу уложиться короче"
                else:
                    messages = list(base_messages) + [
                        {"role": "assistant", "content": text},
                        {"role": "user", "content": prompts.fix_json(error)},
                    ]
                    hint = f"ответ не в нужном формате ({error[:120]}) — прошу модель исправить"
                if attempt < self.cfg.json_retries:
                    self._call(name, stage, "retry", started, detail=hint)
                attempt += 1
                continue
            entry["parsed"] = obj.model_dump()
            self.transcript.append(entry)
            self._call(name, stage, "done", started, cost=round(self.stats[name].cost, 6))
            return obj
        self._call(name, stage, "failed", started, detail=f"некорректный ответ: {error}"[:300])
        raise InvalidResponse(f"{member.name}: некорректный ответ после {self.cfg.json_retries + 1} попыток: {error}")

    MAX_CONTINUATIONS = 4  # сколько раз можно попросить продолжить длинный ответ

    @staticmethod
    def _parse(schema: type[T], text: str, last_part: str | None) -> T:
        """Разбор ответа. Для продолжения пробуем склейку частей, затем — только последнюю часть
        (модель могла начать ответ заново целиком)."""
        try:
            return schema.model_validate(extract_json(text))
        except ValueError:
            if last_part is None:
                raise
            try:
                return schema.model_validate(extract_json(last_part))
            except ValueError:
                pass
            raise

    def _shuffled(self, props: dict[str, Proposal]) -> dict[str, Proposal]:
        """Варианты в случайном порядке (у каждого судьи свой): первые показанные обычно оценивают выше."""
        items = list(props.items())
        self.rng.shuffle(items)
        return dict(items)

    ATTACHMENT_STAGES = {"framing", "proposal", "critique", "revision", "verdict"}
    FILE_STAGES = {"proposal", "revision", "verdict"}  # здесь модели пишут содержательный ответ
    VOTE_DOC_CHARS = 20_000  # документы до такого объёма передаются и на голосование

    def can_see(self, member: MemberConfig) -> bool:
        return self.vision.get(member.model) is not False

    def _with_attachments(self, member: MemberConfig, stage: str, prompt: str) -> str | list[dict[str, Any]]:
        """Добавляет к запросу вложения: текст документов и картинки (или их описание для «незрячих»)."""
        sees = self.can_see(member)
        parts: list[dict[str, Any]] = []
        if stage == "vote":
            # голосующие видят и документы, если они небольшие (большие — дорого, их уже разобрали на критике)
            docs = [a for a in self.attachments if a.kind == "document"]
            extra = [documents_block(self.attachments)] if docs and sum(len(a.text) for a in docs) <= self.VOTE_DOC_CHARS else []
        elif (self.attachments or self.history) and stage in self.ATTACHMENT_STAGES:
            extra = [x for x in (prompts.history_block(self.history), documents_block(self.attachments),
                                 images_note(self.attachments, self.image_notes, sees)) if x]
            parts = image_parts(self.attachments) if sees else []
        else:
            extra = []
        # Общее для всех этапов начало (вложения + вопрос) идёт первым — его кэширует провайдер.
        head = self.head if self.head and prompt.startswith(self.head) else ""
        shared = "\n\n".join(extra + ([head] if head else []))
        rest = prompt[len(head):] if head else prompt
        if not shared:
            return prompt if not parts else [{"type": "text", "text": prompt}, *parts]
        if self._explicit_cache(member) and len(shared) >= self.CACHE_MIN_CHARS:
            # Claude и Gemini кэшируют только по явной метке: отмечаем конец общего начала
            return [{"type": "text", "text": shared, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": rest}, *parts]
        text = shared + ("" if head else "\n\n") + rest
        return text if not parts else [{"type": "text", "text": text}, *parts]

    CACHE_MIN_CHARS = 4000  # меньше ~1024 токенов провайдеры не кэшируют

    @staticmethod
    def _explicit_cache(member: MemberConfig) -> bool:
        return member.model.startswith(("anthropic/", "google/"))

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
        history: list[dict[str, Any]] | None = None,
        followup_of: str = "",
    ) -> SessionResult:
        """history — предыдущие вопросы и решения (для уточняющего вопроса), от старых к новым."""
        mode = "evaluate" if options else (mode or self.cfg.mode)
        if mode == "evaluate" and not options:
            raise CouncilError("для режима evaluate нужны варианты (--option или --options-file)")
        rounds = self.cfg.debate_rounds if rounds is None else rounds
        started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        t0 = time.monotonic()
        members = self.cfg.active_members
        chair = self.cfg.member(self.cfg.chair)

        self.attachments = list(attachments or [])
        self.history = list(history or [])
        if self.history:
            self._emit("followup", of=followup_of, thread=self.history)
        if self.attachments:
            self._emit("attachments", items=[a.meta() for a in self.attachments],
                       blind=[m.name for m in members if not self.can_see(m)])
            await self._describe_images(members, question)

        if not options and not self.cfg.chair_proposes:
            if len([m for m in members if m.name != chair.name]) >= self.cfg.min_quorum:
                self.chair_judge = True

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
                self.requirements = [r.strip() for r in framing.requirements if r.strip()]
            except (ProviderError, InvalidResponse) as e:
                self._emit("member_failed", stage="framing", member=chair.name, error=str(e))
                criteria = []
            criteria = criteria or ["Эффективность", "Реалистичность", "Риски"]
        self._emit("framing", refined_question=refined, criteria=criteria, requirements=self.requirements)
        self.head = prompts.head(refined, question, self.requirements)
        req = self.requirements or None

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
            # Председатель по умолчанию не предлагает свой вариант (нет конфликта «автор и судья»),
            # если без него хватает участников для кворума.
            proposers = [m for m in members if m.name != chair.name] if self.chair_judge else members
            self._emit("stage", stage="proposal", title="Независимые предложения",
                       chair_proposes=not self.chair_judge)
            got = await self._parallel(
                "proposal",
                {m.name: self._ask(m, "proposal", prompts.proposal(refined, criteria, context, question, self.compact, req=req), Proposal)
                 for m in proposers},
            )
            if len(got) < self.cfg.min_quorum:
                raise CouncilError(f"нет кворума: ответили {len(got)} из {len(proposers)}, нужно {self.cfg.min_quorum}")
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
                target = self._shuffled(others(m.name))  # свой порядок для каждого судьи — гасит эффект позиции
                if not target:
                    continue
                expected = list(target)
                jobs[m.name] = self._ask(
                    m,
                    "critique",
                    prompts.critique(refined, criteria, target, question, bool(self.compact), req=req),
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
                    prompts.revision(refined, criteria, own, proposals[own], feedback, others(m.name), question,
                                     self.compact, req=req),
                    Revision,
                    check=_normalize_revision,
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
            target = self._shuffled(others(m.name))
            if not target:
                continue
            expected = list(target)

            def check_ballot(b: Ballot, e: list[str] = expected) -> Ballot:
                return b.model_copy(update={"ranking": normalize_ranking(b.ranking, e)})

            jobs[m.name] = self._ask(
                m,
                "vote",
                prompts.vote(refined, criteria, target, question, bool(self.compact), req=req),
                Ballot,
                check=check_ballot,
                meta={"labels": expected},
            )
        ballots = await self._parallel("vote", jobs)
        rankings = {n: b.ranking for n, b in ballots.items()}
        rows = tally(list(proposals), rankings, critiques)
        agree = agreement(rankings)
        flags = ballot_flags(rows, rankings)
        robust = leader_without(list(proposals), rankings, critiques, set(flags)) if flags else None
        tied = co_leaders(rows)
        decisive = decisive_ballots(list(proposals), rankings, critiques)
        cover = coverage(list(proposals), rankings)
        self._emit("tally", rows=rows, authors=authors, agreement=agree, ballot_flags=flags,
                   leader_without_flagged=robust, co_leaders=tied if len(tied) > 1 else [], decisive=decisive,
                   coverage=cover)
        ballot_notes = ""
        if decisive:
            ballot_notes += ("decisive ballots (the leader changes without one of them): "
                             + ", ".join(f"without {v} the leader would be {alt}" for v, alt in decisive.items()) + ". ")
        if flags:
            parts = [f"{n} ({'; '.join(FLAG_NAME[f] for f in fl['flags'])})" for n, fl in flags.items()]
            ballot_notes += ("ballots strongly diverging from the others' consensus: " + ", ".join(parts)
                            + (f". Without them the leader would be {robust}" if robust and robust != rows[0].label
                               else ". Without them the leader stays the same"))

        # 5½. Проверка фактов лидеров голосования моделью с поиском (если включена)
        facts = await self._fact_check(refined, proposals, rows)

        # 6. Синтез решения председателем
        self._emit("stage", stage="verdict", title="Итоговое решение председателя")
        notes = _critique_notes(critiques)
        valid = list(proposals)

        leader = rows[0].label
        follow = self.cfg.chair_follows_vote

        def check_verdict(v: Verdict) -> Verdict:
            w = normalize_label(v.winner)
            if w not in valid:
                raise ValueError(f"winner must be one of the labels {valid}, got '{v.winner}'")
            deviation = v.deviation.strip()
            if w == leader or (len(tied) > 1 and w in tied):
                deviation = ""  # лидер или один из разделивших первое место — это не отступление
            elif follow and not re.match(r"^\W*(fact|risk|process)\b", deviation, re.I):
                raise ValueError(
                    f"the winner must be the voting leader {leader}; choosing {w} requires 'deviation' that starts with "
                    f"fact, risk or process and explains the specific reason, otherwise set winner to {leader}")
            return v.model_copy(update={"winner": w, "deviation": deviation})

        try:
            verdict = await self._ask(
                chair,
                "verdict",
                prompts.verdict(refined, criteria, proposals, tally_text(rows), notes, question,
                                (f"{agree['level']} (mean Kendall tau between ballots = {agree['tau']:.2f}; "
                                 "1 = identical rankings, 0 = unrelated)") if agree else "",
                                prompts.factcheck_block(facts), ballot_notes, leader=leader, follow_vote=follow,
                                compact=bool(self.compact), req=req, co_leaders=tied if len(tied) > 1 else None),
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
            followup_of=followup_of,
            thread=self.history,
            agreement=agree,
            chair_proposed=not self.chair_judge and mode != "evaluate",
            ballot_flags=flags,
            leader_without_flagged=robust,
            requirements=self.requirements,
            co_leaders=tied if len(tied) > 1 else [],
            decisive=decisive,
            coverage=cover,
            fact_check=facts,
            fact_checker=self.cfg.fact_check.model if facts else "",
        )

    async def _fact_check(self, question: str, proposals: dict[str, Proposal], rows: list[TallyRow]) -> dict[str, dict]:
        """Проверяет ключевые факты в лидирующих вариантах (топ-N голосования). Сбой проверки не прерывает заседание."""
        fc = self.cfg.fact_check
        if not fc.enabled or not rows:
            return {}
        targets = [r.label for r in rows[:fc.top]]
        checker = MemberConfig(name=FACT_CHECKER, model=fc.model, max_tokens=fc.max_tokens, temperature=0.1,
                               reasoning_effort="none")
        self._emit("stage", stage="factcheck", title=f"Проверка фактов: варианты {', '.join(targets)}",
                   targets=targets, model=fc.model)

        def check(r: FactCheck) -> FactCheck:
            return r.model_copy(update={"claims": [c.model_copy(update={"status": normalize_status(c.status)})
                                                   for c in r.claims if c.claim.strip()][:fc.max_claims]})

        jobs = [self._ask(checker, "factcheck", prompts.factcheck(question, l, proposals[l], fc.max_claims), FactCheck,
                          check=check, meta={"label": l}) for l in targets]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        out: dict[str, dict] = {}
        for label, res in zip(targets, results):
            if isinstance(res, BudgetExceeded):
                raise res
            if isinstance(res, BaseException):
                self.stats[FACT_CHECKER].failures.append(f"factcheck: {label}: {res}")
                self._emit("member_failed", stage="factcheck", member=f"{FACT_CHECKER} ({label})", error=str(res))
                continue
            out[label] = res.model_dump()
        self._emit("factcheck", results=out, model=fc.model)
        return out


FACT_CHECKER = "факт-чек"
STATUS_ALIASES = {
    "confirmed": "confirmed", "true": "confirmed", "verified": "confirmed", "supported": "confirmed",
    "подтверждено": "confirmed", "подтверждён": "confirmed", "верно": "confirmed",
    "refuted": "refuted", "false": "refuted", "incorrect": "refuted", "wrong": "refuted",
    "опровергнуто": "refuted", "неверно": "refuted", "ложно": "refuted",
    "user_doc": "user_doc", "user_provided": "user_doc", "from_user_doc": "user_doc", "user": "user_doc",
    "из документов пользователя": "user_doc",
}


POSITION_ALIASES = {"kept": "kept", "keep": "kept", "same": "kept", "сохранена": "kept", "сохранил": "kept",
                    "improved": "improved", "refined": "improved", "strengthened": "improved", "доработана": "improved",
                    "switched": "switched", "changed": "switched", "replaced": "switched", "сменена": "switched"}


def _normalize_revision(r: Revision) -> Revision:
    pos = POSITION_ALIASES.get(str(r.position).strip().lower().split()[0] if str(r.position).strip() else "", "improved")
    return r.model_copy(update={"position": pos})


def readable_error(e: Exception) -> str:
    """Понятное описание ошибки разбора: какие поля не так (вместо «2 validation errors for Proposal»)."""
    errors = getattr(e, "errors", None)
    if callable(errors):
        try:
            items = errors()
        except Exception:  # noqa: BLE001
            items = []
        parts = []
        for it in items[:5]:
            loc = ".".join(str(x) for x in it.get("loc", ())) or "answer"
            got = it.get("input")
            kind = type(got).__name__ if got is not None else "nothing"
            parts.append(f"field '{loc}': {it.get('msg', '')}" + (f" (got {kind})" if it.get("type") != "missing" else ""))
        if parts:
            return "; ".join(parts)[:500]
    text = str(e)
    return text.splitlines()[0][:500] if text else repr(e)


def normalize_status(raw: str) -> str:
    return STATUS_ALIASES.get(str(raw).strip().lower(), "unclear")


def _critique_notes(critiques: dict[str, CritiqueSet], per_label: int = 6) -> str:
    by_label: dict[str, list[str]] = {}
    for cs in critiques.values():
        for r in cs.reviews:
            items = by_label.setdefault(r.label, [])
            items += [f"+ {s}" for s in r.strengths[:2]] + [f"- {w}" for w in r.weaknesses[:2]]
    return "\n".join(
        f"{label}:\n  " + "\n  ".join(items[: per_label * 2]) for label, items in sorted(by_label.items())
    )
