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
    """Модель не ответила или ответила ошибкой. spent — сколько всё равно потрачено на попытки."""

    def __init__(self, message: str, *, spent: "Completion | None" = None) -> None:
        super().__init__(message)
        self.spent = spent


@dataclass
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    model: str = ""
    finish_reason: str | None = None  # "length" — ответ обрезан по лимиту токенов
    max_tokens: int = 0  # с каким лимитом получен ответ (после возможных увеличений)


class LLMClient(Protocol):
    async def complete(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None = None,
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
        max_tokens_cap: int = 32000,
    ) -> None:
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.max_tokens_cap = max_tokens_cap
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
        reasoning_effort: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Completion:
        """Запрос к модели.

        «Думающие» модели (gpt-5, o-серия, Gemini 2.5, DeepSeek R1…) сначала тратят токены на скрытые
        рассуждения. Если лимит кончился раньше ответа, ответ приходит пустым — тогда следующая попытка
        идёт с удвоенным лимитом (до max_tokens_cap). Стоимость всех попыток суммируется.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "usage": {"include": True},  # OpenRouter вернёт фактическую стоимость запроса
        }
        if reasoning_effort:
            # Единый параметр OpenRouter для глубины рассуждений; модели без рассуждений его игнорируют.
            payload["reasoning"] = {"effort": reasoning_effort}
        spent = Completion(text="")  # расходы всех попыток, включая неудачные
        last_error = "неизвестная ошибка"
        bumped = False
        for attempt in range(self.max_retries + 1):
            if attempt and not bumped:
                await asyncio.sleep(self.retry_base_delay * (min(2**attempt, 30) + random.random()))
            bumped = False
            try:
                resp = await self._http.post("/chat/completions", json=payload)
            except httpx.TransportError as e:
                last_error = f"сетевая ошибка: {e!r}"
                continue
            if resp.status_code in RETRY_STATUSES:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                continue
            if resp.status_code >= 400:
                raise ProviderError(f"{model}: HTTP {resp.status_code}: {resp.text[:500]}", spent=spent)
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
                raise ProviderError(f"{model}: {last_error}", spent=spent)
            usage = data.get("usage") or {}
            spent.prompt_tokens += int(usage.get("prompt_tokens") or 0)
            spent.completion_tokens += int(usage.get("completion_tokens") or 0)
            spent.cost += float(usage.get("cost") or 0.0)
            choices = data.get("choices") or []
            if not choices:
                last_error = "пустой список choices"
                continue
            choice = choices[0]
            finish = choice.get("finish_reason") or choice.get("native_finish_reason")
            text = (choice.get("message") or {}).get("content") or ""
            if not text.strip():
                limit = payload["max_tokens"]
                if finish in ("length", "max_tokens", "MAX_TOKENS") and limit < self.max_tokens_cap:
                    last_error = f"пустой ответ: модель израсходовала лимит {limit} токенов на рассуждения"
                    payload["max_tokens"] = min(limit * 2, self.max_tokens_cap)
                    bumped = True
                else:
                    last_error = f"пустой ответ модели (finish_reason={finish}, лимит {limit} токенов)"
                continue
            return Completion(
                text=text,
                prompt_tokens=spent.prompt_tokens,
                completion_tokens=spent.completion_tokens,
                cost=spent.cost,
                model=data.get("model") or model,
                finish_reason=finish,
                max_tokens=payload["max_tokens"],
            )
        raise ProviderError(
            f"{model}: не удалось получить ответ после {self.max_retries + 1} попыток: {last_error}",
            spent=spent,
        )

    async def list_models(self) -> list[dict[str, Any]]:
        resp = await self._http.get("/models")
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def aclose(self) -> None:
        await self._http.aclose()
