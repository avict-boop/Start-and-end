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


def test_key_limit_403_is_recognized():
    import json as _json

    from council.client import KeyLimit, _key_error
    body = _json.dumps({"error": {"message": "Key limit exceeded (weekly limit). $50.38 of $50.00 used", "code": 403,
                                  "metadata": {"limit_source": "openrouter_key_limit", "scope": "key_limit", "limit_usd": 50,
                                               "spend_usd": 50.378, "resets_at": "2026-11-16T00:00:00.000Z"}}})
    e = _key_error(403, body, "m/x")
    assert isinstance(e, KeyLimit) and isinstance(e, OutOfCredits)
    assert "$50.38 из $50" in str(e) and "16.11.2026" in str(e) and classify(e)[0] == "key_limit"
    assert _key_error(403, '{"error": {"message": "region not supported"}}', "m/x") is None
    assert isinstance(_key_error(401, "{}", "m/x"), KeyLimit)


def test_key_limit_mid_session_pauses_instead_of_hollow_result(tmp_path):
    from council.client import KeyLimit
    j = tmp_path / "j.jsonl"
    client = CountingMock(fail_after=10, exc=KeyLimit("достигнут лимит расходов API-ключа OpenRouter"))
    with pytest.raises(KeyLimit):
        run(Council(make_cfg(), client, seed=2, journal=Journal(j)).run("Вопрос?"))
    res = run(Council(make_cfg(), CountingMock(), seed=2, journal=Journal(j)).run("Вопрос?"))
    assert res.ballots and not any(s.failures for s in res.stats.values())


def test_whole_stage_failure_pauses():
    from council.session import CouncilError

    class VoteDown(MockClient):
        async def complete(self, model, messages, **kw):
            if (kw.get("meta") or {}).get("stage") == "vote":
                from council.client import ProviderError
                raise ProviderError(f"{model}: HTTP 500")
            return await super().complete(model, messages, **kw)
    with pytest.raises(CouncilError, match="Голосование"):
        run(Council(make_cfg(), VoteDown(delay=0), seed=1).run("Вопрос?"))


def test_redo_archived_session_reuses_paid_answers(tmp_path):
    """Заседание дошло до архива со сбоями (модель c не отвечала) — «доделать»: старые ответы из стенограммы."""
    broken = CountingMock()
    broken.fail_models = {"m/c"}
    old = run(Council(make_cfg(), broken, seed=4).run("Вопрос?"))
    assert any(s.failures for s in old.stats.values())
    data = old.to_dict()
    j = Journal(None)
    j.load_transcript(data["transcript"])
    fresh = CountingMock()
    c = Council(make_cfg(), fresh, seed=99, journal=j, labels={k: v for k, v in data["authors"].items() if v})
    res = run(c.run("Вопрос?"))
    assert c.replayed > 0 and fresh.n < broken.n + 20
    assert "c" in res.authors.values() and len(res.proposals) == 4  # пропущенный вариант модели c доспросили
    assert {k: v for k, v in res.authors.items() if v != "c"} == old.authors  # прежние варианты — под прежними метками


def test_stale_pause_next_to_newer_archive_record_is_dropped(tmp_path):
    import os
    import time
    cp = Checkpoint.create(tmp_path, "s2", cfg=make_cfg(), question="В?", mode="quick", rounds=1, criteria=None,
                           options=None, context=None, attachments=[], history=[], followup_of="", vision={},
                           request={}, origin="web")
    assert Checkpoint.load(tmp_path, "s2") is not None
    (tmp_path / "s2.json").write_text("{}")  # итог сохранён, а пауза не снята (процесс прервался на отчётах)
    later = time.time() + 5
    os.utime(tmp_path / "s2.json", (later, later))
    assert Checkpoint.load(tmp_path, "s2") is None and Checkpoint.all(tmp_path) == [] and not cp.dir.exists()


def test_resume_after_program_update_reuses_paid_answers(tmp_path, monkeypatch):
    """Программу обновили между паузой и продолжением, тексты запросов изменились — оплаченные ответы
    всё равно берутся из журнала (по участнику, этапу и номеру обращения), заседание не начинается заново."""
    from council import prompts

    j = tmp_path / "j.jsonl"
    first = CountingMock()
    with pytest.raises(BudgetExceeded):
        run(Council(make_cfg(max_cost_usd=0.0075), first, seed=7, journal=Journal(j)).run("Вопрос?"))
    orig = prompts.system
    monkeypatch.setattr(prompts, "system", lambda *a, **k: orig(*a, **k) + "\n(новая версия программы)")
    second = CountingMock()
    c = Council(make_cfg(max_cost_usd=5), second, seed=7, journal=Journal(j))
    res = run(c.run("Вопрос?"))
    assert c.replayed == first.n and res.verdict.winner


def test_continued_long_answer_is_replayed(tmp_path):
    """Длинный ответ, собранный из продолжений, тоже берётся из стенограммы."""
    j = Journal(None)
    j.load_transcript([{"stage": "proposal", "member": "a", "call": 0, "continuation": 2, "parsed": {"x": 1}},
                       {"stage": "proposal", "member": "a", "attempt": 1, "error": "обрезан"},
                       {"stage": "critique", "member": "a", "parsed": {"y": 2}}])
    assert j.loose[("proposal", "a", 0)] == {"x": 1} and j.loose[("critique", "a", 0)] == {"y": 2}
