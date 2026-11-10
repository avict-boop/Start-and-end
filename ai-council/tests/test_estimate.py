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
