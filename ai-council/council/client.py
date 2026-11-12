"""Клиент OpenRouter (OpenAI-совместимый API) с ретраями и учётом стоимости."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import httpx

log = logging.getLogger("council.client")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}
ProgressFn = Callable[[dict[str, Any]], None]


def _cached(usage: dict[str, Any]) -> int:
    """Входные токены, прочитанные из кэша (OpenRouter: usage.prompt_tokens_details.cached_tokens)."""
    details = usage.get("prompt_tokens_details") or {}
    try:
        return int(details.get("cached_tokens") or 0)
    except (TypeError, ValueError):
        return 0


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
    return OutOfCredits(f"{model}: на балансе OpenRouter недостаточно средств для этого запроса (HTTP 402). "
                         "Пополните баланс: https://openrouter.ai/settings/credits — или уменьшите число участников "
                         "и лимит длины ответа.")


def _key_error(code: int, body: str, model: str) -> Exception | None:
    """HTTP 401/403 от OpenRouter про сам ключ (лимит расходов ключа, неверный ключ) — или None, если это другое."""
    try:
        err = (json.loads(body) or {}).get("error") or {}
    except ValueError:
        err = {}
    if not isinstance(err, dict):
        err = {}
    meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    msg = str(err.get("message") or body)
    low = msg.lower()
    if code == 403 and (meta.get("scope") == "key_limit" or "key_limit" in str(meta.get("limit_source", ""))
                        or "key limit" in low or "limit exceeded" in low):
        spent, limit, resets = meta.get("spend_usd"), meta.get("limit_usd"), str(meta.get("resets_at") or "")
        bits = []
        if spent is not None and limit is not None:
            try:
                bits.append(f"потрачено ${float(spent):.2f} из ${float(limit):g}")
            except (TypeError, ValueError):
                pass
        if resets:
            bits.append(f"лимит сбросится {resets[8:10]}.{resets[5:7]}.{resets[:4]}" if len(resets) >= 10 else f"сброс {resets}")
        detail = f" ({', '.join(bits)})" if bits else ""
        return KeyLimit(f"достигнут лимит расходов API-ключа OpenRouter{detail}. Поднимите или снимите лимит ключа: "
                        "https://openrouter.ai/settings/keys — затем нажмите «Продолжить».")
    if code == 401:
        return KeyLimit("OpenRouter не принял API-ключ (HTTP 401): ключ неверный, удалён или отключён. Проверьте ключ "
                        "в .env на сервере и https://openrouter.ai/settings/keys, затем нажмите «Продолжить».")
    return None


def _reasoning(usage: dict[str, Any]) -> int:
    """Сколько выходных токенов ушло на скрытые рассуждения (OpenRouter: completion_tokens_details)."""
    d = usage.get("completion_tokens_details") or {}
    try:
        return int(d.get("reasoning_tokens") or usage.get("reasoning_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def _citations(top: dict[str, Any], message: dict[str, Any]) -> list[str]:
    """Ссылки, которые вернул поиск: «citations» (Perplexity) и «annotations» с url_citation (поиск OpenRouter)."""
    out: list[str] = []
    for c in top.get("citations") or message.get("citations") or []:
        u = c if isinstance(c, str) else (c.get("url") if isinstance(c, dict) else None)
        if isinstance(u, str) and u.startswith("http"):
            out.append(u)
    for a in message.get("annotations") or []:
        if isinstance(a, dict):
            u = (a.get("url_citation") or {}).get("url") or a.get("url")
            if isinstance(u, str) and u.startswith("http"):
                out.append(u)
    return out


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


class OutOfCredits(ProviderError):
    """На балансе OpenRouter кончились деньги (HTTP 402). Заседание приостанавливается, а не теряется."""


class KeyLimit(OutOfCredits):
    """Ключ OpenRouter не пускает дальше: исчерпан лимит расходов ключа (HTTP 403) или ключ неверный/отключён (401).
    Это касается всех моделей сразу — заседание приостанавливается, а не теряет участников по одному."""


# Что модель не поддерживает (узнаём по ответу HTTP 400 и запоминаем до перезапуска программы)
_NO_FEATURE: dict[str, set[str]] = {"tools": set(), "fetch": set(), "json": set()}
FETCH_TOKENS = 8000  # сколько текста страницы (≈ токенов) отдаётся модели при чтении
# Сколько поисков модель может сделать за один запрос: инструмент web_search разрешает искать много раз подряд, и
# каждый результат добавляется ко входу (на заседании 12.11 автор с поиском прочитал 3,6 млн токенов за 4 запроса)
SEARCH_USES = 3
# Модель ничего не пишет (ни текста, ни рассуждений) столько секунд подряд — запрос повторяется. Поиск и чтение
# страниц на сервере OpenRouter идут молча, поэтому запас большой
STALL_S = 150
# Поисковик инструмента web_search. «auto» у OpenAI и Google включает встроенный поиск провайдера, а он не знает
# лимитов max_uses / max_results: на заседании 12.11 автор chatgpt за один запрос предложений прочитал 2 млн токенов
# ($1,14). «exa» — поиск OpenRouter (Exa), где лимиты соблюдаются: не больше SEARCH_USES поисков по 5 результатов
SEARCH_ENGINES = ("exa", "auto", "native", "parallel", "perplexity", "firecrawl")


def _drop_unsupported(payload: dict[str, Any], model: str, err: str) -> str:
    """HTTP 400/404/422 из-за новых возможностей — убрать причину из запроса. Возвращает, что убрано ('' — ничего)."""
    if not any(f" {c}" in err for c in (400, 404, 422)):
        return ""
    low = err.lower()
    if "response_format" in payload and ("response_format" in low or "json" in low or "structured" in low):
        payload.pop("response_format")
        _NO_FEATURE["json"].add(model)
        return "строгий JSON"
    tools = payload.get("tools") or []
    if any(t["type"] == "openrouter:web_fetch" for t in tools) and ("fetch" in low or "tool" in low):
        payload["tools"] = [t for t in tools if t["type"] != "openrouter:web_fetch"]
        if not payload["tools"]:
            payload.pop("tools")
        _NO_FEATURE["fetch"].add(model)
        return "чтение страниц (web_fetch)"
    if any(t["type"] == "openrouter:web_search" for t in tools) and ("tool" in low or "search" in low):
        payload["tools"] = [t for t in tools if t["type"] != "openrouter:web_search"]
        if not payload["tools"]:
            payload.pop("tools")
        payload["plugins"] = [{"id": "web"}]
        _NO_FEATURE["tools"].add(model)
        return "инструмент поиска (возврат к плагину)"
    if "response_format" in payload:  # причина не названа — сначала снимаем самое вероятное
        payload.pop("response_format")
        _NO_FEATURE["json"].add(model)
        return "строгий JSON"
    return ""


@dataclass
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    model: str = ""
    finish_reason: str | None = None  # "length" — ответ обрезан по лимиту токенов
    max_tokens: int = 0  # с каким лимитом получен ответ (после возможных увеличений)
    cached_tokens: int = 0  # сколько входных токенов прочитано из кэша провайдера (дешевле)
    citations: list[str] = field(default_factory=list)  # ссылки, которые реально вернул поиск (не из текста модели)
    gen_id: str = ""  # номер ответа у OpenRouter (gen-…): по нему запрос находится в журнале OpenRouter
    reasoning_tokens: int = 0     # из выходных токенов — скрытые рассуждения модели (если провайдер сообщает)
    reasoning_off: bool = False   # рассуждения пришлось выключить: модель тратила на них весь лимит


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
        web_search: bool = False,
    ) -> Completion: ...

    async def aclose(self) -> None: ...


# Модели, которые уходили в бесконечные скрытые рассуждения: какой параметр «reasoning» им помог.
# Следующие запросы к ним сразу идут с ним — без оплаты пустых попыток (живёт до перезапуска программы).
_REASONING_FIX: dict[str, dict[str, Any]] = {}
REASONING_OFF = {"enabled": False}  # выключить рассуждения (гибридные модели вроде DeepSeek отвечают сразу)


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
        web_search: bool = False,
        web_fetch: int = 0,
        json_mode: bool = False,
        web_tools: bool = True,
        search_engine: str = "exa",
    ) -> Completion:
        """Запрос к модели с потоковой передачей ответа.

        web_tools — поиск через серверный инструмент OpenRouter openrouter:web_search (прежний плагин «web»
        объявлен устаревшим; False — по-старому). web_fetch — сколько страниц модель может прочитать целиком
        инструментом openrouter:web_fetch (0 — не может). json_mode — ответ строго JSON-объектом
        (response_format), чтобы не было повторов из-за сломанного формата. Если модель или провайдер что-то из
        этого не поддерживает (HTTP 400), запрос сразу повторяется без этого, и для модели это запоминается.

        web_search — включить поиск в интернете средствами OpenRouter (у моделей со встроенным поиском,
        например Perplexity, он есть и так). Найденные ссылки возвращаются в Completion.citations.

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
        if model in _REASONING_FIX:
            payload["reasoning"] = dict(_REASONING_FIX[model])
        tools: list[dict[str, Any]] = []
        if web_search and not model.startswith("perplexity/"):  # у Perplexity поиск встроенный
            if web_tools and model not in _NO_FEATURE["tools"]:
                params: dict[str, Any] = {"max_results": 5, "max_uses": SEARCH_USES, "max_total_results": 5 * SEARCH_USES}
                if search_engine and search_engine != "auto":
                    params["engine"] = search_engine
                tools.append({"type": "openrouter:web_search", "parameters": params})
            else:
                payload["plugins"] = [{"id": "web"}]  # прежний способ: встроенный поиск провайдера или Exa
        # Perplexity не принимает инструменты и response_format json_object — не тратим на это запрос с ошибкой
        if web_fetch and model not in _NO_FEATURE["fetch"] and not model.startswith("perplexity/"):
            tools.append({"type": "openrouter:web_fetch",
                          "parameters": {"max_uses": int(web_fetch), "max_content_tokens": FETCH_TOKENS}})
        if tools:
            payload["tools"] = tools
        if json_mode and model not in _NO_FEATURE["json"] and not model.startswith("perplexity/"):
            payload["response_format"] = {"type": "json_object"}
        spent = Completion(text="")  # расходы всех попыток, включая неудачные
        last_error = "неизвестная ошибка"
        wait = 0.0
        attempt = 0
        credit_waits = 0
        empty_runs = 0  # ответы, целиком ушедшие в скрытые рассуждения
        call_start = time.monotonic()
        while attempt < self.max_retries + 1:
            attempt += 1
            if attempt > 1 and time.monotonic() - call_start > self.timeout * 5:
                # все попытки вместе — не дольше ~15 минут: медленный провайдер не держит весь совет
                last_error = f"{last_error}; на все попытки ушло больше {int(self.timeout * 5 / 60)} мин"
                break
            if attempt > 1 or credit_waits:  # о каждом повторе сообщаем, даже если ждать не нужно
                report({"phase": "retry", "attempt": attempt, "wait": round(wait, 1), "detail": last_error})
                if wait:
                    await asyncio.sleep(wait)
            wait = self.retry_base_delay * (min(2**attempt, 30) + random.random())
            report({"phase": "sent", "attempt": attempt, "max_tokens": payload["max_tokens"]})
            log.debug("HTTP → %s попытка %d: max_tokens=%s, reasoning=%s, tools=%s, plugins=%s, response_format=%s, "
                      "сообщений %d, ≈%d зн.", model, attempt, payload["max_tokens"], payload.get("reasoning"),
                      [t["type"] for t in payload.get("tools") or []], [p["id"] for p in payload.get("plugins") or []],
                      (payload.get("response_format") or {}).get("type"), len(payload["messages"]),
                      sum(len(m["content"]) if isinstance(m.get("content"), str) else
                          sum(len(x.get("text", "")) for x in m.get("content") or [] if isinstance(x, dict))
                          for m in payload["messages"]))
            try:
                if self._narrow is not None:
                    async with self._narrow:  # после нехватки резерва — меньше запросов одновременно
                        res = await self._attempt(payload, report)
                else:
                    res = await self._attempt(payload, report)
            except _Retryable as e:
                last_error = str(e)
                log.warning("HTTP ✗ %s попытка %d: %s (повтор)", model, attempt, e)
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
                dropped = _drop_unsupported(payload, model, str(e))
                if dropped:  # модель не умеет что-то из новых возможностей — сразу повторяем без этого
                    log.warning("%s: не поддерживает %s — повтор без этого (%s)", model, dropped, str(e)[:300])
                    attempt -= 1
                    wait = 0.0
                    last_error = f"модель не поддерживает {dropped} — повтор без этого"
                    continue
                if payload.get("reasoning") == REASONING_OFF and empty_runs and " 400" in str(e):
                    # модель не умеет отвечать без рассуждений — понятная ошибка вместо «HTTP 400»
                    raise ProviderError(self._runaway_text(model), spent=spent) from e
                raise
            log.debug("HTTP ← %s попытка %d: %s, вход %d (кэш %d), выход %d (рассужд. %d), $%.6f, finish=%s, %d зн.",
                      model, attempt, res.get("id") or "без номера", res["prompt_tokens"], res.get("cached_tokens", 0),
                      res["completion_tokens"], res.get("reasoning_tokens", 0), res["cost"], res["finish"],
                      len(res["text"]))
            spent.prompt_tokens += res["prompt_tokens"]
            spent.cached_tokens += res.get("cached_tokens", 0)
            spent.completion_tokens += res["completion_tokens"]
            spent.cost += res["cost"]
            spent.citations = list(dict.fromkeys(spent.citations + res.get("citations", [])))
            spent.reasoning_tokens += res.get("reasoning_tokens", 0)
            text, finish = res["text"], res["finish"]
            if not text.strip():
                limit = payload["max_tokens"]
                if finish in ("length", "max_tokens", "MAX_TOKENS"):
                    # «Думающая» модель потратила весь лимит на скрытые рассуждения и не начала ответ.
                    # Просто поднимать лимит бесполезно — она рассуждает ещё дольше (и это оплачивается).
                    # Поэтому вместе с лимитом ограничиваем сами рассуждения, оставляя место для ответа.
                    empty_runs += 1
                    _REASONING_FIX.pop(model, None)  # то, что помогало раньше, больше не помогает
                    if empty_runs > 2 or payload.get("reasoning") == REASONING_OFF:
                        raise ProviderError(self._runaway_text(model), spent=spent)
                    if empty_runs == 1:
                        payload["max_tokens"] = min(max(limit * 2, 4000), self.max_tokens_cap)
                        budget = max(1024, payload["max_tokens"] // 2)
                        payload["reasoning"] = {"max_tokens": budget}
                        last_error = (f"пустой ответ: модель израсходовала лимит {limit} токенов на скрытые рассуждения — "
                                      f"повтор с лимитом {payload['max_tokens']}, рассуждения ограничены {budget} токенами")
                    else:
                        # Даже ограниченные рассуждения съедают весь лимит (так бывает у deepseek-v4.1-flash):
                        # выключаем их совсем — гибридные модели тогда сразу пишут ответ.
                        payload["reasoning"] = dict(REASONING_OFF)
                        last_error = ("пустой ответ: рассуждения снова съели весь лимит — повтор с выключенными "
                                      "рассуждениями (модель ответит сразу)")
                    wait = 0.0  # повторяем сразу (причина уйдёт в событие retry)
                else:
                    last_error = f"пустой ответ модели (finish_reason={finish}, лимит {limit} токенов)"
                log.warning("%s: %s", model, last_error)
                continue
            if empty_runs and payload.get("reasoning"):
                _REASONING_FIX[model] = dict(payload["reasoning"])  # следующие запросы — сразу так
            return Completion(
                text=text,
                prompt_tokens=spent.prompt_tokens,
                completion_tokens=spent.completion_tokens,
                cost=spent.cost,
                model=res["model"] or model,
                finish_reason=finish,
                max_tokens=payload["max_tokens"],
                cached_tokens=spent.cached_tokens,
                citations=spent.citations,
                reasoning_tokens=spent.reasoning_tokens,
                reasoning_off=payload.get("reasoning") == REASONING_OFF,
                gen_id=res.get("id", ""),
            )
        raise ProviderError(
            f"{model}: не удалось получить ответ после {self.max_retries + 1} попыток: {last_error}",
            spent=spent,
        )

    @staticmethod
    def _runaway_text(model: str) -> str:
        return (f"{model}: модель тратит весь лимит на скрытые рассуждения и не даёт ответа — ни с ограничением "
                "рассуждений, ни с выключенными. В «Настройки → Участники» поставьте этому участнику «Глубина "
                "рассуждений: низкая» или выберите другую модель.")

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
                    raise _key_error(resp.status_code, body, model) or ProviderError(
                        f"{model}: HTTP {resp.status_code}: {body[:500]}")
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
            "citations": _citations(data, choice.get("message") or {}),
            "text": _as_text((choice.get("message") or {}).get("content")),
            "finish": choice.get("finish_reason") or choice.get("native_finish_reason"),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "cached_tokens": _cached(usage),
            "reasoning_tokens": _reasoning(usage),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cost": float(usage.get("cost") or 0.0),
            "model": data.get("model") or model,
            "id": str(data.get("id") or ""),
        }

    async def _from_stream(self, resp: httpx.Response, model: str, report: ProgressFn,
                           started: float) -> dict[str, Any]:
        text: list[str] = []
        chars = reasoning_chars = 0
        finish = None
        usage: dict[str, Any] = {}
        real_model = model
        gen_id = ""
        cites: list[str] = []
        last_report = 0.0
        phase = "thinking"
        report({"phase": "thinking", "chars": 0})
        moved = time.monotonic()  # когда в последний раз пришёл новый текст или рассуждения
        seen = 0
        async for line in resp.aiter_lines():
            now = time.monotonic()
            if now - started > self.timeout * 3:
                raise _Retryable(f"ответ идёт слишком долго (более {int(self.timeout * 3)} с)")
            if chars + reasoning_chars > seen:
                seen, moved = chars + reasoning_chars, now
            elif now - moved > STALL_S:
                # сервер шлёт только «я жив», а модель ничего не пишет — провайдер завис: повтор, а не ожидание
                raise _Retryable(f"модель ничего не присылает {int(now - moved)} с — повтор запроса")
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
            gen_id = gen_id or str(chunk.get("id") or "")
            if chunk.get("usage"):
                usage = chunk["usage"]
            cites += _citations(chunk, {})
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                cites += _citations({}, delta)
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
            "citations": list(dict.fromkeys(cites)),
            "text": "".join(text),
            "finish": finish,
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "cached_tokens": _cached(usage),
            "reasoning_tokens": _reasoning(usage),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "cost": float(usage.get("cost") or 0.0),
            "model": real_model,
            "id": gen_id,
        }

    @staticmethod
    def _raise_error(err: Any, model: str) -> None:
        code = err.get("code") if isinstance(err, dict) else None
        msg = err.get("message") if isinstance(err, dict) else str(err)
        if code in RETRY_STATUSES:
            raise _Retryable(_http_reason(code, msg or ""))
        if code == 402:
            raise _credit_error(402, json.dumps({"error": err}), model)
        if code in (401, 403):
            key = _key_error(code, json.dumps({"error": err}), model)
            if key:
                raise key
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

    async def key_limit(self) -> dict[str, float] | None:
        """Лимит расходов ключа OpenRouter (если задан): {"limit", "remaining", "usage"} в $; None — не задан или
        не удалось узнать. Недельный лимит ключа кончается раньше баланса — заседание тогда встаёт на паузу."""
        try:
            resp = await self._http.get("/key")
            if resp.status_code != 200:
                return None
            d = resp.json().get("data") or {}
            if d.get("limit") in (None, 0) or d.get("limit_remaining") is None:
                return None
            return {"limit": round(float(d["limit"]), 2), "remaining": round(float(d["limit_remaining"]), 4),
                    "usage": round(float(d.get("usage") or 0), 4), "reset": str(d.get("limit_reset") or "")}
        except (httpx.HTTPError, ValueError, TypeError):
            return None

    async def list_models(self) -> list[dict[str, Any]]:
        resp = await self._http.get("/models")
        resp.raise_for_status()
        return resp.json().get("data", [])

    async def aclose(self) -> None:
        await self._http.aclose()
