"""Клиент OpenRouter (OpenAI-совместимый API) с ретраями и учётом стоимости."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


class ProviderError(RuntimeError):
    """Модель не ответила или ответила ошибкой."""


@dataclass
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    model: str = ""


class LLMClient(Protocol):
    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        meta: dict[str, Any] | None = None,
    ) -> Completion: ...

    async def aclose(self) -> None: ...


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 180,
        max_retries: int = 3,
        base_url: str = OPENROUTER_BASE,
        app_name: str = "AI Council",
        transport: httpx.AsyncBaseTransport | None = None,
        retry_base_delay: float = 1.0,
    ) -> None:
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self._http = httpx.AsyncClient(
            transport=transport,
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=20),
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Title": app_name,
            },
        )

    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        meta: dict[str, Any] | None = None,
    ) -> Completion:
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "usage": {"include": True},  # OpenRouter вернёт фактическую стоимость запроса
        }
        last_error = "неизвестная ошибка"
        for attempt in range(self.max_retries + 1):
            if attempt:
                await asyncio.sleep(self.retry_base_delay * (min(2**attempt, 30) + random.random()))
            try:
                resp = await self._http.post("/chat/completions", json=payload)
            except httpx.TransportError as e:
                last_error = f"сетевая ошибка: {e!r}"
                continue
            if resp.status_code in RETRY_STATUSES:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                continue
            if resp.status_code >= 400:
                raise ProviderError(f"{model}: HTTP {resp.status_code}: {resp.text[:500]}")
            try:
                data = resp.json()
            except ValueError:
                last_error = f"ответ не JSON: {resp.text[:300]}"
                continue
            if data.get("error"):
                err = data["error"]
                code = err.get("code") if isinstance(err, dict) else None
                last_error = f"ошибка провайдера: {err}"
                if code in RETRY_STATUSES:
                    continue
                raise ProviderError(f"{model}: {last_error}")
            choices = data.get("choices") or []
            if not choices:
                last_error = "пустой список choices"
                continue
            text = (choices[0].get("message") or {}).get("content") or ""
            if not text.strip():
                last_error = "пустой ответ модели (возможно, не хватило max_tokens на рассуждения)"
                continue
            usage = data.get("usage") or {}
            return Completion(
                text=text,
                prompt_tokens=int(usage.get("prompt_tokens") or 0),
                completion_tokens=int(usage.get("completion_tokens") or 0),
                cost=float(usage.get("cost") or 0.0),
                model=data.get("model") or model,
            )
        raise ProviderError(f"{model}: не удалось получить ответ после {self.max_retries + 1} попыток: {last_error}")

    async def list_models(self) -> list[dict[str, Any]]:
        resp = await self._http.get("/models")
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def aclose(self) -> None:
        await self._http.aclose()
