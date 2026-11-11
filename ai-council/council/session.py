"""Оркестратор заседания совета: этапы 0–6, кворум, бюджет, стенограмма."""
from __future__ import annotations

import asyncio
import contextlib
import random
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel

from . import prompts
from .checkpoint import Journal, call_key
from .client import Completion, LLMClient, OutOfCredits, ProviderError
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
    reasoning_tokens: int = 0  # из выходных — скрытые рассуждения (если провайдер сообщает)
    reasoning_off: bool = False  # рассуждения выключались: модель тратила на них весь лимит ответа


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
    ideas: list[dict[str, Any]] = field(default_factory=list)         # идеи «стоит сохранить» и решение по ним
    research: dict[str, Any] = field(default_factory=dict)           # режим «Исследование»: рамки, реестр, подвопросы

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
            "ideas": self.ideas,
            "research": self.research,
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
        journal: Journal | None = None,
        labels: dict[str, str] | None = None,
    ) -> None:
        """labels — метки вариантов, как в прошлом прогоне («доделать» заседание из архива: ответы из стенограммы
        ссылаются на эти метки)."""
        """vision: модель -> умеет ли принимать картинки (из каталога OpenRouter); неизвестно — пробуем."""
        self.cfg = config
        self.client = client
        self.on_event = on_event or (lambda *a, **k: None)
        self.rng = random.Random(seed)
        # Журнал для приостановки: готовые ответы при продолжении берутся из него, а не у модели (см. checkpoint.py)
        self.journal = journal or Journal(None)
        self.budget = config.max_cost_usd  # можно поднять на ходу: set_budget()
        self._budget_warned = False
        self.replayed = 0  # сколько ответов взято из журнала
        self.forced_labels = dict(labels or {})
        self._seen: dict[tuple[str, str], int] = {}  # (этап, участник) -> сколько обращений уже было
        self._sem = asyncio.Semaphore(config.max_concurrency)
        self._narrow = asyncio.Semaphore(2)
        self.stats: dict[str, MemberStats] = {m.name: MemberStats() for m in config.active_members}
        if config.fact_check.enabled:
            self.stats[FACT_CHECKER] = MemberStats()
        self.transcript: list[dict[str, Any]] = list(self.journal.transcript)
        for c in self.journal.costs:  # деньги, потраченные до приостановки, остаются в счёте заседания
            st = self.stats.setdefault(c.get("member", "?"), MemberStats())
            st.calls += int(c.get("calls") or 0)
            st.prompt_tokens += int(c.get("prompt") or 0)
            st.completion_tokens += int(c.get("completion") or 0)
            st.cached_tokens += int(c.get("cached") or 0)
            st.cost += float(c.get("cost") or 0)
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
        s.reasoning_tokens += getattr(comp, "reasoning_tokens", 0) or 0
        if getattr(comp, "reasoning_off", False) and not s.reasoning_off:
            s.reasoning_off = True
            self._emit("warning", stage="", member=member.name,
                       text="модель тратила весь лимит на скрытые рассуждения — они выключены, отвечает без них")
        self.journal.cost(member.name, 1, comp.prompt_tokens, comp.completion_tokens,
                          getattr(comp, "cached_tokens", 0) or 0, comp.cost)
        self._emit("cost", member=member.name, total=self.total_cost)
        if not self._budget_warned and self.budget and self.total_cost >= self.budget * self.BUDGET_WARN:
            self._budget_warned = True
            self._emit("budget_warning", spent=round(self.total_cost, 4), budget=self.budget)

    BUDGET_WARN = 0.8  # при такой доле потраченного бюджета интерфейс предлагает его поднять

    def set_budget(self, budget: float) -> None:
        """Поднять (или изменить) лимит бюджета прямо во время заседания."""
        self.budget = float(budget)
        self._budget_warned = self.total_cost >= self.budget * self.BUDGET_WARN
        self._emit("budget", budget=self.budget, spent=round(self.total_cost, 4))

    def _log(self, entry: dict[str, Any]) -> None:
        self.transcript.append(entry)
        self.journal.entry(entry)

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
        web_search: bool = False,
    ) -> T:
        if stage == "factcheck":
            system = prompts.FACTCHECK_SYSTEM.format(language=self.cfg.language)
        elif stage == "arbitration":
            from . import research
            system = research.ARBITRATION_SYSTEM.format(language=self.cfg.language)
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
        key = call_key(member.model, stage, schema.__name__, base_messages)
        n = self._seen.get((stage, member.name), 0)
        self._seen[(stage, member.name)] = n + 1
        cached = self.journal.results.get(key)
        if cached is None:  # «доделать» из архива: ответ того же участника на том же этапе по порядку
            cached = self.journal.loose.get((stage, member.name, n))
        if cached is not None:  # ответ уже получен и оплачен раньше
            try:
                obj = schema.model_validate(cached)
                if check and (stage, member.name, n) in self.journal.loose:
                    obj = check(obj)  # тот же ли набор меток и критериев — иначе спросим заново
            except ValueError:
                obj = None
            if obj is not None:
                self.replayed += 1
                self._call(member.name, stage, "done", time.monotonic(), detail="из сохранённого",
                           cost=round(self.stats[member.name].cost, 6))
                return obj
        if key in self.journal.failures:  # участник не справился до приостановки — ход заседания тот же
            raise InvalidResponse(self.journal.failures[key])
        try:
            obj = await self._ask_model(member, stage, schema, base_messages, messages, meta, check, web_search)
        except (InvalidResponse, ProviderError) as e:
            if not isinstance(e, OutOfCredits):
                self.journal.fail(key, str(e), stage)
            raise
        self.journal.ok(key, obj.model_dump(), stage)
        return obj

    async def _ask_model(self, member: MemberConfig, stage: str, schema: type[T], base_messages: list, messages: list,
                         meta: dict[str, Any], check: Callable[[T], T] | None, web_search: bool = False) -> T:
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
            if self.total_cost >= self.budget:
                raise BudgetExceeded(
                    f"достигнут лимит бюджета ${self.budget:g} (потрачено ${self.total_cost:.4f})"
                )
            if self._sem.locked():
                self._call(name, stage, "queued", started, detail="ждёт свободного слота")
            try:
                async with self._sem:
                    # у границы бюджета запросы идут по два: меньше запросов «в полёте» — меньше превышение лимита
                    async with (self._narrow if self._budget_warned else contextlib.nullcontext()):
                        extra = {"web_search": True} if web_search else {}
                        comp = await self.client.complete(
                            member.model,
                            messages,
                            temperature=member.temperature if member.temperature is not None else self.cfg.temperature,
                            max_tokens=max_tokens,
                            reasoning_effort=None if effort == "none" else effort,
                            meta=meta,
                            on_progress=progress,
                            **extra,
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
                self._log(entry)
                attempt += 1
                continue
            except ValueError as e:  # pydantic.ValidationError тоже ValueError
                error = readable_error(e)
                truncated = comp.finish_reason in ("length", "max_tokens", "MAX_TOKENS")
                entry["error"] = error
                entry["finish_reason"] = comp.finish_reason
                self._log(entry)
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
            if "search_citations" in type(obj).model_fields:  # ссылки, которые реально вернул поиск
                obj = obj.model_copy(update={"search_citations": list(getattr(comp, "citations", []) or [])})
            entry["parsed"] = obj.model_dump()
            self._log(entry)
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
            if isinstance(e, OutOfCredits):
                raise
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
            if isinstance(res, (BudgetExceeded, OutOfCredits)):
                raise res  # бюджет или деньги кончились — заседание приостанавливается целиком
            if isinstance(res, (ProviderError, InvalidResponse)):
                self.stats[name].failures.append(f"{stage}: {res}")
                self._emit("member_failed", stage=stage, member=name, error=str(res))
            elif isinstance(res, BaseException):
                self.stats[name].failures.append(f"{stage}: {res!r}")
                self._emit("member_failed", stage=stage, member=name, error=repr(res))
            else:
                ok[name] = res
                self._emit("member_done", stage=stage, member=name, result=res)
        if names and not ok and stage != "proposal":  # у предложений своя проверка кворума
            # Не ответил никто — причина общая (сеть, ключ, провайдер), а не в отдельных моделях: идти дальше
            # вхолостую бессмысленно. Заседание встаёт на паузу; при продолжении этап спросят заново.
            first = next((str(r) for r in results if isinstance(r, BaseException)), "")
            raise CouncilError(f"на этапе «{self.STAGE_TITLE.get(stage, stage)}» не ответил ни один участник: {first[:300]}")
        return ok

    def _n_proposers(self) -> int:
        members = self.cfg.active_members
        return len([m for m in members if m.name != self.cfg.chair]) if self.chair_judge else len(members)

    def _labels(self, got: dict[str, Any]) -> dict[str, str]:
        """Случайные метки вариантов; при «доделке» из архива прежние варианты сохраняют свои метки."""
        labels = assign_labels(list(got), self.rng)
        if self.forced_labels:
            labels = {l: n for l, n in self.forced_labels.items() if n in got}
            free = [c for c in string.ascii_uppercase if c not in self.forced_labels]
            fresh = [n for n in got if n not in labels.values()]
            self.rng.shuffle(fresh)
            labels.update(zip(free, fresh))
            labels = dict(sorted(labels.items()))
        return labels

    STAGE_TITLE = {"critique": "Критика", "revision": "Доработка", "vote": "Голосование", "factcheck": "Проверка фактов"}

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

        if mode == "research":  # режим «Исследование» — см. council/research.py
            return await self._run_research(question, criteria=criteria, context=context, followup_of=followup_of,
                                            started=started, t0=t0)

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
                if isinstance(e, OutOfCredits):
                    raise
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
                {m.name: self._ask(m, "proposal", prompts.proposal(refined, criteria, context, question, self.compact, req=req), Proposal,
                                   check=lambda p: check_substance(p, compact=bool(self.compact)))
                 for m in proposers},
            )
            if len(got) < self.cfg.min_quorum:
                raise CouncilError(f"нет кворума: ответили {len(got)} из {len(proposers)}, нужно {self.cfg.min_quorum}")
            labels = self._labels(got)
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
                    check=lambda r, prev=proposals[own]: check_substance(_normalize_revision(r), previous=prev,
                                                                         compact=bool(self.compact)),
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
        ideas = collect_ideas(critiques, exclude={leader})
        ideas_text = "\n".join(f"{i['id']} (from {i['label']}): {i['idea']}" for i in ideas)
        idea_ids = [i["id"] for i in ideas]
        verdict_checks = [0]

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
            if _letters(v.final_answer) < 3 or _is_placeholder(v.final_answer):
                raise ValueError('"final_answer" is empty or a placeholder ("...") — write the complete final answer')
            verdict_checks[0] += 1
            decided = normalize_ideas(v.ideas, idea_ids, strict=verdict_checks[0] <= self.cfg.json_retries)
            return v.model_copy(update={"winner": w, "deviation": deviation, "ideas": decided})

        try:
            verdict = await self._ask(
                chair,
                "verdict",
                prompts.verdict(refined, criteria, proposals, tally_text(rows), notes, question,
                                (f"{agree['level']} (mean Kendall tau between ballots = {agree['tau']:.2f}; "
                                 "1 = identical rankings, 0 = unrelated)") if agree else "",
                                prompts.factcheck_block(facts), ballot_notes, leader=leader, follow_vote=follow,
                                compact=bool(self.compact), req=req, co_leaders=tied if len(tied) > 1 else None,
                                ideas=ideas_text),
                Verdict,
                check=check_verdict,
                meta={"labels": valid, "leader": rows[0].label, "ideas": idea_ids},
            )
        except (ProviderError, InvalidResponse) as e:
            if isinstance(e, OutOfCredits):
                raise
            # Председатель недоступен — итог по результатам голосования без синтеза.
            self._emit("member_failed", stage="verdict", member=chair.name, error=str(e))
            top = proposals[rows[0].label]
            verdict = Verdict(
                winner=rows[0].label,
                final_answer=top.summary,
                why="Председатель не ответил; победитель определён по голосованию.",
                risks=top.risks,
            )

        by_id = {d.id: d for d in verdict.ideas}
        for i in ideas:
            d = by_id.get(i["id"])
            i["decision"], i["reason"] = (d.decision, d.reason) if d else ("not_addressed", "")
        self._emit("verdict", verdict=verdict, ideas=ideas)
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
            ideas=ideas,
        )

    # ------------------------------------------------------------------ режим «Исследование»

    async def _run_research(self, question: str, *, criteria: list[str] | None, context: str | None,
                            followup_of: str, started: str, t0: float) -> SessionResult:
        from . import research as R

        rc = self.cfg.research
        members = self.cfg.active_members
        chair = self.cfg.member(self.cfg.chair)
        rmeta = {"research": True}

        # 0. Постановка: рамки, подвопросы, линзы
        self._emit("stage", stage="framing", title="Постановка исследования: рамки и подвопросы")
        fr: R.ResearchFraming | None = None
        try:
            fr = await self._ask(chair, "framing", R.framing(question, context, len(members)), R.ResearchFraming,
                                 meta=rmeta)
        except (ProviderError, InvalidResponse) as e:
            if isinstance(e, OutOfCredits):
                raise
            self._emit("member_failed", stage="framing", member=chair.name, error=str(e))
        refined = (fr.refined_question.strip() if fr else "") or question
        if criteria:
            criteria = [c.strip() for c in criteria if c.strip()]
        else:
            criteria = [c.strip() for c in (fr.criteria if fr else []) if c.strip()] or list(R.DEFAULT_CRITERIA)
        self.requirements = [r.strip() for r in (fr.requirements if fr else []) if r.strip()]
        subq = (fr.subquestions if fr else [])[:5] or [refined]
        scope, as_of = (fr.scope if fr else ""), (fr.as_of if fr else "")
        lenses = R.fill_lenses(fr.lenses if fr else [], self._n_proposers()) if rc.lenses else []
        self._emit("framing", refined_question=refined, criteria=criteria, requirements=self.requirements,
                   scope=scope, as_of=as_of, subquestions=subq, lenses=lenses)
        req = self.requirements or None
        rhead = R.research_head(refined, question, req, scope, as_of, subq)
        self.head = rhead  # одинаковое начало всех запросов — его кэширует провайдер

        # 1. Слепые предложения с утверждениями; у каждого автора своя линза
        proposers = [m for m in members if m.name != chair.name] if self.chair_judge else members
        pool = list(lenses)
        self.rng.shuffle(pool)
        lens_of = {m.name: (pool[i % len(pool)] if pool else "") for i, m in enumerate(proposers)}
        scout_pool = [m.name for m in proposers if m.name != self.cfg.devil_advocate]
        self.rng.shuffle(scout_pool)
        scouts = set(scout_pool[:rc.fresh_scouts])
        self._emit("stage", stage="proposal", title="Независимые предложения (без поиска)" if not scouts else
                   f"Независимые предложения (с поиском: {', '.join(sorted(scouts))})", chair_proposes=not self.chair_judge)
        got = await self._parallel("proposal", {
            m.name: self._ask(m, "proposal", R.proposal(rhead, criteria, context, lens_of[m.name], rc.max_words,
                                                        rc.max_claims, web=m.name in scouts),
                              R.ResearchProposal, check=lambda p: check_substance(p), meta=rmeta,
                              web_search=m.name in scouts)
            for m in proposers})
        if len(got) < self.cfg.min_quorum:
            raise CouncilError(f"нет кворума: ответили {len(got)} из {len(proposers)}, нужно {self.cfg.min_quorum}")
        authors: dict[str, str | None] = dict(self._labels(got))
        proposals: dict[str, Proposal] = {
            l: R.number_claims(l, got[n] if n in scouts else R.mark_memory_urls(got[n], set()))
            for l, n in authors.items() if n}
        initial = dict(proposals)
        label_of = {n: l for l, n in authors.items() if n}
        self._emit("proposals", proposals=proposals, authors=authors)

        def others(name: str) -> dict[str, Proposal]:
            return {l: p for l, p in proposals.items() if authors[l] != name}

        # 2. Критика: оценки, спорные утверждения, пропущенные подвопросы
        self._emit("stage", stage="critique", title="Критика и спорные утверждения")
        jobs = {}
        for m in members:
            target = self._shuffled(others(m.name))
            if not target:
                continue
            expected = list(target)
            jobs[m.name] = self._ask(m, "critique", R.critique(rhead, criteria, target, req), CritiqueSet,
                                     check=lambda cs, e=expected: normalize_critique(cs, e, criteria),
                                     meta={"labels": expected, "criteria": criteria,
                                           "claims": [c.id for l in expected for c in getattr(target[l], "claims", [])],
                                           **rmeta})
        critiques: dict[str, CritiqueSet] = await self._parallel("critique", jobs)

        # 3. Арбитраж доказательств: поиск по утверждениям всех вариантов, два противоположных задания
        ledger: dict[str, Any] = {}
        if rc.arbitration:
            selected = R.select_claims(proposals, critiques, rc.arbitration_claims)  # type: ignore[arg-type]
            if selected:
                ledger = await self._arbitrate(R, refined, as_of, scope, selected, rc)
        ledger_txt = R.ledger_text(ledger)

        # 4. Доработка по реестру и выжимкам чужих вариантов
        self._emit("stage", stage="revision", title="Доработка по реестру доказательств")
        jobs = {}
        for m in members:
            own = label_of.get(m.name)
            if own is None:
                continue
            feedback = [{"scores": rv.scores, "strengths": rv.strengths, "weaknesses": rv.weaknesses,
                         "disputed_claims": rv.disputed_claims, "missing_subquestions": rv.missing_subquestions}
                        for reviewer, cs in critiques.items() if reviewer != m.name for rv in cs.reviews if rv.label == own]
            jobs[m.name] = self._ask(
                m, "revision",
                R.revision(rhead, criteria, own, proposals[own], feedback, others(m.name), ledger_txt,
                           lens_of.get(m.name, ""), rc.max_words, rc.max_claims, full_others=not rc.revision_digest),
                R.ResearchRevision,
                check=lambda r, prev=proposals[own]: check_substance(_normalize_revision(r), previous=prev),
                meta={"label": own, **rmeta})
        revised = await self._parallel("revision", jobs)
        known_urls = {e["url"] for e in (ledger or {}).get("evidence", [])}
        for name, rev in revised.items():
            rev = rev if name in scouts else R.mark_memory_urls(rev, known_urls)
            proposals[label_of[name]] = R.number_claims(label_of[name], rev)
        self._emit("revisions", revised={label_of[n]: proposals[label_of[n]] for n in revised})

        # 5. Голосование: общий порядок и лучший вариант по каждому подвопросу
        self._emit("stage", stage="vote", title="Голосование: каркас по подвопросам")
        jobs = {}
        for m in members:
            target = self._shuffled(others(m.name))
            if not target:
                continue
            expected = list(target)

            def check_ballot(b: R.ResearchBallot, e: list[str] = expected) -> R.ResearchBallot:
                best = {q: lab for q, lab in b.best_by_subquestion.items() if lab in e}
                return b.model_copy(update={"ranking": normalize_ranking(b.ranking, e), "best_by_subquestion": best})

            jobs[m.name] = self._ask(m, "vote", R.vote(rhead, criteria, target, subq, req, rc.section_vote),
                                     R.ResearchBallot, check=check_ballot,
                                     meta={"labels": expected, "subquestions": len(subq), **rmeta})
        ballots = await self._parallel("vote", jobs)
        rankings = {n: b.ranking for n, b in ballots.items()}
        rows = tally(list(proposals), rankings, critiques)
        agree = agreement(rankings)
        flags = ballot_flags(rows, rankings)
        robust = leader_without(list(proposals), rankings, critiques, set(flags)) if flags else None
        tied = co_leaders(rows)
        decisive = decisive_ballots(list(proposals), rankings, critiques)
        cover = coverage(list(proposals), rankings)
        sections = R.section_tally(subq, ballots, [r.label for r in rows]) if rc.section_vote else {}
        self._emit("tally", rows=rows, authors=authors, agreement=agree, ballot_flags=flags,
                   leader_without_flagged=robust, co_leaders=tied if len(tied) > 1 else [], decisive=decisive,
                   coverage=cover, sections=sections)
        ballot_notes = ""
        if flags:
            ballot_notes = "ballots strongly diverging from the others: " + ", ".join(
                f"{n} ({'; '.join(FLAG_NAME[f] for f in fl['flags'])})" for n, fl in flags.items())

        # 6. Синтез: «известно / спорно / неизвестно»
        self._emit("stage", stage="verdict", title="Синтез исследования")
        leader = rows[0].label if rows else next(iter(proposals))
        valid = list(proposals)
        ideas = collect_ideas(critiques, exclude={leader})
        ideas_text = "\n".join(f"{i['id']} (from {i['label']}): {i['idea']}" for i in ideas)
        idea_ids = [i["id"] for i in ideas]
        checks = [0]
        follow = self.cfg.chair_follows_vote

        def check_verdict(v: R.ResearchVerdict) -> R.ResearchVerdict:
            # В исследовании «winner» — лишь общий каркас для диагностики (итог собирается по подвопросам),
            # поэтому за него ответ не отклоняется: неверная метка — лидер голосования, иной выбор — с пометкой.
            checks[0] += 1
            strict = checks[0] <= self.cfg.json_retries
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
            v = R.check_verdict(v, ledger=ledger, n_subq=len(subq), strict=strict)
            decided = normalize_ideas(v.ideas, idea_ids, strict=strict)
            return v.model_copy(update={"winner": w, "deviation": deviation, "ideas": decided})

        proposals_txt = "\n\n".join(
            R.proposal_block(l, p) + (f"\nChanges after critique: {p.changes}" if getattr(p, "changes", "") else "")
            + (f"\nAdded after the ledger (cannot be known): {'; '.join(p.new_after_ledger)}"
               if getattr(p, "new_after_ledger", None) else "")
            for l, p in proposals.items())
        try:
            verdict = await self._ask(
                chair, "verdict",
                R.verdict(rhead, criteria, proposals_txt, ledger_txt, R.section_text(subq, sections), tally_text(rows),
                          _critique_notes(critiques), ideas_text, leader, valid,
                          (f"{agree['level']} (mean Kendall tau = {agree['tau']:.2f})") if agree else "", ballot_notes,
                          req, tied if len(tied) > 1 else None, subq),
                R.ResearchVerdict, check=check_verdict,
                meta={"labels": valid, "leader": leader, "ideas": idea_ids, "subquestions": len(subq),
                      "evidence_ok": sorted(R.confirmed_ids(ledger)), **rmeta})
        except (ProviderError, InvalidResponse) as e:
            if isinstance(e, OutOfCredits):
                raise
            self._emit("member_failed", stage="verdict", member=chair.name, error=str(e))
            verdict = R.fallback_verdict(leader, proposals, ledger, subq)
        by_id = {d.id: d for d in verdict.ideas}
        for i in ideas:
            d = by_id.get(i["id"])
            i["decision"], i["reason"] = (d.decision, d.reason) if d else ("not_addressed", "")
        self._emit("verdict", verdict=verdict, ideas=ideas)
        return SessionResult(
            question=question, refined_question=refined, criteria=criteria, mode="research", authors=authors,
            initial_proposals=initial, proposals=proposals, critiques=critiques, ballots=ballots, tally=rows,
            verdict=verdict, stats=self.stats, transcript=self.transcript, started_at=started,
            duration_s=round(time.monotonic() - t0, 1), attachments=[a.meta() for a in self.attachments],
            image_notes=dict(self.image_notes), image_notes_by=self.image_notes_by, followup_of=followup_of,
            thread=self.history, agreement=agree, chair_proposed=not self.chair_judge, ballot_flags=flags,
            leader_without_flagged=robust, requirements=self.requirements, co_leaders=tied if len(tied) > 1 else [],
            decisive=decisive, coverage=cover, fact_check={}, fact_checker=ledger.get("model", "") if ledger else "",
            ideas=ideas,
            research={"scope": scope, "as_of": as_of, "subquestions": subq, "lenses": lenses,
                      "lens_of": {label_of[n]: lens_of.get(n, "") for n in label_of},
                      "scouts": sorted(label_of[n] for n in scouts if n in label_of),
                      "ledger": ledger, "sections": sections, "settings": rc.model_dump()},
        )

    async def _arbitrate(self, R: Any, question: str, as_of: str, scope: str, selected: list[dict[str, Any]],
                         rc: Any) -> dict[str, Any]:
        """Два поисковых вызова с противоположными заданиями; сбой поиска не прерывает исследование."""
        fc = self.cfg.fact_check
        checker = MemberConfig(name=FACT_CHECKER, model=fc.model, max_tokens=max(fc.max_tokens, 6000),
                               temperature=0.1, reasoning_effort="none")
        self.stats.setdefault(FACT_CHECKER, MemberStats())
        self._emit("stage", stage="arbitration",
                   title=f"Арбитраж доказательств: {len(selected)} утверждений, поиск {fc.model}",
                   claims=[c["id"] for c in selected], model=fc.model)
        size = R.ARBITRATION_BATCH
        chunks = [selected[k:k + size] for k in range(0, len(selected), size)]
        per_chunk = max(5, rc.max_evidence // max(1, len(chunks)))
        plan = [(i, chunk) for chunk in chunks for i in range(rc.missions)]
        jobs = [self._ask(checker, "arbitration", R.arbitration(question, as_of, scope, R.MISSIONS[i],
                                                                 R.claims_batch(chunk), per_chunk),
                          R.EvidenceResult, meta={"mission": i + 1, "claims": [c["id"] for c in chunk], "research": True},
                          web_search=True)
                for i, chunk in plan]
        results = await asyncio.gather(*jobs, return_exceptions=True)
        ok = []
        for i, res in enumerate(results, 1):
            if isinstance(res, (BudgetExceeded, OutOfCredits)):
                raise res
            if isinstance(res, BaseException):
                self.stats[FACT_CHECKER].failures.append(f"arbitration: задание {i}: {res}")
                self._emit("member_failed", stage="arbitration", member=f"{FACT_CHECKER} (задание {i})", error=str(res))
                continue
            ok.append(res)
        ledger = R.build_ledger(selected, ok, fc.model) if ok else {
            "model": fc.model, "missions": 0, "evidence": [], "gaps": [],
            "claims": [{**c, "status": "search_unavailable", "notes": [], "evidence": []} for c in selected]}
        self._emit("ledger", ledger=ledger)
        return ledger

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
            if isinstance(res, (BudgetExceeded, OutOfCredits)):
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


MIN_LETTERS = 10           # меньше — это не ответ, а заглушка («**url:** **date:**»); настоящие — тысячи букв
MIN_LETTERS_COMPACT = 10
PLACEHOLDER_RE = re.compile(r"complete markdown answer|the full (revised )?answer|full answer here|your answer here|"
                            r"short descriptive title|^\W*(\.\.\.|…)\W*$", re.I | re.S)


def _letters(text: str) -> int:
    return len(re.findall(r"[^\W\d_]", text or ""))


def _is_placeholder(text: str) -> bool:
    t = (text or "").strip()
    return bool(PLACEHOLDER_RE.search(t)) and _letters(t) < 400


def check_substance(p: Proposal, *, previous: Proposal | None = None, compact: bool = False) -> Proposal:
    """Отсеивает «пустышки»: модель вернула заглушку из образца формата («...», «complete Markdown answer»)
    или посторонние поля вместо ответа. Такой ответ возвращается модели с объяснением, а не принимается.
    Доработка, потерявшая почти весь текст, тоже не принимается — иначе она затрёт хороший вариант."""
    need = MIN_LETTERS_COMPACT if compact else MIN_LETTERS
    if _letters(p.summary) < need or _is_placeholder(p.summary):
        raise ValueError('"summary" is empty or a placeholder copied from the format example ("...") — write your '
                         "complete answer in it")
    if _letters(p.title) < 2 or _is_placeholder(p.title):
        raise ValueError('"title" is a placeholder — give a short descriptive title of your proposal')
    if previous is not None:
        before, after = _letters(previous.summary), _letters(p.summary)
        if before > 1500 and after < before * 0.3:
            raise ValueError(f'the revised "summary" lost most of the content ({before} → {after} letters): keep '
                             "your full answer and revise it, do not shorten it to a fragment")
    return p


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


IDEAS_PER_LABEL = 3
IDEAS_MAX = 15


def _idea_key(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def collect_ideas(critiques: dict[str, CritiqueSet], exclude: set[str] = frozenset()) -> list[dict[str, Any]]:
    """Идеи «стоит сохранить» из критики: по каждому варианту (кроме лидера) — до трёх, поровну от разных критиков,
    без повторов. Возвращает [{"id": "I1", "label": "B", "idea": "…", "by": [критики]}]."""
    per_label: dict[str, list[tuple[str, list[str]]]] = {}
    for critic, cs in sorted(critiques.items()):
        for r in cs.reviews:
            if r.label in exclude:
                continue
            items = [k.strip() for k in r.keep if k and k.strip()]
            if items:
                per_label.setdefault(r.label, []).append((critic, items))
    out: list[dict[str, Any]] = []
    for label in sorted(per_label):
        picked: dict[str, dict[str, Any]] = {}
        lists = per_label[label]
        for depth in range(max(len(x[1]) for x in lists)):  # по кругу: первая идея каждого критика, потом вторая…
            for critic, items in lists:
                if depth >= len(items):
                    continue
                key = _idea_key(items[depth])
                if key in picked:
                    picked[key]["by"].append(critic)
                elif len(picked) < IDEAS_PER_LABEL:
                    picked[key] = {"label": label, "idea": items[depth], "by": [critic]}
        out += list(picked.values())
    out = out[:IDEAS_MAX]
    for n, i in enumerate(out, 1):
        i["id"] = f"I{n}"
    return [{"id": i["id"], **{k: v for k, v in i.items() if k != "id"}} for i in out]


DECISION_WORDS = {"included": "included", "include": "included", "accepted": "included", "used": "included",
                  "yes": "included", "включена": "included", "включено": "included", "взята": "included",
                  "rejected": "rejected", "reject": "rejected", "not included": "rejected", "no": "rejected",
                  "отклонена": "rejected", "отклонено": "rejected"}


def normalize_ideas(decisions: list, ids: list[str], *, strict: bool) -> list:
    """Решение по каждой идее; strict — пропущенные идеи считаются ошибкой ответа (председатель ответит заново),
    иначе (последняя попытка) отмечаются как «не рассмотрена», чтобы не потерять всё решение."""
    from .schemas import IdeaDecision
    if not ids:
        return []
    got: dict[str, IdeaDecision] = {}
    for d in decisions:
        m = re.search(r"\d+", d.id or "")
        key = f"I{m.group(0)}" if m else ""
        if key not in ids or key in got:
            continue
        word = DECISION_WORDS.get(d.decision.strip().lower(), "")
        if not word:
            word = "rejected" if re.search(r"reject|not|отклон|не ", d.decision, re.I) else "included"
        got[key] = d.model_copy(update={"id": key, "decision": word, "reason": d.reason.strip()})
    missing = [i for i in ids if i not in got]
    no_reason = [k for k, d in got.items() if d.decision == "rejected" and not d.reason]
    if strict and (missing or no_reason):
        parts = []
        if missing:
            parts.append(f'"ideas" must contain a decision for every idea; missing: {", ".join(missing)}')
        if no_reason:
            parts.append(f'rejected ideas need a specific "reason": {", ".join(no_reason)}')
        raise ValueError("; ".join(parts))
    return [got.get(i) or IdeaDecision(id=i, decision="not_addressed") for i in ids]


def _critique_notes(critiques: dict[str, CritiqueSet], per_label: int = 6) -> str:
    by_label: dict[str, list[str]] = {}
    for cs in critiques.values():
        for r in cs.reviews:
            items = by_label.setdefault(r.label, [])
            items += [f"+ {s}" for s in r.strengths[:2]] + [f"- {w}" for w in r.weaknesses[:2]]
    return "\n".join(
        f"{label}:\n  " + "\n  ".join(items[: per_label * 2]) for label, items in sorted(by_label.items())
    )
