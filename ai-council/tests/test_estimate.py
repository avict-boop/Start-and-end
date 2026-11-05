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
