"""Telegram-бот совета нейросетей.

Запуск: `python -m council telegram`. Нужны переменные окружения (или .env):
  TELEGRAM_BOT_TOKEN       токен от @BotFather
  TELEGRAM_ALLOWED_USERS   Telegram ID пользователей через запятую, кому можно пользоваться ботом
  OPENROUTER_API_KEY       ключ OpenRouter (без него работает только демо-режим)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..client import LLMClient, OpenRouterClient, ProviderError
from ..config import CouncilConfig, OverrideError, load_config, with_overrides
from ..estimate import ModelCatalog, estimate, learn_lengths, parse_pricing, similar_actual, unknown_models, vision_map
from ..mock import MockClient
from ..archive import MIME, Archive, all_reports, available_formats, download_name, key_of
from ..attachments import (MAX_FILE_BYTES, MAX_FILES, Attachment, AttachmentError, assign_files, limit_total,
                           load_attachment, merge)
from ..report import make_stem, save
from ..reports.brand import set_brand
from ..checkpoint import Checkpoint, Journal, classify
from ..session import Council, CouncilError, SessionResult
from .api import TelegramAPI, TelegramError
from ..reports.view import short_title
from .format import esc, md_to_tg, split_message, strip_tags

log = logging.getLogger("council.telegram")
from ..diagnostics import bundle, save_events, save_failed, setup_logging, stamp  # noqa: E402

STAGES = [("framing", "Постановка"), ("proposal", "Предложения"), ("critique", "Критика"),
          ("revision", "Доработка"), ("vote", "Голосование"), ("factcheck", "Факты"), ("verdict", "Вердикт")]
MODE_NAME = {"debate": "Дебаты", "quick": "Быстро", "evaluate": "Оценка вариантов"}
MODE_FULL = {"debate": "Подробное обсуждение", "quick": "Быстрый ответ", "evaluate": "Выбор из ваших вариантов"}
STAGE_HINT = {
    "framing": "председатель уточняет вопрос и критерии",
    "proposal": "каждая модель вслепую предлагает решение",
    "critique": "модели анонимно оценивают чужие решения",
    "revision": "авторы улучшают решения с учётом критики",
    "vote": "модели ранжируют чужие решения",
    "factcheck": "модель с поиском проверяет факты",
    "verdict": "председатель пишет итоговое решение",
}
COMMANDS = [
    ("ask", "задать вопрос совету (в группе)"),
    ("settings", "режим, модели, проверка фактов, демо"),
    ("archive", "прошлые заседания и отчёты"),
    ("stats", "расходы и успехи моделей"),
    ("stop", "приостановить идущее заседание"),
    ("paused", "приостановленные заседания — продолжить"),
    ("budget", "лимит стоимости, напр. /budget 0.5"),
    ("evaluate", "выбрать лучший из ваших вариантов"),
    ("clear", "убрать приложенные файлы"),
    ("cancel", "отменить вопрос или уточнение"),
    ("logs", "журнал для разбора ошибок"),
    ("help", "как пользоваться"),
]
HELP = """<b>Совет нейросетей</b>
Несколько моделей (Claude, ChatGPT, Gemini и др.) вслепую предлагают решения, анонимно критикуют друг друга, голосуют — а председатель пишет итог.

<b>Как спросить</b>
Просто напишите вопрос — или пришлите голосовое 🎤. Файлы 📎 (фото, PDF, Word, Excel) — с подписью-вопросом или перед вопросом.
Бот покажет стоимость → нажмите <b>▶️ Начать</b>. Через несколько минут придёт решение.

<b>Полезное</b>
/settings — режим, модели, проверка фактов, демо
/archive — прошлые заседания и отчёты (<code>/archive кофейня</code> — поиск)
/evaluate — выбрать лучший из ваших вариантов
/budget 0.5 — лимит стоимости заседания
/stop — остановить · /logs — журнал ошибок

В группе вопрос задаётся так: <code>/ask вопрос</code>."""
FORMAT_NAME = {"pdf": "PDF", "docx": "Word", "md": "Markdown"}
ARCHIVE_PAGE = 8


# ---------------------------------------------------------------- состояние


@dataclass
class ChatSettings:
    mode: str = "debate"
    rounds: int = 1
    members: list[str] | None = None  # None — как в config.yaml
    budget: float | None = None
    demo: bool = False
    fact_check: bool | None = None  # None — как в config.yaml
    compact: bool | None = None     # компактные варианты; None — как в config.yaml


@dataclass
class Pending:
    question: str
    options: list[str] | None = None
    card_id: int | None = None
    view: str = "main"  # main | opts | members
    attachments: list[Attachment] = field(default_factory=list)
    last_at: float = 0.0   # когда пришла последняя часть текста вопроса
    last_len: int = 0      # её длина (длинный текст Telegram сам режет на сообщения по ~4096 символов)
    parent: str = ""        # уточнение к заседанию
    parent_title: str = ""


@dataclass
class Progress:
    question: str
    mode: str
    rounds: int
    members: list[str]
    chair: str
    demo: bool
    stage: str | None = None
    stage_title: str = ""
    done_stages: set[str] = field(default_factory=set)
    work: dict[str, str] = field(default_factory=dict)
    proposals: dict[str, str] = field(default_factory=dict)
    authors: dict[str, str | None] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    cost: float = 0.0
    started: float = field(default_factory=time.monotonic)
    stage_started: float = field(default_factory=time.monotonic)
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    saving: bool = False
    chair_judge: bool = False  # председатель без своего варианта
    fact_check: bool = False   # будет ли этап проверки фактов

    def apply(self, event: str, d: dict[str, Any]) -> None:
        if event == "call":
            c = self.calls.get(d["member"])
            if not c or c.get("stage") != d["stage"]:
                c = {"stage": d["stage"], "chars": 0}
            c.update(phase=d["phase"], since=time.monotonic() - float(d.get("elapsed") or 0),
                     detail=d.get("detail") or (c.get("detail") if d["phase"] == "retry" else ""))
            if d.get("chars") is not None:
                c["chars"] = d["chars"]
            if d["phase"] in ("done", "failed"):
                c["final"] = float(d.get("elapsed") or 0)
                if d["phase"] == "done":
                    self.work[d["member"]] = "done"
            self.calls[d["member"]] = c
        elif event == "stage":
            self.stage_started = time.monotonic()
            if self.stage and self.stage != d["stage"]:
                self.done_stages.add(self.stage)
            self.stage, self.stage_title = d["stage"], d["title"]
            if d["stage"] == "proposal":
                self.chair_judge = d.get("chair_proposes") is False
            who = (["факт-чек"] if d["stage"] == "factcheck"
                   else [self.chair] if d["stage"] in ("framing", "verdict")
                   else [m for m in self.members if m != self.chair]
                   if d["stage"] in ("proposal", "revision") and self.chair_judge else self.members)
            self.work = {n: "work" for n in who}
        elif event == "framing":
            self.done_stages.add("framing")
        elif event == "member_done":
            self.work[d["member"]] = "done"
        elif event == "member_failed":
            self.work[d["member"]] = "fail"
            self.failures.append(f"{d['member']} ({d['stage']}): {d['error'][:150]}")
        elif event == "proposals":
            self.proposals = {l: p.title for l, p in d["proposals"].items()}
            self.authors = d["authors"]
        elif event == "revisions":
            self.proposals.update({l: p.title for l, p in d["revised"].items()})
        elif event == "cost":
            self.cost = d["total"]

    def _member_line(self, name: str) -> str:
        c = self.calls.get(name)
        if not c or c.get("stage") != self.stage:
            return f"▫️ {esc(name)} — ожидает"
        phase = c.get("phase")
        active = phase not in ("done", "failed")
        secs = int(time.monotonic() - c["since"]) if active else int(c.get("final") or 0)
        tok = max(1, round((c.get("chars") or 0) / 3.2))
        text = {
            "queued": ("🕓", "в очереди"),
            "sent": ("📨", "запрос отправлен"),
            "thinking": ("🤔", "думает"),
            "writing": ("✍️", f"пишет ответ, ≈{tok} ток."),
            "checking": ("🔎", "проверяю ответ"),
            "retry": ("🔁", f"повтор: {c.get('detail') or 'временная ошибка'}"),
            "done": ("✅", "готово"),
            "failed": ("❌", f"сбой: {c.get('detail') or ''}"),
        }.get(phase, ("⏳", phase or ""))
        return f"{text[0]} {esc(name)} — {esc(text[1][:120])} · {_fmt_secs(secs)}"

    def skipped(self) -> set[str]:
        skip = {"quick": {"critique", "revision"}, "evaluate": {"proposal", "revision"}}.get(self.mode, set())
        return skip if self.fact_check else skip | {"factcheck"}

    def render(self, *, final: str | None = None) -> str:
        head = final or "🏛 <b>Совет заседает…</b>"
        meta = MODE_FULL.get(self.mode, self.mode) + (f" · раундов: {self.rounds}" if self.mode == "debate" and self.rounds > 1 else "")
        if self.demo:
            meta += " · демо"
        lines = [head, f"❓ {esc(short_title(self.question))}", f"<i>{esc(meta)}</i>", ""]
        plan = [(k, n) for k, n in STAGES if k not in self.skipped()]
        done = sum(1 for k, _ in plan if k in self.done_stages)
        idx = next((i for i, (k, _) in enumerate(plan) if k == self.stage), None)
        bar = "▰" * done + "▱" * (len(plan) - done)
        if final:
            lines.append(f"{bar} {done} из {len(plan)} этапов")
        elif self.saving:
            lines += [f"{'▰' * len(plan)} Готовлю отчёты…"]
        elif self.stage and idx is not None:
            ready = sum(1 for s in self.work.values() if s != "work")
            stage_secs = int(time.monotonic() - self.stage_started)
            lines.append(f"{bar} Этап {idx + 1} из {len(plan)}: <b>{esc(dict(plan)[self.stage])}</b>")
            round_ = re.search(r"раунд (\d+)/(\d+)", self.stage_title or "")
            hint = STAGE_HINT.get(self.stage, "")
            if round_ and int(round_.group(2)) > 1:
                hint = f"раунд {round_.group(1)} из {round_.group(2)} — {hint}"
            lines.append(f"<i>{esc(hint)} · готово {ready} из {len(self.work)} · {_fmt_secs(stage_secs)}</i>")
            lines += [self._member_line(n) for n in self.work]
            slow = [n for n, c in self.calls.items() if c.get("stage") == self.stage and self.work.get(n) == "work"
                    and c.get("phase") in ("sent", "thinking") and time.monotonic() - c["since"] > 40]
            if slow:
                lines.append(f"<i>⏳ {esc(', '.join(slow))}: «думающие» модели рассуждают до 1–3 минут — "
                             f"это нормально, процесс не завис.</i>")
        else:
            lines.append(f"{bar} Запускаю…")
        if self.proposals and not final:
            lines += ["", "<b>Варианты:</b> " + ", ".join(sorted(self.proposals))]
        if self.failures:
            lines += ["", "<b>Сбои:</b>"] + [f"❌ {esc(f)}" for f in self.failures[-3:]]
        secs = int(time.monotonic() - self.started)
        lines += ["", f"💰 ${self.cost:.2f} · ⏱ {_fmt_secs(secs)}"]
        return "\n".join(lines)


def _plural(n: int, one: str, few: str, many: str) -> str:
    m10, m100 = n % 10, n % 100
    return one if m10 == 1 and m100 != 11 else few if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else many


def _fmt_secs(secs: int) -> str:
    return f"{secs // 60}:{secs % 60:02d}" if secs >= 60 else f"{secs} с"


@dataclass
class Running:
    task: asyncio.Task
    progress: Progress
    message_id: int
    dirty: bool = True
    council: Any = None            # идущее заседание — чтобы поднять бюджет на ходу
    checkpoint: Any = None


# ---------------------------------------------------------------- бот


class CouncilBot:
    def __init__(
        self,
        api: TelegramAPI,
        config_path: str | Path,
        *,
        allowed_users: set[int],
        llm_factory: Any = None,
        edit_interval: float = 2.0,
        mock_delay: float = 0.8,
    ) -> None:
        self.api = api
        self.config_path = Path(config_path)
        self.last_stem: dict[int, str] = {}   # последнее заседание чата (для /logs)
        self.allowed = allowed_users
        self.edit_interval = edit_interval
        self.mock_delay = mock_delay
        self.llm_factory = llm_factory or self._default_llm
        self.catalog = ModelCatalog()
        self.pending: dict[int, Pending] = {}
        self.running: dict[int, Running] = {}
        self.username = ""
        self.archive_query: dict[int, str] = {}
        self.drafts: dict[int, list[Attachment]] = {}   # файлы, присланные до вопроса
        self.followup_wait: dict[int, str] = {}         # чат ждёт уточняющий вопрос к этому заседанию
        self._album_replied: set[str] = set()
        self._tasks: set[asyncio.Task] = set()          # ссылки на задачи, чтобы их не собрал сборщик мусора
        self._chat_locks: dict[int, asyncio.Lock] = {}  # сообщения одного чата — строго по очереди
        cfg = self.base_config()
        self.runs_dir = Path(cfg.runs_dir) if Path(cfg.runs_dir).is_absolute() else self.config_path.parent / cfg.runs_dir
        self.settings_path = self.runs_dir / "telegram_settings.json"
        self.settings: dict[int, ChatSettings] = self._load_settings()
        self.archive = Archive(self.runs_dir, isolate=True)

    # ------------------------------------------------------------ инфраструктура

    def base_config(self) -> CouncilConfig:
        cfg = load_config(self.config_path)
        set_brand(cfg.branding, self.config_path.parent)
        return cfg

    def _load_settings(self) -> dict[int, ChatSettings]:
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
            return {int(k): ChatSettings(**v) for k, v in raw.items()}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_settings(self) -> None:
        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            data = {str(k): asdict(v) for k, v in self.settings.items()}
            self.settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            log.warning("не удалось сохранить настройки: %s", e)

    def chat_settings(self, chat_id: int) -> ChatSettings:
        if chat_id not in self.settings:
            self.settings[chat_id] = ChatSettings(demo=not os.environ.get("OPENROUTER_API_KEY"))
        return self.settings[chat_id]

    def _default_llm(self, cfg: CouncilConfig, demo: bool) -> LLMClient:
        if demo:
            return MockClient(delay=self.mock_delay)
        return OpenRouterClient(os.environ["OPENROUTER_API_KEY"], timeout=cfg.timeout_s, max_retries=cfg.max_retries,
                            max_tokens_cap=cfg.max_tokens_cap)

    def build_config(self, s: ChatSettings) -> CouncilConfig:
        base = self.base_config()
        members = s.members
        if members is not None:
            known = {m.name for m in base.members}
            members = [m for m in members if m in known]  # участника могли удалить из config.yaml
        return with_overrides(base, members=members, budget=s.budget, fact_check=s.fact_check, compact=s.compact)

    async def send(self, chat_id: int, text: str, **kw: Any) -> dict[str, Any] | None:
        """Отправка длинного HTML с разбиением; при ошибке разметки — простым текстом."""
        parts = split_message(text)
        msg = None
        for i, part in enumerate(parts):
            markup = kw.get("reply_markup") if i == len(parts) - 1 else None
            try:
                msg = await self.api.send_message(chat_id, part, reply_markup=markup)
            except TelegramError as e:
                if "parse" not in e.description.lower():
                    raise
                msg = await self.api.send_message(chat_id, esc(strip_tags(part)), reply_markup=markup)
        return msg

    async def run(self) -> None:
        self.archive.ensure_all_in_background()  # отчёты для заседаний, проведённых до обновления
        delay = 5
        while True:
            try:
                me = await self.api.get_me()
                break
            except TelegramError as e:
                if e.code in (401, 404):
                    raise SystemExit("Неверный TELEGRAM_BOT_TOKEN: проверьте токен у @BotFather") from e
                log.warning("Telegram недоступен (%s), повтор через %d с", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)
        self.username = me.get("username", "")
        try:
            await self.api.set_commands(COMMANDS)
        except TelegramError as e:
            log.warning("setMyCommands: %s", e)
        log.info("бот @%s запущен", self.username)
        await self.notify_interrupted()
        offset = None
        while True:
            try:
                updates = await self.api.get_updates(offset)
            except TelegramError as e:
                log.warning("getUpdates: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                task = asyncio.create_task(self.handle_update_safe(u))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            await asyncio.sleep(0)  # дать поработать обработчикам, даже если сервер ответил мгновенно

    @staticmethod
    def _chat_of(update: dict[str, Any]) -> int | None:
        msg = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
        return (msg.get("chat") or {}).get("id")

    async def handle_update_safe(self, update: dict[str, Any]) -> None:
        """Разные чаты обрабатываются параллельно, а сообщения одного чата — по очереди, в порядке прихода
        (иначе вопрос, отправленный сразу после альбома, мог бы обогнать скачивание фото)."""
        chat = self._chat_of(update)
        lock = self._chat_locks.setdefault(chat, asyncio.Lock()) if chat is not None else None
        try:
            if lock:
                async with lock:
                    await self.handle_update(update)
            else:
                await self.handle_update(update)
        except Exception:  # noqa: BLE001
            log.exception("ошибка обработки update %s", update.get("update_id"))

    async def shutdown(self) -> None:
        for r in self.running.values():
            r.task.cancel()
        await asyncio.gather(*(r.task for r in self.running.values()), return_exceptions=True)

    # ------------------------------------------------------------ входящие

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self.on_callback(update["callback_query"])
        elif "message" in update:
            await self.on_message(update["message"])

    async def on_message(self, msg: dict[str, Any]) -> None:
        text = (msg.get("text") or "").strip()
        chat_id = msg["chat"]["id"]
        user_id = (msg.get("from") or {}).get("id")
        private = msg["chat"].get("type") == "private"
        if msg.get("voice") or msg.get("audio") or msg.get("video_note"):
            if user_id in self.allowed and private:
                await self.on_voice(msg)
            elif user_id not in self.allowed and private:
                await self.send(chat_id, f"⛔ Доступ к боту закрыт.\nВаш Telegram ID: <code>{user_id}</code>")
            return
        if msg.get("photo") or msg.get("document"):
            if user_id in self.allowed and (private or (msg.get("caption") or "").startswith("/ask")):
                await self.on_file(msg)
            elif user_id not in self.allowed and private:
                await self.send(chat_id, f"⛔ Доступ к боту закрыт.\nВаш Telegram ID: <code>{user_id}</code>")
            return
        if not text:
            return
        cmd, _, arg = text.partition(" ") if text.startswith("/") else ("", "", text)
        if cmd and "\n" in cmd:  # "/evaluate\nвопрос..." — команда и текст через перевод строки
            cmd, _, rest = cmd.partition("\n")
            arg = rest + (" " + arg if arg else "")
        cmd = cmd.lower()
        if "@" in cmd:
            cmd, _, target = cmd.partition("@")
            if self.username and target != self.username.lower():
                return
        arg = arg.strip()

        if user_id not in self.allowed:
            if private or cmd in ("/start", "/help"):
                await self.send(chat_id, "⛔ Доступ к боту закрыт.\n"
                                f"Ваш Telegram ID: <code>{user_id}</code>\n"
                                "Владелец бота может добавить его в <code>TELEGRAM_ALLOWED_USERS</code>.")
            return

        if cmd in ("/start", "/help"):
            await self.send(chat_id, HELP)
        elif cmd == "/settings":
            await self.show_settings(chat_id)
        elif cmd == "/budget":
            await self.cmd_budget(chat_id, arg)
        elif cmd == "/stop":
            await self.cmd_stop(chat_id)
        elif cmd == "/paused":
            await self.cmd_paused(chat_id)
        elif cmd == "/evaluate":
            await self.cmd_evaluate(chat_id, arg)
        elif cmd == "/stats":
            await self.cmd_stats(chat_id)
        elif cmd == "/logs":
            await self.cmd_logs(chat_id)
        elif cmd == "/cancel":
            had = self.followup_wait.pop(chat_id, None) or self.pending.pop(chat_id, None)
            await self.send(chat_id, "Отменено." if had else "Нечего отменять.")
        elif cmd == "/clear":
            n = len(self.drafts.pop(chat_id, []))
            p = self.pending.get(chat_id)
            if p and p.attachments:
                n += len(p.attachments)
                p.attachments.clear()
                text, markup = await self.card(chat_id, p)
                await self.api.edit_message(chat_id, p.card_id, text, reply_markup=markup)
            await self.send(chat_id, f"🗑 Вложения убраны ({n})." if n else "Вложений нет.")
        elif cmd == "/archive":
            self.archive_query[chat_id] = arg
            await self.show_archive(chat_id, arg, 0)
        elif cmd == "/ask":
            if not arg:
                await self.send(chat_id, "Напишите вопрос после команды: <code>/ask ваш вопрос</code>")
            else:
                await self.new_question(chat_id, arg)
        elif cmd:
            if private:
                await self.send(chat_id, "Неизвестная команда. /help — справка.")
        elif private:
            if await self.continue_question(chat_id, arg):
                return
            await self.new_question(chat_id, arg, parent=self.followup_wait.pop(chat_id, ""))

    async def on_voice(self, msg: dict[str, Any]) -> None:
        """Голосовое, аудиофайл или видеокружок: расшифровываем и задаём как вопрос (или уточнение)."""
        from ..transcribe import MAX_AUDIO_SECONDS, TranscribeError, transcribe
        chat_id = msg["chat"]["id"]
        media = msg.get("voice") or msg.get("audio") or msg.get("video_note")
        if (media.get("duration") or 0) > MAX_AUDIO_SECONDS:
            await self.send(chat_id, f"⚠️ Запись длиннее {MAX_AUDIO_SECONDS // 60} минут — сократите, пожалуйста.")
            return
        if (media.get("file_size") or 0) > MAX_FILE_BYTES:
            await self.send(chat_id, "⚠️ Запись больше 20 МБ — Telegram не отдаёт ботам такие файлы.")
            return
        s = self.chat_settings(chat_id)
        cfg = self.base_config()
        await self.api.send_typing(chat_id)
        client = MockClient(delay=0.2) if s.demo else (
            OpenRouterClient(os.environ["OPENROUTER_API_KEY"], timeout=cfg.timeout_s, max_retries=1)
            if os.environ.get("OPENROUTER_API_KEY") else None)
        if client is None:
            await self.send(chat_id, "⚠️ Для расшифровки голоса нужен ключ OpenRouter (или включите 🧪 демо-режим).")
            return
        try:
            data = await self.api.download(media["file_id"])
            text, _seconds, _cost = await transcribe(client, cfg.transcribe_model, data)
        except (TelegramError, TranscribeError, ProviderError) as e:
            await self.send(chat_id, f"⚠️ Не удалось распознать речь: {esc(getattr(e, 'description', None) or e)}")
            return
        finally:
            await client.aclose()
        caption = (msg.get("caption") or "").strip()
        question = f"{caption}\n{text}" if caption else text
        await self.send(chat_id, f"🎤 <b>Распознано:</b> {esc(text[:1500])}")
        await self.new_question(chat_id, question, parent=self.followup_wait.pop(chat_id, ""))

    async def on_file(self, msg: dict[str, Any]) -> None:
        """Фото или документ: прикладываем к вопросу (подпись = вопрос) или копим до вопроса."""
        chat_id = msg["chat"]["id"]
        if msg.get("photo"):
            ph = max(msg["photo"], key=lambda x: x.get("file_size") or x.get("width", 0) * x.get("height", 0))
            file_id, name, size = ph["file_id"], f"фото-{msg.get('message_id', 0)}.jpg", ph.get("file_size") or 0
        else:
            doc = msg["document"]
            file_id, name, size = doc["file_id"], doc.get("file_name") or "документ", doc.get("file_size") or 0
        if size > MAX_FILE_BYTES:
            await self.send(chat_id, f"⚠️ «{esc(name)}» больше {MAX_FILE_BYTES // 1024 // 1024} МБ — Telegram не отдаёт ботам такие файлы.")
            return
        draft = self.pending[chat_id].attachments if chat_id in self.pending else self.drafts.setdefault(chat_id, [])
        if len(draft) >= MAX_FILES:
            await self.send(chat_id, f"⚠️ Можно приложить не больше {MAX_FILES} файлов.")
            return
        try:
            data = await self.api.download(file_id)
            att = await asyncio.to_thread(load_attachment, data, name)
        except (TelegramError, AttachmentError) as e:
            await self.send(chat_id, f"⚠️ Не удалось принять «{esc(name)}»: {esc(getattr(e, 'description', None) or e)}")
            return
        draft.append(att)
        warn = f"\n⚠️ {esc(att.warning)}" if att.warning else ""
        caption = (msg.get("caption") or "").strip()
        if caption.startswith("/ask"):
            caption = caption[4:].strip()
        if caption:  # подпись к файлу — это и есть вопрос
            await self.new_question(chat_id, caption)
            return
        p = self.pending.get(chat_id)
        if p and p.card_id:  # вопрос уже есть — обновляем карточку
            text, markup = await self.card(chat_id, p)
            await self.api.edit_message(chat_id, p.card_id, text, reply_markup=markup)
            if warn:
                await self.send(chat_id, f"📎 «{esc(att.name)}» добавлен к вопросу.{warn}")
            return
        group = msg.get("media_group_id")
        if group and group in self._album_replied and not warn:
            return  # на альбом отвечаем один раз
        if group:
            if len(self._album_replied) > 1000:
                self._album_replied.clear()
            self._album_replied.add(group)
        await self.send(chat_id, f"📎 Принял «{esc(att.name)}» (всего вложений: {len(draft)}).{warn}\n"
                                 "Теперь напишите вопрос — или пришлите ещё файлы. Убрать вложения: /clear")

    async def cmd_logs(self, chat_id: int) -> None:
        """Пакет для разбора ошибок: последнее заседание чата (журнал событий, стенограмма) и журнал программы."""
        stem = self.last_stem.get(chat_id)
        data = await asyncio.to_thread(bundle, self.runs_dir, self.config_path, stem=stem,
                                       control_dir=self.config_path.parent / "control")
        name = f"diagnostics-{stem or time.strftime('%Y%m%d-%H%M%S')}.zip"
        caption = ("🧾 Журнал для разбора" + (" последнего заседания" if stem else "") +
                   ": события по моделям, сырые ответы, ошибки программы. Ключи скрыты, вопросы и ответы — внутри.")
        try:
            await self.api.send_document(chat_id, name, data, caption=caption, mime="application/zip")
        except TelegramError as e:
            await self.send(chat_id, f"⚠️ Telegram не принял файл: {esc(e.description)}")

    async def cmd_stats(self, chat_id: int) -> None:
        from ..stats import compute, fmt_duration
        d = await asyncio.to_thread(compute, self.archive)
        if not d["sessions"]:
            await self.send(chat_id, "📊 Пока нет заседаний — статистика появится после первого.")
            return
        lines = ["📊 <b>Статистика</b>",
                 f"Заседаний: <b>{d['sessions']}</b> · потрачено <b>${d['total_cost']:.2f}</b>",
                 f"В этом месяце: ${d['month_cost']:.2f} ({d['month_sessions']} засед.)",
                 f"В среднем: ${d['avg_cost']:.3f} и {fmt_duration(d['avg_duration'])} на заседание", "",
                 "<b>Модели</b> (побед / вариантов · средняя оценка · сбои):"]
        for m in d["models"]:
            rate = f"{round(m['win_rate'] * 100)}%" if m["win_rate"] is not None else "—"
            score = f"{m['avg_score']:.1f}" if m["avg_score"] is not None else "—"
            fail = f" · ⚠️ {m['failures']}" if m["failures"] else ""
            lines.append(f"• <b>{esc(m['name'])}</b>: {m['wins']}/{m['proposals']} ({rate}) · {score}{fail} · ${m['cost']:.3f}")
        fc = d.get("fact_check") or {}
        if fc.get("sessions"):
            lines += ["", f"🔎 <b>Проверка фактов</b>: {fc['sessions']} засед. · ✅ {fc['confirmed']} · "
                          f"❌ {fc['refuted']} · ❔ {fc['unclear']}"]
        if d.get("chairs"):
            lines += ["", "<b>Председатели</b> (выбрал не лидера голосования / решений):"]
            for c in d["chairs"]:
                rate = f"{round(c['override_rate'] * 100)}%" if c["override_rate"] is not None else "—"
                agree = f" · согласие {c['avg_agreement']:.2f}" if c["avg_agreement"] is not None else ""
                fb = f" · не ответил {c['fallbacks']}" if c["fallbacks"] else ""
                lines.append(f"• <b>{esc(c['name'])}</b>: {c['overrides']}/{c['decisions']} ({rate}){agree}{fb}")
        await self.send(chat_id, "\n".join(lines))

    async def repeat(self, chat_id: int, stem: str) -> None:
        """«Повторить»: тот же вопрос и вложения в новой карточке — режим и модели можно поменять кнопками."""
        from ..attachments import load_from_dir
        req = await asyncio.to_thread(self.archive.request_of, stem)
        d = await asyncio.to_thread(self.archive.load, stem)
        atts = await asyncio.to_thread(load_from_dir, self.archive.files_dir(stem), d.get("attachments") or [])
        await self.send(chat_id, "🔁 Повтор: тот же вопрос" + (f" и вложения ({len(atts)})" if atts else "")
                        + ". Поменяйте режим или участников кнопками и нажмите «Начать».")
        await self.new_question(chat_id, req.get("question", ""), req.get("options") or None,
                                parent=d.get("followup_of", ""), attachments=atts)

    async def cmd_budget(self, chat_id: int, arg: str) -> None:
        s = self.chat_settings(chat_id)
        if not arg:
            cur = s.budget or self.base_config().max_cost_usd
            await self.send(chat_id, f"Лимит заседания: <b>${cur:g}</b>. Изменить: <code>/budget 0.5</code>")
            return
        try:
            value = float(arg.replace(",", ".").lstrip("$"))
            if not 0 < value <= 100:
                raise ValueError
        except ValueError:
            await self.send(chat_id, "Укажите число от 0.01 до 100, например <code>/budget 0.5</code>")
            return
        s.budget = value
        self._save_settings()
        await self.send(chat_id, f"✅ Лимит заседания: <b>${value:g}</b>")

    async def cmd_stop(self, chat_id: int) -> None:
        r = self.running.get(chat_id)
        if not r:
            await self.send(chat_id, "Сейчас заседание не идёт.")
            return
        r.task.cancel()

    async def cmd_evaluate(self, chat_id: int, arg: str) -> None:
        text = arg.replace("\r\n", "\n").strip()
        question, _, rest = text.partition("\n")
        rest = rest.strip()
        if "\n---" in rest or rest.startswith("---"):
            options = [o.strip() for o in rest.split("---") if o.strip()]
        else:
            options = [re.sub(r"^\s*(?:[-•*]|\d+[.)])\s+", "", o).strip() for o in rest.split("\n") if o.strip()]
        if not question or len(options) < 2:
            await self.send(chat_id, "Формат:\n<code>/evaluate Какую БД выбрать?\nPostgreSQL\nClickHouse\nBigQuery</code>\n"
                                     "Первая строка — вопрос, дальше минимум 2 варианта (по строке или блоками через ---).")
            return
        if len(options) > 26:
            await self.send(chat_id, "Слишком много вариантов: максимум 26.")
            return
        await self.new_question(chat_id, question.strip(), options)

    # ------------------------------------------------------------ карточка вопроса

    async def new_question(self, chat_id: int, question: str, options: list[str] | None = None,
                           parent: str = "", attachments: list[Attachment] | None = None) -> None:
        if len(question) < 3:
            await self.send(chat_id, "Вопрос слишком короткий.")
            return
        old = self.pending.get(chat_id)
        if old and old.card_id:
            try:
                await self.api.edit_message(chat_id, old.card_id, "<i>Вопрос заменён новым.</i>")
            except TelegramError:
                pass
        p = Pending(question=question, options=options,
                    attachments=(attachments or []) + self.drafts.pop(chat_id, []))
        if parent and self.archive.exists(parent):
            p.parent = parent
            p.parent_title = await asyncio.to_thread(lambda: self.archive.view(parent).title_text)
        self.pending[chat_id] = p
        p.last_at, p.last_len = time.monotonic(), len(question)
        text, markup = await self.card(chat_id, p)
        msg = await self.send(chat_id, text, reply_markup=markup)
        p.card_id = msg["message_id"] if msg else None

    SPLIT_LEN = 3500      # такая длина предыдущей части — признак, что Telegram разрезал длинный текст
    SPLIT_WINDOW = 15.0   # секунд между частями

    async def continue_question(self, chat_id: int, text: str) -> bool:
        """Склеивает длинный вопрос, который приложение Telegram разбило на несколько сообщений.

        Telegram не принимает сообщения длиннее 4096 символов и при вставке длинного текста сам отправляет
        его частями подряд. Если предыдущая часть была почти предельной длины и пришла только что, новое
        сообщение — продолжение того же вопроса, а не новый вопрос.
        """
        p = self.pending.get(chat_id)
        if not p or p.options or p.last_len < self.SPLIT_LEN or time.monotonic() - p.last_at > self.SPLIT_WINDOW:
            return False
        p.question = p.question + "\n" + text
        p.last_at, p.last_len = time.monotonic(), len(text)
        card, markup = await self.card(chat_id, p)
        try:
            await self.api.edit_message(chat_id, p.card_id, card, reply_markup=markup)
        except TelegramError:
            pass
        return True

    async def card(self, chat_id: int, p: Pending) -> tuple[str, dict]:
        s = self.chat_settings(chat_id)
        if p.view == "members":
            return self._members_card(s)
        mode = "evaluate" if p.options else s.mode
        lines = ["🏛 <b>Вопрос для совета</b>"]
        if p.parent:
            lines.append(f"<i>↳ уточнение к «{esc(p.parent_title)}» — совет учтёт его решение и вложения</i>")
        lines.append(esc(p.question[:1500]) + (f"\n<i>… ещё {len(p.question) - 1500:,} симв. — совет получит вопрос "
                                                f"целиком ({len(p.question):,} симв.)</i>".replace(",", " ")
                                                if len(p.question) > 1500 else ""))
        if p.options:
            lines += ["", "<b>Варианты:</b>"] + [f"{chr(65 + i)}. {esc(o[:200])}" for i, o in enumerate(p.options)]
        if p.attachments:
            lines += ["", f"📎 <b>Вложения ({len(p.attachments)}):</b> " + ", ".join(esc(a.name) for a in p.attachments)]
        problem = None
        try:
            cfg = self.build_config(s)
        except OverrideError as e:
            cfg, problem = None, str(e)
        lines.append("")
        lines.append(f"<b>{MODE_FULL[mode]}</b>" + (f" · раундов: {s.rounds}" if mode == "debate" else ""))
        if cfg:
            n = len(cfg.active_members)
            extras = [x for x, on in (("компактно", cfg.compact_proposals), ("проверка фактов", cfg.fact_check.enabled),
                                      ("демо", s.demo)) if on]
            lines.append(f"{n} {_plural(n, 'модель', 'модели', 'моделей')} · председатель {esc(cfg.chair)}"
                         + (" · " + " · ".join(extras) if extras else ""))
            if p.view == "opts":
                lines.append("<i>Участники: " + ", ".join(
                    f"{esc(m.name)}{' 🎩' if m.name == cfg.chair else ''}{' 😈' if m.name == cfg.devil_advocate else ''}"
                    for m in cfg.active_members) + " (🎩 председатель, 😈 адвокат дьявола)</i>")
            est_line, est_problem = await self._estimate_line(cfg, s, mode, p)
            lines.append(est_line)
            problem = problem or est_problem
        if problem:
            lines += ["", f"⚠️ {esc(problem)}"]
        if p.view == "opts":
            kb = self._opts_kb(s, cfg, "", with_mode=not p.options)
            kb.append([self._btn("▶️ Начать", "go"), self._btn("« Назад", "view:main")])
            return "\n".join(lines), {"inline_keyboard": kb}
        kb: list[list[dict]] = [[self._btn("▶️ Начать", "go")],
                                [self._btn("⚙️ Настроить", "view:opts"), self._btn("✖️ Отмена", "drop")]]
        if p.attachments:
            kb.insert(1, [self._btn(f"🗑 Убрать вложения ({len(p.attachments)})", "noatt")])
        return "\n".join(lines), {"inline_keyboard": kb}

    def _opts_kb(self, s: ChatSettings, cfg: CouncilConfig | None, prefix: str, *, with_mode: bool = True) -> list[list[dict]]:
        """Кнопки настроек: режим, раунды, участники, переключатели. prefix «set:» — для сообщения /settings."""
        on = lambda flag: "✅" if flag else "▫️"  # noqa: E731
        kb: list[list[dict]] = []
        if with_mode:
            kb.append([self._btn(("● " if s.mode == m else "") + {"debate": "Подробно", "quick": "Быстро"}[m], f"{prefix}mode:{m}")
                       for m in ("debate", "quick")])
            if s.mode == "debate":
                kb.append([self._btn(("● " if s.rounds == r else "") + f"{r} раунд" + ("а" if r > 1 else ""), f"{prefix}rounds:{r}")
                           for r in (1, 2, 3)])
        n = len(cfg.active_members) if cfg else 0
        fc_on = cfg.fact_check.enabled if cfg else bool(s.fact_check)
        cp_on = cfg.compact_proposals if cfg else bool(s.compact)
        kb.append([self._btn(f"👥 Участники ({n})", f"{prefix}view:members")])
        kb.append([self._btn(f"{on(fc_on)} Проверка фактов", f"{prefix}facts"), self._btn(f"{on(cp_on)} Компактно", f"{prefix}compact")])
        kb.append([self._btn(f"{on(s.demo)} Демо-режим (бесплатно)", f"{prefix}demo")])
        return kb

    def _members_card(self, s: ChatSettings, prefix: str = "") -> tuple[str, dict]:
        base = self.base_config()
        active = set(s.members) if s.members is not None else {m.name for m in base.active_members}
        rows, row = [], []
        for m in base.members:
            row.append(self._btn(("✅ " if m.name in active else "▫️ ") + m.name, f"{prefix}mem:{m.name}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([self._btn("↩️ По умолчанию", f"{prefix}mem:*"), self._btn("« Готово", f"{prefix}view:opts")])
        text = "👥 <b>Участники совета</b>\nНажмите на модель, чтобы включить или выключить. Нужно минимум столько, сколько требует кворум.\n\n" + "\n".join(
            f"{'✅' if m.name in active else '▫️'} <b>{esc(m.name)}</b> — <code>{esc(m.model)}</code>" for m in base.members)
        return text, {"inline_keyboard": rows}

    async def _estimate_line(self, cfg: CouncilConfig, s: ChatSettings, mode: str, p: Pending) -> tuple[str, str | None]:
        pricing: dict = {}
        problem = None
        if not s.demo:
            if not os.environ.get("OPENROUTER_API_KEY"):
                problem = "Нет OPENROUTER_API_KEY — включите демо-режим или добавьте ключ."
            models = await self.catalog.get()
            if models:
                pricing = parse_pricing(models)
                missing = unknown_models(cfg, set(pricing))
                if missing:
                    problem = "На OpenRouter нет моделей: " + "; ".join(
                        m + (f" (возможно: {', '.join(sim)})" if sim else "") for m, sim in missing.items()
                    ) + ". Исправьте config.yaml или снимите участника."
        models = None if s.demo else await self.catalog.get()
        vision = vision_map(cfg, models)
        imgs = sum(1 for a in p.attachments if a.kind == "image")
        lengths = await asyncio.to_thread(learn_lengths, self.runs_dir, compact=cfg.compact_proposals)
        est = estimate(cfg, pricing, mode=mode, rounds=s.rounds,
                       n_options=len(p.options) if p.options else None, has_criteria=False,
                       doc_tokens=sum(a.tokens for a in p.attachments if a.kind == "document"),
                       images=imgs, vision=vision, question_chars=len(p.question),
                       options_chars=sum(len(o) for o in p.options or []), lengths=lengths)
        sim = similar_actual(lengths, mode, len(cfg.active_members))
        blind = [m.name for m in cfg.active_members if vision.get(m.model) is False]
        if imgs and blind and not problem:
            problem = f"ℹ️ {', '.join(blind)} не видят картинки — им передадут их описание."
        if s.demo:
            line = f"💰 Демо-режим: бесплатно ({est.calls} обращений к моделям)"
        elif pricing:
            line = f"💰 ≈ <b>${est.low:.2f} – ${est.high:.2f}</b> · лимит ${cfg.max_cost_usd:g}"
            if sim:
                line += f"\n<i>Похожие заседания ({sim['count']}) стоили ${sim['min']:.2f}–${sim['max']:.2f}</i>"
            if est.high > cfg.max_cost_usd:
                line += "\n⚠️ <i>Верхняя оценка выше лимита — заседание может остановиться по бюджету (/budget).</i>"
            key = os.environ.get("OPENROUTER_API_KEY")
            if key:
                bc = OpenRouterClient(key, timeout=10)
                try:
                    bal = await bc.balance()
                finally:
                    await bc.aclose()
                if bal is not None:
                    line += f" · баланс ${bal:.2f}"
                    if bal < est.high:
                        problem = problem or "На балансе OpenRouter меньше верхней оценки — пополните баланс, иначе заседание может прерваться."
                    elif bal < est.high * 4:
                        line += " <i>(небольшой — часть запросов будет ждать резерва, заседание пройдёт медленнее)</i>"
        else:
            line = f"💰 Стоимость не удалось оценить ({est.calls} обращений, лимит ${cfg.max_cost_usd:g})"
        return line, problem

    @staticmethod
    def _btn(text: str, data: str) -> dict[str, str]:
        return {"text": text, "callback_data": data[:64]}

    def _settings_view(self, chat_id: int, view: str = "opts") -> tuple[str, dict]:
        s = self.chat_settings(chat_id)
        if view == "members":
            return self._members_card(s, "set:")
        try:
            cfg = self.build_config(s)
            members = ", ".join(m.name for m in cfg.active_members)
            budget, chair, problem = cfg.max_cost_usd, cfg.chair, ""
        except OverrideError as e:
            cfg, members, budget, chair, problem = None, "—", s.budget or 0, "—", str(e)
        text = ("⚙️ <b>Настройки чата</b>\nДействуют для всех новых вопросов в этом чате.\n\n"
                f"<b>{MODE_FULL[s.mode]}</b>" + (f" · раундов: {s.rounds}" if s.mode == "debate" else "") + "\n"
                f"Участники: {esc(members)}\nПредседатель: {esc(chair)}\nЛимит заседания: ${budget:g} (изменить: /budget 0.5)"
                + (f"\n\n⚠️ {esc(problem)}" if problem else ""))
        kb = self._opts_kb(s, cfg, "set:")
        kb.append([self._btn("✓ Готово", "set:close")])
        return text, {"inline_keyboard": kb}

    async def show_settings(self, chat_id: int) -> None:
        text, markup = self._settings_view(chat_id)
        await self.send(chat_id, text, reply_markup=markup)

    def _apply_setting(self, s: ChatSettings, kind: str, value: str) -> None:
        """Общая логика переключателей для карточки вопроса и для /settings."""
        if kind == "mode" and value in ("debate", "quick"):
            s.mode = value
        elif kind == "rounds" and value.isdigit():
            s.rounds = max(1, min(5, int(value)))
        elif kind == "demo":
            s.demo = not s.demo
        elif kind == "facts":
            current = s.fact_check if s.fact_check is not None else self.base_config().fact_check.enabled
            s.fact_check = not current
        elif kind == "compact":
            current = s.compact if s.compact is not None else self.base_config().compact_proposals
            s.compact = not current
        elif kind == "mem":
            base = self.base_config()
            if value == "*":
                s.members = None
            elif value in {m.name for m in base.members}:
                cur = list(s.members) if s.members is not None else [m.name for m in base.active_members]
                cur = [x for x in cur if x != value] if value in cur else cur + [value]
                s.members = [m.name for m in base.members if m.name in cur]  # порядок как в конфиге
        self._save_settings()

    # ------------------------------------------------------------ кнопки

    async def on_callback(self, cq: dict[str, Any]) -> None:
        data = cq.get("data") or ""
        user_id = cq["from"]["id"]
        message = cq.get("message") or {}
        chat_id = message.get("chat", {}).get("id")
        if user_id not in self.allowed:
            await self.api.answer_callback(cq["id"], "Нет доступа")
            return
        kind0, _, rest0 = data.partition(":")
        if kind0 == "set":
            await self.on_settings_callback(cq, chat_id, message.get("message_id"), rest0)
            return
        if kind0 in ("arc", "ses", "rep", "res", "fup", "rpt"):
            await self.on_archive_callback(cq, chat_id, message.get("message_id"), kind0, rest0)
            return
        if kind0 in ("cp", "cpb"):
            await self.on_pause_callback(cq, chat_id, kind0, rest0)
            return
        if data == "stop":
            r = self.running.get(chat_id)
            if r and r.message_id == message.get("message_id"):
                r.task.cancel()
                await self.api.answer_callback(cq["id"], "Останавливаю…")
            else:
                await self.api.answer_callback(cq["id"], "Заседание уже завершено")
            return
        p = self.pending.get(chat_id)
        if not p or p.card_id != message.get("message_id"):
            await self.api.answer_callback(cq["id"], "Этот вопрос уже неактуален")
            return
        s = self.chat_settings(chat_id)
        kind, _, value = data.partition(":")
        if kind == "go":
            await self.api.answer_callback(cq["id"])
            await self.start_session(chat_id, p)
            return
        if kind == "drop":
            self.pending.pop(chat_id, None)
            await self.api.answer_callback(cq["id"], "Отменено")
            await self.api.edit_message(chat_id, p.card_id, f"<s>{esc(p.question[:500])}</s>\n<i>Отменено.</i>")
            return
        if kind == "noatt":
            p.attachments.clear()
        elif kind == "view" and value in ("main", "opts", "members"):
            p.view = value
        else:
            # старые кнопки (до обновления) меняли настройки прямо в карточке — работают так же
            self._apply_setting(s, kind, value)
        await self.api.answer_callback(cq["id"])
        text, markup = await self.card(chat_id, p)
        await self.api.edit_message(chat_id, p.card_id, text, reply_markup=markup)

    async def on_settings_callback(self, cq: dict[str, Any], chat_id: int, message_id: int | None, rest: str) -> None:
        kind, _, value = rest.partition(":")
        if kind == "close":
            await self.api.answer_callback(cq["id"], "Сохранено")
            text, _ = self._settings_view(chat_id)
            await self._safe_edit(chat_id, message_id, text + "\n\n<i>✓ Сохранено.</i>")
            return
        view = "opts"
        if kind == "view":
            view = value if value in ("opts", "members") else "opts"
        else:
            self._apply_setting(self.chat_settings(chat_id), kind, value)
            if kind == "mem":
                view = "members"
        await self.api.answer_callback(cq["id"])
        text, markup = self._settings_view(chat_id, view)
        await self._safe_edit(chat_id, message_id, text, markup)

    # ------------------------------------------------------------ заседание

    async def start_session(self, chat_id: int, p: Pending) -> None:
        if chat_id in self.running:
            await self.send(chat_id, "⏳ В этом чате уже идёт заседание. Дождитесь его или нажмите /stop.")
            return
        s = self.chat_settings(chat_id)
        try:
            cfg = self.build_config(s)
        except OverrideError as e:
            await self.send(chat_id, f"⚠️ {esc(e)}")
            return
        if not s.demo and not os.environ.get("OPENROUTER_API_KEY"):
            await self.send(chat_id, "⚠️ Нет OPENROUTER_API_KEY. Включите 🧪 демо-режим или добавьте ключ на сервере.")
            return
        mode = "evaluate" if p.options else s.mode
        self.pending.pop(chat_id, None)
        await self.api.edit_message(chat_id, p.card_id, f"🏛 <b>Вопрос передан совету</b>\n{esc(p.question[:1500])}")

        stem = make_stem(p.question)
        while (self.runs_dir / f"{stem}.json").exists() or Checkpoint.load(self.runs_dir, stem):
            stem += "-1"
        atts = p.attachments
        history: list[dict] = []
        if p.parent:
            history, parent_atts = await asyncio.to_thread(self.archive.followup, p.parent)
            atts = merge(parent_atts, atts)
        assign_files(atts)
        for w in limit_total(atts):
            await self.send(chat_id, f"⚠️ {esc(w)}")
        vision = vision_map(cfg, None if s.demo else await self.catalog.get())
        request = {"question": p.question, "mode": mode, "rounds": s.rounds, "criteria": [],
                   "options": p.options or [], "members": [m.name for m in cfg.active_members],
                   "chair": cfg.chair, "devil_advocate": cfg.devil_advocate or "", "budget": cfg.max_cost_usd,
                   "chair_proposes": cfg.chair_proposes, "fact_check": cfg.fact_check.enabled,
                   "compact_proposals": cfg.compact_proposals}
        # «паспорт» заседания на диске: если оно прервётся, его можно продолжить без потери оплаченного
        cp = await asyncio.to_thread(
            Checkpoint.create, self.runs_dir, stem, cfg=cfg, question=p.question, mode=mode, rounds=s.rounds,
            criteria=None, options=p.options, context=None, attachments=atts, history=history,
            followup_of=p.parent or "", vision=vision, request=request, origin="telegram", mock=s.demo,
            chat_id=chat_id, extra={"parent_title": p.parent_title})
        await self.launch(chat_id, cp)

    async def launch(self, chat_id: int, cp: Checkpoint) -> None:
        """Запускает новое или продолженное заседание по его «паспорту» на диске."""
        sp = cp.spec
        cfg = cp.config()
        stem = cp.id
        mode = sp.get("mode") or "debate"
        resumed = int(sp.get("resumes") or 0)
        progress = Progress(question=sp["question"], mode=mode, rounds=sp.get("rounds") or 1,
                            members=[m.name for m in cfg.active_members], chair=cfg.chair, demo=bool(sp.get("mock")),
                            fact_check=cfg.fact_check.enabled)
        stop_kb = {"inline_keyboard": [[self._btn("⏸ Остановить", "stop")]]}
        msg = await self.api.send_message(chat_id, progress.render(), reply_markup=stop_kb)
        client = self.llm_factory(cfg, bool(sp.get("mock")))
        events: list[dict[str, Any]] = []
        council_ref: dict[str, Council] = {}

        def on_event(event: str, **d: Any) -> None:
            events.append(stamp({"type": event, **d}))
            progress.apply(event, d)
            r = self.running.get(chat_id)
            if r:
                r.dirty = True
            if event == "stage":
                cp.update(stage=d.get("title", ""))
            elif event == "budget_warning":
                asyncio.get_running_loop().create_task(self.send(
                    chat_id, f"💰 <b>Бюджет почти исчерпан:</b> потрачено ${d['spent']:.2f} из ${d['budget']:g}.\n"
                             "Если не хватит — заседание приостановится, а не пропадёт. Можно поднять бюджет сейчас, "
                             "и оно не прервётся:", reply_markup=self._budget_kb(cp.id, live=True)))

        async def execute() -> None:
            updater = asyncio.create_task(self._updater(chat_id, stop_kb))
            council = None
            with cp.lock():
                try:
                    cp.update(status="running", reason="", message="")
                    atts = await asyncio.to_thread(cp.attachments)
                    journal = await asyncio.to_thread(Journal, cp.journal_path)
                    council = Council(cfg, client, on_event=on_event, vision=sp.get("vision") or {},
                                      seed=sp.get("seed"), journal=journal)
                    council_ref["c"] = council
                    r = self.running.get(chat_id)
                    if r:
                        r.council = council
                    log.info("заседание %s: %s (чат %s, %s, участников %d)", stem, "продолжение" if resumed else "старт",
                             chat_id, mode, len(cfg.active_members))
                    res = await council.run(sp["question"], attachments=atts, **cp.run_args())
                    res.request = {**(sp.get("request") or {}), "budget": council.budget}
                    progress.saving = True
                    r = self.running.get(chat_id)
                    if r:
                        r.dirty = True
                    await asyncio.to_thread(save, res, self.runs_dir, stem, atts, True)
                    await asyncio.to_thread(cp.remove)
                    log.info("заседание %s: готово, $%.4f, из журнала %d ответов", stem, res.total_cost, council.replayed)
                    updater.cancel()
                    await self._safe_edit(chat_id, msg["message_id"], progress.render(final="✅ <b>Заседание завершено</b>"))
                    await self.send_result(chat_id, res, stem)
                except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001 — любая остановка = пауза
                    updater.cancel()
                    reason, message = classify(e)
                    if isinstance(e, CouncilError) or reason in ("credits", "stopped"):
                        log.warning("заседание %s приостановлено (%s): %s", stem, reason, message)
                    else:
                        log.exception("заседание %s упало", stem)
                        message = f"внутренняя ошибка: {type(e).__name__}"
                    spent = council.total_cost if council else 0.0
                    cp.update(status="paused", reason=reason, message=message, spent=round(spent, 6),
                              config={**cp.spec["config"], "max_cost_usd": council.budget if council else cp.budget})
                    if reason in ("error", "stopped"):
                        import traceback
                        expected = reason == "stopped" or isinstance(e, CouncilError)
                        keep("cancelled" if reason == "stopped" else "error" if expected else "crash",
                             message if expected else traceback.format_exc())
                    await self._safe_edit(chat_id, msg["message_id"], progress.render(final="⏸ <b>Заседание приостановлено</b>"))
                    await self.send_paused(chat_id, cp)
                finally:
                    self.running.pop(chat_id, None)
                    self.last_stem[chat_id] = stem
                    save_events(self.runs_dir, stem, events)
                    await client.aclose()

        def keep(status: str, error: str) -> None:
            """Прерванное заседание сохраняется с событиями и частичной стенограммой — для разбора."""
            c = council_ref.get("c")
            save_failed(self.runs_dir, stem, question=sp["question"], status=status, error=error, events=events,
                        transcript=c.transcript if c else [], stats=c.stats if c else {},
                        request={"mode": mode, "members": [m.name for m in cfg.active_members], "chair": cfg.chair})

        self.running[chat_id] = Running(task=asyncio.create_task(execute()), progress=progress,
                                        message_id=msg["message_id"], checkpoint=cp)

    # ------------------------------------------------------------ приостановка и продолжение

    @staticmethod
    def _cp_token(sid: str) -> str:
        import hashlib
        return hashlib.sha1(sid.encode("utf-8")).hexdigest()[:12]

    def _find_cp(self, token: str) -> Checkpoint | None:
        return next((c for c in Checkpoint.all(self.runs_dir) if self._cp_token(c.id) == token), None)

    @staticmethod
    def _suggest_budget(budget: float, spent: float) -> float:
        import math
        return math.ceil(max(budget * 1.5, spent + 1) * 2) / 2

    def _budget_kb(self, sid: str, *, live: bool) -> dict:
        t = self._cp_token(sid)
        kind = "cpb" if live else "cp"
        row = [self._btn(f"💰 +${n}", f"{kind}:{t}:+{n}") for n in (1, 3, 5)]
        return {"inline_keyboard": [row]}

    def _paused_kb(self, cp: Checkpoint) -> dict:
        t = self._cp_token(cp.id)
        info = cp.summary()
        budget, spent = info["budget"], info["spent"]
        rows = []
        if budget > spent:
            rows.append([self._btn(f"▶️ Продолжить (бюджет ${budget:g})", f"cp:{t}:go")])
        rows.append([self._btn(f"💰 +${n} и продолжить", f"cp:{t}:+{n}") for n in (1, 3, 5)])
        rows.append([self._btn("🗑 Удалить", f"cp:{t}:del")])
        return {"inline_keyboard": rows}

    async def send_paused(self, chat_id: int, cp: Checkpoint) -> None:
        info = cp.summary()
        help_ = {"budget": "Поднимите бюджет — заседание продолжится с того же места.",
                 "credits": "Пополните баланс на openrouter.ai/settings/credits и нажмите «Продолжить».",
                 "interrupted": "Нажмите «Продолжить» — заседание пойдёт с того места, где остановилось.",
                 "stopped": "Его можно продолжить с того места, где оно остановилось.",
                 "error": "Можно попробовать продолжить: модели, не ответившие на последнем этапе, спросим заново."
                 }.get(info["reason"], "")
        detail = f"\n<i>{esc(info['message'][:400])}</i>" if info["message"] and info["reason"] in ("error", "credits") else ""
        stage = f" · этап «{esc(info['stage'])}»" if info["stage"] else ""
        await self.send(chat_id,
                        f"⏸ <b>Заседание приостановлено:</b> {esc(info['reason_text'])}.{detail}\n"
                        f"❓ {esc(short_title(info['question']))}\n"
                        f"💰 Потрачено ${info['spent']:.2f} из ${info['budget']:g}{stage}\n\n"
                        f"Уже полученные ответы моделей сохранены — при продолжении они не оплачиваются повторно. {help_}",
                        reply_markup=self._paused_kb(cp))

    async def cmd_paused(self, chat_id: int) -> None:
        cps = [c for c in await asyncio.to_thread(Checkpoint.all, self.runs_dir) if c.state != "running"]
        if not cps:
            await self.send(chat_id, "Приостановленных заседаний нет.")
            return
        for cp in cps[:5]:
            await self.send_paused(chat_id, cp)

    async def notify_interrupted(self) -> None:
        """После перезапуска (например, обновления) сообщает о заседаниях бота, прерванных на ходу."""
        try:
            cps = await asyncio.to_thread(Checkpoint.all, self.runs_dir)
        except OSError:
            return
        for cp in cps:
            if cp.spec.get("origin") != "telegram" or not cp.spec.get("chat_id") or cp.state != "interrupted":
                continue
            cp.update(status="paused", reason="interrupted", spent=round(cp.spent_from_journal(), 6))
            try:
                await self.send_paused(int(cp.spec["chat_id"]), cp)
            except TelegramError as e:
                log.warning("не удалось сообщить о прерванном заседании %s: %s", cp.id, e)

    async def on_pause_callback(self, cq: dict[str, Any], chat_id: int, kind: str, rest: str) -> None:
        token, _, action = rest.partition(":")
        if kind == "cpb":  # поднять бюджет идущего заседания
            r = self.running.get(chat_id)
            if not r or not r.council or not r.checkpoint or self._cp_token(r.checkpoint.id) != token:
                await self.api.answer_callback(cq["id"], "Заседание уже не идёт")
                return
            add = float(action.lstrip("+") or 0)
            new = round(r.council.budget + add, 2)
            r.council.set_budget(new)
            await asyncio.to_thread(r.checkpoint.set_budget, new)
            await self.api.answer_callback(cq["id"], f"Бюджет: ${new:g}")
            await self.send(chat_id, f"💰 Бюджет поднят до ${new:g} — заседание продолжается.")
            return
        cp = await asyncio.to_thread(self._find_cp, token)
        if cp is None:
            await self.api.answer_callback(cq["id"], "Заседание уже завершено или удалено")
            return
        if await asyncio.to_thread(cp.is_live):
            await self.api.answer_callback(cq["id"], "Это заседание уже идёт")
            return
        if action == "del":
            await asyncio.to_thread(cp.remove)
            await self.api.answer_callback(cq["id"], "Удалено")
            await self.send(chat_id, f"🗑 Приостановленное заседание удалено: {esc(short_title(cp.spec.get('question', '')))}")
            return
        if chat_id in self.running:
            await self.api.answer_callback(cq["id"], "В этом чате уже идёт заседание")
            return
        info = cp.summary()
        budget = info["budget"]
        if action.startswith("+"):
            budget = round(max(budget, info["spent"]) + float(action[1:] or 0), 2)
            await asyncio.to_thread(cp.set_budget, budget)
        if budget <= info["spent"]:
            await self.api.answer_callback(cq["id"], "Бюджет уже потрачен — поднимите его")
            return
        if not cp.spec.get("mock") and not os.environ.get("OPENROUTER_API_KEY"):
            await self.api.answer_callback(cq["id"], "Нет OPENROUTER_API_KEY")
            return
        cp.update(resumes=int(cp.spec.get("resumes") or 0) + 1)
        await self.api.answer_callback(cq["id"], "Продолжаю…")
        await self.send(chat_id, f"▶️ <b>Продолжаю заседание</b> (бюджет ${budget:g}). Уже полученные ответы "
                                 "берутся из сохранённого — без повторной оплаты.")
        await self.launch(chat_id, cp)

    async def _updater(self, chat_id: int, markup: dict) -> None:
        """Периодически обновляет сообщение с прогрессом (Telegram ограничивает частоту правок)."""
        while True:
            await asyncio.sleep(self.edit_interval)
            r = self.running.get(chat_id)
            if not r:
                return
            r.dirty = False
            await self._safe_edit(chat_id, r.message_id, r.progress.render(), markup)
            await self.api.send_typing(chat_id)

    async def _safe_edit(self, chat_id: int, message_id: int, text: str, markup: dict | None = None) -> None:
        try:
            await self.api.edit_message(chat_id, message_id, text, reply_markup=markup)
        except TelegramError as e:
            log.warning("не удалось обновить сообщение: %s", e)

    SHORT_CHARS = 700

    def _short_answer(self, text: str) -> tuple[str, bool]:
        """Начало итогового ответа: целые абзацы, не больше SHORT_CHARS символов."""
        text = (text or "").strip()
        if len(text) <= self.SHORT_CHARS:
            return text, False
        out = ""
        for para in re.split(r"\n\s*\n", text):
            if out and len(out) + len(para) > self.SHORT_CHARS:
                break
            out += ("\n\n" if out else "") + para
        if len(out) > self.SHORT_CHARS * 1.4 or not out:  # один огромный абзац — режем по слову
            out = text[:self.SHORT_CHARS].rsplit(" ", 1)[0]
        return out.rstrip() + " …", True

    def _result_buttons(self, stem: str) -> dict:
        key = key_of(stem)
        fmt = available_formats()[0]
        return {"inline_keyboard": [
            [self._btn("📖 Полный ответ", f"res:more:{key}"), self._btn("📊 Голоса", f"res:tally:{key}")],
            [self._btn(f"📄 Отчёт {FORMAT_NAME[fmt]}", f"rep:{fmt}:{key}"), self._btn("📎 Другие форматы", f"res:fmt:{key}")],
            [self._btn("💬 Уточнить", f"fup:{key}"), self._btn("🔁 Повторить", f"rpt:{key}")],
        ]}

    async def send_summary(self, chat_id: int, stem: str) -> None:
        """Короткий итог заседания с кнопками: подробнее, голосование, отчёты, уточнить, повторить."""
        try:
            v = await asyncio.to_thread(self.archive.view, stem)
        except (KeyError, OSError, ValueError) as e:
            await self.send(chat_id, f"⚠️ Заседание не найдено: {esc(e)}")
            return
        vd = v.verdict
        w = v.winner
        short, cut = self._short_answer(vd.get("final_answer", ""))
        head = f"🏆 <b>Решение: {esc(v.title(w))}</b>" if w else "🏆 <b>Решение совета</b>"
        row = next((r for r in v.tally if r.get("label") == w), None) if w else None
        sub = [f"вариант {w}" if w else "", f"предложил {esc(v.author(w))}" if w else "",
               f"{row.get('first_places', 0)} из {row.get('ballots', 0)} за первое место" if row and row.get("ballots") else ""]
        lines = [head, f"<i>{' · '.join(x for x in sub if x)}</i>", f"❓ <i>{esc(v.title_text)}</i>", "", md_to_tg(short)]
        if cut:
            lines += ["", "<i>Целиком — «📖 Полный ответ» или в отчёте.</i>"]
        a = v.agreement
        if a:
            icon, word = {"high": ("🟢", "Мнения совета совпадают"), "medium": ("🟡", "Мнения частично расходятся"),
                          "low": ("🔴", "Мнения совета разошлись — посмотрите особые мнения")}[a["level"]]
            lines += ["", f"{icon} {word}"]
        if v.fact_line:
            lines.append(f"🔎 Факты: {esc(v.fact_line)}")
        if v.ideas_line:
            lines.append(f"🧩 Из других вариантов: {esc(v.ideas_line)}")
        if v.deviation_line:
            lines.append(f"⚖️ {esc(v.deviation_line[:400])}")
        if v.tie_line:
            lines.append(f"🤝 {esc(v.tie_line)}")
        if v.ballot_flags:
            lines.append(f"⚠️ Бюллетеней против общего мнения: {len(v.ballot_flags)}. {esc(v.robust_note)}")
        lines += ["", f"💰 ${v.cost:.2f} · ⏱ {v.duration} · вариантов: {len(v.proposals)}"]
        await self.send(chat_id, "\n".join(lines), reply_markup=self._result_buttons(stem))

    async def send_details(self, chat_id: int, stem: str) -> None:
        v = await asyncio.to_thread(self.archive.view, stem)
        vd, w = v.verdict, v.winner
        parts = [f"🏆 <b>Решение совета: вариант {w} — {esc(v.title(w))}</b> ({esc(v.author(w))})", "",
                 md_to_tg(vd.get("final_answer", "")), "", f"<b>Почему:</b> {md_to_tg(vd.get('why', ''))}"]
        if vd.get("dissent"):
            parts += ["", "<b>Особые мнения:</b>"] + [f"• {md_to_tg(x)}" for x in vd["dissent"]]
        if vd.get("risks"):
            parts += ["", "<b>Риски:</b>"] + [f"• {md_to_tg(x)}" for x in vd["risks"]]
        if v.ideas:
            parts += ["", "🧩 <b>Что взято из других вариантов:</b>"]
            for i in v.ideas:
                reason = f" — <i>{md_to_tg(i['reason'])}</i>" if i.get("reason") else ""
                parts.append(f"{v.IDEA_SIGN[i['decision']]} {esc(v.IDEA_WORD[i['decision']])} ({esc(i.get('label', ''))}): "
                             f"{md_to_tg(i['idea'])}{reason}")
        if v.fact_check:
            parts += ["", f"🔎 <b>Проверка фактов</b> ({esc(v.d.get('fact_checker', ''))}):"]
            for label, r in v.fact_check.items():
                parts.append(f"<b>Вариант {esc(label)}</b> — {esc(v.title(label)[:80])}")
                for c in r.get("claims", []):
                    src = [u for u in map(v.safe_url, c.get("sources") or []) if u][:2]
                    links = " ".join(f'<a href="{esc(u)}">[{i + 1}]</a>' for i, u in enumerate(src))
                    parts.append(f"{v.FACT_ICON.get(c.get('status'), '❔')} {md_to_tg(c.get('claim', ''))}"
                                 + (f" — <i>{md_to_tg(c.get('note', ''))}</i>" if c.get("note") else "") + (f" {links}" if links else ""))
        await self.send(chat_id, "\n".join(parts))

    async def send_tally(self, chat_id: int, stem: str) -> None:
        v = await asyncio.to_thread(self.archive.view, stem)
        lines = ["📊 <b>Голосование</b>"]
        for i, r in enumerate(v.tally, 1):
            lab = r.get("label", "")
            avg = r.get("avg_score")
            lines.append(f"{i}. <b>{lab}</b> {esc(v.title(lab)[:60])} ({esc(v.author(lab))}) — "
                         + (f"Шульце {v.schulze_str(r, len(v.tally))}, " if v.by_schulze else "")
                         + f"Борда {float(r.get('borda') or 0):.2f}, попарно {v.pairwise_str(r)}, "
                         f"критика {'—' if avg is None else f'{avg:.1f}'}, "
                         f"1-х мест {r.get('first_places', 0)}/{r.get('ballots', 0)}")
        lines += [f"• {esc(n)}" for n in v.tie_notes]
        if v.agreement_line:
            lines += ["", f"Согласие совета: {esc(v.agreement_line)}"]
        if v.coverage_line:
            lines += ["", esc(v.coverage_line)]
        if v.decisive_notes:
            lines += ["", "<b>Решающие голоса</b>: " + esc("; ".join(v.decisive_notes))]
        if v.flag_notes:
            lines += ["", "⚠️ <b>Бюллетени против общего мнения</b> (голоса учтены):"] + [f"• {esc(n)}" for n in v.flag_notes]
            lines.append(esc(v.robust_note))
        await self.send(chat_id, "\n".join(lines))

    async def send_result(self, chat_id: int, res: SessionResult, stem: str) -> None:
        await self.send_summary(chat_id, stem)

    # ------------------------------------------------------------ отчёты и архив

    def _report_buttons(self, stem: str, skip: str = "") -> list[list[dict]]:
        """Ряды «полный отчёт», «итог» и «только решение» — во всех форматах, кроме уже отправленного."""
        key = key_of(stem)
        full = [self._btn(f"📄 {FORMAT_NAME[f]}", f"rep:{f}:{key}") for f in available_formats() if f != skip]
        short = [self._btn(f"📌 {FORMAT_NAME[f]}", f"rep:summary.{f}:{key}")
                 for f in available_formats() if f"summary.{f}" != skip]
        decision = [self._btn(f"✅ {FORMAT_NAME[f]}", f"rep:decision.{f}:{key}")
                    for f in available_formats() if f"decision.{f}" != skip]
        return [row for row in (full, short, decision) if row]

    async def send_report(self, chat_id: int, stem: str, fmt: str | None = None, caption: str | None = None) -> None:
        """Отправляет отчёт файлом (по умолчанию полный PDF, если он доступен) с кнопками других форматов.

        fmt — «pdf» / «docx» / «md» (полный отчёт) «summary.pdf» и т. п. (итог с обоснованием) или «decision.pdf» и т. п. (только решение).
        """
        fmts = available_formats()
        fmt = fmt or fmts[0]
        if caption is None and fmt.startswith("summary."):
            caption = "📌 Итоговое решение с обоснованием — без вариантов, критики и голосования"
        elif caption is None and fmt.startswith("decision."):
            caption = "✅ Только решение совета"
        try:
            path = await asyncio.to_thread(self.archive.report, stem, fmt)
            content = await asyncio.to_thread(path.read_bytes)
        except (KeyError, RuntimeError, OSError) as e:
            await self.send(chat_id, f"⚠️ Не удалось подготовить отчёт: {esc(e)}")
            return
        kb = self._report_buttons(stem, skip=fmt)
        try:
            await self.api.send_document(chat_id, download_name(stem, fmt), content, caption=caption,
                                         mime=MIME[fmt.rsplit(".", 1)[-1]].split(";")[0],
                                         reply_markup={"inline_keyboard": kb} if kb else None)
        except TelegramError as e:
            log.warning("не удалось отправить отчёт: %s", e)
            await self.send(chat_id, f"⚠️ Telegram не принял файл отчёта: {esc(e.description)}")

    async def show_archive(self, chat_id: int, query: str, page: int, edit_id: int | None = None) -> None:
        items = await asyncio.to_thread(self.archive.list, query)
        pages = max(1, (len(items) + ARCHIVE_PAGE - 1) // ARCHIVE_PAGE)
        page = max(0, min(page, pages - 1))
        chunk = items[page * ARCHIVE_PAGE:(page + 1) * ARCHIVE_PAGE]
        head = "🗂 <b>Архив заседаний</b>" + (f" · поиск «{esc(query)}»" if query else "")
        if not items:
            text = head + ("\nНичего не найдено." if query else "\nПока пусто — задайте первый вопрос.")
            kb: list[list[dict]] = []
        else:
            lines = [head, f"<i>Всего: {len(items)} · страница {page + 1} из {pages}</i>", ""]
            for i, it in enumerate(chunk, page * ARCHIVE_PAGE + 1):
                win = f" · 🏆 {esc(it['winner_title'][:50])}" if it["winner"] else ""
                lines.append(f"<b>{i}.</b> {esc(short_title(it['question'], 100))}\n    <i>{esc(it['date'])}{win}</i>")
            lines += ["", "Нажмите на заседание, чтобы открыть решение и отчёты."]
            text = "\n".join(lines)
            kb = [[self._btn(f"{i}. {short_title(it['question'], 48)}", f"ses:{it['key']}")]
                  for i, it in enumerate(chunk, page * ARCHIVE_PAGE + 1)]
            nav = []
            if page > 0:
                nav.append(self._btn("‹ Новее", f"arc:{page - 1}"))
            if page < pages - 1:
                nav.append(self._btn("Старше ›", f"arc:{page + 1}"))
            if nav:
                kb.append(nav)
        markup = {"inline_keyboard": kb}
        if edit_id:
            await self.api.edit_message(chat_id, edit_id, text, reply_markup=markup)
        else:
            await self.send(chat_id, text, reply_markup=markup)

    async def on_archive_callback(self, cq: dict, chat_id: int, message_id: int | None, kind: str, rest: str) -> None:
        if kind == "arc":
            await self.api.answer_callback(cq["id"])
            page = int(rest) if rest.isdigit() else 0
            await self.show_archive(chat_id, self.archive_query.get(chat_id, ""), page, edit_id=message_id)
            return
        fmt, _, key = rest.partition(":") if kind in ("rep", "res") else ("", "", rest)
        stem = await asyncio.to_thread(self.archive.find_key, key)
        if not stem:
            await self.api.answer_callback(cq["id"], "Заседание не найдено (возможно, удалено)")
            return
        if kind == "ses":
            await self.api.answer_callback(cq["id"])
            await self.send_summary(chat_id, stem)
        elif kind == "res" and fmt == "fmt":
            await self.api.answer_callback(cq["id"])
            await self.send(chat_id, "📎 <b>Отчёты</b> — выберите вид и формат:\n"
                                     "📄 <b>Полный</b> — всё заседание: варианты, критика, голосование, расходы\n"
                                     "📌 <b>Итог</b> — решение с обоснованием, рисками и особыми мнениями\n"
                                     "✅ <b>Только решение</b> — без вопроса, чтобы переслать",
                            reply_markup={"inline_keyboard": self._report_buttons(stem)})
        elif kind == "res":
            await self.api.answer_callback(cq["id"])
            await (self.send_details if fmt == "more" else self.send_tally)(chat_id, stem)
        elif kind == "fup":
            await self.api.answer_callback(cq["id"])
            self.followup_wait[chat_id] = stem
            title = await asyncio.to_thread(lambda: self.archive.view(stem).title_text)
            await self.send(chat_id, f"💬 Напишите уточняющий вопрос к «{esc(title)}».\n"
                                     "Совет учтёт своё решение и вложения. Можно приложить новые файлы. /cancel — отменить.")
        elif kind == "rpt":
            await self.api.answer_callback(cq["id"], "Готовлю повтор…")
            await self.repeat(chat_id, stem)
        elif fmt in all_reports():
            name = FORMAT_NAME[fmt.rsplit(".", 1)[-1]]
            await self.api.answer_callback(cq["id"], f"Отправляю {'итог, ' if fmt.startswith('summary.') else 'решение, ' if fmt.startswith('decision.') else ''}{name}…")
            await self.send_report(chat_id, stem, fmt)
        else:
            await self.api.answer_callback(cq["id"], "Этот формат недоступен")


# ---------------------------------------------------------------- запуск


def parse_allowed(raw: str | None) -> set[int]:
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


async def run_bot(config_path: str | Path) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN: создайте бота у @BotFather и добавьте токен в .env")
    allowed = parse_allowed(os.environ.get("TELEGRAM_ALLOWED_USERS"))
    if not allowed:
        log.warning("TELEGRAM_ALLOWED_USERS пуст: бот никого не пустит, но покажет каждому его Telegram ID")
    api = TelegramAPI(token, base_url=os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"))
    bot = CouncilBot(api, config_path, allowed_users=allowed)
    setup_logging(bot.runs_dir, "bot")
    log.info("бот запущен")
    try:
        await bot.run()
    finally:
        await bot.shutdown()
        await api.aclose()
