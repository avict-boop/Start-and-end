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
