"""Браузер для «трудных» страниц: сайты на JavaScript (текст появляется после загрузки) и с защитой от программ,
которые не отдают страницу простому запросу. Chromium без окна через Playwright; один браузер на процесс,
не больше N вкладок сразу. Нет Playwright или Chromium — ничего не делает (страница остаётся «не открылась»).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

log = logging.getLogger("council.browser")

_state: dict[str, Any] = {"pw": None, "browser": None, "sem": None, "limit": 0, "broken": False, "loop": None,
                          "lock": None}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return not _state["broken"]


def _lock() -> asyncio.Lock:
    """Браузер и замок привязаны к циклу событий: в другом цикле (тесты, командная строка) — заводятся заново."""
    loop = asyncio.get_running_loop()
    if _state["loop"] is not loop:
        _state.update(loop=loop, lock=asyncio.Lock(), pw=None, browser=None, sem=None, limit=0)
    return _state["lock"]


async def _browser(limit: int) -> Any:
    async with _lock():
        if _state["browser"] is not None and _state["browser"].is_connected():
            if _state["limit"] != limit:
                _state["sem"], _state["limit"] = asyncio.Semaphore(limit), limit
            return _state["browser"]
        from playwright.async_api import async_playwright
        try:
            pw = _state["pw"] or await async_playwright().start()
            _state["pw"] = pw
            # свой Chromium (не тот, что ставит Playwright) — путь в CHROMIUM_PATH
            exe = os.environ.get("CHROMIUM_PATH") or None
            _state["browser"] = await pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"],
                                                         executable_path=exe)
        except Exception as e:  # noqa: BLE001 — нет Chromium: больше не пытаемся в этом процессе
            _state["broken"] = True
            log.warning("браузер не запустился: %r", e)
            return None
        _state["sem"], _state["limit"] = asyncio.Semaphore(limit), limit
        return _state["browser"]


async def render(url: str, *, timeout: float = 45, limit: int = 3) -> dict[str, Any]:
    """Открывает страницу в браузере и возвращает {"status", "code", "html"}. PDF и файлы браузер не открывает —
    их скачивает обычный запрос."""
    if not available():
        return {"status": "error", "code": None, "error": "браузера нет"}
    b = await _browser(limit)
    if b is None:
        return {"status": "error", "code": None, "error": "браузер не запустился"}
    async with _state["sem"]:
        ctx = await b.new_context(user_agent=UA, locale="en-US", java_script_enabled=True, accept_downloads=False)
        try:
            page = await ctx.new_page()
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:  # страницы на JavaScript дописывают текст после загрузки
                await page.wait_for_load_state("networkidle", timeout=min(15, timeout / 3) * 1000)
            except Exception:  # noqa: BLE001 — сеть не утихла (реклама, счётчики) — берём, что есть
                pass
            html = await page.content()
            code = resp.status if resp else None
            return {"status": "ok" if code is None or code < 400 else "blocked", "code": code, "html": html}
        except Exception as e:  # noqa: BLE001
            return {"status": "error", "code": None, "error": type(e).__name__}
        finally:
            await ctx.close()


async def close() -> None:
    async with _lock():
        try:
            if _state["browser"] is not None:
                await _state["browser"].close()
            if _state["pw"] is not None:
                await _state["pw"].stop()
        except Exception:  # noqa: BLE001
            pass
        _state["browser"] = _state["pw"] = None
