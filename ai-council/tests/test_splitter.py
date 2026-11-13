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
