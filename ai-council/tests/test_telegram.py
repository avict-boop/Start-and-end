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
        self.files = {}

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
        if request.url.path.startswith("/file/"):
            return httpx.Response(200, content=self.files.get(request.url.path.rsplit("/", 1)[-1], b""))
        if method == "getFile":
            return httpx.Response(200, json={"ok": True, "result": {"file_path": f"docs/{body['file_id']}"}})
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
        # карточка короткая: начать, настроить, отмена; настройки — отдельным экраном той же карточки
        assert {"go", "view:opts", "drop"} <= set(buttons(card)) and "mode:quick" not in buttons(card)
        card_id = bot.pending[CHAT].card_id
        await bot.handle_update(press("view:opts", card_id))
        assert {"mode:debate", "mode:quick", "rounds:2", "view:members", "demo", "go", "view:main"} <= set(buttons(fake.last("editMessageText")))
        await bot.handle_update(press("rounds:2", card_id))
        assert bot.settings[CHAT].rounds == 2 and "раундов: 2" in fake.last("editMessageText")["text"]
        await bot.handle_update(press("go", card_id))
        assert CHAT in bot.running
        await finish(bot)

    asyncio.run(scenario())
    texts = fake.texts()
    assert any("🏆 <b>Решение:" in t for t in texts)  # короткий итог с кнопками
    edits = [b["text"] for m, b in fake.calls if m == "editMessageText"]
    assert any("Совет заседает" in t for t in texts) and "Заседание завершено" in edits[-1]
    assert not any(m == "sendDocument" for m, _ in fake.calls)  # отчёт — по кнопке
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
        await bot.handle_update(press("view:opts", p.card_id))
        assert "mode:debate" not in buttons(fake.last("editMessageText")) and "view:members" in buttons(fake.last("editMessageText"))
        await bot.handle_update(press("go", p.card_id))
        await finish(bot)

    asyncio.run(scenario())
    assert any("🏆 <b>Решение:" in t for t in fake.texts())
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
    summary = next(b for m, b in reversed(fake.calls) if m == "sendMessage" and "🏆" in b.get("text", ""))
    btn = [b["callback_data"].split(":")[0] + ":" + b["callback_data"].split(":")[1] for row in summary["reply_markup"]["inline_keyboard"] for b in row]
    assert [b.split(":")[0] + (":" + b.split(":")[1] if b.split(":")[0] in ("res", "rep") else "") for b in btn] == ["res:more", "res:tally", "rep:pdf", "res:fmt", "fup", "rpt"]
    key = summary["reply_markup"]["inline_keyboard"][0][0]["callback_data"].split(":")[2]
    asyncio.run(bot.handle_update(press(f"res:more:{key}", 999)))
    assert "Решение совета: вариант" in fake.texts()[-1] and "Почему:" in fake.texts()[-1]
    asyncio.run(bot.handle_update(press(f"res:tally:{key}", 999)))
    assert "Голосование" in fake.texts()[-1] and "Борда" in fake.texts()[-1]
    asyncio.run(bot.handle_update(press(f"rep:pdf:{key}", 999)))
    d = docs(fake)[-1]
    assert d["filename"].endswith(".pdf") and d["is_pdf"]
    rows = d["reply_markup"]["inline_keyboard"]
    kb = [b["callback_data"] for b in rows[0]]
    assert [k.split(":")[1] for k in kb] == ["docx", "md"]
    assert [b["callback_data"].split(":")[1] for b in rows[1]] == ["summary.pdf", "summary.docx", "summary.md"]
    assert [b["callback_data"].split(":")[1] for b in rows[2]] == ["decision.pdf", "decision.docx", "decision.md"]
    # кнопка «✅ Решение Word» → документ только с решением совета
    asyncio.run(bot.handle_update(press(rows[2][1]["callback_data"], 999)))
    dd = docs(fake)[-1]
    assert "решение" in dd["filename"] and dd["filename"].endswith(".docx") and "Только решение совета" in dd["_multipart"]
    # кнопка «📌 Итог PDF» → короткий PDF только с решением
    asyncio.run(bot.handle_update(press(rows[1][0]["callback_data"], 999)))
    d2 = docs(fake)[-1]
    assert d2["filename"].endswith("-итог.pdf") and d2["is_pdf"] and "Итоговое решение с обоснованием" in d2["_multipart"]
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
    card = fake.last("sendMessage")
    assert "🏆" in card["text"] and "язык программирования" in card["text"] and "fup:" + ses[0][4:] in buttons(card)
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


def test_files_from_telegram(env):
    import io
    from PIL import Image
    bot, fake, tmp = env
    b = io.BytesIO()
    Image.new("RGB", (640, 480), "green").save(b, "JPEG")
    fake.files["PHOTO1"] = b.getvalue()
    fake.files["DOC1"] = "Бюджет: 1 млн".encode()

    def photo(caption=None):
        m = {"update_id": 5, "message": {"message_id": 7, "from": {"id": OWNER}, "chat": {"id": CHAT, "type": "private"},
                                         "photo": [{"file_id": "small", "width": 90, "height": 90},
                                                   {"file_id": "PHOTO1", "width": 640, "height": 480, "file_size": 9000}]}}
        if caption:
            m["message"]["caption"] = caption
        return m

    async def scenario():
        await bot.handle_update(photo())               # фото без подписи — в черновик
        assert "Теперь напишите вопрос" in fake.last("sendMessage")["text"] and len(bot.drafts[CHAT]) == 1
        await bot.handle_update({"update_id": 6, "message": {"message_id": 8, "from": {"id": OWNER},
                                 "chat": {"id": CHAT, "type": "private"},
                                 "document": {"file_id": "DOC1", "file_name": "бриф.txt", "file_size": 20}}})
        await bot.handle_update(msg("Что на фото и как это связано с брифом?"))
        p = bot.pending[CHAT]
        assert [a.name for a in p.attachments] == ["фото-7.jpg", "бриф.txt"] and CHAT not in bot.drafts
        card = fake.last("sendMessage")
        assert "Вложения (2)" in card["text"] and "noatt" in buttons(card)
        await bot.handle_update(press("go", p.card_id))
        await finish(bot)

    asyncio.run(scenario())
    saved = list((tmp / "runs").glob("*.files/*"))
    assert sorted(x.name for x in saved) == ["01-фото-7.jpg", "02-бриф.txt"]
    # фото с подписью — сразу вопрос; /clear убирает вложения
    asyncio.run(bot.handle_update(photo("Это вопрос с картинкой")))
    assert bot.pending[CHAT].question == "Это вопрос с картинкой" and len(bot.pending[CHAT].attachments) == 1
    asyncio.run(bot.handle_update(msg("/clear")))
    assert not bot.pending[CHAT].attachments and "Вложения убраны (1)" in fake.texts()[-1]
    # чужой не может прислать файл
    n = len(fake.texts())
    asyncio.run(bot.handle_update({"update_id": 9, "message": {"message_id": 9, "from": {"id": STRANGER},
                                   "chat": {"id": STRANGER, "type": "private"}, "photo": [{"file_id": "PHOTO1"}]}}))
    assert "Доступ к боту закрыт" in fake.texts()[-1] and len(fake.texts()) == n + 1


def test_same_chat_updates_processed_in_order(env):
    """Альбом из двух фото и сразу вопрос: вопрос обрабатывается после фото, оба файла приложены."""
    import io
    from PIL import Image
    bot, fake, _ = env
    b = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(b, "PNG")
    fake.files["P1"] = fake.files["P2"] = b.getvalue()

    def photo(i):
        return {"update_id": 20 + i, "message": {"message_id": 30 + i, "media_group_id": "album1", "from": {"id": OWNER},
                                                 "chat": {"id": CHAT, "type": "private"}, "photo": [{"file_id": f"P{i}"}]}}

    async def scenario():
        await asyncio.gather(bot.handle_update_safe(photo(1)), bot.handle_update_safe(photo(2)),
                             bot.handle_update_safe(msg("Сравни эти две картинки")))

    asyncio.run(scenario())
    p = bot.pending[CHAT]
    assert p.question == "Сравни эти две картинки" and [a.name for a in p.attachments] == ["фото-31.jpg", "фото-32.jpg"]
    assert sum("Принял" in t for t in fake.texts()) == 1  # на альбом — один ответ


def test_followup_repeat_voice_stats_in_bot(env):
    bot, fake, tmp = env

    async def run_session(q):
        await bot.handle_update(msg(q))
        await bot.handle_update(press("go", bot.pending[CHAT].card_id))
        await finish(bot)

    asyncio.run(run_session("Как назвать кофейню?"))
    first = next(s for s in bot.archive.stems() if "кофейню" in s)
    from council.archive import key_of
    key = key_of(first)
    # «Уточнить» → бот ждёт вопрос → следующий текст становится уточнением
    asyncio.run(bot.handle_update(press(f"fup:{key}", 999)))
    assert "уточняющий вопрос" in fake.texts()[-1] and bot.followup_wait[CHAT] == first
    asyncio.run(bot.handle_update(msg("А если бюджет меньше?")))
    p = bot.pending[CHAT]
    assert p.parent == first and "уточнение к «Как назвать кофейню?»" in fake.last("sendMessage")["text"]
    async def go():
        await bot.handle_update(press("go", p.card_id))
        await finish(bot)

    asyncio.run(go())
    second = next(s for s in bot.archive.stems() if "бюджет" in s)
    assert bot.archive.load(second)["followup_of"] == first
    # «Повторить» → новая карточка с тем же вопросом
    asyncio.run(bot.handle_update(press(f"rpt:{key}", 999)))
    assert bot.pending[CHAT].question == "Как назвать кофейню?" and "Повтор" in fake.texts()[-2]
    asyncio.run(bot.handle_update(msg("/cancel")))
    assert CHAT not in bot.pending
    # голосовое → расшифровка → вопрос (демо-режим)
    from tests.test_features import audio
    fake.files["VOICE1"] = audio("ogg", "libopus", 2.0)
    asyncio.run(bot.handle_update({"update_id": 40, "message": {"message_id": 41, "from": {"id": OWNER},
        "chat": {"id": CHAT, "type": "private"}, "voice": {"file_id": "VOICE1", "duration": 2, "file_size": 30000}}}))
    assert any("🎤 <b>Распознано:</b>" in t for t in fake.texts())
    assert "демо-расшифровка" in bot.pending[CHAT].question
    # статистика
    asyncio.run(bot.handle_update(msg("/stats")))
    assert "Заседаний: <b>2</b>" in fake.texts()[-1]


def test_fact_check_toggle_in_bot(env):
    bot, fake, tmp = env

    async def scenario():
        await bot.handle_update(msg("Сколько стоит открыть кофейню?"))
        card_id = bot.pending[CHAT].card_id
        await bot.handle_update(press("view:opts", card_id))
        assert "facts" in buttons(fake.last("editMessageText"))
        await bot.handle_update(press("facts", card_id))
        assert bot.settings[CHAT].fact_check is True
        assert "✅ Проверка фактов" in json.dumps(fake.last("editMessageText"), ensure_ascii=False)
        await bot.handle_update(press("go", card_id))
        await finish(bot)

    asyncio.run(scenario())
    summary = next(t for t in reversed(fake.texts()) if "🏆" in t)
    assert "🔎 Факты: проверены варианты" in summary
    key = next(b for b in buttons(fake.last("sendMessage")) if b.startswith("res:more:")).split(":")[2]
    asyncio.run(bot.handle_update(press(f"res:more:{key}", 999)))
    assert "Проверка фактов" in fake.texts()[-1] and "❌" in fake.texts()[-1]


def test_long_question_split_by_telegram_is_joined(env):
    bot, fake, tmp = env
    part1 = "Начало вопроса. " + "текст " * 700 + "конец первой части"   # ~4200 символов, как режет Telegram
    part2 = "вторая часть вопроса"

    async def scenario():
        await bot.handle_update(msg(part1))
        card_id = bot.pending[CHAT].card_id
        await bot.handle_update(msg(part2))
        p = bot.pending[CHAT]
        assert p.card_id == card_id and p.question == part1 + "\n" + part2  # одна карточка, вопрос склеен
        assert "совет получит вопрос целиком" in fake.last("editMessageText")["text"]
        # короткое следующее сообщение после короткого вопроса — уже новый вопрос
        await bot.handle_update(msg("Другой вопрос?"))
        assert bot.pending[CHAT].question == "Другой вопрос?"
        await bot.handle_update(press("go", bot.pending[CHAT].card_id))
        await finish(bot)

    asyncio.run(scenario())


def test_logs_command_sends_diagnostics(env):
    bot, fake, tmp = env

    async def scenario():
        await bot.handle_update(msg("Как назвать кофейню у университета?"))
        await bot.handle_update(press("go", bot.pending[CHAT].card_id))
        await finish(bot)
        await bot.handle_update(msg("/logs"))

    asyncio.run(scenario())
    d = docs(fake)[-1]
    assert d["filename"].startswith("diagnostics-") and d["filename"].endswith(".zip")
    assert "Журнал для разбора последнего заседания" in d["_multipart"]
    assert list((tmp / "runs").glob("*.events.jsonl"))


def test_compact_toggle_in_bot(env):
    bot, fake, tmp = env

    async def scenario():
        await bot.handle_update(msg("Сделай подробный отчёт о рынке кофеен"))
        card_id = bot.pending[CHAT].card_id
        await bot.handle_update(press("view:opts", card_id))
        assert "compact" in buttons(fake.last("editMessageText"))
        await bot.handle_update(press("compact", card_id))
        assert bot.settings[CHAT].compact is True
        assert "✅ Компактно" in json.dumps(fake.last("editMessageText"), ensure_ascii=False)
        await bot.handle_update(press("go", card_id))
        await finish(bot)

    asyncio.run(scenario())
    data = json.loads(next((tmp / "runs").glob("2*-*[!s].json")).read_text())
    assert data["request"]["compact_proposals"] is True


def test_settings_command_is_interactive(env):
    bot, fake, _ = env

    async def scenario():
        await bot.handle_update(msg("/settings"))
        m = fake.last("sendMessage")
        assert "Настройки чата" in m["text"] and {"set:mode:quick", "set:facts", "set:demo", "set:view:members"} <= set(buttons(m))
        await bot.handle_update(press("set:mode:quick", 77))
        assert bot.settings[CHAT].mode == "quick" and "Быстрый ответ" in fake.last("editMessageText")["text"]
        await bot.handle_update(press("set:view:members", 77))
        assert "set:mem:z" in buttons(fake.last("editMessageText"))
        await bot.handle_update(press("set:mem:z", 77))
        assert "z" in bot.settings[CHAT].members and "set:mem:z" in buttons(fake.last("editMessageText"))
        await bot.handle_update(press("set:close", 77))
        assert "Сохранено" in fake.last("editMessageText")["text"]

    asyncio.run(scenario())


def test_old_card_buttons_still_work(env):
    """Карточки, отправленные до обновления, меняли настройки прямо с главного экрана — эти кнопки не ломаются."""
    bot, fake, _ = env

    async def scenario():
        await bot.handle_update(msg("Вопрос со старой карточкой"))
        cid = bot.pending[CHAT].card_id
        await bot.handle_update(press("mode:quick", cid))
        await bot.handle_update(press("demo", cid))
        assert bot.settings[CHAT].mode == "quick"

    asyncio.run(scenario())


def test_other_formats_button(env):
    bot, fake, _ = env

    async def scenario():
        await bot.handle_update(msg("Какой ноутбук купить студенту?"))
        await bot.handle_update(press("go", bot.pending[CHAT].card_id))
        await finish(bot)

    asyncio.run(scenario())
    summary = next(b for m, b in reversed(fake.calls) if m == "sendMessage" and "🏆" in b.get("text", ""))
    fmt = next(b for b in buttons(summary) if b.startswith("res:fmt:"))
    asyncio.run(bot.handle_update(press(fmt, 999)))
    m = fake.last("sendMessage")
    assert "Отчёты" in m["text"] and any(b.startswith("rep:decision.") for b in buttons(m))


def test_markdown_tables_become_lists():
    from council.telegram.format import md_to_tg
    out = md_to_tg("| Метрика | Цель |\n|---|:---:|\n| Конверсия | ≥ 3% |\n| <CAC> | 1 500 ₽ |")
    assert "|" not in out and "• <b>Конверсия</b> — Цель: ≥ 3%" in out and "&lt;CAC&gt;" in out


def test_budget_pause_and_resume_by_button(env):
    bot, fake, tmp = env

    async def scenario():
        await bot.handle_update(msg("/budget 0.005"))
        await bot.handle_update(msg("Вопрос с маленьким бюджетом?"))
        await bot.handle_update(press("go", bot.pending[CHAT].card_id))
        await finish(bot)
        paused = fake.last("sendMessage")
        assert "приостановлено" in paused["text"] and "лимит бюджета" in paused["text"]
        plus = next(b for b in buttons(paused) if b.endswith(":+1"))
        assert any(d.endswith(":del") for d in buttons(paused))
        await bot.handle_update(press(plus, 1))
        await finish(bot)

    asyncio.run(scenario())
    texts = fake.texts()
    assert any("Продолжаю заседание" in t for t in texts) and any("🏆 <b>Решение:" in t for t in texts)
    assert not list((tmp / "runs" / "paused").glob("*"))  # после завершения «паспорт» удалён
    data = json.loads(next((tmp / "runs").glob("2*.json")).read_text())
    assert data["request"]["budget"] > 0.005


def test_bot_reports_sessions_interrupted_by_restart(env):
    from council.checkpoint import Checkpoint
    bot, fake, tmp = env
    cfg = bot.build_config(bot.chat_settings(CHAT))
    Checkpoint.create(tmp / "runs", "20260101-000000-x", cfg=cfg, question="Прерванный вопрос", mode="quick", rounds=1,
                      criteria=None, options=None, context=None, attachments=[], history=[], followup_of="",
                      vision={}, request={}, origin="telegram", chat_id=CHAT, mock=True)

    async def scenario():
        await bot.notify_interrupted()
        note = fake.last("sendMessage")
        assert "прервано перезапуском" in note["text"]
        go = next(b for b in buttons(note) if b.endswith(":go"))
        await bot.handle_update(press(go, 1))
        await finish(bot)
        await bot.notify_interrupted()  # второй раз о том же не пишет

    asyncio.run(scenario())
    assert any("🏆 <b>Решение:" in t for t in fake.texts())
    assert sum("прервано перезапуском" in t for t in fake.texts()) == 1


def test_research_mode_in_bot(env):
    bot, fake, tmp = env

    async def scenario():
        await bot.handle_update(msg("Стоит ли открывать кофейню у вуза?"))
        card_id = bot.pending[CHAT].card_id
        await bot.handle_update(press("view:opts", card_id))
        assert "mode:research" in buttons(fake.last("editMessageText"))
        await bot.handle_update(press("mode:research", card_id))
        assert bot.settings[CHAT].mode == "research"
        await bot.handle_update(press("go", card_id))
        await finish(bot)

    asyncio.run(scenario())
    result = next(t for t in fake.texts() if "🏆 <b>Решение:" in t or "🔬" in t)
    assert "🔬 известно" in result and "проверено" in result
    data = json.loads(next((tmp / "runs").glob("2*.json")).read_text())
    assert data["mode"] == "research" and data["research"]["ledger"]["claims"]
