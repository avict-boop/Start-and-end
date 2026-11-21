"""Экономия: компактные варианты (переключатель) и кэширование общего начала запросов."""
import asyncio
import json

import httpx
import pytest

from council.attachments import load_attachment
from council.client import OpenRouterClient
from council.config import CouncilConfig
from council.mock import MockClient
from council.session import Council


class Recorder(MockClient):
    def __init__(self):
        super().__init__(delay=0)
        self.msgs = []

    async def complete(self, model, messages, **kw):
        self.msgs.append((model, (kw.get("meta") or {}).get("stage"), messages))
        return await super().complete(model, messages, **kw)


def cfg(**over):
    data = {"chair": "a", "members": [{"name": "a", "model": "anthropic/claude-x"}, {"name": "b", "model": "m/b"},
                                      {"name": "c", "model": "m/c"}, {"name": "d", "model": "m/d"}]}
    data.update(over)
    return CouncilConfig.model_validate(data)


def text_of(content):
    return content if isinstance(content, str) else "".join(p.get("text", "") for p in content)


def test_compact_mode_prompts():
    client = Recorder()
    res = asyncio.run(Council(cfg(compact_proposals=True, compact_words=500), client, seed=1).run(
        "Сделай отчёт из 8 разделов с таблицами"))
    by_stage = {}
    for _model, stage, msgs in client.msgs:
        by_stage.setdefault(stage, text_of(msgs[1]["content"]))
    assert "COMPACT MODE" in by_stage["proposal"] and "about 500 words" in by_stage["proposal"]
    assert "COMPACT MODE" in by_stage["revision"]
    assert "compact on purpose" in by_stage["critique"] and "compact on purpose" in by_stage["vote"]
    assert "written compactly on purpose" in by_stage["verdict"] and "exactly the structure" in by_stage["verdict"]
    assert "follow it exactly" not in by_stage["proposal"]  # формат запроса — только в итоге
    assert res.verdict.final_answer


def test_normal_mode_keeps_format_rule():
    client = Recorder()
    asyncio.run(Council(cfg(), client, seed=1).run("Вопрос?"))
    prop = next(text_of(m[1]["content"]) for _, st, m in client.msgs if st == "proposal")
    assert "follow it exactly" in prop and "COMPACT MODE" not in prop


def test_system_prompt_and_prefix_are_stable_across_stages():
    client = Recorder()
    doc = load_attachment(("Данные отчёта. " * 600).encode(), "data.txt")
    asyncio.run(Council(cfg(), client, seed=1).run("Как назвать кофейню?", attachments=[doc]))
    for member in ("m/b", "m/c"):
        calls = [(st, m) for model, st, m in client.msgs if model == member and st in
                 ("proposal", "critique", "revision", "vote")]
        systems = {m[0]["content"] for _, m in calls}
        assert len(systems) == 1, "системная инструкция должна быть одинаковой на всех этапах"
        texts = [text_of(m[1]["content"]) for st, m in calls if st != "vote"]
        prefix = texts[0][:texts[0].index("Question for the council:") + 200]
        assert all(t.startswith(prefix[:len(prefix) - 150]) for t in texts)  # документы + вопрос — общее начало


def test_explicit_cache_mark_for_claude():
    client = Recorder()
    doc = load_attachment(("Данные отчёта. " * 600).encode(), "data.txt")
    asyncio.run(Council(cfg(chair="b"), client, seed=1).run("Вопрос?", attachments=[doc]))
    claude = [m for model, st, m in client.msgs if model == "anthropic/claude-x" and st == "critique"][0]
    parts = claude[1]["content"]
    assert isinstance(parts, list) and parts[0].get("cache_control") == {"type": "ephemeral"}
    assert "Данные отчёта" in parts[0]["text"] and "Question for the council" in parts[0]["text"]
    assert "Review each of these" in parts[1]["text"]
    other = [m for model, st, m in client.msgs if model == "m/b" and st == "critique"][0]
    assert isinstance(other[1]["content"], str)  # остальным — обычный текст (кэш у них автоматический)


def test_cached_tokens_counted():
    def handler(req):
        return httpx.Response(200, json={"model": "x", "choices": [{"message": {"content": "{}"}}],
                                         "usage": {"prompt_tokens": 10000, "completion_tokens": 10, "cost": 0.01,
                                                   "prompt_tokens_details": {"cached_tokens": 8000}}})

    c = OpenRouterClient("k", transport=httpx.MockTransport(handler), retry_base_delay=0)
    comp = asyncio.run(c.complete("m", [], temperature=0, max_tokens=10))
    assert comp.cached_tokens == 8000
    from council.reports.view import View
    v = View({"stats": {"a": {"prompt_tokens": 10000, "cached_tokens": 8000}}})
    assert "8 000 из 10 000" in v.cache_line and "80%" in v.cache_line


def test_web_compact_flag_and_estimate(tmp_path, monkeypatch):
    import yaml
    from fastapi.testclient import TestClient
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "chair": "a", "runs_dir": str(tmp_path / "runs"), "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]}))
    from council.web.app import create_app
    with TestClient(create_app(tmp_path / "config.yaml", mock_delay=0)) as c:
        assert c.get("/api/config").json()["compact"] == {"enabled": False, "words": 700}
        sid = c.post("/api/sessions", json={"question": "Вопрос?", "mock": True, "mode": "quick",
                                            "compact": True}).json()["id"]
        with c.stream("GET", f"/api/sessions/{sid}/events") as r:
            list(r.iter_lines())
        data = json.loads((tmp_path / "runs" / f"{sid}.json").read_text())
        assert data["request"]["compact_proposals"] is True
        assert "компактные" in c.get(f"/api/sessions/{sid}/report?format=md").text
        assert c.post(f"/api/sessions/{sid}/repeat").json()["request"]["compact_proposals"] is True


def test_estimate_compact_is_cheaper():
    from council.estimate import Price, estimate
    base = cfg()
    pr = {m.model: Price(2e-6, 1e-5) for m in base.members}
    learned = {"proposal": 7000, "revision": 8000, "verdict": 5000, "sessions": 5}
    normal = estimate(base, pr, mode="debate", rounds=1, question_chars=3000, lengths=learned)
    compact = estimate(cfg(compact_proposals=True), pr, mode="debate", rounds=1, question_chars=3000)
    assert compact.low < normal.low / 2


@pytest.mark.parametrize("compact", [False, True])
def test_learn_lengths_separates_modes(tmp_path, compact):
    from council.estimate import learn_lengths
    for i, (flag, size) in enumerate([(False, 7000), (True, 1500)]):
        tr = [{"member": "a", "stage": "proposal", "tokens": [100, size], "parsed": {}}]
        (tmp_path / f"2026010{i}-000000-s.json").write_text(json.dumps(
            {"transcript": tr, "request": {"compact_proposals": flag}, "mode": "debate", "stats": {}}))
    got = learn_lengths(tmp_path, compact=compact)
    assert got["proposal"] == (1500 if compact else 7000) and got["sessions"] == 1
