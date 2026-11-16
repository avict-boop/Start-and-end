"""Проверка ссылок реестра источников: открывается ли страница. Бесплатно — модели не нужны.

Статусы:
  ok       — страница открылась (код 2xx/3xx);
  dead     — страницы нет (404, 410) — на неё нельзя опираться;
  blocked  — сайт не пускает автоматическую проверку (401, 403, 429…) — сама ссылка может быть верной;
  error    — не удалось соединиться (таймаут, нет такого сайта, ошибка сертификата и т. п.);
  refused  — адрес внутри сети сервера (localhost, 10.x, 192.168.x…) — не открываем: ссылки приходят из поиска
             и из ответов моделей, это защита от запросов к внутренним сервисам.

Открывается только начало ответа; тело читается, лишь когда нужно найти на странице цитату, которую привёл
поиск (до MAX_BODY байт, только HTML и текст). Перенаправлений — не больше MAX_REDIRECTS, каждое проверяется на
внутренний адрес заново. «Открывается» значит доступна, а не «подтверждает утверждение».
"""
from __future__ import annotations

import asyncio
import html as htmllib
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

DEAD = {404, 410}
BLOCKED = {401, 403, 405, 406, 429, 451, 503}
MAX_REDIRECTS = 5
MAX_BODY = 1_500_000
MAX_PDF = 8_000_000      # PDF (инструкции FDA, EMA, PMDA) — до 8 МБ и первых PDF_PAGES страниц
PDF_PAGES = 40
LINK_DEADLINE = 30.0     # одна ссылка вместе с чтением страницы — не дольше (медленный сайт не держит заседание)
TOTAL_DEADLINE = 150.0   # вся проверка ссылок этапа — не дольше; не успевшие — «не открылась»
MAX_IMAGE = 15_000_000   # картинка (скан сертификата) — до 15 МБ

# Как читать страницы и файлы — из настроек (config.fetch; ставит сайт, бот и командная строка через configure()).
# Без настройки — как раньше на слабом сервере: 40 страниц PDF, без распознавания и без браузера.
OPTIONS: dict = {"pdf_pages": PDF_PAGES, "pdf_mb": MAX_PDF // 1_000_000, "ocr": False, "ocr_langs": "auto",
                 "ocr_pages": 20, "ocr_parallel": 4, "browser": False, "browser_pages": 3, "browser_timeout": 45,
                 "link_deadline": LINK_DEADLINE, "total_deadline": TOTAL_DEADLINE}


def configure(fetch: object | None) -> None:
    """Настройки чтения страниц из config.fetch (FetchConfig)."""
    if fetch is None:
        return
    for k in OPTIONS:
        v = getattr(fetch, k, None)
        if v is not None:
            OPTIONS[k] = v


def plain(text: str) -> str:
    """Текст для поиска цитаты: без регистра, кавычек, разных тире и лишних пробелов."""
    t = (text or "").lower().replace("ё", "е").replace("\u00a0", " ")
    t = re.sub(r"[‐‑‒–—―]", "-", t)
    t = re.sub(r"[«»\"“”„'’`*_|]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def pdf_text(data: bytes) -> str:
    """Текст PDF (первые pdf_pages страниц) — для поиска цитаты. Текстового слоя нет (скан) и распознавание
    включено — страницы распознаются (Tesseract). Не PDF — пусто."""
    import io
    n = 0
    raw = ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages[:int(OPTIONS["pdf_pages"])]
        n = len(pages)
        raw = "\n".join(page.extract_text() or "" for page in pages)
    except Exception:  # noqa: BLE001 — повреждённый или зашифрованный PDF: распознавание может помочь
        raw = ""
    from . import ocr
    if OPTIONS["ocr"] and ocr.looks_scanned(raw, n or 1):
        scanned = ocr.pdf_text(data, langs=OPTIONS["ocr_langs"], pages=int(OPTIONS["ocr_pages"]),
                               parallel=int(OPTIONS["ocr_parallel"]))
        if len(scanned.strip()) > len(raw.strip()):
            raw = scanned
    return plain(re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", raw))


def image_text(data: bytes, ctype: str = "") -> str:
    """Текст картинки (скан сертификата на странице) — если распознавание включено."""
    if not OPTIONS["ocr"]:
        return ""
    from . import ocr
    ext = ".jpg" if "jpeg" in ctype or "jpg" in ctype else ".webp" if "webp" in ctype else ".png"
    return plain(ocr.image_text(data, OPTIONS["ocr_langs"], ext))


def page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    return plain(htmllib.unescape(raw))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; link-check)", "Accept": "text/html,application/pdf,*/*"}


def classify_code(code: int) -> str:
    if code < 400:
        return "ok"
    if code in DEAD:
        return "dead"
    if code in BLOCKED:
        return "blocked"
    return "error"


def _bad_ip(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast or a.is_unspecified


async def is_internal(url: str) -> bool:
    """Адрес ведёт внутрь сети сервера (или его не разобрать) — такую ссылку не открываем."""
    host = urlsplit(url).hostname
    if not host:
        return True
    if host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal", ".localhost")):
        return True
    try:
        return _bad_ip(host)
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False  # имя не разрешается — запрос сам вернёт ошибку
    return any(_bad_ip(i[4][0]) for i in infos)


MAX_TEXT = 300_000  # сколько текста страницы отдаётся программе для отрывков (texts=True)


async def _one(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore, quotes: list[str] | None = None,
               want_text: bool = False) -> dict[str, Any]:
    async with sem:
        try:
            for _ in range(MAX_REDIRECTS + 1):
                if await is_internal(url):
                    return {"status": "refused", "code": None}
                async with client.stream("GET", url) as r:  # тело — только если нужно найти цитату
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(str(r.url), r.headers["location"])
                        continue
                    out: dict[str, Any] = {"status": classify_code(r.status_code), "code": r.status_code}
                    ctype = r.headers.get("content-type", "").lower()
                    out["ctype"] = ctype.split(";")[0]
                    is_pdf = "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf")
                    is_img = ctype.startswith("image/") and OPTIONS["ocr"]
                    if (quotes or want_text) and out["status"] == "ok" and (is_pdf or is_img or "html" in ctype
                                                                         or "text/plain" in ctype):
                        limit = (int(OPTIONS["pdf_mb"]) * 1_000_000 if is_pdf else MAX_IMAGE if is_img
                                 else MAX_BODY)
                        body = b""
                        async for chunk in r.aiter_bytes():
                            body += chunk
                            if len(body) >= limit:
                                break
                        if is_pdf:
                            text = await asyncio.to_thread(pdf_text, body)
                        elif is_img:
                            text = await asyncio.to_thread(image_text, body, ctype)
                        else:
                            text = page_text(body.decode(r.encoding or "utf-8", errors="ignore"))
                        out["text_len"] = len(text)
                        out["pdf"] = is_pdf
                        if quotes:
                            out["quotes"] = {q: plain(q) in text for q in quotes if plain(q)}
                        if want_text:
                            out["text"] = text[:MAX_TEXT]
                    return out
            return {"status": "error", "code": None, "error": "слишком много перенаправлений"}
        except httpx.HTTPError as e:
            return {"status": "error", "code": None, "error": type(e).__name__}
        except Exception as e:  # noqa: BLE001 — неожиданное у одной ссылки не должно ронять проверку
            return {"status": "error", "code": None, "error": type(e).__name__}


def _needs_browser(r: dict[str, Any]) -> bool:
    """Простой запрос не дал текста: сайт не пускает программы (403, 429…), не ответил, или страница почти пустая
    (текст дописывает JavaScript). Мёртвые ссылки и внутренние адреса браузер не открывает."""
    if not OPTIONS["browser"] or r.get("status") in ("dead", "refused") or r.get("pdf"):
        return False
    if r.get("status") in ("blocked", "error"):
        return True
    # открылась, но текста почти нет: страница на JavaScript (только HTML — не файлы и не картинки)
    return r.get("status") == "ok" and "html" in str(r.get("ctype") or "") and int(r.get("text_len") or 0) < 400


async def _via_browser(url: str, r: dict[str, Any], quotes: list[str] | None) -> dict[str, Any]:
    from . import browser
    if await is_internal(url):
        return r
    got = await browser.render(url, timeout=float(OPTIONS["browser_timeout"]), limit=int(OPTIONS["browser_pages"]))
    if got.get("status") != "ok" or not got.get("html"):
        return r
    text = page_text(got["html"])
    if len(text) <= int(r.get("text_len") or 0):
        return r
    out = {"status": "ok", "code": got.get("code"), "text_len": len(text), "pdf": False, "via": "browser",
           "text": text[:MAX_TEXT]}
    if quotes:
        out["quotes"] = {q: plain(q) in text for q in quotes if plain(q)}
    return out


async def check_links(urls: list[str], *, timeout: float = 10, concurrency: int = 5,
                      quotes: dict[str, list[str]] | None = None, texts: bool = False,
                      browser: bool = True) -> dict[str, dict[str, Any]]:
    """Проверяет ссылки параллельно (не больше concurrency сразу); у каждой — свой таймаут. quotes: ссылка ->
    цитаты, которые надо найти на странице. texts=True — вернуть и текст страницы (HTML или PDF) в "text"."""
    todo = [u for u in dict.fromkeys(urls) if str(u).startswith(("http://", "https://"))]
    if not todo:
        return {}
    sem = asyncio.Semaphore(concurrency)

    # чтение текста (PDF на сотни страниц, сканы, браузер) — дольше, чем «открывается ли»
    reading = bool(texts or quotes)
    link_deadline = float(OPTIONS["link_deadline"]) if reading else LINK_DEADLINE
    total_deadline = float(OPTIONS["total_deadline"]) if reading else TOTAL_DEADLINE

    async def one(client: httpx.AsyncClient, u: str) -> dict[str, Any]:
        try:  # медленный сайт, отдающий страницу по байту, не задержит проверку дольше link_deadline
            r = await asyncio.wait_for(_one(client, u, sem, (quotes or {}).get(u), **({"want_text": True} if texts else {})),
                                       link_deadline)
        except asyncio.TimeoutError:
            r = {"status": "error", "code": None, "error": "слишком долго"}
        if texts and browser and _needs_browser(r):  # browser=False — быстрый проход (отсев): без браузера
            r = await _via_browser(u, r, (quotes or {}).get(u))
        return r

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=HEADERS) as client:
        tasks = {u: asyncio.create_task(one(client, u)) for u in todo}
        done, pending = await asyncio.wait(tasks.values(), timeout=total_deadline)
        for t in pending:
            t.cancel()
    out = {}
    for u, t in tasks.items():
        out[u] = t.result() if t in done and not t.cancelled() else {"status": "error", "code": None,
                                                                      "error": "проверка ссылок заняла слишком долго"}
    return out
