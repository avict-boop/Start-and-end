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
    lk = next(d for e, d in events if e == "links")
    assert {k: lk[k] for k in ("ok", "dead", "blocked", "error")} == {"ok": 0, "dead": len(led["evidence"]), "blocked": 0, "error": 0}


def test_blocked_links_keep_confirmation():
    led = {"evidence": [{"id": "E1", "url": "https://gov.example/a"}],
           "claims": [{"id": "A-C1", "status": "confirmed", "evidence": ["E1"]}]}
    R.apply_links(led, {"https://gov.example/a": {"status": "blocked", "code": 403}})
    assert led["claims"][0]["status"] == "confirmed" and led["evidence"][0]["link"] == "blocked"


def test_no_length_limit_switch_is_not_lost():
    from council.config import with_overrides
    # «Без ограничения длины» приходит как 0 — это не «не задано»: настройка по умолчанию (1400) не подставляется
    cfg = with_overrides(make_cfg(mode="research"), research_words=0)
    assert cfg.research.max_words == 0
    long = R.check_length(R.ResearchProposal(title="t", summary="слово " * 5000), 0, 1.5)
    assert long.summary


def test_objects_of_the_request_are_matched_covered_and_checked_first():
    objs = ["山东禹王制药有限公司 / Shandong Yuwang Pharmaceutical", "威海百合生物技术股份有限公司 / Weihai Baihe",
            "广东青云山药业有限公司 / Guangdong Qingyunshan"]
    assert "山东禹王制药" in R.object_keys(objs[0]) and "yuwang" in R.object_keys(objs[0])
    assert "pharmaceutical" not in R.object_keys(objs[0])  # общие слова не считаются признаком

    def prop(*texts):
        return R.ResearchProposal(title="t", summary="текст " * 50,
                                  claims=[R.RClaim(text=t, source_leads=["https://x.example/a"]) for t in texts])

    p = prop("Yuwang выпускает softgel для сторонних брендов", "威海百合 работает по OEM")
    assert R.missing_objects(p, objs) == [objs[2]]
    with pytest.raises(ValueError, match="Qingyunshan") as ei:
        R.check_objects(p, objs, 1, 10)
    sup = ei.value  # дозаявка: ответ принят, просим только недостающие утверждения
    assert "Qingyunshan" in sup.supplement_prompt and sup.obj is p
    merged = sup.merge(R.ClaimsAddon(claims=[R.RClaim(text="Qingyunshan выпускает капсулы"),
                                             R.RClaim(text="Yuwang выпускает softgel для сторонних брендов")]))
    assert len(merged.claims) == 3 and R.missing_objects(merged, objs) == []  # повтор не задвоен
    assert R.check_objects(p, objs, 1, 1) is p  # объектов больше, чем утверждений — не требуем невозможного
    # на проверку сначала идут факты о каждом объекте, даже если их «важность» ниже
    a = R.number_claims("A", prop("Общий факт о рынке БАД 1", "Общий факт о рынке БАД 2", "Общий факт о рынке БАД 3",
                                  "Yuwang выпускает softgel", "Baihe работает по OEM", "Qingyunshan выпускает таблетки"))
    sel, _ = R.prepare_claims({"A": a}, {}, 3, objects=objs, facts_per_object=1)
    assert {c.get("object") for c in sel} == set(objs)
    led = {"claims": [{"id": c["id"], "text": c["text"], "status": "confirmed"} for c in sel[:2]]}
    oc = R.object_coverage(objs, led)
    assert oc["total"] == 3 and oc["checked"] == 2 and oc["missing"] == [objs[2]]


def test_research_with_listed_objects_runs_and_reports_coverage():
    import json as _json
    from council.mock import MockClient

    class Objs(MockClient):
        def __init__(self):
            super().__init__(delay=0)
            self.retries = 0

        async def complete(self, model, messages, **kw):
            st = (kw.get("meta") or {}).get("stage")
            if st == "proposal" and any("Your research answer was accepted" in str(m.get("content") or "") for m in messages):
                self.retries += 1
                # дозаявка: модели не пересылают её длинный ответ (нет сообщения assistant) — только список пропущенных
                assert not any(m.get("role") == "assistant" for m in messages)
            comp = await super().complete(model, messages, **kw)
            if st == "framing":
                d = _json.loads(comp.text)
                d["objects"] = ["Alphacorp Foods", "Betamax Nutrition"]
                comp.text = _json.dumps(d, ensure_ascii=False)
            return comp

    c = Objs()
    cfg = make_cfg(mode="research", research={"catalog": True, "facts_per_object": 2})
    res = run(Council(cfg, c, seed=4).run("Проверь компании Alphacorp Foods и Betamax Nutrition"))
    led = (res.research or {}).get("ledger") or {}
    assert c.retries >= 1  # авторов без фактов об объектах попросили добавить
    assert led.get("objects", {}).get("total") == 2


def test_supplement_failure_keeps_the_paid_answer():
    import json as _json
    from council.client import OutOfCredits
    from council.mock import MockClient

    class Broke(MockClient):
        def __init__(self):
            super().__init__(delay=0)
            self.sup = 0

        async def complete(self, model, messages, **kw):
            st = (kw.get("meta") or {}).get("stage")
            if st == "proposal" and any("Your research answer was accepted" in str(m.get("content") or "") for m in messages):
                self.sup += 1
                raise OutOfCredits("402: кончились деньги")  # дозаявка не прошла — вариант принимается как есть
            comp = await super().complete(model, messages, **kw)
            if st == "framing":
                d = _json.loads(comp.text)
                d["objects"] = ["Alphacorp Foods", "Betamax Nutrition"]
                comp.text = _json.dumps(d, ensure_ascii=False)
            return comp

    c = Broke()
    cfg = make_cfg(mode="research", research={"catalog": True})
    res = run(Council(cfg, c, seed=4).run("Проверь компании Alphacorp Foods и Betamax Nutrition"))
    assert c.sup >= 1 and res.verdict is not None  # заседание дошло до итога: оплаченные варианты не потеряны
    assert any("дозаявка не удалась" in f for s in res.stats.values() for f in s.failures)


def test_summary_checks_skip_known_find_conflicts_and_recheck_open():
    parts = [(1, {"claims": [{"id": "A-C1", "text": "Рынок БАД Китая в 2024 году — 400 млрд юаней", "status": "confirmed"},
                             {"id": "A-C2", "text": "Firm01 экспортирует в ЕС", "status": "unclear", "object": "Firm01"}]}),
             (2, {"claims": [{"id": "B-C1", "text": "Рынок БАД Китая в 2024 году — 520 млрд юаней", "status": "confirmed"},
                             {"id": "B-C2", "text": "Firm02 имеет сертификат GMP", "status": "refuted"}]})]
    # проверенное в частях не перепроверяется, новое — да
    sel = [R.summary_claim("A-C9", "Рынок БАД Китая в 2024 году — 400 млрд юаней", "A"),
           R.summary_claim("A-C10", "Лучший поставщик по совокупности — Firm02", "A")]
    kept, skipped = R.drop_known(sel, parts)
    assert skipped == 1 and [c["id"] for c in kept] == ["A-C10"]
    # противоречие между частями — утверждение для поиска; внутри одной части и выдуманные id — нет
    conf = R.conflict_claims([R.Conflict(ids=["P1-A-C1", "P2-B-C1"], question="Какой объём рынка?"),
                              R.Conflict(ids=["P1-A-C1", "P1-A-C2"]), R.Conflict(ids=["P1-A-C1", "P9-Z"])], parts)
    assert len(conf) == 1 and conf[0]["conflict_of"] == ["P1-A-C1", "P2-B-C1"] and "520" in conf[0]["text"]
    assert "P1-A-C1" in R.conflicts_prompt(parts) and "P2-B-C1" in R.conflicts_prompt(parts)
    # доуточнить — только невыясненное
    rc = R.recheck_claims(parts, 20)
    assert [c["id"] for c in rc] == ["P1-A-C2"] and rc[0]["recheck_of"] == "P1-A-C2"
    # в общем реестре: доуточненная строка заменяет прежнюю (прежний статус — рядом), противоречие отмечено
    led = {"claims": [{**rc[0], "status": "confirmed"}, {**conf[0], "status": "confirmed"}], "evidence": []}
    out = R.merge_inherited(led, parts)
    ids = [c["id"] for c in out["claims"]]
    assert ids.count("P1-A-C2") == 1
    row = next(c for c in out["claims"] if c["id"] == "P1-A-C2")
    assert row["status"] == "confirmed" and row["was_status"] == "unclear" and row["rechecked"]
    assert next(c for c in out["claims"] if c["id"] == "P2-B-C1")["conflict"] == "X-C1"
    assert next(c for c in out["claims"] if c["id"] == "P2-B-C2")["status"] == "refuted"  # статус части — как был


def test_part_numbers_and_unit_definitions_are_not_fact_numbers():
    nums = lambda t: sorted(min(f) for f in R._numbers(t))  # noqa: E731
    assert nums("Часть 1 сообщает мощность 7,2 млрд/год") == ["7.2"]
    assert nums("Часть 2 из 2: ни у одной из 11 компаний; см. P2-D-C3-V1 и chast-1.md") == ["11"]
    assert nums("242 774,46万 (万 = 10 000)") == ["242774.46"]
    assert nums("в 2 компаниях из 3") == ["2", "3"]  # обычные числа по-прежнему проверяются


def test_cheaper_research_patches_caps_and_vote_digest():
    """Доработка правками собирается программой; критика и доработка — с потолком рассуждений; голосование — по
    выжимке, а не по полным текстам."""
    import json as _json
    from council.mock import MockClient

    class Spy(MockClient):
        def __init__(self):
            super().__init__(delay=0)
            self.effort: dict[str, set] = {}
            self.vote_chars = 0
            self.rev_prompts = []

        async def complete(self, model, messages, **kw):
            st = (kw.get("meta") or {}).get("stage")
            self.effort.setdefault(st, set()).add(kw.get("reasoning_effort"))
            text = "".join(str(m.get("content")) for m in messages)
            if st == "vote":
                self.vote_chars = max(self.vote_chars, len(text))
            comp = await super().complete(model, messages, **kw)
            if st == "proposal":  # длинный вариант с разделами — есть что править блоками
                d = _json.loads(comp.text)
                d["summary"] = "\n\n".join(f"## Раздел {i}\n\n" + f"Текст раздела {i}. " * 120 for i in range(1, 6))
                comp.text = _json.dumps(d, ensure_ascii=False)
            if st == "revision":
                self.rev_prompts.append(text)
                d = _json.loads(comp.text)
                d.pop("summary", None)
                d["edits"] = [{"block": "S2", "text": "## Раздел 2\n\nИсправлено по реестру [E1]."}]
                d["delete_blocks"] = ["S5"]
                comp.text = _json.dumps(d, ensure_ascii=False)
            return comp

    c = Spy()
    cfg = make_cfg(mode="research", reasoning_effort="high", max_concurrency=8)
    res = run(Council(cfg, c, seed=3).run("Каков рынок кофеен у вузов?"))
    assert any("[S1]" in p and "edits" in p for p in c.rev_prompts)  # автору показан вариант по блокам
    revised = [p for p in res.proposals.values() if "Исправлено по реестру" in p.summary]
    assert revised and all("Раздел 5" not in p.summary and "Раздел 4" in p.summary for p in revised)  # правки применены
    assert c.effort["critique"] == {"medium"} and c.effort["revision"] == {"medium"}  # потолок: high → medium
    assert c.effort["proposal"] == {"high"} and "high" in c.effort["verdict"]  # варианты и итог — полная глубина
    full = sum(len(p.summary) for p in res.proposals.values())
    assert c.vote_chars < full  # голосующий видит выжимки, а не все полные тексты


def test_chair_searches_at_verdict_only_when_ledger_has_gaps():
    assert R.verdict_gaps({"claims": [{"id": "A-C1", "status": "confirmed", "critical": True}]}) == []
    assert R.verdict_gaps({"claims": [{"id": "A-C2", "status": "unclear", "decisive": True}]}) == ["A-C2"]
    assert R.verdict_gaps({"claims": [{"id": "A-C3", "status": "unclear"}]}) == []  # неважное — не повод искать
    assert R.verdict_gaps({"claims": [], "objects": {"missing": ["Firm01"]}}) == ["object:Firm01"]
