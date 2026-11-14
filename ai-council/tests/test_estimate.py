import asyncio
from collections import Counter

import pytest

from council.estimate import estimate, parse_pricing, plan_calls, unknown_models
from council.mock import MockClient
from council.session import Council
from tests.test_council import make_cfg


@pytest.mark.parametrize(
    "mode,rounds,options,criteria",
    [("debate", 1, None, None), ("debate", 2, None, ["x"]), ("quick", 1, None, None),
     ("evaluate", 1, ["o1", "o2", "o3"], None)],
)
def test_plan_matches_real_session(mode, rounds, options, criteria):
    cfg = make_cfg()
    client = MockClient(delay=0)
    asyncio.run(Council(cfg, client).run("Q", mode=mode, rounds=rounds, options=options, criteria=criteria))
    actual = Counter((c["meta"]["member"], c["meta"]["stage"]) for c in client.calls)
    plan = plan_calls(cfg, "evaluate" if options else mode, rounds, len(options) if options else None, bool(criteria))
    expected = Counter((name, stage) for name, calls in plan.items() for stage, _ in calls)
    assert actual == expected


def test_estimate_cost_and_unknown():
    cfg = make_cfg()
    models = [{"id": "m/a", "pricing": {"prompt": "0.000001", "completion": "0.000002"}},
              {"id": "m/b", "pricing": {"prompt": "0", "completion": "0"}},
              {"id": "m/cc", "pricing": {}}]
    pricing = parse_pricing(models)
    est = estimate(cfg, pricing, mode="debate", rounds=1)
    by = {m.name: m for m in est.members}
    assert by["a"].cost == pytest.approx(by["a"].prompt_tokens * 1e-6 + by["a"].completion_tokens * 2e-6)
    assert by["b"].cost == 0 and by["d"].cost is None
    assert est.high > est.low > 0
    missing = unknown_models(cfg, set(pricing))
    assert set(missing) == {"m/c", "m/d"} and "m/cc" in missing["m/c"]


def test_plan_calls_with_judge_chair():
    from council.config import CouncilConfig, with_overrides
    from council.estimate import plan_calls
    members = [{"name": n, "model": f"m/{n}"} for n in ("a", "b", "c", "d", "e")]
    cfg = CouncilConfig.model_validate({"chair": "a", "members": members})
    total = lambda c, mode, n=None: sum(len(v) for v in plan_calls(c, mode, 1, n, False).values())  # noqa: E731
    assert (total(cfg, "debate"), total(cfg, "quick"), total(cfg, "evaluate", 3)) == (20, 11, 12)
    plan = plan_calls(cfg, "debate", 1, None, False)
    assert ("critique", 4) in plan["a"] and ("vote", 4) in plan["a"] and not any(s == "proposal" for s, _ in plan["a"])
    assert ("critique", 3) in plan["b"]
    assert total(with_overrides(cfg, chair_proposes=True), "debate") == 22


def test_plan_matches_real_calls_with_judge_chair():
    cfg = make_cfg(chair_proposes=False)
    client = MockClient(delay=0)
    asyncio.run(Council(cfg, client, seed=1).run("Вопрос?"))
    real = Counter((c["meta"]["member"], c["meta"]["stage"]) for c in client.calls)
    plan = Counter((name, stage) for name, items in plan_calls(cfg, "debate", 1, None, False).items() for stage, _ in items)
    assert real == plan


def test_fact_check_in_plan_and_estimate():
    from council.estimate import Price
    cfg = make_cfg(chair_proposes=False, fact_check={"enabled": True, "top": 2})
    client = MockClient(delay=0)
    asyncio.run(Council(cfg, client, seed=1).run("Вопрос?"))
    real = Counter((c["meta"]["member"], c["meta"]["stage"]) for c in client.calls)
    plan = Counter((name, stage) for name, items in plan_calls(cfg, "debate", 1, None, False).items() for stage, _ in items)
    assert real == plan and plan[("факт-чек", "factcheck")] == 2
    pricing = {m.model: Price(1e-6, 2e-6) for m in cfg.members}
    pricing["perplexity/sonar-pro"] = Price(3e-6, 15e-6, request=0.005)
    est = estimate(cfg, pricing, mode="debate", rounds=1)
    fc = next(m for m in est.members if m.name == "факт-чек")
    assert fc.calls == 2 and fc.cost > 2 * 0.005


def test_catalog_upgrades_and_families():
    from council.catalog import family, upgrades
    from council.config import CouncilConfig
    assert family("anthropic/claude-sonnet-4.5") == family("anthropic/claude-sonnet-5") == "anthropic/claude-sonnet"
    assert family("google/gemini-2.5-pro") == family("google/gemini-3-pro-preview") == "google/gemini-pro"
    assert family("openai/gpt-5") == family("openai/gpt-5.1") == "openai/gpt"
    assert family("google/gemini-2.5-pro") != family("google/gemini-2.5-flash")
    cat = [
        {"id": "anthropic/claude-sonnet-4.5", "created": 1_750_000_000},
        {"id": "anthropic/claude-sonnet-5", "created": 1_780_000_000, "architecture": {"input_modalities": ["text", "image"]}},
        {"id": "anthropic/claude-sonnet-5:batch", "created": 1_780_000_000},
        {"id": "anthropic/claude-opus-5", "created": 1_781_000_000},
        {"id": "anthropic/claude-3-haiku", "created": 1_700_000_000},
        {"id": "google/gemini-2.5-pro", "created": 1_745_000_000},
        {"id": "google/gemini-3-image", "created": 1_790_000_000, "architecture": {"output_modalities": ["image"]}},
        {"id": "deepseek/deepseek-chat", "created": 1_790_000_000},
    ]
    cfg = CouncilConfig.model_validate({"chair": "claude", "members": [
        {"name": "claude", "model": "anthropic/claude-sonnet-4.5"}, {"name": "gemini", "model": "google/gemini-2.5-pro"},
        {"name": "old", "model": "x/gone-model"}]})
    u = {x["name"]: x for x in upgrades(cfg, cat)}
    assert [m["id"] for m in u["claude"]["same_family"]] == ["anthropic/claude-sonnet-5"]
    assert [m["id"] for m in u["claude"]["newest"]] == ["anthropic/claude-opus-5"]  # старый haiku не предлагается
    assert u["gemini"]["same_family"] == [] and u["gemini"]["newest"] == []  # модель только для картинок — не участник
    assert u["old"]["found"] is False


def _pricing(cfg):
    from council.estimate import Price
    return {m.model: Price(1e-6, 1e-5) for m in cfg.members}


def test_estimate_counts_question_and_answer_length():
    cfg = make_cfg(chair_proposes=False)
    base = estimate(cfg, _pricing(cfg), mode="debate", rounds=1)
    long_q = estimate(cfg, _pricing(cfg), mode="debate", rounds=1, question_chars=9000)
    assert long_q.low > base.low * 1.1 and long_q.question_tokens == 3900  # вопрос уходит в каждый запрос
    long_answers = estimate(cfg, _pricing(cfg), mode="debate", rounds=1,
                            lengths={"proposal": 8000, "revision": 9000, "verdict": 6000, "sessions": 5})
    assert long_answers.low > base.low * 1.7  # чужие варианты читают все — цена растёт с их длиной
    assert long_answers.high / long_answers.low == pytest.approx(1.6)  # по истории — узкий диапазон
    assert base.high / base.low == pytest.approx(2.5)
    assert "по 5 последним заседаниям" in long_answers.basis and "вариант ≈ 8 000" in long_answers.basis


def test_learn_lengths_from_transcripts(tmp_path):
    import json
    from council.estimate import learn_lengths, similar_actual
    for i in range(3):
        tr = [
            # предложение: обрезано и продолжено — 2 записи, принята одна → 6000 токенов на ответ
            {"member": "a", "stage": "proposal", "tokens": [500, 4000]},
            {"member": "a", "stage": "proposal", "tokens": [900, 2000], "parsed": {}},
            {"member": "b", "stage": "proposal", "tokens": [500, 6000], "parsed": {}},
            {"member": "a", "stage": "critique", "tokens": [5000, 900], "parsed": {"reviews": [{}, {}, {}]}},
            {"member": "c", "stage": "verdict", "tokens": [9000, 5000], "parsed": {}},
        ]
        (tmp_path / f"2026010{i}-000000-s.json").write_text(json.dumps(
            {"transcript": tr, "total_cost_usd": 1.0 + i, "mode": "debate", "stats": {"a": {}, "b": {}, "c": {}}}))
    (tmp_path / "20260109-000000-s.events.jsonl").write_text("{}")  # журналы не мешают
    got = learn_lengths(tmp_path)
    assert got["proposal"] == 6000 and got["critique"] == 300 and got["verdict"] == 5000 and got["sessions"] == 3
    sim = similar_actual(got, "debate", 3)
    assert sim == {"count": 3, "min": 1.0, "max": 3.0, "avg": 2.0}
    assert similar_actual(got, "quick", 3) is None
