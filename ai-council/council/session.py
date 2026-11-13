"""Оркестратор заседания совета: этапы 0–6, кворум, бюджет, стенограмма."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel

log = logging.getLogger("council.session")

from . import prompts
from .checkpoint import Journal, call_key
from .client import Completion, KeyLimit, LLMClient, OutOfCredits, ProviderError
from .config import CouncilConfig, MemberConfig, Mode
from .attachments import Attachment, documents_block, image_parts, images_note
from .inflight import InFlight
from .filerefs import FIX_MESSAGE, refs_in
from .schemas import Ballot, CritiqueSet, ImageNotes, Proposal, Revision, SupplementNeeded, Verdict
from .voting import (
    TallyRow,
    assign_labels,
    extract_json,
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
        link_checker: Callable[[list[str]], Awaitable[dict[str, Any]]] | None = None,
        pricing: dict[str, Any] | None = None,
        stage_out: dict[str, Any] | None = None,
        key_limit: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
        shared_dir: Any = None,
        session_id: str = "",
    ) -> None:
        """labels — метки вариантов, как в прошлом прогоне («доделать» заседание из архива: ответы из стенограммы
        ссылаются на эти метки)."""
        """vision: модель -> умеет ли принимать картинки (из каталога OpenRouter); неизвестно — пробуем."""
        self.cfg = config
        self.client = client
        self.on_event = on_event or (lambda *a, **k: None)
        if seed is None and session_id:
            # порядок меток и вариантов у судей — тот же при продолжении и «Доделать»: иначе запросы отличаются
            # от сохранённых, и все этапы оплачиваются заново (12.11 «Доделать» прогнал всё заседание ещё раз)
            import zlib
            seed = zlib.crc32(session_id.encode("utf-8"))
        self.rng = random.Random(seed)
        self.seed = seed
        # своя температура для этапов режима (например, «Креатив»: выше на предложениях и доработке)
        self.stage_temperature: dict[str, float] = {}
        # Журнал для приостановки: готовые ответы при продолжении берутся из него, а не у модели (см. checkpoint.py)
        self.journal = journal or Journal(None)
        self.budget = config.max_cost_usd  # можно поднять на ходу: set_budget()
        self._budget_warned = False
        self.replayed = 0  # сколько ответов взято из журнала
        self._in_flight = 0      # запросы к моделям, отправленные и ещё не вернувшиеся (резерв бюджета)
        self._reserved = 0.0     # оценка их цены
        self.pricing = pricing or {}  # модель -> estimate.Price (каталог OpenRouter): резерв по конкретному запросу
        # сколько токенов модели обычно пишут на этапе (по прошлым заседаниям) — резерв выхода без перестраховки
        self.stage_out = {k: v for k, v in (stage_out or {}).items() if isinstance(v, (int, float))}
        # общий учёт лимита ключа сайта и бота: остаток лимита и запросы всех идущих заседаний
        self.key_limit = key_limit
        self.shared = InFlight(shared_dir, session_id) if shared_dir and session_id else None
        # дата и время старта — метка заседания в журнале; 4 знака от полного номера различают заседания, начатые
        # в одну секунду (части серии) — название тут не годится: оно само может кончаться цифрой («…из-3»)
        import zlib
        self._tag = (f"[{session_id[:15]}·{zlib.crc32(session_id.encode()) & 0xFFFF:04x}] " if session_id else "")
        self._key_left: float | None = None
        self._key_at = 0.0
        self._key_spent = 0.0
        self._requests_done = 0
        self._fresh_stage = ""   # первый этап, где ответ пришлось получать от модели (не из журнала)
        self.link_checker = link_checker  # проверка ссылок реестра (в демо и тестах — нет)
        self.page_cache: dict[str, str] = {}
        # сводное заседание серии: реестры частей [(номер части, реестр)] — входят в общий реестр как проверенные
        self.inherited: list[tuple[int, dict[str, Any]]] = []
        # можно ли председателю разбить большое задание на серию (не у частей серии и не у сводного отчёта)
        self.allow_split = False
        # куда отдавать текст рассуждений, пока модель думает (сайт показывает его вживую; не сохраняется)
        self.thought_sink: Callable[[str, str, str, bool], None] | None = None
        # объекты, заданные программой (часть серии — только свои, сводка — все); иначе их находит постановка задачи
        self.fixed_objects: list[str] = []  # текст страниц, скачанных программой (ссылки авторов) — по адресу
        self._replaying = False
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

    QUIET_EVENTS = {"call", "cost"}  # их пишет _call и учёт расходов — в журнал по-своему

    def _log_event(self, event: str, data: dict[str, Any]) -> None:
        """Каждое событие заседания — в журнал программы: этапы и предупреждения — главное, остальное — подробно."""
        if event in self.QUIET_EVENTS:
            return
        if event == "stage":
            log.info("%s▶ этап %s: %s", self._tag, data.get("stage"), data.get("title"))
        elif event in ("warning", "member_failed"):
            log.warning("%s⚠ %s %s (%s): %s", self._tag, event, data.get("member", ""), data.get("stage", ""),
                        data.get("text") or data.get("error"))
        elif event == "tally":
            rows = data.get("rows") or []
            log.info("%sголосование: %s; согласие %s; ничья %s; решающие %s; спорные бюллетени %s", self._tag,
                     ", ".join(f"{getattr(r, 'label', '?')}={getattr(r, 'borda', 0):.2f}" for r in rows[:8]),
                     (data.get("agreement") or {}).get("level"), data.get("co_leaders") or "—",
                     data.get("decisive") or "—", list((data.get("ballot_flags") or {}).keys()) or "—")
        elif event == "verdict":
            v = data.get("verdict")
            log.info("%sитог: победитель %s, отступление «%s», известно %d, неизвестно %d, идей %d", self._tag,
                     getattr(v, "winner", "?"), getattr(v, "deviation", "")[:120], len(getattr(v, "known", []) or []),
                     len(getattr(v, "unknown", []) or []), len(data.get("ideas") or []))
        elif event == "ledger":
            led = data.get("ledger") or {}
            st: dict[str, int] = {}
            for c in led.get("claims") or []:
                st[c.get("status", "")] = st.get(c.get("status", ""), 0) + 1
            log.info("%sреестр: утверждений %d %s, источников %d, отложено %d", self._tag, len(led.get("claims") or []),
                     st, len(led.get("evidence") or []), len(led.get("deferred") or []))
        elif event in ("budget", "budget_warning", "links", "factcheck", "framing", "lenses", "followup"):
            log.info("%s%s: %s", self._tag, event, _short(data))
        else:
            log.debug("%s%s: %s", self._tag, event, _short(data))

    def _emit(self, event: str, **data: Any) -> None:
        try:
            self._log_event(event, data)
        except Exception:  # noqa: BLE001 — журнал не должен мешать заседанию
            pass
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
        if phase in ("done", "failed", "retry"):
            (log.warning if phase in ("failed", "retry") else log.info)(
                "%s%s %s · %s · %.1f с%s", self._tag, {"done": "✓", "failed": "✗", "retry": "↻"}[phase], member, stage,
                now - started, f" · {data['detail']}" if data.get("detail") else "")
        elif phase == "queued":
            log.debug("%s… %s · %s ждёт свободного слота", self._tag, member, stage)
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
        max_tokens: int | None = None,
        web_fetch: int = 0,
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
        loose = cached is None
        if loose and self._fresh_stage and self._fresh_stage != stage:
            # раньше на другом этапе ответ пришлось получать заново — его вход изменился, и ответы этого этапа
            # из журнала (по номеру обращения) могли быть даны на другой вход: спрашиваем заново
            loose = False
            cached = None
        if loose:  # запрос изменился (обновление программы) или «доделать» из архива: ответ того же участника
            cached = self.journal.loose.get((stage, member.name, n))  # на том же этапе по порядку обращений
        if cached is not None:  # ответ уже получен и оплачен раньше
            try:
                obj = schema.model_validate(cached)
                if check and loose:
                    self._replaying = True  # проверки формата — да, «сократите» к оплаченному ответу — нет
                    try:
                        obj = check(obj)  # тот же ли набор меток и критериев — иначе спросим заново
                    finally:
                        self._replaying = False
            except ValueError:
                obj = None
            if obj is not None:
                self.replayed += 1
                log.info("%s⟲ %s · %s#%d — ответ из сохранённого (%s), без оплаты", self._tag, member.name, stage, n,
                         "по номеру обращения" if loose else "точное совпадение запроса")
                self._call(member.name, stage, "done", time.monotonic(), detail="из сохранённого",
                           cost=round(self.stats[member.name].cost, 6))
                return obj
        if key in self.journal.failures:  # участник не справился до приостановки — ход заседания тот же
            log.info("%s⟲ %s · %s — сбой из сохранённого: %s", self._tag, member.name, stage, self.journal.failures[key][:200])
            raise InvalidResponse(self.journal.failures[key])
        if not self._fresh_stage and stage not in ("describe",):
            self._fresh_stage = stage
        try:
            obj = await self._ask_model(member, stage, schema, base_messages, messages, meta, check, web_search, n,
                                        max_tokens, web_fetch)
        except (InvalidResponse, ProviderError) as e:
            if not isinstance(e, OutOfCredits):
                self.journal.fail(key, str(e), stage)
            raise
        self.journal.ok(key, obj.model_dump(), stage)
        return obj

    async def _ask_model(self, member: MemberConfig, stage: str, schema: type[T], base_messages: list, messages: list,
                         meta: dict[str, Any], check: Callable[[T], T] | None, web_search: bool = False,
                         call_n: int = 0, max_tokens_override: int | None = None, web_fetch: int = 0) -> T:
        max_tokens = max_tokens_override or member.max_tokens or self.cfg.max_tokens
        temperature = member.temperature if member.temperature is not None else self.cfg.temperature
        if stage in self.stage_temperature:
            temperature = max(temperature, self.stage_temperature[stage])
        if stage in self.JUDGE_STAGES and self.cfg.judge_temperature is not None:
            temperature = self.cfg.judge_temperature  # оценки и итог — с меньшей случайностью
        effort = member.reasoning_effort or self.cfg.reasoning_effort
        if stage == "vote" and self.cfg.vote_reasoning:
            effort = self.cfg.vote_reasoning  # ранжирование готовых вариантов — без глубоких скрытых рассуждений
        error = ""
        started = time.monotonic()
        name = member.name

        def progress(ev: dict[str, Any]) -> None:
            ev = dict(ev)
            # текст рассуждения — только для показа вживую: в события (их пишут на диск) он не попадает
            th, th_hidden = ev.pop("thought", ""), ev.pop("thought_hidden", False)
            if self.thought_sink is not None and (th or th_hidden):
                try:
                    self.thought_sink(name, stage, th, bool(th_hidden))
                except Exception:  # noqa: BLE001 — показ мыслей не должен мешать заседанию
                    pass
            phase = ev.pop("phase", "")
            if phase == "sent" and ev.get("attempt", 1) > 1:
                ev["detail"] = f"попытка {ev['attempt']}"
            self._call(name, stage, phase, started, **ev)

        attempt = 0          # попытки получить корректный ответ (исправление формата, ссылки на файлы)
        continuations = 0    # продолжения длинного ответа, упёршегося в лимит (попытками не считаются)
        acc = ""             # уже полученная часть ответа, который продолжается
        while attempt <= self.cfg.json_retries:
            if self.total_cost >= self.budget:
                log.warning("%sбюджет $%g исчерпан (потрачено $%.4f) — пауза", self._tag, self.budget, self.total_cost)
                raise BudgetExceeded(
                    f"достигнут лимит бюджета ${self.budget:g} (потрачено ${self.total_cost:.4f})"
                )
            cost_est = self._call_estimate(member, messages, max_tokens, web_search, stage) if self.cfg.budget_reserve else 0.0
            await self._check_key(cost_est)
            log.debug("%sрезерв: %s · %s — этот запрос ≈$%.4f, в работе ≈$%.4f (%d), потрачено $%.4f из $%g",
                      self._tag, name, stage, cost_est, self._reserved, self._in_flight, self.total_cost, self.budget)
            if self.cfg.budget_reserve and self.total_cost + self._reserved + cost_est >= self.budget:
                log.warning("%sрезерв бюджета: %s · %s не помещается (≈$%.4f) — пауза", self._tag, name, stage, cost_est)
                raise BudgetExceeded(
                    f"бюджет ${self.budget:g} будет превышен: потрачено ${self.total_cost:.4f}, запросы в работе "
                    f"({self._in_flight}) — около ${self._reserved:.2f}, этот запрос — около ${cost_est:.2f}. Пауза, "
                    f"чтобы не превысить лимит; поднимите бюджет примерно до "
                    f"${self.total_cost + self._reserved + cost_est + 0.5:.2f} и продолжите")
            if self._sem.locked():
                self._call(name, stage, "queued", started, detail="ждёт свободного слота")
            try:
                async with self._sem:
                    # у границы бюджета запросы идут по два: меньше запросов «в полёте» — меньше превышение лимита
                    async with (self._narrow if self._budget_warned else contextlib.nullcontext()):
                        extra: dict[str, Any] = ({"web_search": True, "web_tools": self.cfg.web_tools,
                                                  "search_engine": self.cfg.search_engine,
                                                  "search_uses": self.cfg.search_uses,
                                                  # председатель при итоге читает всё заседание — его поиск с пределом
                                                  "search_unlimited": self.cfg.search_unlimited
                                                  and stage in ("proposal", "revision"),
                                                  "search_results": self.cfg.search_results} if web_search else {})
                        if web_fetch:
                            extra["web_fetch"] = web_fetch
                        if self.cfg.json_mode and not acc:  # продолжение обрывка — не JSON-объект целиком
                            extra["json_mode"] = True
                        log.info("%s→ %s [%s] · %s#%d · попытка %d%s: вход ≈%d зн., предел %s, t=%s, рассуждения %s%s%s%s",
                                 self._tag, name, member.model, stage, call_n, attempt + 1,
                                 f" · продолжение {continuations}" if acc else "",
                                 sum(len(m["content"]) if isinstance(m["content"], str) else 0 for m in messages),
                                 max_tokens, temperature, effort or "по умолчанию",
                                 " · поиск" if web_search else "", f" · страниц {web_fetch}" if web_fetch else "",
                                 " · JSON" if extra.get("json_mode") else "")
                        self._in_flight += 1
                        self._reserved += cost_est
                        if self.shared:
                            self.shared.set(self._reserved)
                        try:
                            comp = await self._complete(member, messages, temperature, max_tokens, effort, meta, progress,
                                                        extra)
                        finally:
                            self._in_flight -= 1
                            self._reserved = max(0.0, self._reserved - cost_est)
                            if self.shared:
                                self.shared.set(self._reserved)
                        self._requests_done += 1
            except ProviderError as e:
                if e.spent is not None:  # неудачные попытки тоже стоят денег
                    self._account(member, e.spent)
                self._call(name, stage, "failed", started, detail=str(e)[:300])
                raise
            self._account(member, comp)
            log.info("%s← %s · %s: %s, вход %d (кэш %d), выход %d (рассужд. %d), $%.5f, %.1f с, finish=%s, %d зн., модель %s",
                     self._tag, name, stage, comp.gen_id or "без номера", comp.prompt_tokens, comp.cached_tokens,
                     comp.completion_tokens, comp.reasoning_tokens, comp.cost, time.monotonic() - started,
                     comp.finish_reason, len(comp.text), comp.model or member.model)
            self._call(name, stage, "checking", started, tokens=comp.completion_tokens)
            max_tokens = max(max_tokens, comp.max_tokens or 0)  # клиент мог уже увеличить лимит
            text = acc + comp.text if acc else comp.text
            entry = {
                "stage": stage,
                "member": member.name,
                "model": comp.model or member.model,
                "attempt": attempt + 1,
                "call": call_n,  # номер обращения участника на этапе — по нему продолжение находит ответ
                "raw": comp.text,
                "cost": comp.cost,
                "tokens": [comp.prompt_tokens, comp.completion_tokens],
                "cached": getattr(comp, "cached_tokens", 0) or 0,
            }
            if acc:
                entry["continuation"] = continuations
            try:
                if comp.finish_reason in ("length", "max_tokens", "MAX_TOKENS") and _unclosed(text):
                    # ответ оборван на лимите: найденный в нём целый объект — лишь черновик или часть, а не ответ
                    raise ValueError("JSON-объект оборван (похоже, ответ обрезан по лимиту токенов)")
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
            except SupplementNeeded as e:  # ответ годен, не хватает части — короткая дозаявка, не переписывание
                entry["note"] = f"принят с дозаявкой: {e.what or str(e)[:200]}"
                log.info("%s%s · %s: ответ принят, дозаявка — %s", self._tag, name, stage, e.what or str(e)[:200])
                self._call(name, stage, "retry", started, detail=f"ответ принят, прошу дописать {e.what or 'недостающее'}")
                obj = e.obj
                try:
                    addon = await self._ask_model(
                        member, stage, e.schema, base_messages,
                        list(base_messages) + [{"role": "user", "content": e.supplement_prompt}], meta, None,
                        web_search, call_n, min(max_tokens, 16000), web_fetch)
                    obj = e.merge(addon)
                except (InvalidResponse, ProviderError, BudgetExceeded) as e2:
                    # дозаявка не удалась (в том числе кончились деньги или бюджет) — принимаем оплаченный ответ как
                    # есть; если дело в деньгах, заседание встанет на паузу на следующем запросе, ответ уже сохранён
                    log.warning("%s%s · %s: дозаявка не удалась (%s) — ответ принят без неё", self._tag, name, stage, e2)
                    self.stats[name].failures.append(f"{stage}: дозаявка не удалась: {str(e2)[:200]}")
                if check:
                    try:
                        obj = check(obj)  # повторная проверка уже без дозаявок (их — одна на ответ)
                    except ValueError:
                        pass
            except ValueError as e:  # pydantic.ValidationError тоже ValueError
                error = readable_error(e)
                log.warning("%s%s · %s: ответ не принят (%s)", self._tag, name, stage, error[:400])
                log.debug("%s%s · %s: начало ответа: %r … конец: %r", self._tag, name, stage, text[:600], text[-400:])
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
                    continuations = 0  # исправленный ответ пишется заново — ему снова можно продолжаться
                    hint = f"ответ не в нужном формате ({error[:120]}) — прошу модель исправить"
                if attempt < self.cfg.json_retries:
                    self._call(name, stage, "retry", started, detail=hint)
                attempt += 1
                continue
            if "search_citations" in type(obj).model_fields:  # ссылки, которые реально вернул поиск
                obj = obj.model_copy(update={"search_citations": list(getattr(comp, "citations", []) or [])})
            entry["parsed"] = obj.model_dump()
            entry["gen_id"] = comp.gen_id
            self._log(entry)
            self._call(name, stage, "done", started, cost=round(self.stats[name].cost, 6))
            return obj
        self._call(name, stage, "failed", started, detail=f"некорректный ответ: {error}"[:300])
        raise InvalidResponse(f"{member.name}: некорректный ответ после {self.cfg.json_retries + 1} попыток: {error}")

    MAX_CONTINUATIONS = 4  # сколько раз можно попросить продолжить длинный ответ
    JUDGE_STAGES = {"framing", "critique", "vote", "verdict"}  # здесь — judge_temperature

    def _quorum(self, authors: int, mode: str) -> int:
        """Сколько вариантов нужно: при 4+ авторах в обсуждении и исследовании — не меньше quorum_large
        (при двух вариантах у судьи один чужой, выбор по сути делает председатель)."""
        if mode in ("debate", "research", "creative") and authors >= 4:
            return max(self.cfg.min_quorum, self.cfg.quorum_large)
        return self.cfg.min_quorum

    async def _complete(self, member: MemberConfig, messages: list, temperature: float, max_tokens: int, effort: str,
                        meta: dict[str, Any], progress: Any, extra: dict[str, Any]) -> Completion:
        return await self.client.complete(member.model, messages, temperature=temperature, max_tokens=max_tokens,
                                          reasoning_effort=None if effort == "none" else effort, meta=meta,
                                          on_progress=progress, **extra)

    SEARCH_RESERVE_USD = 0.02  # поиск в интернете на запрос — оценка сверху (у OpenRouter оплачивается отдельно)
    CHARS_PER_TOKEN = 3.0      # для оценки входа: русский текст ~2,5–3 знака на токен, английский ~4

    KEY_REFRESH_S = 300  # остаток лимита ключа уточняется не чаще раза в 5 минут (и когда он близок к концу)

    async def _check_key(self, cost_est: float) -> None:
        """Общий лимит ключа OpenRouter: остаток минус потраченное с последней сверки, минус запросы в работе у всех
        заседаний сайта и бота. Не хватает — пауза «лимит ключа» до того, как ключ откажет посреди этапа."""
        if not (self.cfg.key_reserve and self.key_limit):
            return
        now = time.monotonic()
        left = None if self._key_left is None else self._key_left - (self.total_cost - self._key_spent)
        if left is None or now - self._key_at > self.KEY_REFRESH_S or left < 5:
            try:
                info = await self.key_limit()
            except Exception:  # noqa: BLE001 — не узнали остаток — не мешаем работе
                info = None
            self._key_at = now
            if not info:
                self._key_left = None
                return
            self._key_left, self._key_spent = float(info["remaining"]), self.total_cost
            left = self._key_left
        others = self.shared.others() if self.shared else 0.0
        if left - others - self._reserved - cost_est < 0:
            raise KeyLimit(f"лимита ключа OpenRouter не хватает: осталось ≈${max(left, 0):.2f}, запросы в работе у всех "
                           f"заседаний ≈${others + self._reserved:.2f}, этот запрос ≈${cost_est:.2f}. Пауза — продолжите, "
                           "когда лимит обновится или его поднимут")

    def _call_estimate(self, member: MemberConfig, messages: list, max_tokens: int | None, web: bool,
                       stage: str = "") -> float:
        """Сколько может стоить этот запрос: вход по длине, выход — доля предела ответа, поиск. Цена модели — по
        каталогу; если её нет — средняя цена уже сделанных запросов (так было раньше)."""
        chars = 0
        for m in messages:
            c = m.get("content")
            chars += len(c) if isinstance(c, str) else sum(len(p.get("text", "")) for p in c or [] if isinstance(p, dict))
        fetch = 0.01 * (self.cfg.fact_check.fetch_pages if stage in ("arbitration", "recheck") else 0)
        price = self.pricing.get(member.model)
        if price is None:
            return self.total_cost / self._requests_done if self._requests_done else 0.0
        limit = max_tokens or member.max_tokens or self.cfg.max_tokens or 4000
        hist = self.stage_out.get(stage)
        if hist:  # по прошлым заседаниям: обычная длина ответа на этапе с запасом (критика — на все варианты сразу)
            out = min(limit, hist * (6 if stage == "critique" else 1) * 1.5)
        else:
            out = limit * self.cfg.reserve_output_share
        # с поиском модель получает весь запрос заново на каждом шаге поиска (вход растёт в разы), а сам поиск
        # оплачивается по числу результатов (Exa ≈ $0,004 за результат)
        unlimited = self.cfg.search_unlimited and stage in ("proposal", "revision")  # авторы; не итог и не проверка
        uses = 10 if unlimited else self.cfg.search_uses  # без ограничения — резерв как на 10 поисков
        steps = 1 + uses / 2 if web else 1
        search = (max(self.SEARCH_RESERVE_USD, 0.004 * uses * self.cfg.search_results) if web else 0.0)
        return (chars / self.CHARS_PER_TOKEN * price.prompt * steps + out * price.completion + price.request
                + search + fetch)

    def _docs_text(self) -> dict[str, str]:
        """Текст документов пользователя и описания картинок по файлам — для сверки цитат «из документов»."""
        out = {a.name: a.text for a in self.attachments if a.kind == "document" and a.text}
        out.update({str(n): str(x) for n, x in (self.image_notes or {}).items() if x})
        return out

    def _sits_out(self, m: MemberConfig, stage: str, members: list[MemberConfig]) -> bool:
        """Эксперимент пилота: председатель без своего варианта не критикует и/или не голосует (если без него
        останется хотя бы два судьи)."""
        if m.name != self.cfg.chair or not self.chair_judge or len(members) - 1 < 2:
            return False
        return (stage == "critique" and not self.cfg.chair_critiques) or (stage == "vote" and not self.cfg.chair_votes)

    def _input_gaps(self, gaps: list[str], chair: MemberConfig) -> None:
        """Председатель заметил, что вход обрезан или недоступен, — показать это пользователю."""
        for g in gaps[:5]:
            self._emit("warning", stage="framing", member=chair.name, text=f"во входе может не хватать: {g}")

    def verdict_tokens(self, chair: MemberConfig) -> int | None:
        """Предел ответа председателя на итоге: длинный синтез одним ответом, без склеек из продолжений."""
        return max(chair.max_tokens or 0, self.cfg.verdict_max_tokens) or None

    def author_tokens(self, member: MemberConfig) -> int | None:
        """Предел ответа автора исследования: не меньше общего «Лимита длины ответа» из настроек (думающим моделям
        нужно место на скрытые рассуждения, а платится только написанное) и не меньше длины варианта × токенов на
        слово; личный предел участника важнее."""
        rc = self.cfg.research
        if member.max_tokens or not rc.author_tokens_per_word:
            return member.max_tokens or None
        return max(self.cfg.max_tokens or 0, rc.author_min_tokens, int(rc.max_words * rc.author_tokens_per_word))

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

        # Каждый режим — свой файл в council/modes/: debate, quick, evaluate (общие этапы — common), research
        from .modes import creative, debate, evaluate, quick
        common = {"criteria": criteria, "context": context, "followup_of": followup_of, "started": started, "t0": t0}
        if mode == "research":
            return await self._run_research(question, **common)
        if mode == "evaluate":
            return await evaluate.run(self, question, options=options or [], **common)
        if mode == "quick":
            return await quick.run(self, question, **common)
        if mode == "creative":
            return await creative.run(self, question, **common)
        return await debate.run(self, question, rounds=rounds, **common)

    # ------------------------------------------------------------------ режим «Исследование» — council/modes/research.py

    async def _run_research(self, question: str, *, criteria: list[str] | None, context: str | None,
                            followup_of: str, started: str, t0: float) -> SessionResult:
        from .modes import research as mode
        return await mode.run(self, question, criteria=criteria, context=context, followup_of=followup_of,
                              started=started, t0=t0)

    # Обёртки для пилота («переиграть проверку») и тестов — сам код в council/modes/research.py
    def _ledger(self, R: Any, selected: list[dict[str, Any]], results: list[Any], rc: Any,
                deferred: list[dict[str, Any]]) -> dict[str, Any]:
        from .modes import research as mode
        return mode.make_ledger(self, R, selected, results, rc, deferred)

    async def _links(self, R: Any, ledger: dict[str, Any], rc: Any) -> None:
        from .modes import research as mode
        await mode.check_links(self, R, ledger, rc)

    async def _search(self, R: Any, question: str, as_of: str, scope: str, selected: list[dict[str, Any]], rc: Any,
                      stage: str, *, recheck: bool = False, missions: int | None = None) -> list[Any]:
        from .modes import research as mode
        return await mode.search(self, R, question, as_of, scope, selected, rc, stage, recheck=recheck, missions=missions)

    async def _fact_check(self, question: str, proposals: dict[str, Proposal], rows: list[TallyRow]) -> dict[str, dict]:
        from .modes import common
        return await common.fact_check(self, question, proposals, rows)


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


def _short(data: dict[str, Any], limit: int = 600) -> str:
    """Краткий вид события для журнала: без длинных текстов вариантов."""
    import json as _json
    try:
        t = _json.dumps(data, ensure_ascii=False, default=lambda o: getattr(o, "model_dump", lambda: str(o))())
    except (TypeError, ValueError):
        t = str(data)
    return t if len(t) <= limit else t[:limit] + f"… (+{len(t) - limit} зн.)"


def _letters(text: str) -> int:
    return len(re.findall(r"[^\W\d_]", text or ""))


def _is_placeholder(text: str) -> bool:
    t = (text or "").strip()
    return bool(PLACEHOLDER_RE.search(t)) and _letters(t) < 400


def _unclosed(text: str) -> bool:
    """В конце ответа остался незакрытый JSON-объект (скобки вне строк не сходятся)."""
    depth, in_str, esc = 0, False, False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
    return depth > 0 or in_str


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


def ideas_from_ballots(ballots: dict[str, Ballot], exclude: set[str], valid: set[str]) -> list[dict[str, Any]]:
    """«Быстрый ответ»: критики нет — ценное из вариантов не на первом месте называют голосующие."""
    picked: dict[str, dict[str, Any]] = {}
    for voter, b in sorted(ballots.items()):
        for k in b.keep_from_others:
            label = k.get("label") or ""
            if label in exclude or (label and label not in valid):
                continue
            key = _idea_key(k["idea"])
            if key in picked:
                picked[key]["by"].append(voter)
            elif len(picked) < IDEAS_MAX:
                picked[key] = {"label": label or "?", "idea": k["idea"], "by": [voter]}
    out = list(picked.values())
    return [{"id": f"I{n}", **i} for n, i in enumerate(out, 1)]


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
