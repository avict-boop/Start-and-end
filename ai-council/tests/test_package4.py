"""Пакет 4: обязательные требования, ничья лидеров, решающие голоса, покрытие пар, доработка у председателя,
случайный порядок вариантов, статус «из документов пользователя»."""
import asyncio

from council import prompts
from council.config import CouncilConfig
from council.mock import MockClient
from council.reports.html import render_html
from council.reports.md import render_md
from council.reports.view import View
from council.schemas import Framing, Proposal, Revision
from council.session import Council, normalize_status
from council.voting import co_leaders, coverage, decisive_ballots, tally


class Recorder(MockClient):
    def __init__(self):
        super().__init__(delay=0)
        self.msgs = []

    async def complete(self, model, messages, **kw):
        c = messages[1]["content"]
        self.msgs.append(((kw.get("meta") or {}).get("stage"), c if isinstance(c, str) else str(c)))
        return await super().complete(model, messages, **kw)


def cfg():
    return CouncilConfig.model_validate({"chair": "a", "members": [{"name": n, "model": f"m/{n}"} for n in "abcd"]})


def test_requirements_flow_through_all_stages():
    client = Recorder()
    res = asyncio.run(Council(cfg(), client, seed=1).run("Дай отчёт с таблицей"))
    assert res.requirements == ["Ответ на русском языке", "Привести таблицу с цифрами"]
    by = {}
    for stage, text in client.msgs:
        by.setdefault(stage, text)
    for stage in ("proposal", "critique", "revision", "vote", "verdict"):
        assert "R2. Привести таблицу с цифрами" in by[stage], stage
    assert "VIOLATES Rn" in by["critique"] and "ranks below every proposal" in by["vote"]
    assert "satisfy every one of them" in by["verdict"]
    html = render_html(View(res.to_dict()))
    assert "Обязательные требования" in html and "Привести таблицу с цифрами" in html
    assert "Обязательные требования" in render_md(View(res.to_dict()))


def test_framing_requirements_parsing():
    f = Framing.model_validate({"refined_question": "Q", "criteria": ["a"], "must_have": "таблица"})
    assert f.requirements == ["таблица"]
    f = Framing.model_validate({"refined_question": "Q", "criteria": ["a"], "requirements": [f"r{i}" for i in range(50)]})
    assert len(f.requirements) == 40  # предел схемы; рабочий предел — max_requirements
    assert Framing.model_validate({"refined_question": "Q", "criteria": ["a"]}).requirements == []


def test_tie_for_first_place():
    ballots = {"x": ["A", "B"], "y": ["B", "A"]}
    rows = tally(["A", "B"], ballots, {})
    assert co_leaders(rows) == ["A", "B"]
    assert co_leaders(tally(["A", "B", "C"], {"x": ["A", "B", "C"], "y": ["A", "C", "B"]}, {})) == ["A"]
    v = prompts.verdict("Q", ["c"], {"A": Proposal(title="t", summary="s"), "B": Proposal(title="t", summary="s")},
                        "1. A", "", None, leader="A", follow_vote=True, co_leaders=["A", "B"])
    assert "Tie for first place: A, B" in v and "without a deviation reason" in v
    d = {"tally": [{"label": "A"}, {"label": "B"}], "co_leaders": ["A", "B"], "verdict": {"winner": "B"}}
    view = View(d)
    assert view.deviation is None and "Ничья за первое место: A, B" in view.tie_line and "выбрал B" in view.tie_line


def test_decisive_ballots_and_coverage():
    ballots = {"p": ["A", "B", "C"], "q": ["B", "A", "C"], "r": ["C", "B", "A"]}
    assert tally(list("ABC"), ballots, {})[0].label == "B"
    dec = decisive_ballots(list("ABC"), ballots, {})
    assert dec["r"] == "A"
    cov = coverage(list("ABCD"), {"a": ["B", "C", "D"], "b": ["A", "C", "D"], "c": ["A", "B", "D"],
                                  "d": ["A", "B", "C"], "chair": ["A", "B", "C", "D"]})
    assert cov == {"min": 3, "max": 3, "options": 4, "ballots": 5}
    v = View({"coverage": {"min": 1, "max": 1, "options": 2, "ballots": 3}})
    assert "меньше трёх" in v.coverage_line
    v = View({"coverage": cov, "decisive": {"r": "A"}})
    assert "3 раз" in v.coverage_line and v.decisive_notes == ["без бюллетеня r лидером был бы вариант A"]


def test_chair_sees_revisions_and_rules():
    props = {"A": Revision(title="t", summary="s", changes="убрал SQL-инъекцию", not_accepted=["отказ от MVP"])}
    v = prompts.verdict("Q", ["c"], props, "1. A", "", None)
    assert "Changes after critique: убрал SQL-инъекцию" in v and "Critique not accepted by the author: отказ от MVP" in v
    assert "BEFORE revision" in v and "(добавлено председателем)" in v


def test_each_judge_gets_own_order():
    council = Council(cfg(), MockClient(delay=0), seed=7)
    props = {l: Proposal(title=l, summary=l) for l in "ABCDE"}
    orders = {tuple(council._shuffled(props)) for _ in range(20)}
    assert len(orders) > 1 and all(sorted(o) == list("ABCDE") for o in orders)


def test_user_doc_fact_status():
    assert normalize_status("user_doc") == "user_doc" and normalize_status("user_provided") == "user_doc"
    v = View({"fact_check": {"A": {"claims": [{"claim": "x", "status": "user_doc"}, {"claim": "y", "status": "confirmed"}]}}})
    assert "📄 1 из документов пользователя" in v.fact_line
