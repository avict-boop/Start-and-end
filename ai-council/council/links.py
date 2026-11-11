"""Проверка ссылок реестра источников: открывается ли страница. Бесплатно — модели не нужны.

Статусы:
  ok       — страница открылась (код 2xx/3xx);
  dead     — страницы нет (404, 410) — на неё нельзя опираться;
  blocked  — сайт не пускает автоматическую проверку (401, 403, 429…) — сама ссылка может быть верной;
  error    — не удалось соединиться (таймаут, нет такого сайта, ошибка сертификата и т. п.).
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

DEAD = {404, 410}
BLOCKED = {401, 403, 405, 406, 429, 451, 503}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; link-check)", "Accept": "text/html,application/pdf,*/*"}


def classify_code(code: int) -> str:
    if code < 400:
        return "ok"
    if code in DEAD:
        return "dead"
    if code in BLOCKED:
        return "blocked"
    return "error"


async def _one(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore) -> dict[str, Any]:
    async with sem:
        try:
            async with client.stream("GET", url) as r:  # тело не читаем — достаточно ответа сервера
                return {"status": classify_code(r.status_code), "code": r.status_code}
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
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=HEADERS) as client:
        res = await asyncio.gather(*(_one(client, u, sem) for u in todo))
    return dict(zip(todo, res))
