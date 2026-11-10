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


def _as_text(content: Any) -> str:
    """Текст ответа. Некоторые провайдеры присылают content списком частей ([{"type": "text", "text": …}, …])."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
        return "".join(out)
    return str(content)


class _Retryable(Exception):
    """Временная ошибка: стоит повторить запрос."""

    def __init__(self, message: str, *, retry_after: float | None = None, credits: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.credits = credits  # OpenRouter зарезервировал баланс под другие идущие запросы


CREDIT_RETRIES = 6          # сколько раз ждать освобождения резерва баланса (попытками не считается)
CREDIT_WAIT_MAX = 90.0      # не ждать дольше этого за один раз, даже если OpenRouter просит больше
NARROW_SLOTS = 2            # сколько запросов одновременно после первой нехватки резерва


def _credit_error(code: int, body: str, model: str) -> Exception:
    """HTTP 402 от OpenRouter: временная нехватка резерва (повторить позже) или настоящая нехватка денег."""
    try:
        err = (json.loads(body) or {}).get("error") or {}
    except ValueError:
        err = {}
    meta = err.get("metadata") or {}
    reason = str(meta.get("reason") or "")
    msg = str(err.get("message") or body)
    if "in_flight" in reason or "in-flight" in msg.lower() or "in flight" in msg.lower():
        retry_after = None
        hdrs = meta.get("headers") or {}
        try:
            retry_after = float(hdrs.get("Retry-After") or hdrs.get("retry-after") or 0) or None
        except (TypeError, ValueError):
            retry_after = None
        return _Retryable("OpenRouter временно зарезервировал баланс под другие идущие запросы — жду их завершения "
                          "(на балансе мало средств для стольких запросов сразу)", retry_after=retry_after, credits=True)
    return ProviderError(f"{model}: на балансе OpenRouter недостаточно средств для этого запроса (HTTP 402). "
                         "Пополните баланс: https://openrouter.ai/settings/credits — или уменьшите число участников "
                         "и лимит длины ответа.")


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
        self._narrow: asyncio.Semaphore | None = None  # ограничение после нехватки резерва баланса
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
        attempt = 0
        credit_waits = 0
        empty_runs = 0  # ответы, целиком ушедшие в скрытые рассуждения
        while attempt < self.max_retries + 1:
            attempt += 1
            if attempt > 1 or credit_waits:  # о каждом повторе сообщаем, даже если ждать не нужно
                report({"phase": "retry", "attempt": attempt, "wait": round(wait, 1), "detail": last_error})
                if wait:
                    await asyncio.sleep(wait)
            wait = self.retry_base_delay * (min(2**attempt, 30) + random.random())
            report({"phase": "sent", "attempt": attempt, "max_tokens": payload["max_tokens"]})
            try:
                if self._narrow is not None:
                    async with self._narrow:  # после нехватки резерва — меньше запросов одновременно
                        res = await self._attempt(payload, report)
                else:
                    res = await self._attempt(payload, report)
            except _Retryable as e:
                last_error = str(e)
                if e.credits and credit_waits < CREDIT_RETRIES:
                    # Резерв баланса освободится, когда закончатся идущие запросы: ждём и пробуем снова,
                    # не тратя обычные попытки, и дальше шлём меньше запросов одновременно.
                    credit_waits += 1
                    attempt -= 1
                    if self._narrow is None:
                        self._narrow = asyncio.Semaphore(NARROW_SLOTS)
                    base = e.retry_after if e.retry_after else 20.0
                    wait = min(base, CREDIT_WAIT_MAX) * self.retry_base_delay + random.random() * 5 * self.retry_base_delay
                    last_error = f"{e} — повтор через {wait:.0f} с"
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
                if finish in ("length", "max_tokens", "MAX_TOKENS"):
                    # «Думающая» модель потратила весь лимит на скрытые рассуждения и не начала ответ.
                    # Просто поднимать лимит бесполезно — она рассуждает ещё дольше (и это оплачивается).
                    # Поэтому вместе с лимитом ограничиваем сами рассуждения, оставляя место для ответа.
                    empty_runs += 1
                    if empty_runs > 2:
                        raise ProviderError(
                            f"{model}: модель тратит весь лимит на скрытые рассуждения и не даёт ответа даже при "
                            "ограничении рассуждений. В «Настройки → Участники» поставьте этому участнику "
                            "«Глубина рассуждений: не задавать» или «низкая», либо выберите другую модель.",
                            spent=spent)
                    payload["max_tokens"] = min(max(limit * 2, 4000), self.max_tokens_cap)
                    budget = max(1024, payload["max_tokens"] // 2) if empty_runs == 1 else 2048
                    payload["reasoning"] = {"max_tokens": budget}
                    last_error = (f"пустой ответ: модель израсходовала лимит {limit} токенов на скрытые рассуждения — "
                                  f"повтор с лимитом {payload['max_tokens']}, рассуждения ограничены {budget} токенами")
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
                if resp.status_code == 402:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    err = _credit_error(402, body, model)
                    if isinstance(err, _Retryable) and err.retry_after is None and resp.headers.get("retry-after"):
                        try:
                            err.retry_after = float(resp.headers["retry-after"])
                        except ValueError:
                            pass
                    raise err
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
            "text": _as_text((choice.get("message") or {}).get("content")),
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
                piece = _as_text(delta.get("content"))
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
        if code == 402:
            raise _credit_error(402, json.dumps({"error": err}), model)
        raise ProviderError(f"{model}: ошибка провайдера: {msg or err}")

    async def balance(self) -> float | None:
        """Остаток на балансе OpenRouter в $ (None — не удалось узнать)."""
        try:
            resp = await self._http.get("/credits")
            if resp.status_code != 200:
                return None
            d = resp.json().get("data") or {}
            return round(float(d.get("total_credits") or 0) - float(d.get("total_usage") or 0), 4)
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def list_models(self) -> list[dict[str, Any]]:
        resp = await self._http.get("/models")
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def aclose(self) -> None:
        await self._http.aclose()
