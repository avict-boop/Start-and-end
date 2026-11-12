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


def plain(text: str) -> str:
    """Текст для поиска цитаты: без регистра, кавычек, разных тире и лишних пробелов."""
    t = (text or "").lower().replace("ё", "е").replace("\u00a0", " ")
    t = re.sub(r"[‐‑‒–—―]", "-", t)
    t = re.sub(r"[«»\"“”„'’`*_|]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def pdf_text(data: bytes) -> str:
    """Текст PDF (первые PDF_PAGES страниц) — для поиска цитаты; не PDF или без текстового слоя — пусто."""
    import io
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for page in reader.pages[:PDF_PAGES]:
            parts.append(page.extract_text() or "")
        return plain(re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", "\n".join(parts)))
    except Exception:  # noqa: BLE001 — повреждённый или зашифрованный PDF
        return ""


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


async def _one(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore, quotes: list[str] | None = None
               ) -> dict[str, Any]:
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
                    is_pdf = "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf")
                    if quotes and out["status"] == "ok" and (is_pdf or "html" in ctype or "text/plain" in ctype):
                        limit = MAX_PDF if is_pdf else MAX_BODY
                        body = b""
                        async for chunk in r.aiter_bytes():
                            body += chunk
                            if len(body) >= limit:
                                break
                        if is_pdf:
                            text = await asyncio.to_thread(pdf_text, body)
                        else:
                            text = page_text(body.decode(r.encoding or "utf-8", errors="ignore"))
                        out["text_len"] = len(text)
                        out["pdf"] = is_pdf
                        out["quotes"] = {q: plain(q) in text for q in quotes if plain(q)}
                    return out
            return {"status": "error", "code": None, "error": "слишком много перенаправлений"}
        except httpx.HTTPError as e:
            return {"status": "error", "code": None, "error": type(e).__name__}
        except Exception as e:  # noqa: BLE001 — неожиданное у одной ссылки не должно ронять проверку
            return {"status": "error", "code": None, "error": type(e).__name__}


async def check_links(urls: list[str], *, timeout: float = 10, concurrency: int = 5,
                      quotes: dict[str, list[str]] | None = None) -> dict[str, dict[str, Any]]:
    """Проверяет ссылки параллельно (не больше concurrency сразу); у каждой — свой таймаут. quotes: ссылка ->
    цитаты, которые надо найти на странице."""
    todo = [u for u in dict.fromkeys(urls) if str(u).startswith(("http://", "https://"))]
    if not todo:
        return {}
    sem = asyncio.Semaphore(concurrency)

    async def one(client: httpx.AsyncClient, u: str) -> dict[str, Any]:
        try:  # медленный сайт, отдающий страницу по байту, не задержит проверку дольше LINK_DEADLINE
            return await asyncio.wait_for(_one(client, u, sem, (quotes or {}).get(u)), LINK_DEADLINE)
        except asyncio.TimeoutError:
            return {"status": "error", "code": None, "error": "слишком долго"}

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=HEADERS) as client:
        tasks = {u: asyncio.create_task(one(client, u)) for u in todo}
        done, pending = await asyncio.wait(tasks.values(), timeout=TOTAL_DEADLINE)
        for t in pending:
            t.cancel()
    out = {}
    for u, t in tasks.items():
        out[u] = t.result() if t in done and not t.cancelled() else {"status": "error", "code": None,
                                                                      "error": "проверка ссылок заняла слишком долго"}
    return out
