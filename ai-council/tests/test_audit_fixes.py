"""Исправления по внешнему аудиту 16.11: F01, F02, F03, F04, F06, F10."""
import asyncio
import json

from council import selection as S
from council.checkpoint import Journal
from council.session import same_model


def test_f10_truncated_tail_does_not_swallow_next_record(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text('{"t":"ok"', encoding="utf-8")  # авария посреди записи
    j = Journal(p)
    j._write({"t": "cost", "member": "a", "cost": 0.4})
    j2 = Journal(p)
    assert [c["cost"] for c in j2.costs] == [0.4]


def test_f02_loose_answer_only_for_same_model(tmp_path):
    p = tmp_path / "j.jsonl"
    p.write_text(json.dumps({"t": "entry", "entry": {"stage": "proposal", "member": "a", "call": 0,
                                                      "requested": "old/model", "parsed": {"x": 1}}}) + "\n")
    j = Journal(p)
    assert j.loose_model[("proposal", "a", 0)] == "old/model"
    assert same_model("openai/gpt-5:online", "openai/gpt-5") and not same_model("old/model", "different/model")


def test_f03_negated_quote_does_not_support_yes():
    a = S.Attribute(id="A1", name="GMP", kind="bool")
    assert not S.quote_supports(a, "да", "", "The company is not GMP certified.")
    assert not S.quote_supports(a, "yes", "", "该公司未取得GMP认证证书")
    assert S.quote_supports(a, "да", "", "The company is GMP certified since 2015, No. 8 workshop")
    assert S.quote_supports(a, "нет", "", "The company is not GMP certified.")


def test_f06_leader_change_shown_even_for_small_weight_change():
    attrs = [S.Attribute(id="A1", name="x", kind="bool", better="yes", weight=1),
             S.Attribute(id="A2", name="y", kind="bool", better="yes", weight=0.9)]
    t = {"A": {"A1": {"status": "document", "value": "да"}, "A2": {"status": "document", "value": "нет"}},
         "B": {"A1": {"status": "document", "value": "нет"}, "A2": {"status": "document", "value": "да"}}}
    sens = S.sensitivity(attrs, t, {"A": "A", "B": "B"})
    assert sens and sens[0]["a"] == "A" and sens[0]["b"] == "B"


def test_f01_budget_rechecked_after_queue(tmp_path):
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.session import BudgetExceeded, Council
    from council.schemas import Proposal
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": str(tmp_path), "max_concurrency": 1,
                                        "max_cost_usd": 1.0, "budget_reserve": True,
                                        "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]})
    c = Council(cfg, MockClient(delay=0.05))
    c._call_estimate = lambda *a, **k: 0.4
    sent = []
    orig = c._complete

    async def comp(member, *a, **k):
        sent.append(member.name)
        r = await orig(member, *a, **k)
        r.cost = 0.4  # каждый ответ стоит ровно столько, сколько оценено
        return r
    c._complete = comp

    async def go():
        ms = [cfg.member(n) for n in "abc"]
        return await asyncio.gather(*(c._ask(m, "proposal", "q", Proposal) for m in ms), return_exceptions=True)
    res = asyncio.run(go())
    assert len(sent) <= 2 and any(isinstance(r, BudgetExceeded) for r in res)


def test_no_candidate_limit_zero():
    """«Без предела кандидатов»: max_candidates 0 — в отсев идут все найденные, без отрезки."""
    from council.config import SelectionConfig
    assert SelectionConfig(max_candidates=0).max_candidates == 0


def test_rewind_journal_forgets_and_strict(tmp_path):
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    j.ok("k1", {"a": 1}, "screen")
    j.ok("k2", {"b": 2}, "fill")
    j.rewind(["screen"], ["fill"])
    j2 = Journal(p)
    assert "k1" not in j2.results and "k2" in j2.results and {"screen", "fill"} <= j2.strict


def test_foreign_site_not_merged_and_passes_order_and_deep_unlimited():
    from council.modes import selection as SM
    from council.config import SelectionConfig
    found = [(f"a{i}", S.Candidate(name=f"Company {n}", website="https://foodtalks.cn/news/1")) for i, n in
             enumerate(["Alpha", "Beta", "Gamma"])]
    found += [("b", S.Candidate(name="Zeta Bio", website="https://zeta.cn")),
              ("c", S.Candidate(name="泽塔生物", website="http://www.zeta.cn/about"))]
    ents = S.merge_candidates(found)
    assert len(ents) == 4  # три с чужим сайтом не склеены, у Zeta английское и китайское — одна компания
    assert all(not e["website"] for e in ents if "Company" in e["name"])
    e1 = {"id": "O1", "angles": ["x"], "screen": {"result": "unclear", "checks": [{"verdict": "pass"}] * 4}}
    e2 = {"id": "O2", "angles": ["x"], "screen": {"result": "unclear", "checks": [{"verdict": "pass"}]}}
    assert [e["id"] for e in SM.diverse_order([e2, e1])] == ["O1", "O2"]
    assert SelectionConfig(max_deep=0).max_deep == 0
