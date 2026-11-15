"""Режим «Отбор»: разбор чисел и единиц, склейка дублей, полнота, цитаты, сведение ячеек, ранг, устойчивость,
«что выяснить», заседание целиком в демо (поиск, отсев, спор, остановки «проверьте список»), Excel."""
import asyncio

import pytest

from council import selection as S
from council.mock import MockClient, mock_pages
from council.modes import selection as M
from council.session import Council
from tests.test_council import make_cfg


def test_numbers_with_chinese_and_russian_multipliers():
    assert S.parse_number("25亿粒") == 2.5e9
    assert S.parse_number("242 774,46万") == pytest.approx(2.4277446e9)
    assert S.parse_number("242,774.46万") == pytest.approx(2.4277446e9)
    assert S.parse_number("2,428 млрд") == pytest.approx(2.428e9)  # запятая дробная при русском множителе
    assert S.parse_number("1 056,25 млн шт.") == pytest.approx(1.05625e9)
    assert S.parse_number("11 000 т/год") == 11000
    assert S.parse_number("38–40 т/сутки") == 39
    assert S.parse_number("7.2 billion") == 7.2e9
    assert S.parse_number("нет данных") is None


def test_values_agree_by_kind():
    num = S.Attribute(name="мощность", kind="number")
    assert S.values_agree({"value": "25亿粒"}, {"value": "2,5 млрд"}, num.kind)
    assert not S.values_agree({"value": "2500"}, {"value": "500"}, num.kind)
    assert S.values_agree({"value": "да"}, {"value": "yes, certified"}, "bool")
    assert S.values_agree({"value": "2026-03-15"}, {"value": "15.03.2026"}, "date")
    assert S.values_agree({"value": "капсулы, таблетки"}, {"value": "таблетки; капсулы; порошки"}, "list")


def test_merge_candidates_by_name_and_site_but_not_by_marketplace():
    found = [("выставка", S.Candidate(name="Weihai Baihe Biotechnology Co., Ltd.", website="https://www.baiheshengwu.com")),
             ("биржа", S.Candidate(name="Baihe", local_name="威海百合", website="http://baiheshengwu.com/en")),
             ("площадка", S.Candidate(name="Sirio Pharma", website="https://sirio.en.alibaba.com")),
             ("площадка", S.Candidate(name="Other Co", website="https://other.en.alibaba.com"))]
    ents = S.merge_candidates(found, seeds=["Yuwang"])
    names = sorted(e["name"] for e in ents)
    assert len(ents) == 4, names  # Baihe склеен по сайту; площадка не склеивает разные компании
    baihe = next(e for e in ents if "baiheshengwu.com" in e["website"])
    assert set(baihe["angles"]) == {"выставка", "биржа"} and baihe["merged"]
    assert any(e["seed"] and e["name"] == "Yuwang" for e in ents)


def test_completeness_by_recaptures():
    many_once = [{"angles": [f"a{i}"]} for i in range(10)] + [{"angles": ["a1", "a2"]}]
    c = S.completeness(many_once, 3)
    assert c["share"] < 0.5  # почти все найдены одним направлением — список далёк от полного
    often = [{"angles": ["a1", "a2", "a3"]} for _ in range(10)] + [{"angles": ["a1", "a2"]}] * 3
    assert S.completeness(often, 3)["share"] == 1.0


def test_find_quote_exact_spacing_and_chinese():
    t = S.plain("Компания 威海百合: 年产软胶囊25亿粒，片剂10亿片. Founded in 2005 — OEM/ODM.")
    assert S.find_quote(t, "年产软胶囊25亿粒") >= 0
    assert S.find_quote(t, "软胶囊 25亿粒") >= 0
    assert S.find_quote(t, "Founded in 2005 - OEM/ODM") >= 0
    assert S.find_quote(t, "年产软胶囊99亿粒") == -1


def test_source_kind():
    assert S.source_kind("http://static.cninfo.com.cn/finalpage/a.PDF") == "document"
    assert S.source_kind("https://info.nsf.org/Certified/x", "sirio.com") == "document"
    assert S.source_kind("https://en.yuwangpharma.com/q", "https://www.yuwangpharma.com") == "official"
    assert S.source_kind("https://news.example.org/x", "yuwangpharma.com") == "secondary"


def test_resolve_cell_rules():
    a = S.Attribute(name="мощность", kind="number")
    ok = lambda v, by, found=True, kind="official": {"value": v, "by": by, "found": found, "kind": kind, "url": "u",  # noqa: E731
                                                       "unit": "млн капсул/год" if v.isdigit() else ""}
    assert S.resolve_cell(a, [ok("2500", "a"), ok("2,5 млрд", "b", kind="document")])["status"] == "document"
    assert S.resolve_cell(a, [ok("2500", "a"), ok("500", "b")])["status"] == "disputed"
    # непроверенное расхождение при проверенном значении не спорит: решает найденная цитата
    assert S.resolve_cell(a, [ok("2500", "a"), ok("500", "b", found=False)])["status"] == "official"
    assert S.resolve_cell(a, [ok("2500", "a", found=False)])["status"] == "unverified"
    assert S.resolve_cell(a, [])["status"] == "none"


def _table():
    attrs = [S.Attribute(id="A1", name="мощность", kind="number", weight=5, better="high"),
             S.Attribute(id="A2", name="GMP", kind="bool", weight=3, better="yes"),
             S.Attribute(id="A3", name="заметки", kind="text", weight=0)]
    t = {"O1": {"A1": {"status": "document", "value": "1000"}, "A2": {"status": "official", "value": "да"}},
         "O2": {"A1": {"status": "official", "value": "800"}, "A2": {"status": "none", "value": ""}},
         "O3": {"A1": {"status": "secondary", "value": "100"}, "A2": {"status": "document", "value": "да"}}}
    return attrs, t


def test_ranking_weights_trust_and_sensitivity():
    attrs, t = _table()
    r = S.ranking(attrs, t)
    assert [x["object"] for x in r] == ["O1", "O2", "O3"]
    assert r[0]["score"] == 92.5  # мощность 1 × документ 1 × 5 + GMP 1 × сайт 0,8 × 3 → 7,4 из 8
    # без мощности первым идёт тот, у кого GMP подтверждён документом
    r2 = S.ranking(attrs, t, weights={"A1": 0})
    assert r2[0]["object"] in ("O1", "O3") and r2[-1]["object"] == "O2"
    # перестановка от изменения веса на десятые — не показывается; на единицы — показывается
    assert S.sensitivity(attrs, t, {"O1": "Первый", "O2": "Второй", "O3": "Третий"}) == []
    attrs[1] = attrs[1].model_copy(update={"weight": 1})
    sens = S.sensitivity(attrs, t, {"O1": "Первый", "O2": "Второй", "O3": "Третий"})
    assert any(x["a"] == "O2" and x["b"] == "O3" and x["attr"] == "A2" and x["weight"] > 3 for x in sens)


def test_value_of_information_names_cells_that_change_the_first_group():
    attrs, t = _table()
    t["O1"]["A2"] = {"status": "none", "value": ""}       # у лидера по мощности GMP не найден…
    t["O2"]["A2"] = {"status": "document", "value": "да"}  # …и первым идёт O2
    names = {"O1": "Первый", "O2": "Второй", "O3": "Третий"}
    assert S.ranking(attrs, t)[0]["object"] == "O2"
    voi = S.value_of_information(attrs, t, names, top_n=1)
    assert voi and voi[0]["object"] == "O1" and voi[0]["attr"] == "A2" and voi[0]["crosses"]
    assert "может поднять Первый" in voi[0]["text"]


def _council(start="auto", **sel):
    cfg = make_cfg().model_copy(update={"mode": "selection"})
    cfg.selection.start = start
    for k, v in sel.items():
        setattr(cfg.selection, k, v)
    return Council(cfg, MockClient(delay=0), link_checker=mock_pages)


def test_selection_find_end_to_end_demo():
    c = _council("find", skip_neutral_disputes=False)  # здесь проверяем путь спора через совет
    r = asyncio.run(c.run("Найти контрактных производителей софтгелей", mode="selection"))
    sel = r.selection
    assert r.mode == "selection" and sel["candidates"] >= 5
    assert [x["name"] for x in sel["excluded"]] == ["Demo Plant 8"]  # отсев: перепродавец
    assert sel["stats"]["official"] > 0 and sel["stats"]["ruled"] == 1  # спорная ячейка решена советом
    plant3 = next(o["id"] for o in sel["objects"] if o["name"] == "Demo Plant 3")
    assert sel["table"][plant3]["A2"]["ruled"]
    # завод 10 есть только в демо-списке участников выставки — его нашли готовые списки, и он первый по мощности
    assert r.verdict.winner == "Demo Plant 10" and "### Ранг" in r.verdict.final_answer
    assert sel["top_n"] == 3 and "«" in r.verdict.final_answer  # цитаты в карточках
    stages = {e["meta"]["stage"] for e in r.transcript if e.get("meta")} or {e.get("stage") for e in r.transcript}
    assert {"frame", "discover", "screen", "sources", "fill", "dispute", "conclusion"} <= stages
    assert not {"framing", "verdict"} & stages  # свои имена этапов: не смешиваются с длинами ответов других режимов


def test_selection_check_list_skips_search_and_screening():
    c = _council()
    r = asyncio.run(c.run("Проверить заводы:\n| 1 | Demo Plant 2 |\n| 2 | Demo Plant 3 |\n| 3 | Demo Plant 5 |",
                          mode="selection"))
    sel = r.selection
    assert sel["start"] == "check" and [o["name"] for o in sel["objects"]] == ["Demo Plant 2", "Demo Plant 3", "Demo Plant 5"]
    stages = {e.get("stage") for e in r.transcript}
    assert "discover" not in stages and "screen" not in stages


def test_review_gates_pause_and_resume_with_decision():
    c = _council("find")
    c.review_enabled = True
    with pytest.raises(S.ReviewNeeded) as e:
        asyncio.run(c.run("Найти производителей", mode="selection"))
    assert e.value.gate == "candidates" and e.value.payload["items"]
    drop = e.value.payload["items"][0]["id"]
    c2 = _council("find")
    c2.review_enabled = True
    c2.review_decisions = {"candidates": {"drop": [drop], "add": ["Demo Plant 9"]}}
    with pytest.raises(S.ReviewNeeded) as e2:
        asyncio.run(c2.run("Найти производителей", mode="selection"))
    assert e2.value.gate == "shortlist"
    items = e2.value.payload["items"]
    assert drop not in {x["id"] for x in items} and any(x["name"] == "Demo Plant 9" for x in items)
    keep = [x["id"] for x in items][:3]
    c3 = _council("find")
    c3.review_enabled = True
    c3.review_decisions = {"candidates": {"drop": [drop], "add": ["Demo Plant 9"]}, "shortlist": {"keep": keep}}
    r = asyncio.run(c3.run("Найти производителей", mode="selection"))
    assert [o["id"] for o in r.selection["objects"]] == keep


def test_rerank_and_xlsx():
    from council.reports.xlsx import selection_xlsx
    c = _council("find")
    r = asyncio.run(c.run("Найти производителей", mode="selection"))
    d = r.to_dict()
    rr = M.rerank(d["selection"], {"A2": 0, "A3": 10, "A4": 0})
    assert rr["weights"]["A3"] == 10 and rr["ranking"]
    data = selection_xlsx(d, {"A2": 0})
    assert data[:2] == b"PK" and len(data) > 3000


def test_frame_edits_and_list_edits():
    f = S.SelectionFrame(attributes=[S.Attribute(name="мощность", kind="number"), S.Attribute(name="GMP", kind="bool")])
    g = M.apply_frame_edits(f, {"weights": {"A1": 7}, "drop": ["A2"], "add": [{"name": "MOQ", "kind": "number"}]})
    assert [a.name for a in g.attributes] == ["мощность", "MOQ"] and g.attributes[0].weight == 7
    assert [a.id for a in g.attributes] == ["A1", "A2"]
    ents = [{"id": "O1", "name": "A", "names": ["A"]}, {"id": "O2", "name": "B", "names": ["B"]}]
    out = M.apply_list_edits(ents, {"drop": ["O1"], "add": ["C", "b"]})
    assert [e["name"] for e in out] == ["B", "C"]


def test_procurement_alerts_compare_and_diff():
    from datetime import date

    from council import procurement as P
    sup = {"id": "O1", "name": "Weihai Baihe Biotechnology", "names": ["Weihai Baihe Biotechnology", "威海百合"],
           "status": "sent", "sent_at": "2026-11-01T10:00:00+00:00", "replies": [], "documents": [],
           "cells": {"Сертификат GMP действует до": {"value": "2026-12-01", "status": "official", "kind": "date"},
                     "Мощность до": {"value": "2000 тонн", "status": "official", "kind": "number"}}}
    p = {"suppliers": [sup], "skus": [{"name": "Omega-3"}], "rates": {"CNY": 0.14}, "base_currency": "USD"}
    today = date(2026, 11, 15)
    texts = [a["text"] for a in P.alerts(p, today)]
    assert any("нет ответа 14 дн." in t for t in texts) and any("истекает" in t for t in texts)
    assert not any("Мощность" in t for t in texts)  # число не принимается за год
    assert P.doc_flags({"kind": "SC", "number": "1", "holder": "威海百合生物技术股份有限公司", "valid_until": "2028-01-01"}, sup, today) == []
    assert "нет номера" in P.doc_flags({"kind": "GMP", "holder": "Baihe", "valid_until": "2028-01-01"}, sup, today)
    P.add_reply(p, "O1", P.ReplyParsed(items=[P.QuoteItem(sku="omega-3 softgel", price="8,0", currency="CNY", unit="per bottle")]),
                source="письмо", text="…")
    rows = P.compare(p)[0]
    assert rows["sku"] == "Omega-3" and rows["rows"][0]["base"] == 1.12 and sup["status"] == "replied"
    d = {"selection": {"objects": [{"id": "X", "name": "Weihai Baihe Biotechnology"}],
                       "attributes": [{"id": "A1", "name": "Сертификат GMP действует до", "kind": "date"}],
                       "table": {"X": {"A1": {"status": "document", "value": "2028-12-01"}}}}}
    ch = {x["attr"]: x for x in P.diff(p, d)}
    assert ch["Сертификат GMP действует до"]["old"] == "2026-12-01" and ch["Сертификат GMP действует до"]["new"] == "2028-12-01"
    assert ch["Мощность до"]["new"] == ""  # в новой таблице значения нет — тоже изменение


def test_trim_snapshots_removes_oldest(tmp_path):
    import os
    import time
    for i in range(4):
        f = tmp_path / f"{i:016x}.json.gz"
        f.write_bytes(b"x" * 400_000)
        os.utime(f, (time.time() - 100 + i, time.time() - 100 + i))
    removed = M.trim_snapshots(tmp_path, 1)  # 1 МБ: из 1,6 МБ уходят две самые старые копии
    assert removed == 2 and sorted(p.name[:16] for p in tmp_path.glob("*.json.gz")) == [f"{2:016x}", f"{3:016x}"]


def test_quote_must_carry_the_value():
    num = S.Attribute(name="мощность", kind="number")
    assert S.quote_supports(num, "2500", "млн капсул/год", "年产软胶囊25亿粒")
    assert not S.quote_supports(num, "2500", "млн капсул/год", "annual capacity was expanded in 2024")  # числа нет
    assert not S.quote_supports(S.Attribute(name="формы", kind="list"), "softgel", "", "softgel")  # обрывок
    assert S.quote_supports(S.Attribute(name="срок", kind="date"), "2027-03-15", "", "certificate valid until March 2027")
    assert not S.quote_supports(S.Attribute(name="срок", kind="date"), "2027-03-15", "", "certificate valid until 2025")


def test_added_objects_never_reuse_numbers():
    ents = [{"id": f"O{i}", "name": f"N{i}", "names": [f"N{i}"]} for i in (1, 2, 3, 10)]
    out = M.apply_list_edits(ents, {"drop": ["O2"], "add": ["Новый"]})
    assert out[-1]["id"] == "O11"
    more = M.apply_list_edits([], {"add": ["Ещё"]}, start_n=M._max_id(out))
    assert more[0]["id"] == "O12"


def test_bool_unknown_and_numbers_after_year():
    assert S.parse_bool("нет данных") is None and S.parse_bool("N/A") is None and S.parse_bool("Yuwang") is None
    assert S.parse_bool("нет") is False and S.parse_bool("Yes, NSF") is True
    assert 5e8 in S.numbers_in("в 2024, 500 млн капсул")
    num = S.Attribute(name="мощность", kind="number")
    assert S.quote_supports(num, "500", "млн капсул", "мощность в 2024, 500 млн капсул в год")


def test_judge_choice_parsing():
    assert [M._choice(x) for x in ("a", "(b)", "Option C", "вариант d", "none", "Ни один", "", "abc")] == \
        ["a", "b", "c", "d", "none", "none", "none", "none"]


def test_all_models_search_candidates_with_own_engine():
    c = _council("find")
    r = asyncio.run(c.run("Найти производителей", mode="selection"))
    calls = [e for e in r.transcript if e.get("stage") == "discover"]
    who = {e.get("member") for e in calls}
    assert who == {"факт-чек", *[m.name for m in c.cfg.active_members]}  # модель поиска и все участники
    assert c.stage_search_engine["discover"] == "native"
    assert any(len(e.get("models") or []) >= 2 for e in r.selection["objects"])  # найден несколькими моделями
    c2 = _council("find", discover_by="search")
    r2 = asyncio.run(c2.run("Найти производителей", mode="selection"))
    assert {e.get("member") for e in r2.transcript if e.get("stage") == "discover"} == {"факт-чек"}


def test_models_without_own_search_get_different_engines():
    c = _council("find")
    cfg = c.cfg
    for m, model in zip(cfg.members, ["openai/gpt-x", "moonshotai/kimi-x", "deepseek/ds-x", "qwen/q-x", "google/g-x"]):
        m.model = model
    asyncio.run(c.run("Найти производителей", mode="selection"))
    eng = {k.split(":", 1)[1]: v for k, v in c.stage_search_engine.items() if k.startswith("discover:")}
    others = [m.name for m in cfg.active_members if not m.model.startswith(("openai/", "google/"))]
    assert set(eng) == set(others) and len(set(eng.values())) == min(2, len(others))


def test_unknown_engine_falls_back_to_exa_not_off():
    from council.client import _drop_unsupported
    payload = {"tools": [{"type": "openrouter:web_search", "parameters": {"engine": "parallel"}}]}
    what = _drop_unsupported(payload, "m/x", "HTTP 400: invalid engine 'parallel' for web search")
    assert what.startswith("поисковик") and payload["tools"][0]["parameters"]["engine"] == "exa"


def test_units_text_lists_and_short_chinese_names():
    cap = S.Attribute(id="A1", name="Мощность", kind="number", unit="млн капсул/год", weight=5, better="high")
    assert S.comparable(cap, {"value": "2500", "unit": "млн капсул/год"})
    assert not S.comparable(cap, {"value": "120", "unit": "млн юаней/год"})        # деньги
    assert not S.comparable(cap, {"value": "1 亿粒", "unit": "粒/сутки"})           # в сутки
    assert not S.comparable(cap, {"value": "40000", "unit": "тонн/год"})            # тонны
    t = {"O1": {"A1": {"status": "official", "value": "2500", "unit": "млн капсул/год"}},
         "O2": {"A1": {"status": "official", "value": "120", "unit": "млн юаней/год"}}}
    r = {x["object"]: x["score"] for x in S.ranking([cap], t)}
    assert r["O1"] > 0 and r["O2"] == 0                                               # несопоставимое — не в ранг
    txt = S.Attribute(id="A2", name="Сертификаты", kind="text")
    o = lambda v, k, f=True: {"value": v, "kind": k, "found": f, "quote": v, "url": "u"}  # noqa: E731
    assert S.resolve_cell(txt, [o("NSF GMP; ISO 22000", "official"), o("ISO 22000, HACCP", "document")])["status"] == "document"
    lst = S.Attribute(id="A3", name="Формы", kind="list")
    c = S.resolve_cell(lst, [o("softgel, таблетки", "official"), o("таблетки; порошки", "official")])
    assert c["status"] == "official" and c["value"] == "softgel, таблетки, порошки"
    b = S.Attribute(id="A4", name="OEM", kind="bool")
    assert S.resolve_cell(b, [o("True", "official"), o("да — работает под СТМ", "official")])["value"] == "да"
    assert S.Cell(attr="A1", value="### annual **bottles:** 17000000").value == "annual bottles: 17000000"
    ents = S.merge_candidates([("a", S.Candidate(name="仙乐健康科技股份有限公司")),
                               ("b", S.Candidate(name="Sirio Pharma Co., Ltd.", aliases=["仙乐健康"]))])
    assert len(ents) == 1


def test_discover_plan_rotates_two_models_per_angle():
    from council.config import MemberConfig
    from council.modes.selection import discover_plan
    hunters = [MemberConfig(name=n, model=f"m/{n}") for n in ["факт-чек", "a", "b", "c", "d", "e", "f"]]
    angles = [f"A{i}" for i in range(8)]
    plan = discover_plan(angles, hunters, 2)
    assert len(plan) == 8 * 3                                  # модель поиска + 2 участника на направление
    for a in angles:
        who = [m.name for x, m in plan if x == a]
        assert who[0] == "факт-чек" and len(set(who)) == 3      # разные модели на одном направлении
    load = {n: sum(1 for _, m in plan if m.name == n) for n in "abcdef"}
    assert max(load.values()) - min(load.values()) <= 1        # поровну
    assert len(discover_plan(angles, hunters, 0)) == 8 * 7     # 0 — все по всем
    assert len(discover_plan(angles, hunters[:3], 2)) == 8 * 3  # участников мало — все


def test_discover_uses_own_search_limits_and_short_reasoning(tmp_path):
    """Поиск кандидатов: свой предел поиска (не общий 8×10) и короткие рассуждения у участников."""
    import asyncio
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.modes import selection as SM
    from council.session import Council
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": str(tmp_path), "search_uses": 8, "search_results": 10,
                                        "selection": {"lists": 0},
                                        "members": [{"name": n, "model": f"m/{n}", "reasoning_effort": "high"} for n in "abcd"]})
    seen = []
    client = MockClient(delay=0)
    orig = client.complete

    async def spy(model, messages, **kw):
        seen.append((model, kw.get("search_uses"), kw.get("search_results"), kw.get("reasoning_effort"), kw.get("web_search")))
        return await orig(model, messages, **kw)

    client.complete = spy
    c = Council(cfg, client)
    for st in ("discover", "screen"):
        c.stage_search_limits[st] = (cfg.selection.search_uses, cfg.selection.search_results)
    frame = SM.S.SelectionFrame(refined_question="q", angles=[f"A{i}" for i in range(4)])
    asyncio.run(SM.discover(c, frame, []))
    web = [x for x in seen if x[4]]
    assert web and all(x[1] == 3 and x[2] == 8 for x in web)
    assert all(x[3] in ("low", None) for x in web)  # участники — коротко; модель поиска — без параметра
    members = [x for x in web if x[0].startswith("m/")]
    assert len(members) == 4 * 2  # по 2 участника на направление


def test_drop_repeats_keeps_first_occurrence():
    from council.selection import drop_repeats
    menu = "главная о компании продукция контакты сертификаты новости вакансии карта сайта политика конфиденциальности " * 3
    a = ("https://x.cn/a", menu + "мощность завода 7 млрд капсул в год по данным отчёта за 2025 год")
    b = ("https://x.cn/b", menu + "сертификат gmp действует до 2027 года выдан органом провинции")
    out, cut = drop_repeats([a, b])
    assert cut > 200
    assert "главная о компании" in out[0][1]           # первое появление осталось
    assert "главная о компании" not in out[1][1]       # на второй странице меню убрано
    assert "сертификат gmp" in out[1][1] and "7 млрд" in out[0][1]


def test_document_beats_site_without_judges():
    from council.selection import Attribute, resolve_cell
    a = Attribute(id="A1", name="Мощность", kind="number", unit="млн капсул/год")
    doc = {"value": "7000", "unit": "млн капсул/год", "found": True, "kind": "document", "quote": "q1", "url": "https://r/a.pdf"}
    site = {"value": "9000", "unit": "млн капсул/год", "found": True, "kind": "official", "quote": "q2", "url": "https://x.cn"}
    c = resolve_cell(a, [doc, site])
    assert c["status"] == "document" and c["value"] == "7000" and "документ" in c["auto"]
    # два сайта расходятся — по-прежнему спор для совета
    site2 = {**site, "value": "5000", "quote": "q3"}
    assert resolve_cell(a, [site, site2])["status"] == "disputed"


def test_fill_prompt_shared_start_first():
    from council.selection import CACHE_SPLIT, Attribute, SelectionFrame, fill_prompt
    fr = SelectionFrame(refined_question="q")
    attrs = [Attribute(id="A1", name="Мощность", kind="number")]
    p1 = fill_prompt(fr, {"name": "Завод 1"}, attrs, [("https://a", "текст 1")])
    p2 = fill_prompt(fr, {"name": "Завод 2"}, attrs, [("https://b", "текст 2")], only=["A1"])
    assert p1.split(CACHE_SPLIT)[0] == p2.split(CACHE_SPLIT)[0]   # общее начало одинаково — его кэширует провайдер
    assert "Завод 1" not in p1.split(CACHE_SPLIT)[0] and "ONLY these attributes" in p2


def test_second_read_only_when_needed(tmp_path):
    """Первое прочтение подтвердило всё, что влияет на ранг, цитатой с сайта компании — второе не запускается."""
    import asyncio
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.modes import selection as SM
    from council.session import Council
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": str(tmp_path),
                                        "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]})
    c = Council(cfg, MockClient(delay=0))
    page = "demo plant 1 annual capacity is 7000 million capsules per year"
    calls = []

    async def ask(m, stage, prompt, schema, **kw):
        calls.append((m.name, stage, "ONLY these" in prompt))
        if stage == "sources":
            return SM.S.ObjectSources()
        return SM.S.ObjectCells(cells=[SM.S.Cell(attr="A1", value="7000", unit="млн капсул/год", url="https://p1.cn/about",
                                                 quote="annual capacity is 7000 million capsules per year")])

    async def fetch(c_, urls):
        return {u: {"text": page, "fetched": ""} for u in urls}

    c._ask = ask
    SM.fetch_pages, orig = fetch, SM.fetch_pages
    try:
        attrs = [SM.S.Attribute(id="A1", name="Мощность", kind="number", unit="млн капсул/год", weight=5),
                 SM.S.Attribute(id="A2", name="Примечание", kind="text", weight=0)]
        e = {"id": "O1", "name": "Demo Plant 1", "names": ["Demo Plant 1"], "website": "https://p1.cn",
             "url": "https://p1.cn/about", "urls": ["https://p1.cn/about"]}
        fr = SM.S.SelectionFrame(refined_question="q")
        row = asyncio.run(SM.fill_object(c, fr, attrs, e, [cfg.member("b"), cfg.member("c")]))
    finally:
        SM.fetch_pages = orig
    fills = [x for x in calls if x[1] == "fill"]
    assert len(fills) == 1 and fills[0][0] == "b"                  # второе прочтение не понадобилось
    assert row["cells"]["A1"]["status"] == "official"


def test_discover_waves_stop_when_list_is_full(tmp_path):
    import asyncio
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.modes import selection as SM
    from council.session import Council

    def run(same: bool) -> set[str]:
        cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": str(tmp_path), "selection": {"rounds": 1, "lists": 0},
                                            "members": [{"name": n, "model": f"m/{n}"} for n in "abcd"]})
        c = Council(cfg, MockClient(delay=0))
        seen: set[str] = set()
        n = [0]

        async def ask(m, stage, prompt, schema, meta=None, **kw):
            seen.add(meta["angle"])
            n[0] += 1
            names = [f"Plant {i}" for i in range(6)] if same else [f"Plant {n[0]}-{i}" for i in range(3)]
            return SM.S.Candidates(items=[SM.S.Candidate(name=x) for x in names])

        c._ask = ask
        fr = SM.S.SelectionFrame(refined_question="q", angles=[f"A{i}" for i in range(8)])
        asyncio.run(SM.discover(c, fr, []))
        return seen

    assert len(run(True)) == 4    # все находят одних и тех же — список полон, вторая волна не нужна
    assert len(run(False)) == 8   # каждый находит своих — список далёк от полного, ищем по всем направлениям


CATALOG = [
    {"id": "google/gemini-2-flash", "created": 100, "context_length": 1_000_000, "pricing": {"prompt": "0.0000001"}},
    {"id": "google/gemini-3-flash", "created": 200, "context_length": 1_000_000, "pricing": {"prompt": "0.0000003"}},
    {"id": "google/gemini-3-flash-image", "created": 300, "context_length": 1_000_000, "pricing": {"prompt": "0.0000003"}},
    {"id": "google/gemini-3-pro", "created": 250, "context_length": 1_000_000, "pricing": {"prompt": "0.000002"}},
    {"id": "openai/gpt-6-mini", "created": 220, "context_length": 400_000, "pricing": {"prompt": "0.00000025"},
     "supported_parameters": ["tools", "reasoning"]},
    {"id": "openai/gpt-6-nano", "created": 230, "context_length": 400_000, "pricing": {"prompt": "0.00000005"}},
    {"id": "anthropic/claude-haiku-5.5", "created": 210, "context_length": 200_000, "pricing": {"prompt": "0.000001"}},
    {"id": "anthropic/claude-sonnet-5.5", "created": 215, "context_length": 200_000, "pricing": {"prompt": "0.000003"}},
    {"id": "x-ai/grok-5-fast", "created": 205, "context_length": 2_000_000, "pricing": {"prompt": "0.0000002"}},
    {"id": "perplexity/sonar", "created": 1, "context_length": 128_000, "pricing": {"prompt": "0.000001"}},
    {"id": "perplexity/sonar-pro", "created": 1, "context_length": 200_000, "pricing": {"prompt": "0.000003"}},
]


def test_pick_scouts_newest_junior_models():
    from council.scouts import perplexity_pair, pick_scouts, scout_name
    s = pick_scouts(CATALOG)
    assert s == {"google": "google/gemini-3-flash", "openai": "openai/gpt-6-mini",
                 "anthropic": "anthropic/claude-haiku-5.5", "x-ai": "x-ai/grok-5-fast"}
    assert pick_scouts(None) == {}
    assert perplexity_pair("perplexity/sonar-pro", CATALOG) == ["perplexity/sonar"]
    assert scout_name("x-ai/grok-5-fast") == "поиск xAI" and scout_name("google/gemini-3-flash") == "поиск Google"


def test_scouts_search_every_angle(tmp_path):
    """Разведчики: каждый поисковик — по каждому направлению; модели совета в поиске кандидатов не участвуют;
    xAI — своим поиском."""
    import asyncio
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.modes import selection as SM
    from council.session import Council
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": str(tmp_path), "selection": {"rounds": 1, "discover_waves": False, "lists": 0},
                                        "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]})
    c = Council(cfg, MockClient(delay=0))
    c.catalog = CATALOG
    asked = []

    async def ask(m, stage, prompt, schema, meta=None, **kw):
        asked.append((m.name, m.model, meta["angle"], m.reasoning_effort))
        return SM.S.Candidates(items=[SM.S.Candidate(name=f"Plant {len(asked)}")])

    c._ask = ask
    fr = SM.S.SelectionFrame(refined_question="q", angles=[f"A{i}" for i in range(3)])
    asyncio.run(SM.discover(c, fr, []))
    who = {n for n, *_ in asked}
    assert who == {"факт-чек", "поиск Perplexity", "поиск Google", "поиск OpenAI", "поиск Anthropic", "поиск xAI"}
    assert len(asked) == 3 * 6 and not any(m.startswith("m/") for _, m, *_ in asked)
    assert "discover:поиск xAI" not in c.stage_search_engine  # свой поиск xAI
    assert all(e == "low" for n, m, _, e in asked if m.startswith(("google/", "openai/", "anthropic/")))


def test_neutral_dispute_is_not_judged():
    """Спорная ячейка, при любом значении которой первая группа и лидер те же, — без судей, оба значения в таблице."""
    from council.modes.selection import neutral_disputes
    attrs = [S.Attribute(id="A1", name="Мощность", kind="number", weight=5, better="high"),
             S.Attribute(id="A2", name="Формы", kind="number", weight=1, better="high")]
    num = lambda v: {"status": "official", "value": v}  # noqa: E731
    disp = lambda a, b: {"status": "disputed", "value": "", "options": [  # noqa: E731
        {"value": a, "found": True, "kind": "official"}, {"value": b, "found": True, "kind": "official"}]}
    rows = {"O1": {"cells": {"A1": num("9000"), "A2": num("5")}},
            "O2": {"cells": {"A1": num("5000"), "A2": num("4")}},
            "O3": {"cells": {"A1": num("1000"), "A2": disp("1", "2")}},      # последний при любом значении
            "O4": {"cells": {"A1": disp("8000", "200"), "A2": num("3")}}}   # от значения зависит, попадёт ли в группу
    got = neutral_disputes(attrs, rows, top_n=2)
    assert got == [("O3", "A2")]
    assert rows["O3"]["cells"]["A2"]["neutral"] and rows["O3"]["cells"]["A2"]["value"] == "1 / 2"
    assert not rows["O4"]["cells"]["A1"].get("neutral")


def test_third_judge_only_when_first_two_disagree(tmp_path):
    import asyncio
    from council.modes import selection as SM
    c = _council("find")
    asked = []

    async def ask(m, stage, prompt, schema, meta=None, **kw):
        asked.append((m.name, prompt.count("O1:")))
        pick = {"a": "a", "b": "a" if "O1:A1" in prompt and len(asked) <= 2 else "b"}
        items = [S.Ruling(key="O1:A1", choice="a", reason="r")]
        if "O1:A2" in prompt:
            items.append(S.Ruling(key="O1:A2", choice="a" if len(asked) % 2 else "b", reason="r"))
        return S.Rulings(items=items)

    c._ask = ask
    attrs = [S.Attribute(id="A1", name="Мощность", kind="number"), S.Attribute(id="A2", name="Год", kind="number")]
    opts = [{"value": "1", "found": True, "kind": "official"}, {"value": "2", "found": True, "kind": "official"}]
    rows = {"O1": {"cells": {"A1": {"status": "disputed", "options": opts}, "A2": {"status": "disputed", "options": opts}}}}
    c.cfg.selection.judges = 3
    asyncio.run(SM.resolve_disputes(c, S.SelectionFrame(refined_question="q"), attrs, [{"id": "O1", "name": "Z"}], rows, []))
    assert len(asked) == 3                       # двое сразу, третий — только из-за A2
    assert asked[2][1] == 1                      # третьему — только расхождение (одна ячейка)
    assert rows["O1"]["cells"]["A1"]["value"] == "1"


def test_failing_hunter_is_skipped_after_two_strikes(tmp_path):
    """Модель дважды подряд сбоила на поиске кандидатов — её остальные направления не оплачиваются."""
    import asyncio
    from council.client import ProviderError
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.modes import selection as SM
    from council.session import Council
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": str(tmp_path),
                                        "selection": {"rounds": 1, "discover_waves": False, "discover_by": "all", "discover_models": 0, "lists": 0},
                                        "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]})
    c = Council(cfg, MockClient(delay=0))
    calls = []

    async def ask(m, stage, prompt, schema, meta=None, **kw):
        calls.append(m.name)
        await asyncio.sleep(0)
        if m.name == "b":
            raise ProviderError("пустой ответ модели (finish_reason=error)")
        return SM.S.Candidates(items=[SM.S.Candidate(name=f"Plant {len(calls)}")])

    c._ask = ask
    fr = SM.S.SelectionFrame(refined_question="q", angles=[f"A{i}" for i in range(8)])
    asyncio.run(SM.discover(c, fr, []))
    assert calls.count("b") == 2          # два сбоя — дальше «b» не спрашивают
    assert calls.count("a") == 8 and calls.count("c") == 8


def test_ready_made_lists_add_candidates_and_count_for_completeness():
    """Готовые списки: модель поиска даёт ссылку, программа скачивает, читатель выписывает компании; список —
    независимый источник находок (полнота), цитата — строка списка."""
    import asyncio
    from council.modes import selection as SM
    c = _council("find", rounds=1, discover_waves=False)
    fr = SM.S.SelectionFrame(refined_question="q", object_kind="завод", angles=["демо: участники выставки"])
    listed, n = asyncio.run(SM.harvest_lists(c, fr, SM.discover_hunters(c)))
    names = {x.name for _, x in listed}
    assert n == 1 and "Demo Plant 10" in names and len(names) == 10
    label, cand = listed[0]
    assert label.startswith("список: Демо-выставка") and cand.url == "https://expo.example.com/exhibitors"
    assert "stand a" in cand.quote
    c2 = _council("find", lists=0)
    assert asyncio.run(SM.harvest_lists(c2, fr, SM.discover_hunters(c2))) == ([], 0)


def test_named_competitors_do_not_turn_search_into_check():
    """Задание называет конкурентов (Эвалар, GLS) и исключённую площадку (ORZAX), а просит НАЙТИ заводы: председатель
    решает «find», названные — не кандидаты: поиск идёт, они в список не попадают (случай 15.11)."""
    from council.modes import selection as SM
    c = _council()
    orig = c._ask

    async def ask(m, stage, prompt, schema, **kw):
        if stage == "frame":
            fr = await orig(m, stage, prompt, schema, **kw)
            return fr.model_copy(update={"objects": [], "benchmarks": ["Эвалар", "GLS", "ORZAX", "Demo Plant 7"],
                                         "start": "find"})
        return await orig(m, stage, prompt, schema, **kw)

    c._ask = ask
    q = ("Найди крупные заводы в Китае. Конкуренты для сравнения — Эвалар и GLS.\n"
         "Не включай ранее исключённую площадку ORZAX.\n| 1 | Эвалар |\n| 2 | GLS |\n| 3 | ORZAX |")
    r = asyncio.run(c.run(q, mode="selection"))
    sel = r.selection
    names = [o["name"] for o in sel["objects"]]
    assert sel["start"] == "find"
    assert "discover" in {e.get("stage") for e in r.transcript}
    assert not {"Эвалар", "GLS", "ORZAX", "Demo Plant 7"} & set(names)  # названные «не кандидаты» отброшены
    assert names and all(n.startswith("Demo Plant") for n in names)
