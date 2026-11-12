import asyncio
import json

import httpx
import pytest

from council.client import OpenRouterClient, ProviderError


@pytest.fixture(autouse=True)
def _fresh_reasoning_memory():
    from council import client as cl
    cl._REASONING_FIX.clear()
    yield
    cl._REASONING_FIX.clear()


def make(handler, retries=2):
    return OpenRouterClient("k", transport=httpx.MockTransport(handler), max_retries=retries, retry_base_delay=0)


def ok_body(text="{}"):
    return {"model": "x/y", "choices": [{"message": {"content": text}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.0123}}


def test_parses_response_and_sends_usage_flag():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return httpx.Response(200, json=ok_body('{"a":1}'))

    c = make(handler)
    comp = asyncio.run(c.complete("x/y", [{"role": "user", "content": "hi"}], temperature=0.5, max_tokens=100))
    assert comp.text == '{"a":1}' and comp.cost == 0.0123 and comp.prompt_tokens == 10
    assert seen["body"]["usage"] == {"include": True} and seen["auth"] == "Bearer k"


def test_retries_on_429_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, text="rate") if len(calls) < 3 else httpx.Response(200, json=ok_body("ok"))

    comp = asyncio.run(make(handler).complete("m", [], temperature=0, max_tokens=1))
    assert comp.text == "ok" and len(calls) == 3


def test_client_error_is_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "bad model"}})

    with pytest.raises(ProviderError, match="400"):
        asyncio.run(make(handler).complete("m", [], temperature=0, max_tokens=1))
    assert len(calls) == 1


def test_empty_content_exhausts_retries():
    with pytest.raises(ProviderError, match="пустой ответ"):
        asyncio.run(make(lambda r: httpx.Response(200, json=ok_body(""))).complete("m", [], temperature=0, max_tokens=1))


def test_reasoning_model_empty_answer_gets_more_tokens():
    """Случай gpt-5: рассуждения съели весь лимит, content пустой, finish_reason=length."""
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                                             "usage": {"prompt_tokens": 100, "completion_tokens": 4000, "cost": 0.04}})
        return httpx.Response(200, json=ok_body('{"ok": 1}'))

    comp = asyncio.run(make(handler).complete("openai/gpt-5", [], temperature=0, max_tokens=4000,
                                              reasoning_effort="low"))
    assert comp.text == '{"ok": 1}'
    assert [b["max_tokens"] for b in bodies] == [4000, 8000]
    assert bodies[0]["reasoning"] == {"effort": "low"}
    assert comp.cost == pytest.approx(0.04 + 0.0123)  # неудачная попытка тоже учтена
    assert comp.max_tokens == 8000


def test_persistent_empty_answer_reports_spent_cost():
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                                         "usage": {"cost": 0.01}})

    c = OpenRouterClient("k", transport=httpx.MockTransport(handler), max_retries=2, retry_base_delay=0,
                         max_tokens_cap=8000)
    with pytest.raises(ProviderError, match="лимит") as e:
        asyncio.run(c.complete("m", [], temperature=0, max_tokens=4000))
    assert e.value.spent.cost == pytest.approx(0.03)


def test_no_reasoning_param_when_disabled():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=ok_body("x"))

    asyncio.run(make(handler).complete("m", [], temperature=0, max_tokens=10))
    assert "reasoning" not in seen


def sse(*chunks, comments=True):
    lines = [": OPENROUTER PROCESSING"] if comments else []
    lines += [f"data: {json.dumps(c, ensure_ascii=False)}" for c in chunks] + ["data: [DONE]"]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=("\n\n".join(lines) + "\n\n").encode())


def test_streaming_phases_text_and_cost():
    events = []
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return sse(
            {"model": "x/y", "choices": [{"delta": {"reasoning": "хм"}}]},
            {"choices": [{"delta": {"reasoning": " думаю…"}}]},
            {"choices": [{"delta": {"content": '{"ok": '}}]},
            {"choices": [{"delta": {"content": "1}"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.007}},
        )

    c = OpenRouterClient("k", transport=httpx.MockTransport(handler), retry_base_delay=0)
    comp = asyncio.run(c.complete("x/y", [], temperature=0, max_tokens=100, on_progress=events.append))
    assert seen["stream"] is True
    assert comp.text == '{"ok": 1}' and comp.cost == 0.007 and comp.finish_reason == "stop" and comp.model == "x/y"
    phases = [e["phase"] for e in events]
    assert phases[0] == "sent" and "thinking" in phases and phases[-1] == "writing"
    assert events[-1]["chars"] == len('{"ok": 1}')


def test_streaming_retry_on_overload_and_error_chunk():
    events, n = [], []

    def handler(req):
        n.append(1)
        if len(n) == 1:
            return httpx.Response(503, text="overloaded")
        if len(n) == 2:  # ошибка посреди потока
            return sse({"error": {"code": 429, "message": "rate limited"}})
        return sse({"choices": [{"delta": {"content": "готово"}, "finish_reason": "stop"}]})

    c = OpenRouterClient("k", transport=httpx.MockTransport(handler), retry_base_delay=0)
    comp = asyncio.run(c.complete("m", [], temperature=0, max_tokens=10, on_progress=events.append))
    assert comp.text == "готово" and len(n) == 3
    retries = [e for e in events if e["phase"] == "retry"]
    assert len(retries) == 2 and "перегружен" in retries[0]["detail"] and "частоту" in retries[1]["detail"]


def test_streaming_empty_answer_bumps_limit():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            return sse({"choices": [{"delta": {"content": ""}, "finish_reason": "length"}], "usage": {"cost": 0.01}})
        return sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}], "usage": {"cost": 0.02}})

    events = []
    c = OpenRouterClient("k", transport=httpx.MockTransport(handler), retry_base_delay=0)
    comp = asyncio.run(c.complete("m", [], temperature=0, max_tokens=4000, on_progress=events.append))
    assert [b["max_tokens"] for b in bodies] == [4000, 8000] and comp.cost == pytest.approx(0.03)
    assert any(e["phase"] == "retry" and "8000" in e["detail"] for e in events)


INFLIGHT = {"error": {"message": "This request would exceed your available credits given your current in-flight requests.",
                      "code": 402, "metadata": {"reason": "in_flight_budget_exhausted",
                                                "headers": {"Retry-After": "120"}}}}


def test_in_flight_credit_limit_waits_and_retries_without_using_attempts():
    calls = []
    events = []

    def handler(req):
        calls.append(1)
        return httpx.Response(402, json=INFLIGHT) if len(calls) <= 4 else httpx.Response(200, json=ok_body("ok"))

    c = make(handler, retries=1)  # обычных попыток всего 2 — а ожиданий резерва больше
    comp = asyncio.run(c.complete("m", [], temperature=0, max_tokens=1, on_progress=events.append))
    assert comp.text == "ok" and len(calls) == 5
    assert c._narrow is not None  # дальше — меньше запросов одновременно
    retries = [e for e in events if e.get("phase") == "retry"]
    assert retries and "зарезервировал баланс" in retries[0]["detail"]


def test_in_flight_gives_up_after_limit():
    c = make(lambda req: httpx.Response(402, json=INFLIGHT), retries=0)
    with pytest.raises(ProviderError, match="зарезервировал баланс"):
        asyncio.run(c.complete("m", [], temperature=0, max_tokens=1))


def test_real_insufficient_credits_is_clear():
    body = {"error": {"message": "Insufficient credits", "code": 402}}
    c = make(lambda req: httpx.Response(402, json=body))
    with pytest.raises(ProviderError, match="недостаточно средств"):
        asyncio.run(c.complete("m", [], temperature=0, max_tokens=1))


def test_balance():
    def handler(req):
        assert req.url.path.endswith("/credits")
        return httpx.Response(200, json={"data": {"total_credits": 20, "total_usage": 7.5}})

    assert asyncio.run(make(handler).balance()) == 12.5
    assert asyncio.run(make(lambda req: httpx.Response(500)).balance()) is None


def test_reasoning_runaway_gets_thinking_budget_then_clear_error():
    """Случай deepseek-v4.1-flash: каждый раз весь лимит уходит в рассуждения — не жжём попытки бесконечно."""
    from council import client as cl
    cl._REASONING_FIX.clear()
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                                         "usage": {"cost": 0.01}})

    c = OpenRouterClient("k", transport=httpx.MockTransport(handler), max_retries=5, retry_base_delay=0,
                         max_tokens_cap=32000)
    with pytest.raises(ProviderError, match="скрытые рассуждения") as e:
        asyncio.run(c.complete("deepseek/x", [], temperature=0, max_tokens=8000, reasoning_effort="low"))
    assert len(bodies) == 3  # не 6: ограничить рассуждения, выключить их — и понятная ошибка
    assert bodies[0]["reasoning"] == {"effort": "low"}
    assert bodies[1]["max_tokens"] == 16000 and bodies[1]["reasoning"] == {"max_tokens": 8000}
    assert bodies[2]["reasoning"] == {"enabled": False}
    assert "Глубина рассуждений" in str(e.value) and e.value.spent.cost == pytest.approx(0.03)


def test_reasoning_off_rescues_runaway_model_and_is_remembered():
    from council import client as cl
    cl._REASONING_FIX.clear()
    bodies = []

    def handler(req):
        b = json.loads(req.content)
        bodies.append(b)
        if b.get("reasoning") == {"enabled": False}:
            return httpx.Response(200, json=ok_body("ответ"))
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                                         "usage": {"cost": 0.01}})

    c = make(handler, retries=5)
    comp = asyncio.run(c.complete("deepseek/v", [], temperature=0, max_tokens=8000, reasoning_effort="low"))
    assert comp.text == "ответ" and len(bodies) == 3
    comp = asyncio.run(c.complete("deepseek/v", [], temperature=0, max_tokens=8000, reasoning_effort="low"))
    assert comp.text == "ответ" and len(bodies) == 4 and bodies[3]["reasoning"] == {"enabled": False}  # сразу
    cl._REASONING_FIX.clear()


def test_model_that_cannot_disable_reasoning_gets_clear_error():
    from council import client as cl
    cl._REASONING_FIX.clear()

    def handler(req):
        b = json.loads(req.content)
        if b.get("reasoning") == {"enabled": False}:
            return httpx.Response(400, json={"error": {"message": "Reasoning is mandatory for this endpoint"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                                         "usage": {"cost": 0.01}})

    with pytest.raises(ProviderError, match="скрытые рассуждения"):
        asyncio.run(make(handler, retries=5).complete("o/x", [], temperature=0, max_tokens=8000, reasoning_effort="low"))


def test_thinking_budget_lets_model_answer():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                                             "usage": {"cost": 0.01}})
        return httpx.Response(200, json=ok_body("ответ"))

    comp = asyncio.run(make(handler).complete("m", [], temperature=0, max_tokens=8000, reasoning_effort="low"))
    assert comp.text == "ответ" and bodies[1]["reasoning"] == {"max_tokens": 8000}


def test_reasoning_tokens_and_reasoning_off_are_reported():
    def handler(req):
        b = json.loads(req.content)
        if b.get("reasoning") == {"enabled": False}:
            return httpx.Response(200, json={**ok_body("ответ"), "usage": {"completion_tokens": 50, "cost": 0.001}})
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                                         "usage": {"completion_tokens": 8000, "cost": 0.01,
                                                   "completion_tokens_details": {"reasoning_tokens": 8000}}})

    comp = asyncio.run(make(handler, retries=5).complete("deepseek/r", [], temperature=0, max_tokens=8000,
                                                         reasoning_effort="low"))
    assert comp.reasoning_off and comp.reasoning_tokens == 16000


@pytest.fixture
def _fresh_features():
    from council import client as cl
    for v in cl._NO_FEATURE.values():
        v.clear()
    yield
    for v in cl._NO_FEATURE.values():
        v.clear()


def test_web_tools_fetch_and_json_mode_in_request(_fresh_features):
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=ok_body('{"a":1}'))

    c = make(handler)
    asyncio.run(c.complete("x/y", [], temperature=0, max_tokens=100, web_search=True, web_fetch=3, json_mode=True))
    b = seen[0]
    assert b["tools"] == [{"type": "openrouter:web_search", "parameters": {"max_results": 5, "max_uses": 3, "max_total_results": 15}},
                          {"type": "openrouter:web_fetch", "parameters": {"max_uses": 3, "max_content_tokens": 8000}}]
    assert b["response_format"] == {"type": "json_object"} and "plugins" not in b
    asyncio.run(c.complete("perplexity/sonar-pro", [], temperature=0, max_tokens=100, web_search=True))
    assert "tools" not in seen[1] and "plugins" not in seen[1]  # у Perplexity поиск встроенный
    asyncio.run(c.complete("x/y", [], temperature=0, max_tokens=100, web_search=True, web_tools=False))
    assert seen[2]["plugins"] == [{"id": "web"}] and "tools" not in seen[2]


def test_unsupported_feature_is_dropped_and_remembered(_fresh_features):
    seen = []

    def handler(req):
        b = json.loads(req.content)
        seen.append(b)
        if "response_format" in b:
            return httpx.Response(400, json={"error": {"message": "response_format is not supported by this provider"}})
        if any(t["type"] == "openrouter:web_fetch" for t in b.get("tools", [])):
            return httpx.Response(400, json={"error": {"message": "tool openrouter:web_fetch not supported"}})
        return httpx.Response(200, json=ok_body('{"a":1}'))

    c = make(handler, retries=0)
    comp = asyncio.run(c.complete("x/y", [], temperature=0, max_tokens=100, web_search=True, web_fetch=2, json_mode=True))
    assert comp.text == '{"a":1}' and len(seen) == 3
    assert seen[-1]["tools"] == [{"type": "openrouter:web_search", "parameters": {"max_results": 5, "max_uses": 3, "max_total_results": 15}}]
    asyncio.run(c.complete("x/y", [], temperature=0, max_tokens=100, web_fetch=2, json_mode=True))
    assert "response_format" not in seen[-1] and "tools" not in seen[-1] and len(seen) == 4  # запомнено
