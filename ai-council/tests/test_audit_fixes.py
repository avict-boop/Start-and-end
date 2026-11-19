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


def test_gate_decision_follows_names_when_ids_shift():
    from council.modes import selection as SM
    ents = [{"id": "O5", "name": "BioGrowing Co., Ltd.", "names": ["BioGrowing Co., Ltd.", "均瑶润盈"]},
            {"id": "O6", "name": "Alpha", "names": ["Alpha"]}]
    dec = {"keep": ["O1"], "drop": ["O2"], "_names": {"O1": "BioGrowing Co., Ltd.", "O2": "Alpha"}}
    out = SM.remap_decision(dec, ents)
    assert out["keep"] == ["O5"] and out["drop"] == ["O6"]
    assert SM.remap_decision({"keep": ["O1"]}, ents) == {"keep": ["O1"]}
    two = SM.remap_decision({"keep": ["O9"], "_names": {"O9": "BioGrowing Co., Ltd. / 均瑶润盈"}}, ents)
    assert two["keep"] == ["O5"]  # старые решения без названий — как были


def test_screen_logs_exact_reason_for_missing_rows(tmp_path):
    """Отсев: если модель не вернула строку по кандидату или пачка не удалась — причина в журнале и у кандидата."""
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.session import Council
    from council.modes import selection as SM

    async def pages(urls, texts=False, **kw):
        return {u: {"status": "ok", "text": "demo plant page oem capsules factory in guangzhou"} for u in urls}
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": str(tmp_path),
                                        "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]})
    c = Council(cfg, MockClient(delay=0), link_checker=pages)
    events = []
    c._emit = lambda t, **k: events.append({"type": t, **k})

    asked = []

    async def ask(m, stage, prompt, schema, meta=None, **kw):
        asked.append(m.name)
        return S.ScreenBatch(rows=[])  # модель «забыла» всех
    c._ask = ask
    attrs = [S.Attribute(id="A1", name="OEM", kind="bool", screen=True, knockout="нет")]
    ents = [{"id": "O1", "name": "Alpha", "names": ["Alpha"], "url": "https://alpha.cn/", "website": "https://alpha.cn"}]
    out = asyncio.run(SM.screen_pages(c, S.SelectionFrame(refined_question="q"), attrs, ents))
    assert "нет строки" in out["O1"]["reason"]
    assert any(e["type"] == "warning" and "не ответила" in e["text"] for e in events)
    assert len(asked) == 2 and asked[0] != asked[1]  # не ответила первая — дочитывала вторая модель


def test_screen_backup_model_answers_missed():
    """Первая модель пропустила кандидата — вторая дочитала, у кандидата её ответ, а не «не выяснено»."""
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.session import Council
    from council.modes import selection as SM
    import tempfile

    async def pages(urls, texts=False, **kw):
        return {u: {"status": "ok", "text": "alpha oem factory guangzhou"} for u in urls}
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": tempfile.mkdtemp(),
                                        "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]})
    c = Council(cfg, MockClient(delay=0), link_checker=pages)
    first = []

    async def ask(m, stage, prompt, schema, meta=None, **kw):
        if not first:
            first.append(m.name)
            return S.ScreenBatch(rows=[])
        return S.ScreenBatch.model_validate({"rows": [{"object": "Alpha", "checks": [
            {"attr": "A1", "verdict": "pass", "value": "да", "url": "https://alpha.cn/", "quote": "alpha oem factory"}]}]})
    c._ask = ask
    attrs = [S.Attribute(id="A1", name="OEM", kind="bool", screen=True, knockout="нет")]
    ents = [{"id": "O1", "name": "Alpha", "names": ["Alpha"], "url": "https://alpha.cn/", "website": "https://alpha.cn"}]
    out = asyncio.run(SM.screen_pages(c, S.SelectionFrame(refined_question="q"), attrs, ents))
    assert out["O1"]["result"] == "pass"


def test_second_look_reads_contact_page():
    """Отсев: адреса на главной нет — программа берёт ссылку «Контакты» со страницы и дочитывает её."""
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.session import Council
    from council.modes import selection as SM
    import tempfile

    async def pages(urls, texts=False, **kw):
        out = {}
        for u in urls:
            if u.endswith("/lxwm/"):
                out[u] = {"status": "ok", "text": "地址：广东省广州市白云区 alpha factory"}
            else:
                out[u] = {"status": "ok", "text": "alpha oem capsules", "links": ["https://alpha.cn/lxwm/"]}
        return out
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": tempfile.mkdtemp(),
                                        "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]})
    c = Council(cfg, MockClient(delay=0), link_checker=pages)

    async def ask(m, stage, prompt, schema, meta=None, **kw):
        found = "广州市" in prompt
        return S.ScreenBatch.model_validate({"rows": [{"object": "Alpha", "checks": [
            {"attr": "A1", "verdict": "pass" if found else "unclear", "value": "Гуанчжоу" if found else "",
             "url": "https://alpha.cn/lxwm/" if found else "", "quote": "广东省广州市白云区" if found else ""}]}]})
    c._ask = ask
    attrs = [S.Attribute(id="A1", name="Площадка", kind="text", screen=True, knockout="вне Китая")]
    ents = [{"id": "O1", "name": "Alpha", "names": ["Alpha"], "url": "https://alpha.cn/", "website": "https://alpha.cn"}]
    out = asyncio.run(SM.screen_pages(c, S.SelectionFrame(refined_question="q"), attrs, ents))
    assert out["O1"]["result"] == "pass"


def test_required_coverage_and_group_merge():
    from council.modes import selection as SM
    fr = S.SelectionFrame(refined_question="q", required_angles=["пробиотики", "омега"])
    mk = lambda i, a: {"id": f"O{i}", "name": f"N{i}", "angles": [a + S.BY + "x"]}
    keep = [mk(1, "омега"), mk(2, "омега"), mk(3, "омега"), mk(4, "пробиотики"), mk(5, "пробиотики")]
    cov = SM.required_coverage(fr, keep, keep[:3])
    pro = next(x for x in cov if x["angle"] == "пробиотики")
    assert pro["added"] == 2 and [e["id"] for e in pro["_add"]] == ["O4", "O5"]
    ents = [{"id": "O1", "name": "BioGrowing", "names": ["BioGrowing"], "group": "均瑶健康", "angles": ["a"], "urls": []},
            {"id": "O2", "name": "均瑶健康", "names": ["均瑶健康"], "group": "", "angles": ["b"], "urls": []}]
    hints = S.group_hints(ents)
    assert hints and set(hints[0]["ids"]) == {"O1", "O2"}
    merged = S.merge_entities(ents, [["O1", "O2"]])
    assert len(merged) == 1 and set(merged[0]["names"]) == {"BioGrowing", "均瑶健康"} and len(merged[0]["angles"]) == 2


def test_rule_less_screen_attribute_is_preference():
    """Признак отсева без правила исключения не исключает; «подходит» — по условиям с правилом."""
    hard = S.Attribute(id="A1", name="Площадка", kind="text", screen=True, knockout="вне Китая")
    soft = S.Attribute(id="A2", name="Собственное сырьё", kind="text", screen=True, knockout="")
    q = "сырьё закупается по всему миру у поставщиков"
    r = S.screen_decide([S.ScreenCheck(attr="A1", verdict="pass", value="Гуанчжоу"),
                         S.ScreenCheck(attr="A2", verdict="fail", value="закупает", quote=q)], [hard, soft])
    assert r[0] == "pass"
    r = S.screen_decide([S.ScreenCheck(attr="A1", verdict="unclear"),
                         S.ScreenCheck(attr="A2", verdict="fail", value="закупает", quote=q)], [hard, soft])
    assert r[0] == "unclear" and "Собственное" not in r[1]
    assert "PREFERENCE" in S.screen_read_prompt(S.SelectionFrame(refined_question="q"), [hard, soft],
                                                [{"id": "O1", "name": "X"}], {"O1": []})


def test_weak_sources_intermediary_and_city_core_merge(tmp_path):
    """Разбор 17.11: «подходит» по карточке Alibaba или пресс-релизу — не подтверждение; признаки посредника —
    не «подходит»; «威海百合» и «百合（威海）» — один кандидат; прежний запуск сохраняется в диагностике."""
    from council.diagnostics import save_events
    hard = S.Attribute(id="A1", name="OEM", kind="bool", screen=True, knockout="нет")
    ok = S.ScreenCheck(attr="A1", verdict="pass", value="да", url="https://www.alibaba.com/product/1.html",
                       quote="oem odm service available")
    assert S.screen_decide([ok], [hard])[0] == "unclear"
    own = ok.model_copy(update={"url": "https://www.factory-x.cn/oem"})
    assert S.screen_decide([own], [hard])[0] == "pass"
    mid = own.model_copy(update={"quote": "production runs in our partner facility in guangzhou"})
    assert S.screen_decide([mid], [hard])[0] == "unclear"
    a = S.Candidate(name="威海百合生物技术股份有限公司")
    b = S.Candidate(name="百合生物（威海）", location="")
    c = S.Candidate(name="上海百合")
    ents = S.merge_candidates([("x", a), ("y", b), ("z", c)])
    assert len(ents) == 2  # Вэйхай склеен, Шанхай — другая компания
    save_events(tmp_path, "s1", [{"type": "start", "ts": 100.0}])
    save_events(tmp_path, "s1", [{"type": "start", "ts": 200.0}])
    assert (tmp_path / "s1.events.100.jsonl").is_file() and (tmp_path / "s1.events.jsonl").is_file()


def test_own_site_and_find_sites():
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.session import Council
    from council.modes import selection as SM
    import tempfile
    assert SM.own_site({"website": "https://www.sirio.com"}) and not SM.own_site({"website": "https://foodtalks.cn/a"})
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": tempfile.mkdtemp(),
                                        "members": [{"name": n, "model": f"m/{n}"} for n in "ab"]})
    c = Council(cfg, MockClient(delay=0))

    async def ask(m, stage, prompt, schema, meta=None, **kw):
        assert stage == "sites"
        return S.Candidates(items=[S.Candidate(name="Sirio", website="https://www.siriopharma.com"),
                                   S.Candidate(name="Beta", website="https://www.alibaba.com/beta")])
    c._ask = ask
    ents = [{"id": "O1", "name": "Sirio", "names": ["Sirio"], "website": ""},
            {"id": "O2", "name": "Beta", "names": ["Beta"], "website": ""}]
    assert asyncio.run(SM.find_sites(c, ents)) == 1
    assert ents[0]["website"].endswith("siriopharma.com") and ents[1]["website"] == ""


def test_traders_registry_and_required_only():
    """Разбор 17.11: торговцы по названию — мимо отсева; находка из реестра поднимает кандидата;
    GPT mini ищет только по обязательным поискам."""
    from council.modes import selection as SM
    assert S.trader_name({"names": ["Guangzhou ABC Trading Co., Ltd."]})
    assert S.trader_name({"names": ["广州某某贸易有限公司"]})
    assert not S.trader_name({"names": ["仙乐健康科技股份有限公司"]})
    reg = {"id": "O1", "angles": ["реестр: SC" + S.BY + "x"], "screen": {"checks": []}}
    plain = {"id": "O2", "angles": ["список: выставка" + S.BY + "x"], "screen": {"checks": []}}
    assert SM.screen_passes(reg) > SM.screen_passes(plain)


def test_rewind_keeps_answers_received_after_it(tmp_path):
    """17.11: после «поиск заново» и затем «отсев заново» поиск снова оплачивался — ответы поиска, полученные после
    первого возврата, не подставлялись по номеру. Теперь не подставляются только ответы ДО возврата."""
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    j.entry({"stage": "discover", "member": "a", "call": 0, "parsed": {"v": "old"}})
    j.rewind(["discover"], ["screen"])
    j.entry({"stage": "discover", "member": "a", "call": 0, "parsed": {"v": "new"}})
    j.entry({"stage": "screen", "member": "a", "call": 0, "parsed": {"v": "s1"}})
    j.rewind(["screen"], [])
    j2 = Journal(p)
    assert j2.loose[("discover", "a", 0)] == {"v": "new"}   # поиск после возврата — подставляется
    assert ("screen", "a", 0) not in j2.loose              # отсев забыт — спросим заново


def test_prior_finds_survive_new_search(tmp_path):
    """Компания, найденная в прежнем запуске (потом «поиск заново»), не теряется."""
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.session import Council
    from council.modes import selection as SM
    p = tmp_path / "j.jsonl"
    j = Journal(p)
    j.entry({"stage": "list_read", "member": "x", "call": 0,
             "parsed": {"items": [{"name": "Penglai Marine (Shandong) Co., Ltd", "local_name": "蓬莱海洋（山东）股份有限公司"}]}})
    j.rewind(["lists", "list_read", "discover"], ["screen"])
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": str(tmp_path),
                                        "members": [{"name": n, "model": f"m/{n}"} for n in "ab"]})
    c = Council(cfg, MockClient(delay=0), journal=Journal(p))
    names = [cd.name for _, cd in SM.prior_finds(c)]
    assert any("Penglai" in n for n in names)


def test_screen_consistency_planned_and_pending():
    """Разбор итога 17.11: «подходит» понижается признаком посредника только по цитате (не по пересказу модели);
    строящаяся линия — не производство; кто не вошёл в таблицу — в «требуют проверки» с открытым вопросом;
    свой сайт важнее карточки площадки при склейке; торговое название — вопрос, а не исключение."""
    from council.modes import selection as SM
    hard = S.Attribute(id="A1", name="Готовые формы", kind="bool", screen=True, knockout="нет")
    ok = S.ScreenCheck(attr="A1", verdict="pass", value="Manufacturer/Factory & Trading Company",
                       url="https://www.biogrowing.com/about", quote="softgel and tablet production lines")
    assert S.screen_decide([ok], [hard])[0] == "pass"
    mid = ok.model_copy(update={"quote": "we are a sourcing agent for nutrition brands"})
    r = S.screen_decide([mid], [hard])
    assert r[0] == "unclear" and "biogrowing.com" in r[1]
    plan = ok.model_copy(update={"quote": "公司计划建设年产10亿粒软胶囊生产线，预计2027年投产"})
    r = S.screen_decide([plan], [hard])
    assert r[0] == "unclear" and "проект" in r[1]
    a = S.Candidate(name="Aland", website="https://www.alibaba.com/aland")
    b = S.Candidate(name="Aland", website="https://www.ivcinc.cn")
    assert S.merge_candidates([("x", a), ("y", b)])[0]["website"] == "https://www.ivcinc.cn"
    ents = [{"id": "O1", "name": "A", "angles": ["p"], "screen": {"result": "pass", "checks": []}},
            {"id": "O2", "name": "B", "names": ["B", "乙"], "angles": ["p", "q"],
             "screen": {"result": "unclear", "reason": "не выяснено: Готовые формы", "checks": []}},
            {"id": "O3", "name": "C", "angles": [], "screen": {"result": "fail", "reason": "нет", "checks": []}},
            {"id": "O4", "name": "D", "angles": [], "screen": {"result": "pass", "checks": []}}]
    pend = SM.pending_list(ents, {"O1"}, {"O3"}, [hard])
    assert [x["id"] for x in pend] == ["O4", "O2"]
    assert pend[1]["open"] == ["Готовые формы"] and pend[0]["passed"]
    md = S.report_md({"frame": {}, "attributes": [], "objects": [], "table": {}, "ranking": [], "top_n": 0,
                      "pending": pend}, None)
    assert "требуют проверки (2)" in md and "乙" in md


def test_expert_opinion_any_yes_keeps():
    """Эксперт по памяти: «да» хоть одного — эксперт или проверка — кандидат подходит; пометка, кто подтвердил."""
    assert S.agree_label({"expert": {"verdict": "yes"}, "screen": {"result": "pass"}}) == "подтвердили эксперт и проверка"
    assert S.agree_label({"expert": {"verdict": "yes"}, "screen": {"result": "fail"}}).startswith("только эксперт")
    assert S.agree_label({"expert": {"verdict": "no"}, "screen": {"result": "pass"}}).startswith("подтвердила проверка")
    assert S.agree_label({"expert": {"verdict": "no"}, "screen": {"result": "unclear"}}).startswith("не подтвердили")
    assert S.suits({"expert": {"verdict": "yes"}, "screen": {"result": "fail"}})
    assert not S.suits({"expert": {"verdict": "unknown"}, "screen": {"result": "unclear"}})
    assert S.ExpertItem.model_validate({"n": 1, "verdict": "Да"}).verdict == "yes"
    assert S.ExpertItem.model_validate({"n": 1, "verdict": "maybe"}).verdict == "unknown"
    p = S.expert_prompt(S.SelectionFrame(refined_question="q"), [], [{"name": "Sirio", "names": ["Sirio", "仙乐"]}])
    assert "1. Sirio (also: 仙乐)" in p and "unknown" in p


def test_expert_in_run_mock(tmp_path):
    """Демо-заседание: эксперт опрашивается, его «да» попадает в таблицу с пометкой."""
    from council.config import CouncilConfig
    from council.mock import MockClient
    from council.modes import selection as SM
    from council.session import Council
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": str(tmp_path),
                                        "members": [{"name": n, "model": f"m/{n}"} for n in "ab"]})
    c = Council(cfg, MockClient(delay=0))
    ents = [{"id": f"O{i}", "name": f"Demo Plant {i}", "names": [f"Demo Plant {i}"], "angles": [],
             "screen": {"result": "unclear", "checks": []}} for i in range(1, 7)]
    asyncio.run(SM.expert_opinion(c, S.SelectionFrame(refined_question="q"), [], ents))
    yes = [e["id"] for e in ents if e["expert"]["verdict"] == "yes"]
    assert yes == ["O3", "O6"] and ents[2]["expert"]["yes"] == ["a"]
    assert sorted(ents, key=SM._order)[0]["id"] in yes


def test_several_experts_any_yes(tmp_path):
    """Несколько экспертов — любые модели OpenRouter: «да» хоть одного — подходит; видно, кто что сказал; счёт
    по экспертам и сколько их «да» подтвердила проверка."""
    from council.config import CouncilConfig, SelectionConfig
    from council.mock import MockClient
    from council.modes import selection as SM
    from council.session import Council
    import pytest
    with pytest.raises(ValueError):
        SelectionConfig(experts=["gpt 5"])
    assert SelectionConfig(experts="x/a, y/b\nx/a").experts == ["x/a", "y/b"]
    cfg = CouncilConfig.model_validate({"chair": "a", "runs_dir": str(tmp_path),
                                        "members": [{"name": n, "model": f"m/{n}"} for n in "ab"],
                                        "selection": {"experts": ["m/b", "il/model-x"]}})
    c = Council(cfg, MockClient(delay=0))
    whos = SM.expert_members(c)
    assert [m.name for m in whos] == ["b", "эксперт model-x"]

    async def ask(m, stage, prompt, schema, meta=None, **kw):
        assert stage == "expert"
        v = {"b": ["yes", "no"], "эксперт model-x": ["no", "unknown"]}[m.name]
        return S.Expert(items=[S.ExpertItem(n=1, verdict=v[0], knows=f"{m.name} знает"), S.ExpertItem(n=2, verdict=v[1])])
    c._ask = ask
    ents = [{"id": "O1", "name": "Alpha", "angles": [], "screen": {"result": "pass", "checks": []}},
            {"id": "O2", "name": "Beta", "angles": [], "screen": {"result": "unclear", "checks": []}}]
    st = asyncio.run(SM.expert_opinion(c, S.SelectionFrame(refined_question="q"), [], ents))
    assert ents[0]["expert"]["verdict"] == "yes" and ents[0]["expert"]["no"] == ["эксперт model-x"]
    assert ents[1]["expert"]["verdict"] == "no"
    assert S.expert_votes(ents[0]) == "да — b; нет — эксперт model-x"
    assert st[0] == {"name": "b", "model": "m/b", "yes": 1, "no": 1, "unknown": 0, "confirmed": 1}
    md = S.report_md({"frame": {}, "attributes": [], "objects": [], "table": {}, "ranking": [], "top_n": 0,
                      "experts": st}, None)
    assert "Эксперты: чьи «да»" in md and "il/model-x" in md


def test_order_tiers_survive_diverse_order():
    """Чередование по направлениям — только внутри ступени: подтверждённые страницей и экспертом → страницей →
    только экспертом → прочие; «не выяснено» не обгоняет подтверждённых экспертом."""
    from council.modes import selection as SM

    def e(i, res, yes, ang):
        return {"id": i, "name": i, "angles": [ang], "screen": {"result": res, "checks": []},
                "expert": {"verdict": "yes" if yes else "unknown", "yes": ["x"] if yes else []}}
    ents = [e("U1", "unclear", False, "a"), e("X1", "unclear", True, "b"), e("P1", "pass", False, "c"),
            e("B1", "pass", True, "d"), e("U2", "unclear", False, "e"), e("X2", "fail", True, "a")]
    out = SM.diverse_order(sorted(ents, key=SM._order))
    ids = [x["id"] for x in out]
    assert ids[:2] == ["B1", "P1"] and set(ids[2:4]) == {"X1", "X2"} and set(ids[4:]) == {"U1", "U2"}


def test_biogrowing_group_and_subsidiary_one_object():
    """19.11: BioGrowing склеился с материнской 湖北均瑶大健康饮品 и назывался ею; отсев счёл страницу дочерней
    «не про объект». Теперь имя — не группа «через дочернюю», а все названия — один объект для отсева и экспертов."""
    cs = [S.Candidate(name="Hubei Junyao Healthy Beverage Co., Ltd. (через 均瑶润盈 / Junyao Runying)",
                      local_name="湖北均瑶大健康饮品股份有限公司", website="https://www.biogrowing.cn/"),
          S.Candidate(name="均瑶润盈生物科技（上海）有限公司", website="https://www.biogrowing.cn/", group="湖北均瑶大健康饮品股份有限公司"),
          S.Candidate(name="均瑶润盈生物科技（上海）有限公司")]
    ents = S.merge_candidates([("a", cs[0]), ("b", cs[1]), ("c", cs[2])])
    assert len(ents) == 1 and ents[0]["name"].startswith("均瑶润盈")
    p = S.screen_read_prompt(S.SelectionFrame(refined_question="q"), [], ents, {})
    assert "ONE OBJECT = ALL ITS NAMES" in p and "湖北均瑶大健康饮品股份有限公司" in p
    assert "SAME company" in S.expert_prompt(S.SelectionFrame(refined_question="q"), [], ents)


def test_review_choice_survives_and_accepted_checks():
    """Разбор 18.11: отметки на неизменном списке сохраняются по номерам; тот же номер с его названием — он же;
    принятая программой оценка признака (площадка → «не выяснено») хранится и идёт в карточку."""
    from council.modes import selection as SM
    ents = [{"id": "O239", "name": "Henan Runling Pharmaceutical Co., Ltd. / Runling Technology Group",
             "names": ["Henan Runling Pharmaceutical Co., Ltd."]},
            {"id": "O19", "name": "威海百合生物技术股份有限公司", "names": ["威海百合生物技术股份有限公司"]},
            {"id": "O63", "name": "威海百合生物技術股份有限公司", "names": ["威海百合生物技術股份有限公司"]}]
    dec = {"keep": ["O239", "O19", "O63"], "_names": {e["id"]: e["name"] for e in ents}, "_snap": SM.snapshot_key(ents)}
    assert SM.remap_decision(dec, ents)["keep"] == ["O239", "O19", "O63"]
    dec2 = {**dec, "_snap": "other"}
    assert SM.remap_decision(dec2, ents)["keep"] == ["O239", "O19", "O63"]   # номер и название — его
    moved = [{**ents[0], "id": "O240"}, ents[1], ents[2]]
    assert SM.remap_decision({"keep": ["O239"], "_names": {"O239": ents[0]["name"]}}, moved)["keep"] == ["O240"]
    weak = S.ScreenCheck(attr="A1", verdict="pass", url="https://www.made-in-china.com/x", quote="oem")
    assert S.accepted_checks([weak])[0].verdict == "unclear"


def test_prior_generic_alias_and_waves_without_share():
    """Прежняя находка с общим алиасом «Biotech» не пропадает; пустая первая волна не останавливает поиск."""
    a = S.Candidate(name="Delta Sunrise", aliases=["Biotech"])
    b = S.Candidate(name="Ocean Aurora", aliases=["Biotech"])
    k = lambda cd: {S.norm_name(x) for x in [cd.name, *cd.aliases] if S._name_key_ok(S.norm_name(x))}
    assert not (k(a) & k(b))


def test_one_wrong_find_does_not_bridge_two_companies():
    """19.11: Perplexity выдал «BioGrowing / 百合生物科技股份有限公司» — BioGrowing склеился с Weihai Baihe. Две группы
    с разными своими сайтами по имени не склеиваются; полное китайское юрназвание до знака — склеивает и при двух
    сайтах; «A / B» и «… (Biohigh)» — те же названия."""
    f = [("a", S.Candidate(name="BioGrowing Co., Ltd.", local_name="均瑶润盈生物科技（上海）有限公司", website="https://www.biogrowing.cn/")),
         ("b", S.Candidate(name="Weihai Baihe Biotechnology Co., Ltd.", local_name="百合生物科技股份有限公司", website="http://www.baiheshengwu.com")),
         ("c", S.Candidate(name="BioGrowing Co., Ltd.", local_name="百合生物科技股份有限公司", website="https://www.made-in-china.com/x")),
         ("d", S.Candidate(name="Weihai Baihe Biology Technological Co., Ltd. (Biohigh)")),
         ("e", S.Candidate(name="威海百合生物技术股份有限公司 / Weihai Baihe Biology Technological Co., Ltd.", website="http://www.baiheshengwu.com")),
         ("f", S.Candidate(name="南京泽朗生物科技有限公司", website="https://zlagr.com")),
         ("g", S.Candidate(name="南京泽朗生物科技有限公司", website="https://zelangjt.com.cn"))]
    ents = S.merge_candidates(f)
    by = {e["name"]: e for e in ents}
    bio = next(e for e in ents if "biogrowing.cn" in e["website"])
    baihe = next(e for e in ents if "baiheshengwu" in e["website"])
    assert bio is not baihe and "Weihai Baihe Biology Technological Co., Ltd. (Biohigh)" in baihe["names"]
    assert sum(1 for e in ents if "泽朗" in e["name"]) == 1
