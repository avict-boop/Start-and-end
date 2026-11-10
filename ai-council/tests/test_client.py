import asyncio
import json

import httpx
import pytest

from council.client import OpenRouterClient, ProviderError


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
