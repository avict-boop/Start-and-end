"""Приостановка и продолжение: уже полученные ответы не оплачиваются второй раз, итог тот же."""
import asyncio

import pytest

from council.checkpoint import Checkpoint, Journal, classify
from council.client import Completion, OutOfCredits
from council.mock import MockClient
from council.session import BudgetExceeded, Council
from tests.test_council import make_cfg


class CountingMock(MockClient):
    def __init__(self, fail_after: int | None = None, exc: Exception | None = None) -> None:
        super().__init__(delay=0)
        self.n = 0
        self.fail_after = fail_after
        self.exc = exc

    async def complete(self, model, messages, **kw) -> Completion:
        if self.fail_after is not None and self.n >= self.fail_after:
            raise self.exc
        self.n += 1
        return await super().complete(model, messages, **kw)


def run(coro):
    return asyncio.run(coro)


def summary(res):
    return (res.verdict.winner, res.verdict.final_answer, sorted(res.authors.items()),
            {k: v.ranking for k, v in res.ballots.items()}, [i["idea"] for i in res.ideas])


def test_budget_pause_and_resume_gives_the_same_result_without_paying_twice(tmp_path):
    full_client = CountingMock()
    full = run(Council(make_cfg(), full_client, seed=7).run("Вопрос?"))
    total_calls = full_client.n

    j = tmp_path / "j.jsonl"
    cfg = make_cfg(max_cost_usd=0.0075)  # мок: $0.001 за запрос — бюджет кончится посреди заседания
    first = CountingMock()
    with pytest.raises(BudgetExceeded):
        run(Council(cfg, first, seed=7, journal=Journal(j)).run("Вопрос?"))
    assert 0 < first.n < total_calls

    second = CountingMock()
    c = Council(make_cfg(max_cost_usd=5), second, seed=7, journal=Journal(j))
    res = run(c.run("Вопрос?"))
    assert summary(res) == summary(full)
    assert second.n == total_calls - first.n  # оплачено только недостающее
    assert c.replayed == first.n
    assert res.total_cost == pytest.approx(full.total_cost)  # в счёте и то, что потрачено до паузы


def test_out_of_credits_pauses_instead_of_dropping_members(tmp_path):
    j = tmp_path / "j.jsonl"
    client = CountingMock(fail_after=6, exc=OutOfCredits("m/x: на балансе OpenRouter недостаточно средств (HTTP 402)"))
    with pytest.raises(OutOfCredits) as e:
        run(Council(make_cfg(), client, seed=3, journal=Journal(j)).run("Вопрос?"))
    assert classify(e.value)[0] == "credits"
    res = run(Council(make_cfg(), CountingMock(), seed=3, journal=Journal(j)).run("Вопрос?"))
    assert len(res.proposals) == 4 and not any(s.failures for s in res.stats.values())


def test_budget_can_be_raised_during_the_session(tmp_path):
    events = []
    box = {}

    def on_event(ev, **d):
        events.append(ev)
        if ev == "budget_warning":
            box["c"].set_budget(5)

    c = Council(make_cfg(max_cost_usd=0.01), CountingMock(), seed=1, on_event=on_event)
    box["c"] = c
    res = run(c.run("Вопрос?"))
    assert "budget_warning" in events and "budget" in events and res.verdict.winner


def test_checkpoint_roundtrip_and_interrupted_state(tmp_path):
    cfg = make_cfg(max_cost_usd=2)
    cp = Checkpoint.create(tmp_path, "s1", cfg=cfg, question="Вопрос?", mode="debate", rounds=1, criteria=None,
                           options=None, context=None, attachments=[], history=[], followup_of="", vision={},
                           request={}, origin="web")
    again = Checkpoint.load(tmp_path, "s1")
    assert again.config().active_members[0].name == "a" and again.config(budget=7).max_cost_usd == 7
    assert again.state == "interrupted"  # «идёт», но ни один процесс его не ведёт — значит, прервано перезапуском
    with again.lock() as got:
        assert got and Checkpoint.load(tmp_path, "s1").state == "running"
    again.update(status="paused", reason="budget")
    assert [c.summary()["reason"] for c in Checkpoint.all(tmp_path)] == ["budget"]
    again.remove()
    assert Checkpoint.all(tmp_path) == []


def test_resume_after_lost_quorum_asks_failed_models_again(tmp_path):
    from council.session import CouncilError
    j = tmp_path / "j.jsonl"
    broken = MockClient(delay=0, fail_models={"m/a", "m/b", "m/c", "m/d"})
    with pytest.raises(CouncilError, match="кворума"):
        run(Council(make_cfg(), broken, seed=5, journal=Journal(j)).run("Вопрос?"))
    res = run(Council(make_cfg(), CountingMock(), seed=5, journal=Journal(j)).run("Вопрос?"))
    assert len(res.proposals) == 4 and res.verdict.winner
