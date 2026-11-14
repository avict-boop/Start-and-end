import asyncio

from council import series as SR
from council import splitter as SP


def test_fix_groups_removes_extra_and_duplicates_and_keeps_lost():
    objs = ["A", "B", "C", "D", "E"]
    groups, notes = SP.fix_groups(objs, [["A", "B", "Z"], ["B", "C"]], per_part=2)
    assert SR.check_split(objs, groups)["ok"]
    assert any("Z" in n for n in notes) and any("повтор" in n for n in notes) and any("не распределила" in n for n in notes)


def test_part_text_keeps_whole_task():
    task = "Задание с правилами.\n\n| 1 | A Ltd | x |\n| 2 | B Ltd | y |"
    t = SR.part_text(task, ["A Ltd"], 1, 2)
    assert t.startswith(task) and "- A Ltd" in t and "1 из 2" in t


def test_check_split_reports_problems():
    c = SR.check_split(["A", "B"], [["A", "A"], ["X"]])
    assert not c["ok"] and c["missing"] == ["B"] and c["twice"] == ["A"] and c["extra"] == ["X"]


def test_plan_without_model_uses_table_and_splits():
    task = "Проверь:\n| № | Компания |\n|---|---|\n" + "\n".join(f"| {i} | Firm{i:02d} Co |" for i in range(1, 21))
    d = asyncio.run(SP.plan(task, None, None, per_part=8, facts=2))
    assert len(d["objects"]) == 20 and d["need_split"] and d["checks"]["ok"] and len(d["parts"]) == 3
    assert all(p["settings"]["arbitration_claims"] >= len(p["objects"]) * 2 for p in d["parts"])


def test_plan_small_or_no_list_does_not_split():
    assert not asyncio.run(SP.plan("Каков рынок кофеен?", None, None))["need_split"]
    task = "| 1 | Alpha Co |\n| 2 | Beta Co |"
    d = asyncio.run(SP.plan(task, None, None, per_part=8))
    assert not d["need_split"] and d["parts"][0]["objects"] == ["Alpha Co", "Beta Co"]


class _Fake:
    def __init__(self, text):
        self.text, self.prompts = text, []

    async def complete(self, model, messages, **kw):
        self.prompts.append(messages[-1]["content"])

        class C:
            pass
        c = C()
        c.text = self.text
        return c


def test_replan_follows_principle_and_count_and_keeps_everything():
    objs = [f"Firm{i:02d}" for i in range(1, 13)]
    import json
    fake = _Fake(json.dumps({"reason": "по провинциям", "groups": [
        {"title": "Шаньдун", "objects": objs[:4]}, {"title": "Гуандун", "objects": objs[4:8]},
        {"title": "Прочие", "objects": objs[8:]}]}))
    d = asyncio.run(SP.replan("Задание", fake, "m", objs, n_parts=3, principle="по провинциям"))
    assert [p["title"] for p in d["parts"]] == ["Шаньдун", "Гуандун", "Прочие"] and d["checks"]["ok"]
    assert "по провинциям" in fake.prompts[0] and "EXACTLY 3 parts" in fake.prompts[0]
    # просили 4, модель дала 3 — программа выравнивает до 4, ничего не теряя
    d = asyncio.run(SP.replan("Задание", fake, "m", objs, n_parts=4))
    assert len(d["parts"]) == 4 and d["checks"]["ok"] and any("вместо 4" in n for n in d["notes"])


def test_replan_without_model_chunks_into_requested_parts():
    objs = [f"Firm{i:02d}" for i in range(1, 11)]
    d = asyncio.run(SP.replan("Задание", None, None, objs, n_parts=5, principle="по размеру"))
    assert len(d["parts"]) == 5 and d["checks"]["ok"] and any("демо" in n for n in d["notes"])


def test_apply_spec_gives_part_objects_and_part_ledgers_same_for_web_and_bot():
    class Arch:
        def load(self, sid):
            if sid == "gone":
                raise KeyError(sid)
            return {"research": {"ledger": {"claims": [{"id": "A-C1", "status": "confirmed"}], "evidence": []}}}

    class C:
        fixed_objects, inherited = [], []

    c = C()
    SR.apply_spec(c, {"objects": ["A Ltd", "B Ltd"], "inherit": [[1, "s1"], [2, "gone"]]}, Arch())
    assert c.fixed_objects == ["A Ltd", "B Ltd"] and [n for n, _ in c.inherited] == [1]
    SR.apply_spec(c, {}, Arch())  # обычное заседание: ничего лишнего
    assert c.fixed_objects == [] and c.inherited == []


def test_fix_groups_accepts_slightly_different_spelling():
    objs = ["山东禹王制药 / Shandong Yuwang", "Weihai Baihe (威海百合)"]
    groups, notes = SP.fix_groups(objs, [["山东禹王制药/Shandong Yuwang", "weihai baihe  （威海百合）"]], 8)
    assert groups == [objs] and not notes  # то же название в другом написании — не «лишний» объект


def test_cap_groups_respects_objects_per_part():
    groups, titles, notes = SP.cap_groups([[f"o{i}" for i in range(15)], ["x", "y"]], ["Шаньдун", "Прочие"], 10)
    assert [len(g) for g in groups] == [8, 7, 2] and titles[:2] == ["Шаньдун (1/2)", "Шаньдун (2/2)"] and notes


def test_advisor_and_chair_use_one_rule_and_model_facts():
    import json
    objs = [f"Firm{i:02d} Co" for i in range(1, 13)]
    task = "Проверь по 3 факта:\n" + "\n".join(f"| {i} | {o} |" for i, o in enumerate(objs, 1))
    # модель говорит «помещается в одно заседание», но объектов больше «объектов в части» — делим, как и председатель
    fake = _Fake(json.dumps({"objects": objs, "one_session": True, "reason": "влезет", "groups": []}))
    d = asyncio.run(SP.plan(task, fake, "m", per_part=10, facts=1))
    assert d["need_split"] and SP.need_split(12, 10) and "влезет" not in d["reason"]
    assert d["facts_per_object"] == 1  # модель не назвала число фактов — из настроек, а не «3 по умолчанию»
    assert all(len(p["objects"]) <= 10 for p in d["parts"]) and d["checks"]["ok"]


def test_chair_split_audits_missing_objects_and_keeps_model_facts():
    import json as _json
    from council import series as _SR
    from council.mock import MockClient
    from council.session import Council
    from tests.test_research import make_cfg, run

    objs = [f"Firm{i:02d} Co" for i in range(1, 13)]
    q = "Проверь каждую компанию:\n" + "\n".join(f"| {i} | {o} |" for i, o in enumerate(objs, 1)) + "\n| 13 | Zeta Extra Co |"

    class C(MockClient):
        async def complete(self, model, messages, **kw):
            st = (kw.get("meta") or {}).get("stage")
            comp = await super().complete(model, messages, **kw)
            if st == "framing":
                d = _json.loads(comp.text)
                d["objects"] = objs  # постановка задачи «потеряла» последнюю строку таблицы
                comp.text = _json.dumps(d, ensure_ascii=False)
            elif st == "split_audit":
                comp.text = _json.dumps({"missing": ["Zeta Extra Co", "Придуманная Корп"]}, ensure_ascii=False)
            elif st == "split":
                comp.text = _json.dumps({"facts_per_object": 3, "groups": [{"title": "Все", "objects": objs}]},
                                        ensure_ascii=False)
            return comp

    cfg = make_cfg(mode="research", research={"auto_split": True, "objects_per_part": 10})
    c = Council(cfg, C(delay=0), seed=1)
    c.allow_split = True
    try:
        run(c.run(q))
        raise AssertionError("должно было разбиться")
    except _SR.SplitRequested as e:
        plan = e.plan
    assert "Zeta Extra Co" in plan["objects"] and "Придуманная Корп" not in plan["objects"]  # пропуск найден, выдумка — нет
    assert plan["checks"]["ok"] and plan["facts_per_object"] == 3
    assert all(len(p["objects"]) <= 10 for p in plan["parts"])  # группа «Все» из 12 — разделена по настройке
