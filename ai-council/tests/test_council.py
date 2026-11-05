import asyncio
import json

import pytest

from council.client import Completion, ProviderError
from council.config import CouncilConfig
from council.mock import MockClient
from council.report import render_markdown
from council.schemas import CritiqueSet
from council.session import BudgetExceeded, Council, CouncilError
from council.voting import extract_json, normalize_critique, normalize_ranking, tally


def make_cfg(**over):
    data = {
        "chair": "a",
        "members": [{"name": n, "model": f"m/{n}"} for n in ("a", "b", "c", "d")],
        "json_retries": 1,
    }
    data.update(over)
    return CouncilConfig.model_validate(data)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- parsing / voting


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Вот ответ:\n```json\n{"a": {"b": 2}}\n```\nготово') == {"a": {"b": 2}}
    assert extract_json('мысли {не json} и потом {"ok": true}') == {"ok": True}
    with pytest.raises(ValueError):
        extract_json("без json")


def test_normalize_ranking():
    assert normalize_ranking(["Вариант B", "a", "B", "Option C"], ["A", "B", "C"]) == ["B", "A", "C"]
    with pytest.raises(ValueError):
        normalize_ranking(["A"], ["A", "B"])


def test_normalize_critique_maps_keys():
    cs = CritiqueSet.model_validate(
        {"reviews": [{"label": "a", "scores": {"цена": 5, "риск": 7}}, {"label": "Z", "scores": {}},
                     {"label": "B", "scores": {"x": 1, "y": 2}}]}
    )
    out = normalize_critique(cs, ["A", "B"], ["Цена", "Риск"])
    assert [r.label for r in out.reviews] == ["A", "B"]
    assert out.reviews[0].scores == {"Цена": 5, "Риск": 7}
    assert out.reviews[1].scores == {"Цена": 1, "Риск": 2}  # сопоставлено по порядку
    with pytest.raises(ValueError):
        normalize_critique(cs, ["A", "B", "C"], ["Цена", "Риск"])


def test_borda_normalized_and_tiebreak():
    ballots = {"a": ["B", "C"], "b": ["C", "A"], "c": ["B", "A"]}
    rows = tally(["A", "B", "C"], ballots, {})
    assert [r.label for r in rows] == ["B", "C", "A"]
    assert rows[0].borda == 1.0 and rows[0].first_places == 2
    assert rows[-1].borda == 0.0


# ---------------------------------------------------------------- full sessions


def test_debate_session_full_flow():
    client = MockClient(delay=0)
    res = run(Council(make_cfg(), client, seed=1).run("Вопрос?"))
    assert res.mode == "debate"
    assert res.criteria == ["Польза", "Стоимость", "Риски"]
    assert sorted(res.authors.values()) == ["a", "b", "c", "d"]
    assert all("доработан" in p.title for p in res.proposals.values())
    # никто не голосует и не критикует свой вариант
    own = {name: label for label, name in res.authors.items()}
    for voter, b in res.ballots.items():
        assert own[voter] not in b.ranking and len(b.ranking) == 3
    for reviewer, cs in res.critiques.items():
        assert own[reviewer] not in [r.label for r in cs.reviews]
    assert res.verdict.winner == res.tally[0].label
    assert res.total_cost > 0
    assert "Решение совета" in render_markdown(res)
    json.dumps(res.to_dict(), ensure_ascii=False)
    stages = [c["meta"]["stage"] for c in client.calls]
    assert stages.count("proposal") == 4 and stages.count("revision") == 4 and stages[-1] == "verdict"


def test_quick_mode_skips_critique():
    client = MockClient(delay=0)
    res = run(Council(make_cfg(), client).run("Q", mode="quick", criteria=["x", "y"]))
    stages = {c["meta"]["stage"] for c in client.calls}
    assert stages == {"proposal", "vote", "verdict"}
    assert res.criteria == ["x", "y"] and not res.critiques


def test_evaluate_mode_with_user_options():
    client = MockClient(delay=0)
    res = run(Council(make_cfg(), client).run("Что выбрать?", options=["Вариант раз", "Вариант два", "Вариант три"]))
    assert res.mode == "evaluate"
    assert list(res.proposals) == ["A", "B", "C"] and set(res.authors.values()) == {None}
    assert all(len(b.ranking) == 3 for b in res.ballots.values())
    assert "revision" not in {c["meta"]["stage"] for c in client.calls}


def test_failed_member_is_dropped_but_session_continues():
    client = MockClient(delay=0, fail_models={"m/b"})
    res = run(Council(make_cfg(), client).run("Q"))
    assert "b" not in res.authors.values() and len(res.proposals) == 3
    assert res.stats["b"].failures


def test_no_quorum():
    cfg = make_cfg(min_quorum=3)
    client = MockClient(delay=0, fail_models={"m/b", "m/c"})
    with pytest.raises(CouncilError, match="кворум"):
        run(Council(cfg, client).run("Q"))


def test_chair_failure_falls_back_to_vote():
    client = MockClient(delay=0, fail_models={"m/a"})
    res = run(Council(make_cfg(), client).run("Q"))
    assert res.verdict.winner == res.tally[0].label
    assert res.criteria  # критерии по умолчанию


class BrokenJsonOnce(MockClient):
    """Первый ответ каждого участника на proposal — мусор; проверяем повтор с подсказкой."""

    def __init__(self):
        super().__init__(delay=0)
        self.seen = set()

    async def complete(self, model, messages, **kw):
        meta = kw.get("meta") or {}
        if meta.get("stage") == "proposal" and model not in self.seen:
            self.seen.add(model)
            return Completion(text="Извините, вот мой ответ без JSON", model=model)
        return await super().complete(model, messages, **kw)


def test_invalid_json_is_retried():
    client = BrokenJsonOnce()
    council = Council(make_cfg(), client)
    res = run(council.run("Q", mode="quick"))
    assert len(res.proposals) == 4
    assert any("error" in e for e in res.transcript)


def test_budget_limit_stops_session():
    cfg = make_cfg(max_cost_usd=0.003)  # mock: $0.001 за вызов
    with pytest.raises(BudgetExceeded):
        run(Council(cfg, MockClient(delay=0)).run("Q"))


def test_config_validation():
    with pytest.raises(ValueError):
        make_cfg(chair="nobody")
    with pytest.raises(ValueError):
        CouncilConfig.model_validate({"chair": "a", "members": [{"name": "a", "model": "x"}]})


def test_load_dotenv(tmp_path, monkeypatch):
    import os
    from council.config import load_dotenv
    for k in ("A1", "A2", "A3", "A4"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("A4", "keep")
    (tmp_path / ".env").write_text('# c\nA1=plain  # comment\nA2="quoted # not comment"\nexport A3=x:y\nA4=override\n')
    load_dotenv(tmp_path / ".env")
    assert (os.environ["A1"], os.environ["A2"], os.environ["A3"], os.environ["A4"]) == ("plain", "quoted # not comment", "x:y", "keep")
