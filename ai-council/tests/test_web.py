import json

import pytest
import yaml
from fastapi.testclient import TestClient

from council.web.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = {
        "chair": "a", "devil_advocate": "b", "runs_dir": str(tmp_path / "runs"),
        "members": [{"name": n, "model": f"m/{n}"} for n in "abc"] + [{"name": "z", "model": "m/z", "enabled": False}],
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with TestClient(create_app(path, mock_delay=0)) as c:
        yield c


def read_events(client, sid):
    events = []
    with client.stream("GET", f"/api/sessions/{sid}/events") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def test_config_and_index(client):
    c = client.get("/api/config").json()
    assert [m["name"] for m in c["members"]] == ["a", "b", "c", "z"] and c["has_key"] is False
    assert "Совет нейросетей" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


def test_estimate_mock(client):
    r = client.post("/api/estimate", json={"question": "Вопрос", "mock": True, "members": ["a", "b"]})
    assert r.status_code == 200
    e = r.json()
    assert [m["name"] for m in e["members"]] == ["a", "b"] and e["low"] is None and e["calls"] > 0


@pytest.mark.parametrize("body", [
    {"question": "Вопрос", "mock": True, "members": ["a"]},           # нет кворума
    {"question": "Вопрос", "mock": True, "members": ["a", "x"]},      # неизвестный участник
    {"question": "Вопрос", "mock": True, "mode": "evaluate", "options": ["один"]},
    {"question": "Вопрос"},                                           # нет ключа
])
def test_bad_requests(client, body):
    r = client.post("/api/sessions", json=body)
    assert r.status_code == 400 and r.json()["detail"]


def test_full_mock_session_history_report_delete(client):
    r = client.post("/api/sessions", json={"question": "Как назвать кофейню?", "mock": True, "rounds": 1,
                                           "members": ["a", "b", "c"], "chair": "b", "devil_advocate": ""})
    sid = r.json()["id"]
    events = read_events(client, sid)
    types = [e["type"] for e in events]
    assert types[0] == "start" and types[-1] == "done"
    for t in ("framing", "proposals", "revisions", "tally", "verdict", "cost"):
        assert t in types
    assert events[0]["chair"] == "b"
    done = events[-1]
    assert done["total_cost"] > 0 and set(done["stats"]) == {"a", "b", "c"}

    # повторное подключение с Last-Event-ID отдаёт только продолжение
    with client.stream("GET", f"/api/sessions/{sid}/events", headers={"Last-Event-ID": str(len(events) - 2)}) as r2:
        tail = [json.loads(l[6:]) for l in r2.iter_lines() if l.startswith("data: ")]
    assert [e["type"] for e in tail] == ["done"]

    hist = client.get("/api/sessions").json()
    assert hist[0]["id"] == sid and hist[0]["status"] == "done" and hist[0]["winner"]
    rep = client.get(f"/api/sessions/{sid}/report")
    assert rep.status_code == 200 and "Решение совета" in rep.text
    assert client.delete(f"/api/sessions/{sid}").json() == {"ok": True}
    assert client.get(f"/api/sessions/{sid}/report").status_code == 404


def test_history_replay_from_file(client, tmp_path):
    sid = client.post("/api/sessions", json={"question": "Какую БД выбрать?", "mock": True, "mode": "evaluate",
                                             "options": ["Postgres", "Mongo", "SQLite"]}).json()["id"]
    live = read_events(client, sid)
    # новый сервер не знает о заседании в памяти — события восстанавливаются из файла
    app2 = create_app(tmp_path / "config.yaml", mock_delay=0)
    with TestClient(app2) as c2:
        replay = read_events(c2, sid)
    pick = lambda evs, t: next(e for e in evs if e["type"] == t)  # noqa: E731
    assert pick(replay, "verdict") == pick(live, "verdict")
    assert pick(replay, "tally")["rows"] == pick(live, "tally")["rows"]
    assert pick(replay, "proposals")["proposals"] == pick(live, "proposals")["proposals"]
    assert replay[-1]["type"] == "done"


def test_path_traversal_rejected(client):
    assert client.get("/api/sessions/..%2Fconfig/report").status_code == 404
    assert client.get("/api/sessions/nope/events").status_code == 404
