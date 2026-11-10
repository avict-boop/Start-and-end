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

from ..client import LLMClient, OpenRouterClient
from ..config import CouncilConfig, OverrideError, load_config, with_overrides
from ..estimate import ModelCatalog, estimate, parse_pricing, unknown_models
from ..mock import MockClient
from ..archive import MIME, Archive, available_formats, key_of
from ..report import make_stem, save
from ..session import Council, CouncilError, SessionResult
from .api import TelegramAPI, TelegramError
from ..reports.view import short_title
from .format import esc, md_to_tg, split_message, strip_tags

log = logging.getLogger("council.telegram")

STAGES = [("framing", "Постановка"), ("proposal", "Предложения"), ("critique", "Критика"),
          ("revision", "Доработка"), ("vote", "Голосование"), ("verdict", "Вердикт")]
MODE_NAME = {"debate": "Дебаты", "quick": "Быстро", "evaluate": "Оценка вариантов"}
COMMANDS = [
    ("ask", "задать вопрос совету"),
    ("evaluate", "выбрать из ваших вариантов"),
    ("settings", "режим, раунды, участники, демо"),
    ("budget", "лимит стоимости, напр. /budget 0.5"),
    ("archive", "архив заседаний и отчёты"),
    ("stop", "остановить идущее заседание"),
    ("help", "как пользоваться"),
]
HELP = """<b>Совет нейросетей</b>
Несколько моделей (Claude, ChatGPT, Gemini, Kimi, DeepSeek…) решают вашу задачу как совет экспертов: предлагают варианты вслепую, анонимно критикуют друг друга, дорабатывают, голосуют, а председатель выносит решение.

<b>Как пользоваться</b>
• Просто напишите вопрос (в группе: <code>/ask вопрос</code>) — бот покажет настройки и оценку стоимости, нажмите «Начать».
• <code>/evaluate</code> — выбор из готовых вариантов: первая строка — вопрос, дальше по варианту на строку (или блоки через <code>---</code>).
• /settings — режим, раунды, участники, демо-режим. Настройки запоминаются для чата.
• /budget 0.5 — лимит стоимости одного заседания в $.
• /archive — архив всех заседаний: отчёты в PDF, Word и Markdown. Поиск: <code>/archive кофейня</code>
• /stop — остановить заседание.

После заседания придут решение, итоги голосования и полный отчёт в PDF (по кнопкам — Word и Markdown)."""
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


@dataclass
class Pending:
    question: str
    options: list[str] | None = None
    card_id: int | None = None
    view: str = "main"  # main | members


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
            who = [self.chair] if d["stage"] in ("framing", "verdict") else self.members
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
            return f"▫️ {esc(name)} — ожидает запуска"
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
        return {"quick": {"critique", "revision"}, "evaluate": {"proposal", "revision"}}.get(self.mode, set())

    def render(self, *, final: str | None = None) -> str:
        head = final or "🏛 <b>Совет заседает…</b>"
        meta = MODE_NAME.get(self.mode, self.mode) + (f", раундов: {self.rounds}" if self.mode == "debate" else "")
        if self.demo:
            meta += " · демо"
        lines = [head, f"<i>{esc(meta)}</i>", f"❓ {esc(short_title(self.question))}", ""]
        steps = []
        for key, name in STAGES:
            if key in self.skipped():
                continue
            icon = "✅" if key in self.done_stages else "⏳" if key == self.stage else "▫️"
            steps.append(f"{icon} {name}")
        lines.append("  ".join(steps))
        if self.saving and not final:
            lines.append("<b>Сейчас:</b> готовлю отчёты PDF, Word и Markdown…")
        elif self.stage and not final:
            ready = sum(1 for s in self.work.values() if s != "work")
            stage_secs = int(time.monotonic() - self.stage_started)
            lines.append(f"<b>Сейчас:</b> {esc(self.stage_title)} — готово {ready} из {len(self.work)} · "
                         f"{_fmt_secs(stage_secs)} на этапе")
            lines += [self._member_line(n) for n in self.work]
            slow = [n for n, c in self.calls.items() if c.get("stage") == self.stage and self.work.get(n) == "work"
                    and c.get("phase") in ("sent", "thinking") and time.monotonic() - c["since"] > 40]
            if slow:
                lines.append(f"<i>⏳ {esc(', '.join(slow))}: «думающие» модели рассуждают до 1–3 минут — "
                             f"это нормально, процесс не завис.</i>")
        if self.proposals:
            lines += ["", "<b>Варианты:</b>"]
            lines += [f"<b>{l}</b> — {esc(t[:80])}" for l, t in sorted(self.proposals.items())]
        if self.failures:
            lines += ["", "<b>Сбои:</b>"] + [f"❌ {esc(f)}" for f in self.failures[-5:]]
        secs = int(time.monotonic() - self.started)
        lines += ["", f"💰 ${self.cost:.4f} · ⏱ {secs // 60} мин {secs % 60} с" if secs >= 60
                  else f"💰 ${self.cost:.4f} · ⏱ {secs} с"]
        return "\n".join(lines)


def _fmt_secs(secs: int) -> str:
    return f"{secs // 60}:{secs % 60:02d}" if secs >= 60 else f"{secs} с"


@dataclass
class Running:
    task: asyncio.Task
    progress: Progress
    message_id: int
    dirty: bool = True


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
        self.allowed = allowed_users
        self.edit_interval = edit_interval
        self.mock_delay = mock_delay
        self.llm_factory = llm_factory or self._default_llm
        self.catalog = ModelCatalog()
        self.pending: dict[int, Pending] = {}
        self.running: dict[int, Running] = {}
        self.username = ""
        self.archive_query: dict[int, str] = {}
        cfg = self.base_config()
        self.runs_dir = Path(cfg.runs_dir) if Path(cfg.runs_dir).is_absolute() else self.config_path.parent / cfg.runs_dir
        self.settings_path = self.runs_dir / "telegram_settings.json"
        self.settings: dict[int, ChatSettings] = self._load_settings()
        self.archive = Archive(self.runs_dir)

    # ------------------------------------------------------------ инфраструктура

    def base_config(self) -> CouncilConfig:
        return load_config(self.config_path)

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
        return with_overrides(base, members=members, budget=s.budget)

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
                asyncio.create_task(self.handle_update_safe(u))
            await asyncio.sleep(0)  # дать поработать обработчикам, даже если сервер ответил мгновенно

    async def handle_update_safe(self, update: dict[str, Any]) -> None:
        try:
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
        elif cmd == "/evaluate":
            await self.cmd_evaluate(chat_id, arg)
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
            await self.new_question(chat_id, arg)

    async def cmd_budget(self, chat_id: int, arg: str) -> None:
        s = self.chat_settings(chat_id)
        if not arg:
            cur = s.budget or self.base_config().max_cost_usd
            await self.send(chat_id, f"Лимит заседания: <b>${cur:.2f}</b>. Изменить: <code>/budget 0.5</code>")
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
        await self.send(chat_id, f"✅ Лимит заседания: <b>${value:.2f}</b>")

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

    async def new_question(self, chat_id: int, question: str, options: list[str] | None = None) -> None:
        if len(question) < 3:
            await self.send(chat_id, "Вопрос слишком короткий.")
            return
        old = self.pending.get(chat_id)
        if old and old.card_id:
            try:
                await self.api.edit_message(chat_id, old.card_id, "<i>Вопрос заменён новым.</i>")
            except TelegramError:
                pass
        p = Pending(question=question[:20000], options=options)
        self.pending[chat_id] = p
        text, markup = await self.card(chat_id, p)
        msg = await self.send(chat_id, text, reply_markup=markup)
        p.card_id = msg["message_id"] if msg else None

    async def card(self, chat_id: int, p: Pending) -> tuple[str, dict]:
        s = self.chat_settings(chat_id)
        if p.view == "members":
            return self._members_card(s)
        mode = "evaluate" if p.options else s.mode
        lines = ["🏛 <b>Вопрос для совета</b>", esc(p.question[:1500])]
        if p.options:
            lines += ["", "<b>Варианты:</b>"] + [f"{chr(65 + i)}. {esc(o[:200])}" for i, o in enumerate(p.options)]
        problem = None
        try:
            cfg = self.build_config(s)
        except OverrideError as e:
            cfg, problem = None, str(e)
        lines.append("")
        lines.append(f"<b>Режим:</b> {MODE_NAME[mode]}" + (f", раундов: {s.rounds}" if mode == "debate" else ""))
        if cfg:
            lines.append("<b>Участники:</b> " + ", ".join(
                f"{esc(m.name)}{' 🎩' if m.name == cfg.chair else ''}{' 😈' if m.name == cfg.devil_advocate else ''}"
                for m in cfg.active_members))
            lines.append("<i>🎩 председатель · 😈 адвокат дьявола</i>")
            est_line, est_problem = await self._estimate_line(cfg, s, mode, p)
            lines.append(est_line)
            problem = problem or est_problem
        if problem:
            lines += ["", f"⚠️ {esc(problem)}"]
        kb: list[list[dict]] = []
        if not p.options:
            kb.append([self._btn(("✓ " if s.mode == m else "") + MODE_NAME[m], f"mode:{m}") for m in ("debate", "quick")])
            if s.mode == "debate":
                kb.append([self._btn(("✓ " if s.rounds == r else "") + f"{r} раунд" + ("а" if r > 1 else ""), f"rounds:{r}")
                           for r in (1, 2, 3)])
        n = len(cfg.active_members) if cfg else 0
        kb.append([self._btn(f"👥 Участники ({n})", "view:members"),
                   self._btn(f"🧪 Демо: {'вкл' if s.demo else 'выкл'}", "demo")])
        kb.append([self._btn("▶️ Начать", "go"), self._btn("✖️ Отмена", "drop")])
        return "\n".join(lines), {"inline_keyboard": kb}

    def _members_card(self, s: ChatSettings) -> tuple[str, dict]:
        base = self.base_config()
        active = set(s.members) if s.members is not None else {m.name for m in base.active_members}
        rows, row = [], []
        for m in base.members:
            row.append(self._btn(("✅ " if m.name in active else "▫️ ") + m.name, f"mem:{m.name}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([self._btn("↩️ Как в config.yaml", "mem:*"), self._btn("« Назад", "view:main")])
        text = "👥 <b>Участники совета</b>\nОтметьте модели. Нужно минимум столько, сколько требует кворум.\n\n" + "\n".join(
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
        est = estimate(cfg, pricing, mode=mode, rounds=s.rounds,
                       n_options=len(p.options) if p.options else None, has_criteria=False)
        if s.demo:
            line = f"<b>Стоимость:</b> демо-режим, бесплатно ({est.calls} вызовов)"
        elif pricing:
            line = f"<b>Стоимость:</b> ≈ ${est.low:.3f} – ${est.high:.3f} ({est.calls} вызовов, лимит ${cfg.max_cost_usd:.2f})"
        else:
            line = f"<b>Стоимость:</b> не удалось оценить ({est.calls} вызовов, лимит ${cfg.max_cost_usd:.2f})"
        return line, problem

    @staticmethod
    def _btn(text: str, data: str) -> dict[str, str]:
        return {"text": text, "callback_data": data[:64]}

    async def show_settings(self, chat_id: int) -> None:
        s = self.chat_settings(chat_id)
        try:
            cfg = self.build_config(s)
            members = ", ".join(m.name for m in cfg.active_members)
            budget = cfg.max_cost_usd
        except OverrideError as e:
            members, budget = f"⚠️ {e}", s.budget or 0
        await self.send(chat_id, "⚙️ <b>Настройки чата</b>\n"
                        f"Режим: {MODE_NAME[s.mode]}" + (f", раундов: {s.rounds}" if s.mode == "debate" else "") + "\n"
                        f"Участники: {esc(members)}\nЛимит: ${budget:.2f}\nДемо-режим: {'вкл' if s.demo else 'выкл'}\n\n"
                        "Меняются кнопками под вопросом — напишите любой вопрос. Лимит: /budget 0.5")

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
        if kind0 in ("arc", "ses", "rep"):
            await self.on_archive_callback(cq, chat_id, message.get("message_id"), kind0, rest0)
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
        if kind == "mode" and value in ("debate", "quick"):
            s.mode = value
        elif kind == "rounds" and value.isdigit():
            s.rounds = max(1, min(5, int(value)))
        elif kind == "demo":
            s.demo = not s.demo
        elif kind == "view" and value in ("main", "members"):
            p.view = value
        elif kind == "mem":
            base = self.base_config()
            if value == "*":
                s.members = None
            elif value in {m.name for m in base.members}:
                cur = list(s.members) if s.members is not None else [m.name for m in base.active_members]
                cur = [x for x in cur if x != value] if value in cur else cur + [value]
                s.members = [m.name for m in base.members if m.name in cur]  # порядок как в конфиге
        self._save_settings()
        await self.api.answer_callback(cq["id"])
        text, markup = await self.card(chat_id, p)
        await self.api.edit_message(chat_id, p.card_id, text, reply_markup=markup)

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

        progress = Progress(question=p.question, mode=mode, rounds=s.rounds,
                            members=[m.name for m in cfg.active_members], chair=cfg.chair, demo=s.demo)
        stop_kb = {"inline_keyboard": [[self._btn("⏹ Остановить", "stop")]]}
        msg = await self.api.send_message(chat_id, progress.render(), reply_markup=stop_kb)
        client = self.llm_factory(cfg, s.demo)

        def on_event(event: str, **d: Any) -> None:
            progress.apply(event, d)
            r = self.running.get(chat_id)
            if r:
                r.dirty = True

        async def execute() -> None:
            updater = asyncio.create_task(self._updater(chat_id, stop_kb))
            stem = make_stem(p.question)
            try:
                council = Council(cfg, client, on_event=on_event)
                res = await council.run(p.question, options=p.options, mode=mode, rounds=s.rounds)
                progress.saving = True
                r = self.running.get(chat_id)
                if r:
                    r.dirty = True
                await asyncio.to_thread(save, res, self.runs_dir, stem)
                updater.cancel()
                await self._safe_edit(chat_id, msg["message_id"], progress.render(final="✅ <b>Заседание завершено</b>"))
                await self.send_result(chat_id, res, stem)
            except asyncio.CancelledError:
                updater.cancel()
                await self._safe_edit(chat_id, msg["message_id"], progress.render(final="⏹ <b>Заседание остановлено</b>"))
            except CouncilError as e:
                updater.cancel()
                await self._safe_edit(chat_id, msg["message_id"], progress.render(final="⚠️ <b>Заседание прервано</b>"))
                await self.send(chat_id, f"⚠️ {esc(e)}")
            except Exception as e:  # noqa: BLE001
                updater.cancel()
                log.exception("заседание упало")
                await self._safe_edit(chat_id, msg["message_id"], progress.render(final="⚠️ <b>Внутренняя ошибка</b>"))
                await self.send(chat_id, f"⚠️ Внутренняя ошибка: {esc(repr(e))[:500]}")
            finally:
                self.running.pop(chat_id, None)
                await client.aclose()

        self.running[chat_id] = Running(task=asyncio.create_task(execute()), progress=progress,
                                        message_id=msg["message_id"])

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

    async def send_result(self, chat_id: int, res: SessionResult, stem: str) -> None:
        v = res.verdict
        win = res.proposals[v.winner]
        author = lambda l: res.authors.get(l) or "пользователь"  # noqa: E731
        parts = [f"🏆 <b>Решение совета: вариант {v.winner} — {esc(win.title)}</b> ({esc(author(v.winner))})", "",
                 md_to_tg(v.final_answer), "", f"<b>Почему:</b> {md_to_tg(v.why)}"]
        if v.dissent:
            parts += ["", "<b>Особые мнения:</b>"] + [f"• {md_to_tg(x)}" for x in v.dissent]
        if v.risks:
            parts += ["", "<b>Риски:</b>"] + [f"• {md_to_tg(x)}" for x in v.risks]
        await self.send(chat_id, "\n".join(parts))

        lines = ["📊 <b>Голосование</b>"]
        for i, r in enumerate(res.tally, 1):
            avg = f"{r.avg_score:.1f}" if r.avg_score is not None else "—"
            lines.append(f"{i}. <b>{r.label}</b> {esc(res.proposals[r.label].title[:60])} ({esc(author(r.label))}) — "
                         f"Борда {r.borda:.2f}, критика {avg}, 1-х мест {r.first_places}/{r.ballots}")
        lines.append(f"\n💰 ${res.total_cost:.4f} · ⏱ {res.duration_s} с")
        await self.send(chat_id, "\n".join(lines))
        await self.send_report(chat_id, stem, caption="📄 Полный отчёт: все варианты, критика, бюллетени")

    # ------------------------------------------------------------ отчёты и архив

    def _report_buttons(self, stem: str, skip: str = "") -> list[list[dict]]:
        key = key_of(stem)
        row = [self._btn(FORMAT_NAME[f], f"rep:{f}:{key}") for f in available_formats() if f != skip]
        return [row] if row else []

    async def send_report(self, chat_id: int, stem: str, fmt: str | None = None, caption: str | None = None) -> None:
        """Отправляет отчёт файлом (по умолчанию PDF, если он доступен) с кнопками других форматов."""
        fmts = available_formats()
        fmt = fmt or fmts[0]
        try:
            path = await asyncio.to_thread(self.archive.report, stem, fmt)
            content = await asyncio.to_thread(path.read_bytes)
        except (KeyError, RuntimeError, OSError) as e:
            await self.send(chat_id, f"⚠️ Не удалось подготовить отчёт: {esc(e)}")
            return
        kb = self._report_buttons(stem, skip=fmt)
        try:
            await self.api.send_document(chat_id, path.name, content, caption=caption,
                                         mime=MIME[fmt].split(";")[0],
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
                win = f"🏆 {esc(it['winner'])}: {esc(it['winner_title'][:50])}" if it["winner"] else ""
                lines.append(f"<b>{i}.</b> {esc(short_title(it['question'], 100))}\n    <i>{esc(it['date'])}</i> · {win}")
            lines += ["", "Нажмите номер, чтобы получить отчёт."]
            text = "\n".join(lines)
            nums = [self._btn(str(i), f"ses:{it['key']}") for i, it in enumerate(chunk, page * ARCHIVE_PAGE + 1)]
            kb = [nums[:4], nums[4:]] if len(nums) > 4 else [nums]
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
        fmt, _, key = rest.partition(":") if kind == "rep" else ("", "", rest)
        stem = await asyncio.to_thread(self.archive.find_key, key)
        if not stem:
            await self.api.answer_callback(cq["id"], "Заседание не найдено (возможно, удалено)")
            return
        if kind == "ses":
            await self.api.answer_callback(cq["id"], "Готовлю отчёт…")
            s = await asyncio.to_thread(self.archive.summary, stem)
            caption = None
            if s:
                win = f"\n🏆 {esc(s['winner'])}: {esc(s['winner_title'][:80])} ({esc(s['winner_author'])})" if s["winner"] else ""
                caption = (f"<b>{esc(short_title(s['question']))}</b>\n<i>{esc(s['date'])} · {esc(s['mode_name'])} · "
                           f"${s['cost']:.4f}</i>{win}")[:1000]
            await self.send_report(chat_id, stem, caption=caption)
        elif fmt in available_formats():
            await self.api.answer_callback(cq["id"], f"Отправляю {FORMAT_NAME[fmt]}…")
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
    try:
        await bot.run()
    finally:
        await bot.shutdown()
        await api.aclose()
