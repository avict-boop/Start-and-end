"""Проверка ссылок реестра источников: открывается ли страница. Бесплатно — модели не нужны.

Статусы:
  ok       — страница открылась (код 2xx/3xx);
  dead     — страницы нет (404, 410) — на неё нельзя опираться;
  blocked  — сайт не пускает автоматическую проверку (401, 403, 429…) — сама ссылка может быть верной;
  error    — не удалось соединиться (таймаут, нет такого сайта, ошибка сертификата и т. п.);
  refused  — адрес внутри сети сервера (localhost, 10.x, 192.168.x…) — не открываем: ссылки приходят из поиска
             и из ответов моделей, это защита от запросов к внутренним сервисам.

Открывается только начало ответа (тело не читается); перенаправлений — не больше MAX_REDIRECTS, каждое
проверяется на внутренний адрес заново. «Открывается» значит доступна, а не «подтверждает утверждение».
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

DEAD = {404, 410}
BLOCKED = {401, 403, 405, 406, 429, 451, 503}
MAX_REDIRECTS = 5
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


async def _one(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        try:
            for _ in range(MAX_REDIRECTS + 1):
                if await is_internal(url):
                    return {"status": "refused", "code": None}
                async with client.stream("GET", url) as r:  # тело не читаем — достаточно ответа сервера
                    if r.is_redirect and r.headers.get("location"):
                        url = urljoin(str(r.url), r.headers["location"])
                        continue
                    return {"status": classify_code(r.status_code), "code": r.status_code}
            return {"status": "error", "code": None, "error": "слишком много перенаправлений"}
        except httpx.HTTPError as e:
            return {"status": "error", "code": None, "error": type(e).__name__}
        except Exception as e:  # noqa: BLE001 — неожиданное у одной ссылки не должно ронять проверку
            return {"status": "error", "code": None, "error": type(e).__name__}


async def check_links(urls: list[str], *, timeout: float = 10, concurrency: int = 5) -> dict[str, dict[str, Any]]:
    """Проверяет ссылки параллельно (не больше concurrency сразу); у каждой — свой таймаут."""
    todo = [u for u in dict.fromkeys(urls) if str(u).startswith(("http://", "https://"))]
    if not todo:
        return {}
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=HEADERS) as client:
        res = await asyncio.gather(*(_one(client, u, sem) for u in todo))
    return dict(zip(todo, res))
