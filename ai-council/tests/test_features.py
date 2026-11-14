"""Уточнение, повтор, голос, статистика, оформление отчётов."""
import asyncio
import io
import json
import math

import pytest
import yaml
from fastapi.testclient import TestClient

from council.archive import Archive
from council.attachments import assign_files, load_attachment
from council.config import BrandingConfig, CouncilConfig
from council.mock import MockClient
from council.report import save
from council.reports import brand
from council.session import Council


def cfg3():
    return CouncilConfig.model_validate({"chair": "a", "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]})


class Recorder(MockClient):
    def __init__(self):
        super().__init__(delay=0)
        self.msgs = []

    async def complete(self, model, messages, **kw):
        self.msgs.append(((kw.get("meta") or {}).get("stage"), json.dumps(messages[-1]["content"], ensure_ascii=False)))
        return await super().complete(model, messages, **kw)


def audio(fmt: str, codec: str, seconds: float = 2.0) -> bytes:
    import av
    import numpy as np
    buf = io.BytesIO()
    with av.open(buf, "w", format=fmt) as out:
        st = out.add_stream(codec, rate=48000)
        st.layout = "mono"
        t = np.arange(int(48000 * seconds)) / 48000
        samples = (np.sin(2 * math.pi * 300 * t) * 6000).astype("int16")
        for i in range(0, len(samples), 960):
            chunk = samples[i:i + 960]
            if len(chunk) < 960:
                break
            fr = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
            fr.rate = 48000
            for p in st.encode(fr):
                out.mux(p)
        for p in st.encode(None):
            out.mux(p)
    return buf.getvalue()


# ------------------------------------------------------------------ уточнение и повтор

def test_followup_chain_and_inherited_files(tmp_path):
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (300, 200), "red").save(b, "PNG")
    att = load_attachment(b.getvalue(), "схема.png")
    assign_files([att])
    first = asyncio.run(Council(cfg3(), MockClient(delay=0)).run("Как назвать кофейню?", mode="quick", criteria=["x"],
                                                                 attachments=[att]))
    save(first, tmp_path, "20260101-000001-first", [att])
    arch = Archive(tmp_path)
    thread, atts = arch.followup("20260101-000001-first")
    assert [t["question"] for t in thread] == ["Как назвать кофейню?"] and "Итог" in thread[0]["answer"]
    assert [a.name for a in atts] == ["схема.png"]
    rec = Recorder()
    second = asyncio.run(Council(cfg3(), rec).run("А если дешевле?", mode="quick", criteria=["x"], attachments=atts,
                                                  history=thread, followup_of="20260101-000001-first"))
    proposal = next(c for st, c in rec.msgs if st == "proposal")
    assert "FOLLOW-UP" in proposal and "Как назвать кофейню?" in proposal and "image_url" in proposal
    vote = next(c for st, c in rec.msgs if st == "vote")
    assert "FOLLOW-UP" not in vote
    d = second.to_dict()
    assert d["followup_of"] == "20260101-000001-first" and len(d["thread"]) == 1
    save(second, tmp_path, "20260101-000002-second", atts)
    # цепочка растёт, но хранится не больше 4 последних шагов
    stem = "20260101-000002-second"
    for i in range(3, 8):
        th, a2 = arch.followup(stem)
        r = asyncio.run(Council(cfg3(), MockClient(delay=0)).run(f"Шаг {i}?", mode="quick", criteria=["x"],
                                                                 history=th, followup_of=stem))
        stem = f"20260101-00000{i}-step"
        save(r, tmp_path, stem)
    assert len(arch.followup(stem)[0]) == Archive.MAX_THREAD
    md = arch.report("20260101-000002-second", "md").read_text(encoding="utf-8")
    assert "Уточнение к заседанию:** Как назвать кофейню?" in md


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = {"chair": "a", "runs_dir": str(tmp_path / "runs"),
           "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    from council.web.app import create_app
    with TestClient(create_app(tmp_path / "config.yaml", mock_delay=0)) as c:
        yield c


def wait_done(c, sid):
    with c.stream("GET", f"/api/sessions/{sid}/events") as r:
        return [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]


def test_web_followup_repeat_transcribe_stats(web):
    up = web.post("/api/uploads?name=заметки.txt", content="Бюджет 100 тыс".encode()).json()
    first = web.post("/api/sessions", json={"question": "Как назвать кофейню?", "mock": True, "mode": "quick",
                                            "criteria": ["звучание"], "members": ["a", "b"], "budget": 0.5,
                                            "attachments": [up["id"]]}).json()["id"]
    wait_done(web, first)
    # уточнение
    sid = web.post("/api/sessions", json={"question": "А если дешевле?", "mock": True, "mode": "quick",
                                          "parent": first}).json()["id"]
    ev = wait_done(web, sid)
    assert ev[0]["parent"]["id"] == first and ev[0]["parent"]["title"] == "Как назвать кофейню?"
    assert any(e["type"] == "followup" for e in ev) and any(e["type"] == "attachments" for e in ev)
    assert web.get("/api/sessions").json()["items"][0]["followup_of"] == first
    replay = wait_done(web, sid)  # повторное открытие из архива
    assert replay[0]["parent"]["id"] == first
    assert web.post("/api/sessions", json={"question": "Вопрос?", "mock": True, "parent": "nope"}).status_code == 400
    # повтор: исходные настройки и вложения
    r = web.post(f"/api/sessions/{first}/repeat").json()
    assert r["request"]["question"] == "Как назвать кофейню?" and r["request"]["members"] == ["a", "b"]
    assert r["request"]["criteria"] == ["звучание"] and r["request"]["budget"] == 0.5 and r["request"]["mode"] == "quick"
    assert [u["name"] for u in r["uploads"]] == ["заметки.txt"] and len(r["uploads"][0]["id"]) == 16
    # голос
    t = web.post("/api/transcribe?mock=1", content=audio("webm", "libopus")).json()
    assert "демо-расшифровка" in t["text"] and 1.5 < t["seconds"] < 2.5
    assert web.post("/api/transcribe", content=audio("ogg", "libopus")).status_code == 400  # без ключа — понятный отказ
    assert web.post("/api/transcribe?mock=1", content=b"garbage").status_code == 400
    # статистика
    s = web.get("/api/stats").json()
    assert s["sessions"] == 2 and s["followups"] == 1 and s["with_files"] == 2
    assert {m["name"] for m in s["models"]} >= {"a", "b"} and sum(m["wins"] for m in s["models"]) == 2


def test_transcribe_formats():
    from council.transcribe import TranscribeError, to_wav
    for fmt, codec in (("ogg", "libopus"), ("webm", "libopus"), ("mp4", "aac"), ("mp3", "mp3")):
        wav, sec = to_wav(audio(fmt, codec, 1.5))
        assert wav[:4] == b"RIFF" and 1.2 < sec < 1.8, fmt
    with pytest.raises(TranscribeError):
        to_wav(audio("ogg", "libopus", 0.1))


# ------------------------------------------------------------------ оформление и оглавление

def test_branding_and_toc(tmp_path):
    from docx import Document
    from PIL import Image

    from council.reports.docx import render_docx
    from council.reports.html import render_html
    from council.reports.view import View
    (tmp_path / "logo.png").write_bytes(b"")
    Image.new("RGB", (100, 80), "green").save(tmp_path / "logo.png")  # знак: название пишется рядом
    try:
        brand.set_brand(BrandingConfig(title="ООО «Ромашка»", logo="logo.png", color="#0a7d5a", footer="Конфиденциально"),
                        tmp_path)
        res = asyncio.run(Council(cfg3(), MockClient(delay=0)).run("Вопрос?", mode="quick", criteria=["x"]))
        v = View(res.to_dict())
        html = render_html(v)
        assert '<span class="brand-name">ООО «Ромашка»</span>' in html and "Отчёт о заседании" in html
        assert '<img src="data:image/png' in html  # квадратный логотип — рядом с названием
        assert "#0a7d5a" in html and "#5b5bd6" not in html and "target-counter(attr(href), page)" in html
        assert 'content: "ООО «Ромашка» · Конфиденциально"' in html and "Содержание" in html
        doc = Document(io.BytesIO(render_docx(v)))
        assert "ООО «Ромашка»" in doc.sections[0].header.paragraphs[0].text
        # широкий логотип с надписью: название рядом не повторяется (иначе «ООО Ромашка ООО Ромашка»)
        Image.new("RGB", (400, 80), "green").save(tmp_path / "logo.png")
        brand.set_brand(BrandingConfig(title="ООО «Ромашка»", logo="logo.png"), tmp_path)
        html = render_html(v)
        assert 'class="wordmark"' in html and '<span class="brand-name">' not in html
        assert "Конфиденциально" in doc.sections[0].footer.paragraphs[0].text
        body = doc.element.body.xml
        assert 'TOC \\o "1-2"' in body and "Голосование" in body
        # оглавление — поле Word TOC (номера страниц Word проставит по F9); без вопроса «обновить поля?» при открытии
        body = doc.element.body.xml
        assert 'TOC \\o "1-2"' in body and "updateFields" not in doc.settings.element.xml
        # битый путь к логотипу не ломает отчёты
        brand.set_brand(BrandingConfig(logo="нет-такого.png", color="zzz"), tmp_path)
        assert brand.current().logo_png == b"" and brand.current().color == brand.DEFAULT_COLOR
        render_docx(v)
    finally:
        brand.set_brand(None, tmp_path)


def test_web_fact_check_and_chair_stats(web):
    cfg = web.get("/api/config").json()
    assert cfg["fact_check"]["enabled"] is False and cfg["fact_check"]["model"] == "perplexity/sonar-pro"
    sid = web.post("/api/sessions", json={"question": "Сколько стоит кофейня?", "mock": True, "mode": "quick",
                                          "fact_check": True}).json()["id"]
    ev = wait_done(web, sid)
    fc = next(e for e in ev if e["type"] == "factcheck")
    assert len(fc["results"]) == 2 and fc["model"] == "perplexity/sonar-pro"
    replay = wait_done(web, sid)  # из архива — те же результаты проверки
    assert next(e for e in replay if e["type"] == "factcheck")["results"] == fc["results"]
    assert "Проверка фактов" in web.get(f"/api/sessions/{sid}/report?format=md").text
    assert web.post(f"/api/sessions/{sid}/repeat").json()["request"]["fact_check"] is True
    s = web.get("/api/stats").json()
    assert s["fact_check"]["sessions"] == 1 and s["fact_check"]["refuted"] == 2
    assert "факт-чек" not in {m["name"] for m in s["models"]}
    assert s["chairs"][0]["name"] == "a" and s["chairs"][0]["decisions"] == 1
