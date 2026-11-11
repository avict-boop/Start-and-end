"""Режим «Исследование»: реестр доказательств, «известно» только с источниками, голосование по подвопросам."""
import asyncio

import pytest

from council import research as R
from council.checkpoint import Journal
from council.mock import MockClient
from council.session import Council
from tests.test_council import make_cfg


def run(coro):
    return asyncio.run(coro)


def test_research_session_end_to_end():
    res = run(Council(make_cfg(mode="research"), MockClient(delay=0), seed=3).run("Стоит ли открывать кофейню у вуза?"))
    d = res.to_dict()
    r = d["research"]
    assert res.mode == "research" and len(r["subquestions"]) == 3 and r["scope"]
    assert all(lab in r["lens_of"] for lab in res.authors)  # у каждого автора своя линза
    led = r["ledger"]
    # одинаковые утверждения разных авторов проверены один раз; «не удалось проверить» — второй поиск
    assert led["missions"] == 3 and led["evidence"] and any(c.get("duplicate_of") for c in led["claims"])
    assert {c["status"] for c in led["claims"]} >= {"confirmed"}
    ok = R.confirmed_ids(led)
    assert res.verdict.known and all(set(k.evidence_ids) <= ok | {"DOC"} for k in res.verdict.known)
    assert set(r["sections"]) == {"Q1", "Q2", "Q3"} and all(s["leader"] for s in r["sections"].values())
    assert {c.subquestion_id for c in res.verdict.coverage_audit} == {"Q1", "Q2", "Q3"}
    assert all(c.id.startswith(lab + "-C") for lab, p in res.proposals.items() for c in p.claims)
    assert not any(s.failures for s in res.stats.values())


def test_ledger_rejects_sources_not_returned_by_search_and_merges_conflicts():
    sel = [{"id": "A-C1", "label": "A", "text": "x", "basis": "memory", "volatility": "stable"},
           {"id": "B-C1", "label": "B", "text": "y", "basis": "memory", "volatility": "stable"}]
    m1 = R.EvidenceResult(search_citations=["https://real.example/a"], checked_claims=[
        R.CheckedClaim(claim_id="A-C1", status="confirmed", sources=[R.ESource(url="https://invented.example/z")]),
        R.CheckedClaim(claim_id="B-C1", status="confirmed", sources=[R.ESource(url="https://real.example/a/")])])
    m2 = R.EvidenceResult(checked_claims=[R.CheckedClaim(claim_id="B-C1", status="refuted",
                                                         sources=[R.ESource(url="https://other.example", relation="contradicts")])])
    led = R.build_ledger(sel, [m1, m2], "perplexity/sonar-pro")
    st = {c["id"]: c["status"] for c in led["claims"]}
    assert st == {"A-C1": "unclear", "B-C1": "contested_in_sources"}  # выдуманная ссылка подтверждением не считается
    inv = next(e for e in led["evidence"] if "invented" in e["url"])
    assert inv["search_returned"] is False and "NOT among search results" in R.ledger_text(led)


def test_known_needs_confirmed_evidence():
    led = {"claims": [{"id": "A-C1", "status": "confirmed", "evidence": ["E1"]},
                      {"id": "A-C2", "status": "unclear", "evidence": ["E2"]}],
           "evidence": [{"id": "E1"}, {"id": "E2"}]}
    v = R.ResearchVerdict(winner="A", final_answer="итог", why="", known=[
        {"statement": "подтверждено", "evidence_ids": ["E1"]}, {"statement": "из памяти", "evidence_ids": []},
        {"statement": "неясно", "evidence_ids": ["E2"]}, {"statement": "из таблицы", "evidence_ids": ["[DOC: file]"]}],
        coverage_audit=[{"subquestion_id": "Q1"}])
    with pytest.raises(ValueError, match="known"):
        R.check_verdict(v, ledger=led, n_subq=2, strict=True)
    out = R.check_verdict(v, ledger=led, n_subq=2, strict=False, require_claims=False, docs_text="")
    assert [k.statement for k in out.known] == ["подтверждено"]  # «из документа» без документа и цитаты — нельзя
    assert {u.gap for u in out.unknown} == {"из памяти", "неясно", "из таблицы"} and len(out.coverage_audit) == 2


def test_claims_selection_covers_every_author_and_prefers_disputed():
    from council.schemas import CritiqueSet
    props = {l: R.number_claims(l, R.ResearchProposal(title="t", summary="текст ответа", claims=[
        {"text": f"{l} цифра 10%", "basis": "memory"}, {"text": f"{l} из таблицы", "basis": "user_doc"},
        {"text": f"{l} вывод", "basis": "calc"}])) for l in "AB"}
    crit = {"x": CritiqueSet(reviews=[{"label": "B", "scores": {"k": 5}, "disputed_claims": [{"claim_id": "B-C3", "reason": "?"}]}])}
    sel = R.select_claims(props, crit, 3)
    ids = [c["id"] for c in sel]
    assert ids[0] == "B-C3" and {c["label"] for c in sel} == {"A", "B"} and not any(i.endswith("C2") for i in ids)


def test_research_pause_and_resume_replays_paid_answers(tmp_path):
    from council.session import BudgetExceeded
    j = tmp_path / "j.jsonl"
    with pytest.raises(BudgetExceeded):
        run(Council(make_cfg(mode="research", max_cost_usd=0.009), MockClient(delay=0), seed=5,
                    journal=Journal(j)).run("Вопрос?"))
    c = Council(make_cfg(mode="research", max_cost_usd=5), MockClient(delay=0), seed=5, journal=Journal(j))
    res = run(c.run("Вопрос?"))
    assert c.replayed >= 8 and res.research["ledger"]["claims"]


def test_chair_failure_gives_honest_fallback():
    class NoChair(MockClient):
        async def complete(self, model, messages, **kw):
            if (kw.get("meta") or {}).get("stage") == "verdict":
                from council.client import ProviderError
                raise ProviderError(f"{model}: HTTP 500")
            return await super().complete(model, messages, **kw)

    res = run(Council(make_cfg(mode="research"), NoChair(delay=0), seed=1).run("Вопрос?"))
    assert "Синтез не выполнен" in res.verdict.final_answer and res.verdict.known


def test_revision_gets_digests_not_full_texts_unless_disabled():
    others = {"B": R.ResearchProposal(title="Другой", summary="ПОЛНЫЙ_ТЕКСТ " * 50,
                                      claims=[{"id": "B-C1", "text": "утверждение"}])}
    own = R.ResearchProposal(title="Мой", summary="мой текст")
    short = R.revision("H", ["k"], "A", own, [], others, "", "", 1000, 8, full_others=False)
    full = R.revision("H", ["k"], "A", own, [], others, "", "", 1000, 8, full_others=True)
    assert "ПОЛНЫЙ_ТЕКСТ" not in short and "B-C1" in short and "ПОЛНЫЙ_ТЕКСТ" in full


def test_research_reports_in_every_format(tmp_path):
    from docx import Document
    import io
    from council.archive import Archive, all_reports
    from council.reports.docx import render_docx
    from council.reports.html import render_html
    from council.reports.md import render_md
    from council.reports.view import View
    res = run(Council(make_cfg(mode="research"), MockClient(delay=0), seed=3).run("Стоит ли открывать кофейню у вуза?"))
    d = res.to_dict()
    v = View(d, "x")
    html = render_html(v)
    assert all(f'id="{i}"' in html for i in ("kcu", "scope", "sources", "claims")) and "Итог исследования" in html
    assert 'href="#src-E1"' in html and 'id="src-E1"' in html
    summary = render_html(v, summary=True)
    assert 'id="kcu"' in summary and 'id="claims"' not in summary  # итог короче: без проверки утверждений
    md = render_md(v)
    assert "### ✓ Известно" in md and "## Источники" in md and "## Проверка утверждений" in md
    doc = Document(io.BytesIO(render_docx(v)))
    heads = [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]
    assert "Что известно, что спорно, чего не знает никто" in heads and "Источники" in heads
    built = Archive(tmp_path).build("r", all_reports(), data=d)
    assert not [k for k, x in built.items() if str(x).startswith("ошибка")]


def test_memory_urls_are_marked_unless_found_by_search():
    p = R.ResearchProposal(title="t", summary="См. https://made.up/report и https://real.example/a, а также [ссылка](https://x.io).")
    out = R.mark_memory_urls(p, {"https://real.example/a"}).summary
    assert "https://made.up/report" + R.MEMORY_URL_NOTE in out
    assert "https://real.example/a" + R.MEMORY_URL_NOTE not in out and "](https://x.io)" in out
    again = R.mark_memory_urls(R.ResearchProposal(title="t", summary=out), {"https://real.example/a"}).summary
    assert again == out  # повторная разметка метку не удваивает


def test_every_author_gets_a_distinct_lens():
    out = R.fill_lenses(["рынок и первичные данные", "экономика и расчёты", "скептик"], 4)
    assert len(out) == 4 and out[3].startswith("технология")  # добавлен недостающий угол, а не повтор «рынка»
    res = run(Council(make_cfg(mode="research"), MockClient(delay=0), seed=3).run("Вопрос?"))
    lenses = list(res.research["lens_of"].values())
    assert len(lenses) == 4 and len(set(lenses)) == 4


def test_chair_choosing_another_backbone_is_not_rejected():
    class OtherWinner(MockClient):
        async def complete(self, model, messages, **kw):
            c = await super().complete(model, messages, **kw)
            meta = kw.get("meta") or {}
            if meta.get("stage") == "verdict":
                import json as _j
                d = _j.loads(c.text)
                d["winner"] = next(l for l in meta["labels"] if l != meta["leader"])
                d.pop("deviation", None)
                c.text = _j.dumps(d, ensure_ascii=False)
            return c

    res = run(Council(make_cfg(mode="research"), OtherWinner(delay=0), seed=3).run("Вопрос?"))
    assert "Синтез не выполнен" not in res.verdict.final_answer and res.verdict.deviation.startswith("process")


def test_absence_claims_go_last():
    props = {"A": R.number_claims("A", R.ResearchProposal(title="t", summary="текст ответа", claims=[
        {"text": "Ни один препарат не зарегистрирован в ГРЛС", "basis": "memory"},
        {"text": "Препарат X одобрен FDA в 2024 году", "basis": "memory"},
        {"text": "Цена 10% выше", "basis": "memory"}]))}
    sel = [c["id"] for c in R.select_claims(props, {}, 2)]
    assert "A-C1" not in sel


def test_chair_failure_is_recorded_and_recovered_for_old_sessions():
    from council.reports.view import View
    from council.web.app import events_from_result

    class NoChair(MockClient):
        async def complete(self, model, messages, **kw):
            if (kw.get("meta") or {}).get("stage") == "verdict":
                from council.client import ProviderError
                raise ProviderError(f"{model}: HTTP 500")
            return await super().complete(model, messages, **kw)
    res = run(Council(make_cfg(mode="research"), NoChair(delay=0), seed=1).run("Вопрос?"))
    assert any(f.startswith("verdict:") for f in res.stats["a"].failures)
    # заседание до исправления: сбой не записан — восстанавливается по стенограмме, и появляется «Доделать»
    d = res.to_dict()
    d["stats"]["a"]["failures"] = []
    d["transcript"].append({"stage": "verdict", "member": "a", "error": '"known" may contain ONLY claims backed by …'})
    fails = View(d, "x").failures()
    assert fails and "known" in fails[-1][2] and fails[-1][0] == "a"
    ev = [e for e in events_from_result(d, "x") if e["type"] == "member_failed" and e["stage"] == "verdict"]
    assert ev and "не сдал итог" in ev[0]["error"]


class Recording(MockClient):
    def __init__(self) -> None:
        super().__init__(delay=0)
        self.seen: list[tuple[str, str]] = []

    async def complete(self, model, messages, **kw):
        text = "".join(c if isinstance(c, str) else "".join(p.get("text", "") for p in c)
                       for c in (m["content"] for m in messages))
        self.seen.append((kw.get("meta", {}).get("stage", ""), text))
        return await super().complete(model, messages, **kw)


def test_research_followup_sees_earlier_decision_at_every_content_stage():
    """Уточнение к исследованию: прошлый вопрос, решение и подтверждённые факты видны на постановке,
    предложениях, критике, доработке и синтезе."""
    client = Recording()
    history = [{"question": "Стоит ли открывать кофейню?", "answer": "ПРОШЛОЕ_РЕШЕНИЕ: да, у вуза",
                "evidence": "Confirmed by sources: ПРОШЛЫЙ_ФАКТ"}]
    events = []
    run(Council(make_cfg(mode="research"), client, seed=2, on_event=lambda e, **d: events.append(e))
        .run("А какой бюджет нужен?", history=history, followup_of="x"))
    stages = {st for st, txt in client.seen if "ПРОШЛОЕ_РЕШЕНИЕ" in txt and "ПРОШЛЫЙ_ФАКТ" in txt}
    assert {"framing", "proposal", "critique", "revision", "verdict"} <= stages
    assert "followup" in events and "lenses" in events


def test_compact_option_does_not_apply_to_research():
    from council.reports.view import View
    res = run(Council(make_cfg(mode="research"), MockClient(delay=0), seed=3).run("Вопрос?"))
    d = res.to_dict()
    d["request"] = {"compact_proposals": True}
    assert View(d, "x").compact_note == ""


def test_verdict_with_unsupported_known_is_repaired_not_thrown_away():
    """Дорогой итог не отправляется переписывать из-за «известно» без источника: это исправляется само."""
    import json as _json

    class Chair(MockClient):
        verdicts = 0

        async def complete(self, model, messages, **kw):
            comp = await super().complete(model, messages, **kw)
            if kw.get("meta", {}).get("stage") == "verdict":
                Chair.verdicts += 1
                d = _json.loads(comp.text)
                d.setdefault("known", []).append({"statement": "НЕПОДКРЕПЛЁННОЕ", "evidence_ids": ["E999"]})
                d["coverage_audit"] = []
                comp.text = _json.dumps(d, ensure_ascii=False)
            return comp

    events = []
    res = run(Council(make_cfg(mode="research"), Chair(delay=0), seed=3,
                      on_event=lambda e, **d: events.append((e, d))).run("Вопрос?"))
    assert Chair.verdicts == 1 and not any(s.failures for s in res.stats.values())
    assert all(k.statement != "НЕПОДКРЕПЛЁННОЕ" for k in res.verdict.known)
    assert any(u.gap == "НЕПОДКРЕПЛЁННОЕ" for u in res.verdict.unknown)
    assert len(res.verdict.coverage_audit) == len(res.to_dict()["research"]["subquestions"])
    assert any(e == "warning" and "перенесено" in d.get("text", "") for e, d in events)


def test_requirement_violation_marker_is_readable_in_reports():
    from council.reports.view import readable_flaw
    assert readable_flaw("VIOLATES R9: нет дат проверки") == "Нарушает обязательное требование R9: нет дат проверки"
    assert readable_flaw("обычное замечание") == "обычное замечание"


def test_too_long_proposal_is_sent_back_when_enabled_and_length_is_per_session():
    """Длина варианта задаётся для заседания; слишком длинный вариант возвращается на сокращение,
    последняя попытка принимается как есть, а оплаченный ответ из журнала не переспрашивается."""
    import json as _json

    from council.config import with_overrides

    class Wordy(MockClient):
        asks: dict = {}

        async def complete(self, model, messages, **kw):
            comp = await super().complete(model, messages, **kw)
            st = kw.get("meta", {}).get("stage")
            if st == "proposal":
                key = (model, st)
                Wordy.asks[key] = Wordy.asks.get(key, 0) + 1
                if Wordy.asks[key] == 1:  # первый ответ — втрое длиннее предела
                    d = _json.loads(comp.text)
                    d["summary"] = "слово " * 1000
                    comp.text = _json.dumps(d, ensure_ascii=False)
            return comp

    cfg = with_overrides(make_cfg(mode="research"), research_words=300, research_shorten=True)
    assert cfg.research.max_words == 300 and cfg.research.shorten_long
    res = run(Council(cfg, Wordy(delay=0), seed=3).run("Вопрос?"))
    assert all(n == 2 for n in Wordy.asks.values())  # каждого автора попросили сократить один раз
    assert all(len(p.summary.split()) < 600 for p in res.initial_proposals.values())
    assert make_cfg(mode="research").research.shorten_long  # по рекомендации аудита — включено по умолчанию


def test_dead_links_do_not_confirm_and_are_reported():
    from council.links import classify_code
    assert [classify_code(c) for c in (200, 301, 404, 410, 403, 429, 500)] == ["ok", "ok", "dead", "dead", "blocked", "blocked", "error"]
    checked = []

    async def checker(urls):
        checked.extend(urls)
        return {u: {"status": "dead", "code": 404} for u in urls}  # все ссылки мёртвые

    events = []
    res = run(Council(make_cfg(mode="research"), MockClient(delay=0), seed=3, link_checker=checker,
                      on_event=lambda e, **d: events.append((e, d))).run("Стоит ли открывать кофейню у вуза?"))
    led = res.to_dict()["research"]["ledger"]
    assert checked and all(e["link"] == "dead" for e in led["evidence"]) and led["links"]["dead"] == len(led["evidence"])
    assert not any(c["status"] == "confirmed" for c in led["claims"])  # мёртвые ссылки ничего не подтверждают
    assert any("не открываются" in " ".join(c.get("notes") or []) for c in led["claims"])
    assert not res.verdict.known  # в «известно» опереться не на что
    assert ("links", {"ok": 0, "dead": len(led["evidence"]), "blocked": 0, "error": 0}) in events


def test_blocked_links_keep_confirmation():
    led = {"evidence": [{"id": "E1", "url": "https://gov.example/a"}],
           "claims": [{"id": "A-C1", "status": "confirmed", "evidence": ["E1"]}]}
    R.apply_links(led, {"https://gov.example/a": {"status": "blocked", "code": 403}})
    assert led["claims"][0]["status"] == "confirmed" and led["evidence"][0]["link"] == "blocked"
