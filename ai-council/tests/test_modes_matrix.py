"""Все режимы × их настройки × связи между ними (уточнение из другого режима, «Доделать», отчёты, оценка) — демо.

Каждый случай: заседание доходит до итога, в архиве есть итог и отчёт, этапы — те, что положены режиму, настройки
другого режима на него не влияют.
"""
from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from council.web.app import create_app


@pytest.fixture(scope="module")
def mc(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("matrix")
    cfg = {"chair": "a", "devil_advocate": "b", "runs_dir": str(tmp / "runs"),
           "members": [{"name": n, "model": f"m/{n}"} for n in "abcd"]}
    path = tmp / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    import os
    os.environ.pop("OPENROUTER_API_KEY", None)
    with TestClient(create_app(path, mock_delay=0)) as c:
        c.runs = tmp / "runs"
        yield c


def events(c, sid):
    out = []
    with c.stream("GET", f"/api/sessions/{sid}/events") as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


BASE = {"mock": True, "members": ["a", "b", "c", "d"], "chair": "a"}
Q = "Как назвать кофейню у университета и стоит ли её открывать?"
OPTS = ["Кофейня «Конспект»", "Кофейня «Перемена»", "Кофейня «Сессия»"]

# режим → (этапы, которые должны быть, этапов, которых быть не должно)
STAGES = {
    "debate": ({"framing", "proposal", "critique", "revision", "vote", "verdict"}, {"arbitration"}),
    "quick": ({"framing", "proposal", "vote", "verdict"}, {"critique", "revision", "arbitration"}),
    "evaluate": ({"framing", "critique", "vote", "verdict"}, {"proposal", "revision", "arbitration"}),
    "research": ({"framing", "proposal", "critique", "arbitration", "revision", "vote", "verdict"}, {"factcheck"}),
    "creative": ({"framing", "proposal", "critique", "revision", "vote", "verdict"}, {"factcheck", "arbitration"}),
}

CASES = [
    ("debate", {}),
    ("debate", {"rounds": 2, "compact": True, "chair_proposes": True, "fact_check": True, "answer_size": "full"}),
    ("debate", {"web_search": ["b"], "devil_advocate": "c", "answer_size": "brief"}),
    ("quick", {}),
    ("quick", {"compact": True, "fact_check": True, "chair_proposes": True}),
    ("evaluate", {"options": OPTS}),
    ("evaluate", {"options": OPTS, "compact": True, "chair_proposes": True}),  # вариантов авторов нет — без влияния
    ("evaluate", {"options": OPTS, "criteria": ["Запоминаемость", "Уместность"], "fact_check": True,
                  "web_search": ["b"]}),  # поиск у «выбора» не нужен — не должен ломать
    ("research", {}),
    ("research", {"research_words": 0, "research": {"catalog": True, "facts_per_object": 2}, "web_search": ["b", "c"]}),
    ("research", {"fact_check": True, "compact": True}),  # чужие для исследования галочки — без вреда
    ("creative", {}),
    ("creative", {"fact_check": True, "compact": True, "web_search": ["b"], "rounds": 3}),  # чужие — без вреда
]


@pytest.mark.parametrize("mode,extra", CASES, ids=[f"{m}-{i}" for i, (m, _) in enumerate(CASES)])
def test_every_mode_with_its_options(mc, mode, extra):
    body = {**BASE, "question": Q, "mode": mode, **extra}
    est = mc.post("/api/estimate", json=body)
    assert est.status_code == 200, est.text
    plan_stages = {s for m in est.json()["members"] for s in (m.get("stages") or {})} if est.json().get("members") else set()
    r = mc.post("/api/sessions", json=body)
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    ev = events(mc, sid)
    kinds = [e["type"] for e in ev]
    assert "done" in kinds, [e for e in ev if e["type"] in ("error", "paused", "member_failed")][:3]
    seen = {e.get("stage") for e in ev if e["type"] == "stage"}
    must, never = STAGES[mode]
    if extra.get("criteria"):  # свои критерии — постановка задачи не нужна (так задумано)
        must = must - {"framing"}
    assert must <= seen, (mode, must - seen)
    assert not (never & seen), (mode, never & seen)
    if mode == "creative" or mode == "research":  # в «Креативе» и «Исследовании» нет отдельной проверки фактов
        assert "factcheck" not in seen and "factcheck" not in plan_stages
    if extra.get("fact_check") and mode in ("debate", "quick", "evaluate"):
        assert "factcheck" in seen
    d = json.loads((mc.runs / f"{sid}.json").read_text(encoding="utf-8"))
    assert d["mode"] == mode and d.get("verdict") and d["verdict"].get("final_answer")
    if mode == "evaluate":
        assert len(d["proposals"]) == len(OPTS)  # варианты пользователя, а не авторов
    if mode == "debate" and extra.get("rounds") == 2:
        assert sum(1 for e in ev if e["type"] == "stage" and e.get("stage") == "critique") >= 2
    if mode in ("research", "creative", "evaluate"):  # «компактные варианты» к ним не применяются
        assert not d["request"].get("compact_proposals")
    # отчёты собираются во всех форматах, что есть на сервере
    for fmt in ("md", "docx", "html"):
        rep = mc.get(f"/api/sessions/{sid}/report?format={fmt}")
        assert rep.status_code == 200 and len(rep.content) > 500, (fmt, rep.status_code, rep.text[:200])
    summ = mc.get(f"/api/sessions/{sid}/report?format=md&summary=1")
    assert summ.status_code == 200 and len(summ.content) > 200
    arch = next(x for x in mc.get("/api/sessions").json()["items"] if x["id"] == sid)
    assert arch["mode"] == mode and arch["status"] == "done"


@pytest.mark.parametrize("parent_mode,child_mode", [("debate", "research"), ("research", "quick"),
                                                    ("evaluate", "debate"), ("creative", "creative"),
                                                    ("quick", "evaluate")])
def test_followup_across_modes(mc, parent_mode, child_mode):
    """Уточнение к заседанию любого режима — в любом режиме: совет видит прежнее решение, разбиения нет."""
    pb = {**BASE, "question": Q, "mode": parent_mode, **({"options": OPTS} if parent_mode == "evaluate" else {})}
    pid = mc.post("/api/sessions", json=pb).json()["id"]
    assert "done" in [e["type"] for e in events(mc, pid)]
    cb = {**BASE, "question": "А если бюджет вдвое меньше?", "mode": child_mode, "parent": pid,
          **({"options": OPTS[:2]} if child_mode == "evaluate" else {})}
    r = mc.post("/api/sessions", json=cb)
    assert r.status_code == 200, r.text
    ev = events(mc, r.json()["id"])
    kinds = [e["type"] for e in ev]
    assert "done" in kinds and "followup" in kinds and "split_series" not in kinds
    start = next(e for e in ev if e["type"] == "start")
    assert start["parent"]["id"] == pid and start["mode"] == child_mode


@pytest.mark.parametrize("mode", ["debate", "quick", "evaluate", "research", "creative"])
def test_redo_and_repeat_keep_mode(mc, mode):
    """«Доделать» из архива и «Повторить» (форма из запроса) — тот же режим и настройки."""
    body = {**BASE, "question": Q + f" ({mode})", "mode": mode, **({"options": OPTS} if mode == "evaluate" else {})}
    sid = mc.post("/api/sessions", json=body).json()["id"]
    assert "done" in [e["type"] for e in events(mc, sid)]
    d = json.loads((mc.runs / f"{sid}.json").read_text(encoding="utf-8"))
    req = d["request"]
    assert req["mode"] == mode and req["members"] == ["a", "b", "c", "d"]
    rep = mc.post(f"/api/sessions/{sid}/repeat").json()["request"]  # «Повторить»: форма заполняется тем же
    assert rep["mode"] == mode and rep["members"] == ["a", "b", "c", "d"]
    if mode == "evaluate":
        assert rep.get("options") == OPTS
    r = mc.post(f"/api/sessions/{sid}/redo", json={"budget": 5})
    assert r.status_code == 200, r.text
    ev = events(mc, sid)
    assert "done" in [e["type"] for e in ev]
    d2 = json.loads((mc.runs / f"{sid}.json").read_text(encoding="utf-8"))
    assert d2["mode"] == mode and d2["verdict"]["final_answer"]


def test_modes_with_attachment(mc):
    up = mc.post("/api/uploads?name=spravka.txt", content="Аренда у вуза — 120 тыс. ₽ в месяц.".encode()).json()
    for mode in ("debate", "quick", "evaluate", "research", "creative"):
        body = {**BASE, "question": Q, "mode": mode, "attachments": [up["id"]],
                **({"options": OPTS} if mode == "evaluate" else {})}
        r = mc.post("/api/sessions", json=body)
        assert r.status_code == 200, (mode, r.text)
        ev = events(mc, r.json()["id"])
        assert "done" in [e["type"] for e in ev], mode
        att = next(e for e in ev if e["type"] == "attachments")
        assert att["items"] and att["items"][0]["name"] == "spravka.txt"
        # файл загрузки удаляется после первого заседания — следующему нужен новый
        up = mc.post("/api/uploads?name=spravka.txt", content="Аренда у вуза — 120 тыс. ₽ в месяц.".encode()).json()
