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
    sens = S.sensitivity(attrs, t, {"O1": "Первый", "O2": "Второй", "O3": "Третий"})
    assert any(x["a"] == "O2" and x["b"] == "O3" and x["attr"] == "A2" for x in sens)


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
    c = _council("find")
    r = asyncio.run(c.run("Найти контрактных производителей софтгелей", mode="selection"))
    sel = r.selection
    assert r.mode == "selection" and sel["candidates"] >= 5
    assert [x["name"] for x in sel["excluded"]] == ["Demo Plant 8"]  # отсев: перепродавец
    assert sel["stats"]["official"] > 0 and sel["stats"]["ruled"] == 1  # спорная ячейка решена советом
    plant3 = next(o["id"] for o in sel["objects"] if o["name"] == "Demo Plant 3")
    assert sel["table"][plant3]["A2"]["ruled"]
    assert r.verdict.winner == "Demo Plant 7" and "### Ранг" in r.verdict.final_answer
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
