"""Минимальный асинхронный клиент Telegram Bot API (long polling) на httpx."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

log = logging.getLogger("council.telegram")
MAX_TEXT = 4096


class TelegramError(RuntimeError):
    def __init__(self, method: str, description: str, code: int | None = None) -> None:
        super().__init__(f"{method}: {description}")
        self.description = description
        self.code = code


class TelegramAPI:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://api.telegram.org",
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
    ) -> None:
        self.max_retries = max_retries
        self._http = httpx.AsyncClient(
            base_url=f"{base_url}/bot{token}",
            timeout=httpx.Timeout(70, connect=15),
            transport=transport,
        )

    async def call(self, method: str, files: dict[str, tuple[str, bytes, str]] | None = None, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(self.max_retries + 1):
            try:
                if files:
                    data = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
                            for k, v in params.items()}
                    resp = await self._http.post(f"/{method}", data=data, files=files)
                else:
                    resp = await self._http.post(f"/{method}", json=params)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise TelegramError(method, f"сетевая ошибка: {e!r}") from e
                await asyncio.sleep(2**attempt)
                continue
            try:
                body = resp.json()
            except ValueError:
                body = {"ok": False, "description": resp.text[:200]}
            if body.get("ok"):
                return body.get("result")
            code = body.get("error_code", resp.status_code)
            desc = body.get("description", "unknown error")
            retry_after = (body.get("parameters") or {}).get("retry_after")
            if (code == 429 or code >= 500) and attempt < self.max_retries:
                await asyncio.sleep(min(float(retry_after if retry_after is not None else 2**attempt), 30))
                continue
            raise TelegramError(method, desc, code)
        raise TelegramError(method, "исчерпаны попытки")

    # ------------------------------------------------------------ обёртки

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe")

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict[str, Any]]:
        return await self.call("getUpdates", offset=offset, timeout=timeout,
                               allowed_updates=["message", "callback_query"])

    async def send_message(self, chat_id: int, text: str, *, reply_markup: dict | None = None,
                           reply_to: int | None = None) -> dict[str, Any]:
        return await self.call("sendMessage", chat_id=chat_id, text=text[:MAX_TEXT], parse_mode="HTML",
                               disable_web_page_preview=True, reply_markup=reply_markup,
                               reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
                               if reply_to else None)

    async def edit_message(self, chat_id: int, message_id: int, text: str, *,
                           reply_markup: dict | None = None) -> None:
        try:
            await self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:MAX_TEXT],
                            parse_mode="HTML", disable_web_page_preview=True,
                            reply_markup=reply_markup or {"inline_keyboard": []})
        except TelegramError as e:
            if "message is not modified" not in e.description:
                raise

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        try:
            await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError as e:  # устаревший callback — не страшно
            log.debug("answerCallbackQuery: %s", e)

    async def send_document(self, chat_id: int, filename: str, content: bytes, caption: str | None = None) -> None:
        await self.call("sendDocument", files={"document": (filename, content, "text/markdown")},
                        chat_id=chat_id, caption=caption, parse_mode="HTML" if caption else None)

    async def send_typing(self, chat_id: int) -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action="typing")
        except TelegramError:
            pass

    async def set_commands(self, commands: list[tuple[str, str]]) -> None:
        await self.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])

    async def aclose(self) -> None:
        await self._http.aclose()
