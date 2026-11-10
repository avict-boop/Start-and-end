"""Клиент OpenRouter (OpenAI-совместимый API) с ретраями и учётом стоимости."""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import httpx

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
ProgressFn = Callable[[dict[str, Any]], None]


class _Retryable(Exception):
    """Временная ошибка: стоит повторить запрос."""


def _http_reason(code: int, body: str) -> str:
    human = {429: "провайдер ограничил частоту запросов", 408: "истекло время ожидания",
             502: "провайдер модели недоступен", 503: "провайдер модели перегружен",
             504: "провайдер модели не ответил вовремя"}.get(code, "временная ошибка сервера")
    return f"{human} (HTTP {code})"


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
        on_progress: ProgressFn | None = None,
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
        stream: bool = True,
    ) -> None:
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.max_tokens_cap = max_tokens_cap
        self.stream = stream
        self.timeout = timeout
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
        on_progress: ProgressFn | None = None,
    ) -> Completion:
        """Запрос к модели с потоковой передачей ответа.

        Поток нужен, чтобы видеть, что модель жива: «думает» (ответа ещё нет), «пишет» (идут токены).
        «Думающие» модели (gpt-5, o-серия, Gemini 2.5, DeepSeek R1…) сначала тратят токены на скрытые
        рассуждения. Если лимит кончился раньше ответа, ответ приходит пустым — тогда следующая попытка
        идёт с удвоенным лимитом (до max_tokens_cap). Стоимость всех попыток суммируется.
        """
        report = on_progress or (lambda ev: None)
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "usage": {"include": True},  # OpenRouter вернёт фактическую стоимость запроса
            "stream": self.stream,
        }
        if reasoning_effort:
            # Единый параметр OpenRouter для глубины рассуждений; модели без рассуждений его игнорируют.
            payload["reasoning"] = {"effort": reasoning_effort}
        spent = Completion(text="")  # расходы всех попыток, включая неудачные
        last_error = "неизвестная ошибка"
        wait = 0.0
        for attempt in range(1, self.max_retries + 2):
            if attempt > 1:  # о каждом повторе сообщаем, даже если ждать не нужно
                report({"phase": "retry", "attempt": attempt, "wait": round(wait, 1), "detail": last_error})
                if wait:
                    await asyncio.sleep(wait)
            wait = self.retry_base_delay * (min(2**attempt, 30) + random.random())
            report({"phase": "sent", "attempt": attempt, "max_tokens": payload["max_tokens"]})
            try:
                res = await self._attempt(payload, report)
            except _Retryable as e:
                last_error = str(e)
                continue
            except ProviderError as e:
                e.spent = spent
                raise
            spent.prompt_tokens += res["prompt_tokens"]
            spent.completion_tokens += res["completion_tokens"]
            spent.cost += res["cost"]
            text, finish = res["text"], res["finish"]
            if not text.strip():
                limit = payload["max_tokens"]
                if finish in ("length", "max_tokens", "MAX_TOKENS") and limit < self.max_tokens_cap:
                    payload["max_tokens"] = min(limit * 2, self.max_tokens_cap)
                    last_error = (f"пустой ответ: модель израсходовала лимит {limit} токенов на рассуждения, "
                                  f"повтор с лимитом {payload['max_tokens']}")
                    wait = 0.0  # повторяем сразу (причина уйдёт в событие retry)
                else:
                    last_error = f"пустой ответ модели (finish_reason={finish}, лимит {limit} токенов)"
                continue
            return Completion(
                text=text,
                prompt_tokens=spent.prompt_tokens,
                completion_tokens=spent.completion_tokens,
                cost=spent.cost,
                model=res["model"] or model,
                finish_reason=finish,
                max_tokens=payload["max_tokens"],
            )
        raise ProviderError(
            f"{model}: не удалось получить ответ после {self.max_retries + 1} попыток: {last_error}",
            spent=spent,
        )

    async def _attempt(self, payload: dict[str, Any], report: ProgressFn) -> dict[str, Any]:
        """Одна попытка. Возвращает текст и расходы; бросает _Retryable или ProviderError."""
        model = payload["model"]
        started = time.monotonic()
        try:
            async with self._http.stream("POST", "/chat/completions", json=payload) as resp:
                if resp.status_code in RETRY_STATUSES:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise _Retryable(_http_reason(resp.status_code, body))
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    raise ProviderError(f"{model}: HTTP {resp.status_code}: {body[:500]}")
                if "text/event-stream" not in resp.headers.get("content-type", ""):
                    try:
                        data = json.loads(await resp.aread())
                    except ValueError as e:
                        raise _Retryable("ответ не JSON") from e
                    return self._from_json(data, model)
                return await self._from_stream(resp, model, report, started)
        except httpx.TransportError as e:
            raise _Retryable(f"сетевая ошибка: {e.__class__.__name__}") from e

    def _from_json(self, data: dict[str, Any], model: str) -> dict[str, Any]:
        if data.get("error"):
            self._raise_error(data["error"], model)
        usage = data.get("usage") or {}
        choices = data.get("choices") or []
        if not choices:
            raise _Retryable("пустой список choices")
        choice = choices[0]
        return {
            "text": (choice.get("message") or {}).get("content") or "",
            "finish": choice.get("finish_reason") or choice.get("native_finish_reason"),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cost": float(usage.get("cost") or 0.0),
            "model": data.get("model") or model,
        }

    async def _from_stream(self, resp: httpx.Response, model: str, report: ProgressFn,
                           started: float) -> dict[str, Any]:
        text: list[str] = []
        chars = reasoning_chars = 0
        finish = None
        usage: dict[str, Any] = {}
        real_model = model
        last_report = 0.0
        phase = "thinking"
        report({"phase": "thinking", "chars": 0})
        async for line in resp.aiter_lines():
            if time.monotonic() - started > self.timeout * 3:
                raise _Retryable(f"ответ идёт слишком долго (более {int(self.timeout * 3)} с)")
            if not line or line.startswith(":"):  # «: OPENROUTER PROCESSING» — сервер жив, ответа ещё нет
                continue
            if not line.startswith("data:"):
                continue
            data_s = line[5:].strip()
            if data_s == "[DONE]":
                break
            try:
                chunk = json.loads(data_s)
            except ValueError:
                continue
            if chunk.get("error"):
                self._raise_error(chunk["error"], model)
            real_model = chunk.get("model") or real_model
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content") or ""
                thought = delta.get("reasoning") or ""
                if piece:
                    text.append(piece)
                    chars += len(piece)
                reasoning_chars += len(thought)
                finish = choice.get("finish_reason") or choice.get("native_finish_reason") or finish
            now = time.monotonic()
            new_phase = "writing" if chars else "thinking"
            if new_phase != phase or now - last_report >= 0.5:
                phase, last_report = new_phase, now
                report({"phase": phase, "chars": chars, "reasoning_chars": reasoning_chars})
        report({"phase": phase, "chars": chars, "reasoning_chars": reasoning_chars})  # итог потока
        return {
            "text": "".join(text),
            "finish": finish,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cost": float(usage.get("cost") or 0.0),
            "model": real_model,
        }

    @staticmethod
    def _raise_error(err: Any, model: str) -> None:
        code = err.get("code") if isinstance(err, dict) else None
        msg = err.get("message") if isinstance(err, dict) else str(err)
        if code in RETRY_STATUSES:
            raise _Retryable(_http_reason(code, msg or ""))
        raise ProviderError(f"{model}: ошибка провайдера: {msg or err}")

    async def list_models(self) -> list[dict[str, Any]]:
        resp = await self._http.get("/models")
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def aclose(self) -> None:
        await self._http.aclose()
