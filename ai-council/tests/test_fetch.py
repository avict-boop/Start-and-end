"""Чтение страниц и файлов на мощном сервере: распознавание сканов (Tesseract), PDF целиком, браузер для страниц,
которые не отдают текст простому запросу. Без программ на сервере — всё работает как раньше, без них."""
import asyncio
import io
import shutil

import pytest

from council import links, ocr


@pytest.fixture(autouse=True)
def opts():
    saved = dict(links.OPTIONS)
    yield links.OPTIONS
    links.OPTIONS.clear()
    links.OPTIONS.update(saved)


def _scan_pdf(lines):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1700, 140 + 120 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 48)
    except OSError:
        f = ImageFont.load_default()
    for i, t in enumerate(lines):
        d.text((60, 60 + 120 * i), t, fill="black", font=f)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=200)
    return buf.getvalue()


@pytest.mark.skipif(not (shutil.which("tesseract") and shutil.which("pdftoppm")), reason="нет Tesseract или poppler")
def test_scanned_pdf_is_recognized_when_ocr_on(opts):
    pdf = _scan_pdf(["GMP certificate valid until 2027-03-15"])
    opts["ocr"] = False
    assert links.pdf_text(pdf) == ""          # без распознавания скан пуст, как раньше
    opts["ocr"] = True
    assert "valid until 2027-03-15" in links.pdf_text(pdf)


def test_without_tesseract_nothing_breaks(monkeypatch, opts):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    opts["ocr"] = True
    assert ocr.pdf_text(b"%PDF-1.4") == "" and ocr.image_text(b"x") == ""
    assert links.pdf_text(b"not a pdf") == ""


def test_looks_scanned():
    assert ocr.looks_scanned("", 3) and ocr.looks_scanned("x" * 150, 3)
    assert not ocr.looks_scanned("текст " * 200, 3)


def test_configure_from_settings(opts):
    from council.config import FetchConfig
    links.configure(FetchConfig(pdf_pages=500, ocr=False, browser=False))
    assert opts["pdf_pages"] == 500 and opts["ocr"] is False and opts["browser"] is False


def test_browser_fallback_for_blocked_and_js_pages(monkeypatch, opts):
    """403 у простого запроса или страница без текста (JavaScript) — браузер; мёртвая ссылка — нет."""
    from council import browser
    opts["browser"] = True
    calls = []

    async def fake_one(client, url, sem, quotes=None, want_text=False):
        return {"/blocked": {"status": "blocked", "code": 403},
                "/js": {"status": "ok", "code": 200, "ctype": "text/html", "text_len": 12, "text": "loading…"},
                "/dead": {"status": "dead", "code": 404},
                "/pdf": {"status": "ok", "code": 200, "ctype": "application/pdf", "pdf": True, "text_len": 0}}[
            "/" + url.rsplit("/", 1)[-1]]

    async def fake_render(url, timeout=45, limit=3):
        calls.append(url)
        return {"status": "ok", "code": 200, "html": "<html><body><p>Annual capacity 25亿粒 softgels</p></body></html>"}

    async def not_internal(url):
        return False

    monkeypatch.setattr(links, "_one", fake_one)
    monkeypatch.setattr(links, "is_internal", not_internal)
    monkeypatch.setattr(browser, "render", fake_render)
    urls = [f"https://x.example/{p}" for p in ("blocked", "js", "dead", "pdf")]
    got = asyncio.run(links.check_links(urls, texts=True))
    assert got[urls[0]]["via"] == "browser" and "25亿粒" in got[urls[0]]["text"]
    assert got[urls[1]]["via"] == "browser"
    assert got[urls[2]]["status"] == "dead" and got[urls[3]].get("via") is None
    assert sorted(calls) == sorted(urls[:2])
    opts["browser"] = False
    calls.clear()
    got = asyncio.run(links.check_links(urls, texts=True))
    assert not calls and got[urls[0]]["status"] == "blocked"
