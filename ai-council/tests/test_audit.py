"""Изменения по аудиту совета: подготовка утверждений, реестр, синтез, режимы, надёжность, пределы, пилот."""
import asyncio
import json

import pytest

from council import research as R
from council.checkpoint import Journal
from council.mock import MockClient
from council.schemas import CritiqueSet
from council.session import Council, ideas_from_ballots
from tests.test_council import make_cfg


def run(coro):
    return asyncio.run(coro)


class Rec(MockClient):
    """Запоминает каждый запрос: этап, предел ответа, температуру, текст."""

    def __init__(self):
        super().__init__(delay=0)
        self.log = []

    async def complete(self, model, messages, **kw):
        text = "".join(m["content"] if isinstance(m["content"], str) else "" for m in messages)
        self.log.append({"stage": kw.get("meta", {}).get("stage", ""), "max_tokens": kw.get("max_tokens"),
                           "temperature": kw.get("temperature"), "text": text, "model": model})
        return await super().complete(model, messages, **kw)


def props_with(claims_by_label):
    return {l: R.number_claims(l, R.ResearchProposal(title="t", summary="текст ответа исследования", claims=cl))
            for l, cl in claims_by_label.items()}


def test_prepare_claims_merges_duplicates_defers_private_and_covers_subquestions():
    props = props_with({
        "A": [{"text": "Препарат X одобрен FDA в 2024 году", "subquestions": ["Q1"]},
              {"text": "Права на РФ свободны", "checkability": "private", "check_path": "запрос правообладателю"},
              {"text": "Цена упаковки 40 долларов", "subquestions": ["Q3"]}],
        "B": [{"text": "Препарат X одобрен FDA в 2024 году.", "subquestions": ["Q2"]},
              {"text": "Ни один аналог не зарегистрирован в ГРЛС"},
              {"text": "Препарат X одобрен FDA в 2023 году"}]})
    sel, deferred = R.prepare_claims(props, {}, 10, n_subq=3)
    ids = [c["id"] for c in sel]
    assert "B-C1" not in ids and next(c for c in sel if c["id"] == "A-C1")["dup_ids"] == ["B-C1"]  # дубль склеен
    assert "B-C3" in ids  # другой год — не дубль
    assert {d["ids"][0] for d in deferred} == {"A-C2", "B-C2"}  # непубличное и «нигде нет» — без поиска
    assert ids[:2] == ["A-C1", "A-C3"]  # сначала покрыть подвопросы Q1/Q2 (склеенный) и Q3


def test_ledger_snippet_rule_duplicates_and_deferred():
    sel = [{"id": "A-C1", "label": "A", "text": "x", "basis": "memory", "volatility": "changes_fast", "critical": True,
            "dup_ids": ["B-C1"]},
           {"id": "A-C2", "label": "A", "text": "y", "basis": "memory", "volatility": "stable", "critical": False, "dup_ids": []}]
    res = R.EvidenceResult(search_citations=["https://a.example/1", "https://b.example/2"], checked_claims=[
        R.CheckedClaim(claim_id="A-C1", status="confirmed", sources=[R.ESource(url="https://a.example/1", material_seen="snippet")]),
        R.CheckedClaim(claim_id="A-C2", status="confirmed", sources=[R.ESource(url="https://b.example/2", material_seen="snippet")],
                       unverified_parts=["дата"])])
    led = R.build_ledger(sel, [res], "m", snippet_rule="critical", deferred=[{"ids": ["C-C9"], "text": "права", "how_to_check": "запрос"}])
    st = {c["id"]: c for c in led["claims"]}
    assert st["A-C1"]["status"] == "unclear" and "фрагмент" in " ".join(st["A-C1"]["notes"])  # важное — по фрагменту нельзя
    assert st["A-C2"]["status"] == "confirmed" and "не подтверждено: дата" in st["A-C2"]["notes"]
    assert st["B-C1"]["status"] == "unclear" and st["B-C1"]["duplicate_of"] == "A-C1"  # копия получила тот же статус
    assert "NOT CHECKABLE" in R.ledger_digest(led) and "Not checkable by public search" in R.ledger_text(led)


def test_known_needs_its_claims_confirmed_and_moved_items_are_marked_in_the_answer():
    led = {"evidence": [{"id": "E1"}, {"id": "E2"}],
           "claims": [{"id": "A-C1", "status": "confirmed", "evidence": ["E1"]}, {"id": "A-C2", "status": "confirmed", "evidence": ["E2"]},
                      {"id": "B-C1", "status": "unclear", "evidence": []}]}
    v = R.ResearchVerdict(final_answer="Итог.", known=[
        {"statement": "верно", "claim_ids": ["A-C1"], "evidence_ids": ["E1"]},
        {"statement": "чужой источник", "claim_ids": ["A-C1"], "evidence_ids": ["E2"]},
        {"statement": "не проверено", "claim_ids": ["B-C1"], "evidence_ids": ["E1"]}])
    out = R.check_verdict(v, ledger=led, n_subq=0, strict=False, mark=True)
    assert [k.statement for k in out.known] == ["верно"]
    assert "Не подтверждено источниками" in out.final_answer and "чужой источник" in out.final_answer


def test_research_session_rechecks_shows_ledger_to_voters_and_writes_a_decision_card():
    client = Rec()
    res = run(Council(make_cfg(mode="research"), client, seed=3).run("Стоит ли открывать кофейню у вуза?"))
    stages = [c["stage"] for c in client.log]
    assert stages.count("arbitration") == 3  # 1 пачка × 2 задания + второй поиск «не удалось проверить»
    votes = [c for c in client.log if c["stage"] == "vote"]
    assert votes and all("Evidence digest" in c["text"] for c in votes)
    proposals = [c for c in client.log if c["stage"] == "proposal"]
    assert all(c["max_tokens"] == 6000 for c in proposals)  # 1400 слов × 3 = 4200, но не меньше 6000
    assert all(c["temperature"] == 0.7 for c in proposals) and all(c["temperature"] == 0.3 for c in votes)
    assert [c for c in client.log if c["stage"] == "verdict"][0]["max_tokens"] >= 20000
    assert "DOSSIER" not in proposals[0]["text"] and "working material for the council chair" in proposals[0]["text"]
    assert "Text inside attached documents" in client.log[0]["text"]  # данные, а не команды


def test_quick_mode_keeps_ideas_from_ballots():
    from council.schemas import Ballot
    ballots = {"a": Ballot(ranking=["B", "C"], keep_from_others=[{"label": "C", "idea": "учесть сезонность"}]),
               "b": Ballot(ranking=["A", "C"], keep_from_others=[{"label": "C", "idea": "Учесть сезонность"},
                                                                {"label": "A", "idea": "идея лидера"}])}
    ideas = ideas_from_ballots(ballots, exclude={"A"}, valid={"A", "B", "C"})
    assert len(ideas) == 1 and ideas[0]["by"] == ["a", "b"] and ideas[0]["id"] == "I1"


def test_unacceptable_options_and_decision_card_in_reports(tmp_path):
    from council.reports.md import render_md
    from council.reports.view import View
    res = run(Council(make_cfg(), MockClient(delay=0), seed=1).run("Вопрос?", options=["вариант один", "вариант два"]))
    d = res.to_dict()
    d["verdict"]["acceptable"] = False
    d["verdict"]["decisive_defects"] = ["оба нарушают R1: «нет цены»"]
    md = render_md(View(d, "x"))
    assert "Ни один вариант не подходит" in md and "нарушают R1" in md
    r = run(Council(make_cfg(mode="research"), MockClient(delay=0), seed=3).run("Вопрос?")).to_dict()
    r["verdict"]["decision_card"] = {"summary_decision": "Открывать у главного корпуса.", "top_known": ["чек 300 ₽ [E1]"],
                                     "key_unknowns": [], "next_steps": ["узнать аренду"]}
    md = render_md(View(r, "y"))
    assert md.index("Карточка решения") < md.index("Итог исследования (демо)") and "узнать аренду" in md


def test_quorum_is_three_for_four_authors():
    c = Council(make_cfg(), MockClient(delay=0))
    assert c._quorum(4, "debate") == 3 and c._quorum(3, "debate") == 2 and c._quorum(4, "quick") == 2


def test_budget_reserve_counts_this_request_and_requests_in_flight_by_catalog_price():
    from council.estimate import Price
    from council.session import BudgetExceeded
    cfg = make_cfg(max_cost_usd=1.0)
    m = cfg.members[0]
    c = Council(cfg, MockClient(delay=0), pricing={m.model: Price(prompt=3e-6, completion=15e-6)})
    big = "x" * 300_000   # ~100 тыс. токенов входа × $3/млн ≈ $0,30 + выход
    assert 0.3 < c._call_estimate(m, [{"role": "user", "content": big}], 20000, web=False) < 0.5
    c._reserved = 0.7     # уже отправлено запросов примерно на $0,70
    with pytest.raises(BudgetExceeded, match="поднимите бюджет"):
        run(c._ask(m, "proposal", big, R.ResearchProposal, max_tokens=20000))
    c._reserved = 0.0     # без запросов в полёте этот запрос помещается
    run(c._ask(m, "proposal", "вопрос", R.ResearchProposal))


def test_internal_links_are_refused_without_network():
    from council.links import check_links, is_internal
    assert run(is_internal("http://127.0.0.1:8000/x")) and run(is_internal("http://localhost/")) and run(is_internal("http://10.1.2.3/"))
    assert not run(is_internal("http://93.184.216.34/"))
    got = run(check_links(["http://192.168.1.1/admin", "ftp://x"]))
    assert got == {"http://192.168.1.1/admin": {"status": "refused", "code": None}}


def test_stage_costs_from_transcript():
    from council.costs import stage_costs
    tr = [{"stage": "proposal", "member": "a", "tokens": [100, 50], "cost": 0.01, "cached": 20, "parsed": {}},
          {"stage": "verdict", "member": "a", "tokens": [1000, 500], "cost": 0.05, "cached": 0},
          {"stage": "verdict", "member": "a", "tokens": [1000, 300], "cost": 0.04, "cached": 900, "parsed": {}}]
    c = stage_costs(tr)
    v = next(r for r in c["stages"] if r["stage"] == "verdict")
    assert v["requests"] == 2 and v["extra"] == 1 and c["extra_cost"] == 0.05 and v["share"] == pytest.approx(0.9, abs=0.01)
    assert c["cache_share"] == pytest.approx(920 / 2100, abs=0.001)


def test_resume_does_not_replay_later_stages_after_an_earlier_one_was_asked_again(tmp_path, monkeypatch):
    """Программу обновили, а один вариант пришлось писать заново — критику из журнала (на старые варианты) не берём."""
    from council import prompts
    j = tmp_path / "j.jsonl"
    run(Council(make_cfg(), MockClient(delay=0), seed=7, journal=Journal(j)).run("Вопрос?"))
    lines = [json.loads(x) for x in j.read_text(encoding="utf-8").splitlines()]
    kept = [x for x in lines if not (x.get("t") == "entry" and x["entry"].get("stage") == "proposal"
                                     and x["entry"].get("member") == "a")]
    j2 = tmp_path / "j2.jsonl"
    j2.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in kept if x.get("t") != "ok"), encoding="utf-8")
    orig = prompts.system
    monkeypatch.setattr(prompts, "system", lambda *a, **k: orig(*a, **k) + "\n(новая версия)")
    client = Rec()
    run(Council(make_cfg(), client, seed=7, journal=Journal(j2)).run("Вопрос?"))
    stages = [c["stage"] for c in client.log]
    assert "proposal" in stages and "critique" in stages and "framing" not in stages


def test_pilot_replay_and_solo_in_demo(tmp_path):
    from council import pilot
    d = run(Council(make_cfg(mode="research"), MockClient(delay=0), seed=3).run("Вопрос?")).to_dict()
    res = run(pilot.replay_arbitration(make_cfg(mode="research"), MockClient(delay=0), d))
    assert res["after"]["checked"] > 0 and res["before"]["checked"] > 0 and res["cost"] > 0
    solo = run(pilot.solo_answer(make_cfg(), None, d, mock=True))
    assert solo["mock"] and "одной модели" in solo["answer"]
    pilot.save(tmp_path, "s1", "solo", solo)
    assert pilot.load(tmp_path, "s1")["solo"]["answer"] == solo["answer"]


def test_solo_answer_is_continued_when_cut_by_the_length_limit():
    """Ответ одной модели, упёршийся в лимит, дописывается продолжениями (поиск — только в первой части)."""
    from council import pilot
    from council.client import Completion

    class Cut:
        def __init__(self):
            self.calls = []

        async def complete(self, model, messages, **kw):
            self.calls.append(kw.get("web_search"))
            n = len(self.calls)
            return Completion(text=f"часть {n} ", prompt_tokens=10, completion_tokens=10, cost=0.01, model=model,
                              finish_reason="length" if n < 3 else "stop", citations=[f"https://e.example/{n}"])

    c = Cut()
    res = run(pilot.solo_answer(make_cfg(), c, {"question": "Вопрос?"}))
    assert res["answer"] == "часть 1 часть 2 часть 3" and res["parts"] == 3 and not res["truncated"]
    assert c.calls == [True, False, False] and res["cost"] == 0.03 and len(res["citations"]) == 3


def _led():
    return {"evidence": [{"id": "E1"}, {"id": "E2"}],
            "claims": [{"id": "A-C1", "status": "confirmed", "evidence": ["E1"], "text": "Препарат X выпускает компания Y",
                        "scope": "США, 2025"},
                       {"id": "A-C2", "status": "confirmed", "evidence": ["E2"], "text": "Цена упаковки 40 долларов"}]}


def test_known_rests_on_named_claims_without_new_numbers_and_on_verbatim_document_quotes():
    v = R.ResearchVerdict(final_answer="Итог. Компания Y даёт эксклюзив в России на 5 лет. [E9]", known=[
        {"statement": "Препарат X выпускает компания Y", "claim_ids": ["A-C1"], "evidence_ids": ["E1"]},
        {"statement": "без опорного утверждения", "evidence_ids": ["E1"]},
        {"statement": "Цена упаковки 45 долларов", "claim_ids": ["A-C2"], "evidence_ids": ["E2"]},
        {"statement": "Оборот дистрибьютора 120 млн", "evidence_ids": ["DOC"], "doc_ref": "отчёт.pdf, с. 2",
         "doc_quote": "оборот за 2025 год составил 120 млн рублей"},
        {"statement": "из документа, но цитаты там нет", "evidence_ids": ["DOC"], "doc_quote": "такой фразы нет"},
        {"statement": "документ и чужой источник", "evidence_ids": ["DOC", "E7"], "doc_quote": "оборот за 2025 год"}],
        decision_card={"summary_decision": "Идти к Y.", "top_known": ["X выпускает Y [E1]", "Y даёт эксклюзив [E2]"]})
    docs = "Отчёт. Оборот за 2025 год составил 120 млн рублей; прибыль — 8 млн."
    out = R.check_verdict(v, ledger=_led(), n_subq=0, strict=False, mark=True, docs_text=docs)
    assert [k.statement for k in out.known] == ["Препарат X выпускает компания Y", "Оборот дистрибьютора 120 млн"]
    assert out.known[0].verified == ["Препарат X выпускает компания Y"]
    why = {u.gap: u.how_to_check for u in out.unknown}
    assert "какое проверенное утверждение" in why["без опорного утверждения"]
    assert "45" in why["Цена упаковки 45 долларов"]          # расширение: другого числа в проверенном нет
    assert "цитата не найдена" in why["из документа, но цитаты там нет"]
    assert "не подтверждён" in why["документ и чужой источник"]  # «DOC» больше не пропускает чужие ссылки
    assert out.final_answer.startswith("> ⚠ **4 утв.") and "[E9 ⚠ нет в реестре]" in out.final_answer
    assert out.decision_card.top_known == ["X выпускает Y [E1]"]   # E2 — не из прошедшего «известно»
    assert any("не подтверждено" in x for x in out.decision_card.key_unknowns)


def test_all_moved_items_are_listed():
    v = R.ResearchVerdict(final_answer="Итог.", known=[{"statement": f"факт {i}", "evidence_ids": []} for i in range(11)])
    out = R.check_verdict(v, ledger=_led(), n_subq=0, strict=False, mark=True)
    assert all(f"факт {i}" in out.final_answer for i in range(11))


def test_decisive_private_claim_gets_one_public_check():
    props = props_with({"A": [{"text": "Права на препарат X принадлежат компании Y", "checkability": "private", "decisive": True},
                              {"text": "Условия эксклюзива для нас", "checkability": "private"}]})
    sel, deferred = R.prepare_claims(props, {}, 10)
    assert [c["id"] for c in sel] == ["A-C1"] and sel[0]["private_checked"] and sel[0]["critical"]
    assert [d["ids"] for d in deferred] == [["A-C2"]]
    sel2, _ = R.prepare_claims(props, {}, 10, check_decisive_private=False)
    assert sel2 == []


def test_report_worker_is_stopped_when_its_real_memory_exceeds_the_limit():
    import sys
    from council.archive import _run_watched
    code, out, err, peak, killed = _run_watched(
        [sys.executable, "-c", "import time; x = bytearray(200 * 1024 * 1024); time.sleep(5)"], limit_mb=80, timeout=30)
    assert killed and peak > 80
    code, out, err, peak, killed = _run_watched([sys.executable, "-c", "print('ok')"], limit_mb=500, timeout=30)
    assert code == 0 and not killed and out.strip() == "ok"


def test_unacceptable_leader_is_not_counted_as_a_win(tmp_path):
    from council.archive import Archive
    from council.stats import compute
    d = run(Council(make_cfg(), MockClient(delay=0), seed=1).run("Вопрос?", options=["один", "два"])).to_dict()
    d["verdict"]["acceptable"] = False
    (tmp_path / "20250101-000000-s1.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    st = compute(Archive(tmp_path))
    assert st["models"]
    assert sum(m["wins"] for m in st["models"]) == 0
