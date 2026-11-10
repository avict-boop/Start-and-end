import asyncio
import json

import httpx
import pytest
import yaml

from council.telegram.api import TelegramAPI, TelegramError
from council.telegram.bot import CouncilBot, parse_allowed
from council.telegram.format import md_to_tg, split_message

OWNER, STRANGER, CHAT = 111, 999, 111


class FakeTelegram:
    """Имитация Bot API: записывает вызовы, выдаёт message_id."""

    def __init__(self):
        self.calls = []
        self.next_id = 100
        self.fail = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if request.headers.get("content-type", "").startswith("application/json"):
            body = json.loads(request.content or b"{}")
        else:
            raw = request.content
            body = {"_multipart": raw.decode("utf-8", "replace")}
            import re as _re
            fn = _re.search(rb'filename="([^"]+)"', raw)
            body["filename"] = fn.group(1).decode("utf-8", "replace") if fn else ""
            body["is_pdf"] = b"%PDF-" in raw
            body["is_docx"] = b"PK\x03\x04" in raw
            mk = _re.search(rb'name="reply_markup"\r\n\r\n(.*?)\r\n--', raw, _re.S)
            body["reply_markup"] = json.loads(mk.group(1)) if mk else None
        self.calls.append((method, body))
        if self.fail.get(method):
            f = self.fail[method]
            code, desc = f.pop(0) if isinstance(f, list) else self.fail.pop(method)
            return httpx.Response(200, json={"ok": False, "error_code": code, "description": desc})
        if method in ("sendMessage", "sendDocument"):
            self.next_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": []})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "council_bot"}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def texts(self, method="sendMessage"):
        return [b.get("text", "") for m, b in self.calls if m == method]

    def last(self, method):
        return next(b for m, b in reversed(self.calls) if m == method)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = {"chair": "a", "runs_dir": str(tmp_path / "runs"),
           "members": [{"name": n, "model": f"m/{n}"} for n in "abc"] + [{"name": "z", "model": "m/z", "enabled": False}]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    fake = FakeTelegram()
    api = TelegramAPI("T", transport=httpx.MockTransport(fake.handler), max_retries=1)
    bot = CouncilBot(api, path, allowed_users={OWNER}, edit_interval=0.01, mock_delay=0)
    return bot, fake, tmp_path


def msg(text, user=OWNER, chat=CHAT, chat_type="private"):
    return {"update_id": 1, "message": {"message_id": 1, "text": text, "from": {"id": user},
                                        "chat": {"id": chat, "type": chat_type}}}


def press(data, message_id, user=OWNER, chat=CHAT):
    return {"update_id": 2, "callback_query": {"id": "cb", "data": data, "from": {"id": user},
                                               "message": {"message_id": message_id, "chat": {"id": chat}}}}


def buttons(body):
    return [b["callback_data"] for row in body["reply_markup"]["inline_keyboard"] for b in row]


async def finish(bot):
    for r in list(bot.running.values()):
        await r.task


def test_stranger_gets_id_and_nothing_else(env):
    bot, fake, _ = env
    asyncio.run(bot.handle_update(msg("Привет", user=STRANGER, chat=STRANGER)))
    assert len(fake.texts()) == 1 and f"<code>{STRANGER}</code>" in fake.texts()[0]
    asyncio.run(bot.handle_update(press("go", 5, user=STRANGER)))
    assert fake.last("answerCallbackQuery")["text"] == "Нет доступа"
    assert not bot.pending


def test_question_card_and_full_demo_session(env):
    bot, fake, tmp = env

    async def scenario():
        await bot.handle_update(msg("Как назвать кофейню у университета?"))
        card = fake.last("sendMessage")
        assert "Вопрос для совета" in card["text"] and "демо" in card["text"]
        assert {"mode:debate", "mode:quick", "rounds:2", "view:members", "demo", "go", "drop"} <= set(buttons(card))
        card_id = bot.pending[CHAT].card_id
        await bot.handle_update(press("rounds:2", card_id))
        assert bot.settings[CHAT].rounds == 2 and "раундов: 2" in fake.last("editMessageText")["text"]
        await bot.handle_update(press("go", card_id))
        assert CHAT in bot.running
        await finish(bot)

    asyncio.run(scenario())
    texts = fake.texts()
    assert any("Решение совета" in t for t in texts)
    assert any("Голосование" in t and "Борда" in t for t in texts)
    edits = [b["text"] for m, b in fake.calls if m == "editMessageText"]
    assert any("Совет заседает" in t for t in texts) and "Заседание завершено" in edits[-1]
    assert any(m == "sendDocument" for m, _ in fake.calls)
    assert list((tmp / "runs").glob("*.json"))  # заседание видно и в веб-истории
    assert json.loads((tmp / "runs" / "telegram_settings.json").read_text())[str(CHAT)]["rounds"] == 2


def test_members_toggle_and_quorum_problem(env):
    bot, fake, _ = env

    async def scenario():
        await bot.handle_update(msg("Вопрос про выбор"))
        cid = bot.pending[CHAT].card_id
        await bot.handle_update(press("view:members", cid))
        assert "mem:z" in buttons(fake.last("editMessageText"))
        await bot.handle_update(press("mem:z", cid))
        assert bot.settings[CHAT].members == ["a", "b", "c", "z"]
        for n in ("b", "c", "z"):
            await bot.handle_update(press(f"mem:{n}", cid))
        assert bot.settings[CHAT].members == ["a"]
        await bot.handle_update(press("view:main", cid))
        assert "⚠️" in fake.last("editMessageText")["text"]  # один участник — нет кворума
        await bot.handle_update(press("go", cid))
        assert not bot.running and "⚠️" in fake.texts()[-1]
        await bot.handle_update(press("mem:*", cid))
        assert bot.settings[CHAT].members is None

    asyncio.run(scenario())


def test_evaluate_command(env):
    bot, fake, _ = env

    async def scenario():
        await bot.handle_update(msg("/evaluate Какую БД выбрать?\n1. PostgreSQL\n- ClickHouse\n1С-Аналитика"))
        p = bot.pending[CHAT]
        assert p.question == "Какую БД выбрать?" and p.options == ["PostgreSQL", "ClickHouse", "1С-Аналитика"]
        assert "mode:debate" not in buttons(fake.last("sendMessage"))
        await bot.handle_update(press("go", p.card_id))
        await finish(bot)

    asyncio.run(scenario())
    assert any("Решение совета" in t for t in fake.texts())
    asyncio.run(bot.handle_update(msg("/evaluate только вопрос")))
    assert "Формат" in fake.texts()[-1]


def test_stop_button_cancels(env):
    bot, fake, _ = env
    bot.mock_delay = 0.2

    async def scenario():
        await bot.handle_update(msg("Долгий вопрос"))
        await bot.handle_update(press("go", bot.pending[CHAT].card_id))
        await asyncio.sleep(0.05)
        r = bot.running[CHAT]
        await bot.handle_update(press("stop", r.message_id))
        await asyncio.gather(r.task, return_exceptions=True)

    asyncio.run(scenario())
    edits = [b["text"] for m, b in fake.calls if m == "editMessageText"]
    assert "остановлено" in edits[-1]
    assert not any("Решение совета" in t for t in fake.texts())


def test_real_mode_without_key_is_refused(env):
    bot, fake, _ = env

    async def scenario():
        await bot.handle_update(msg("/budget 0,5"))
        assert bot.settings[CHAT].budget == 0.5
        bot.settings[CHAT].demo = False
        bot.catalog.get = lambda: asyncio.sleep(0, result=None)
        await bot.handle_update(msg("Вопрос без ключа"))
        assert "Нет OPENROUTER_API_KEY" in fake.last("sendMessage")["text"]
        await bot.handle_update(press("go", bot.pending[CHAT].card_id))
        assert not bot.running

    asyncio.run(scenario())


def test_group_commands(env):
    bot, fake, _ = env
    bot.username = "council_bot"
    asyncio.run(bot.handle_update(msg("просто болтовня", chat=-5, chat_type="group")))
    assert not fake.texts()
    asyncio.run(bot.handle_update(msg("/ask@other_bot вопрос", chat=-5, chat_type="group")))
    assert not fake.texts()
    asyncio.run(bot.handle_update(msg("/ask@council_bot Что выбрать?", chat=-5, chat_type="group")))
    assert bot.pending[-5].question == "Что выбрать?"


def test_parse_fallback_to_plain_text(env):
    bot, fake, _ = env
    fake.fail["sendMessage"] = (400, "Bad Request: can't parse entities")
    asyncio.run(bot.send(CHAT, "<b>жирный</b> & текст"))
    assert fake.texts()[-1] == "жирный &amp; текст"


def test_api_retries_429():
    n = []

    def handler(req):
        n.append(1)
        if len(n) == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many",
                                             "parameters": {"retry_after": 0}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

    api = TelegramAPI("T", transport=httpx.MockTransport(handler))
    assert asyncio.run(api.send_message(1, "x"))["message_id"] == 7
    with pytest.raises(TelegramError):
        api2 = TelegramAPI("T", transport=httpx.MockTransport(
            lambda r: httpx.Response(400, json={"ok": False, "error_code": 400, "description": "bad"})))
        asyncio.run(api2.send_message(1, "x"))


def test_format_helpers():
    out = md_to_tg("# Итог\n**Важно** <script> & `a<b`\n- пункт\n[ссылка](https://x.ru)")
    assert out == '<b>Итог</b>\n<b>Важно</b> &lt;script&gt; &amp; <code>a&lt;b</code>\n• пункт\n<a href="https://x.ru">ссылка</a>'
    long = "\n\n".join(["<b>абзац</b> " + "слово " * 300] * 5)
    parts = split_message(long, limit=4000)
    assert len(parts) > 1 and all(len(p) <= 4000 for p in parts)
    assert all(p.count("<b>") == p.count("</b>") for p in parts)
    huge = split_message("x" * 9000, limit=4000)
    assert all(len(p) <= 4000 for p in huge) and sum(map(len, huge)) == 9000
    assert parse_allowed("1, 2;x, -100") == {1, 2, -100}


def test_run_exits_on_bad_token(env):
    bot, fake, _ = env
    fake.fail["getMe"] = (401, "Unauthorized")
    with pytest.raises(SystemExit, match="Неверный TELEGRAM_BOT_TOKEN"):
        asyncio.run(bot.run())


def test_run_retries_when_telegram_unavailable(env, monkeypatch):
    bot, fake, _ = env
    fake.fail["getMe"] = [(502, "Bad Gateway")] * 4  # больше, чем повторов внутри API-клиента
    sleeps = []

    async def fake_sleep(d):
        sleeps.append(d)
        if any(m == "getUpdates" for m, _ in fake.calls):  # Telegram «ожил», бот дошёл до опроса
            raise asyncio.CancelledError

    monkeypatch.setattr("council.telegram.bot.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("council.telegram.api.asyncio.sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot.run())
    assert sum(m == "getMe" for m, _ in fake.calls) == 5 and 5 in sleeps  # был повтор на уровне бота


def docs(fake):
    return [b for m, b in fake.calls if m == "sendDocument"]


def test_reports_and_archive(env):
    bot, fake, tmp = env

    async def scenario():
        await bot.handle_update(msg("Какой язык программирования учить первым?"))
        await bot.handle_update(press("go", bot.pending[CHAT].card_id))
        await finish(bot)

    asyncio.run(scenario())
    d = docs(fake)[-1]
    assert d["filename"].endswith(".pdf") and d["is_pdf"]
    kb = [b["callback_data"] for row in d["reply_markup"]["inline_keyboard"] for b in row]
    assert [k.split(":")[1] for k in kb] == ["docx", "md"]
    # кнопка Word → .docx
    asyncio.run(bot.handle_update(press(kb[0], 999)))
    d = docs(fake)[-1]
    assert d["filename"].endswith(".docx") and d["is_docx"]
    # архив: список и выбор заседания
    asyncio.run(bot.handle_update(msg("/archive")))
    arc = fake.last("sendMessage")
    assert "Архив заседаний" in arc["text"] and "язык программирования" in arc["text"]
    ses = [b for b in buttons(arc) if b.startswith("ses:")]
    assert len(ses) == 1
    asyncio.run(bot.handle_update(press(ses[0], 999)))
    d = docs(fake)[-1]
    assert d["is_pdf"] and "язык программирования" in d["_multipart"]
    # поиск
    asyncio.run(bot.handle_update(msg("/archive кофейня")))
    assert "Ничего не найдено" in fake.last("sendMessage")["text"]
    # посторонний не получает отчёты
    n = len(docs(fake))
    asyncio.run(bot.handle_update(press(ses[0], 999, user=STRANGER)))
    assert len(docs(fake)) == n and fake.last("answerCallbackQuery")["text"] == "Нет доступа"


def test_archive_pages_and_backfill(env):
    bot, fake, tmp = env
    import shutil

    async def one(q):
        await bot.handle_update(msg(q))
        await bot.handle_update(press("go", bot.pending[CHAT].card_id))
        await finish(bot)

    async def scenario():
        await one("Первый вопрос для архива")
    asyncio.run(scenario())
    runs = tmp / "runs"
    src = next(runs.glob("*.json"))
    for i in range(10):  # «старые» заседания: только стенограммы, без отчётов
        shutil.copy(src, runs / f"20200101-0000{i:02d}-old-{i}.json")
    asyncio.run(bot.handle_update(msg("/archive")))
    arc = fake.last("sendMessage")
    assert "страница 1 из 2" in arc["text"] and "arc:1" in buttons(arc)
    asyncio.run(bot.handle_update(press("arc:1", 777)))
    assert "страница 2 из 2" in fake.last("editMessageText")["text"]
    built, failed = bot.archive.ensure_all()
    # 10 «старых» + 1 свежее: при первом запуске версии отчётов ещё нет, поэтому пересобирается всё
    assert built == 11 and failed == 0
    assert bot.archive.ensure_all() == (0, 0)  # повторно — ничего не делает
    assert all((runs / f"20200101-0000{i:02d}-old-{i}.{ext}").is_file() for i in range(10) for ext in ("pdf", "docx", "md"))


def test_progress_shows_each_model():
    from council.telegram.bot import Progress
    pr = Progress(question="Длинный вопрос. С подробностями\n- пункт", mode="debate", rounds=1,
                  members=["a", "b", "c"], chair="a", demo=True)
    pr.apply("stage", {"stage": "proposal", "title": "Независимые предложения"})
    pr.apply("call", {"member": "a", "stage": "proposal", "phase": "thinking", "elapsed": 50})
    pr.apply("call", {"member": "b", "stage": "proposal", "phase": "writing", "elapsed": 3, "chars": 640})
    pr.apply("call", {"member": "c", "stage": "proposal", "phase": "done", "elapsed": 12})
    text = pr.render()
    assert "готово 1 из 3" in text and "❓ Длинный вопрос." in text
    assert "🤔 a — думает · 50 с" in text and "✍️ b — пишет ответ, ≈200 ток." in text and "✅ c — готово · 12 с" in text
    assert "процесс не завис" in text  # a думает дольше 40 с
