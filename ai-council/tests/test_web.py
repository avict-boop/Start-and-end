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
        c.app_state_runs = cfg["runs_dir"]
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


def tmp_runs(client):
    from pathlib import Path
    return Path(client.app_state_runs)


def test_full_mock_session_history_report_delete(client):
    r = client.post("/api/sessions", json={"question": "Как назвать кофейню?", "mock": True, "rounds": 1,
                                           "members": ["a", "b", "c"], "chair": "b", "devil_advocate": ""})
    sid = r.json()["id"]
    events = read_events(client, sid)
    types = [e["type"] for e in events]
    assert types[0] == "start" and types[-1] == "done"
    for t in ("framing", "proposals", "revisions", "tally", "verdict", "cost", "call", "saving"):
        assert t in types
    assert events[0]["chair"] == "b"
    done = events[-1]
    assert done["total_cost"] > 0 and set(done["stats"]) == {"a", "b", "c"}

    # повторное подключение с Last-Event-ID отдаёт только продолжение
    with client.stream("GET", f"/api/sessions/{sid}/events", headers={"Last-Event-ID": str(len(events) - 2)}) as r2:
        tail = [json.loads(l[6:]) for l in r2.iter_lines() if l.startswith("data: ")]
    assert [e["type"] for e in tail] == ["done"]

    hist = client.get("/api/sessions").json()
    first = hist["items"][0]
    assert first["id"] == sid and first["status"] == "done" and first["winner"] and first["winner_title"]
    assert set(hist["formats"]) >= {"docx", "md"}
    md = client.get(f"/api/sessions/{sid}/report?format=md")
    assert md.status_code == 200 and "Решение совета" in md.text and "attachment" in md.headers["content-disposition"]
    docx = client.get(f"/api/sessions/{sid}/report?format=docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"  # docx — это zip
    pdf = client.get(f"/api/sessions/{sid}/report?format=pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-" and "inline" in pdf.headers["content-disposition"]
    # «только итог»: отдельный файл, без вариантов и голосования
    sm = client.get(f"/api/sessions/{sid}/report?format=md&summary=1")
    assert sm.status_code == 200 and "Решение совета" in sm.text and "Голосование" not in sm.text
    assert "итог.md" in sm.headers["content-disposition"] or "%D0%B8%D1%82%D0%BE%D0%B3.md" in sm.headers["content-disposition"]
    assert client.get(f"/api/sessions/{sid}/report?format=pdf&summary=1").content[:5] == b"%PDF-"
    assert client.get(f"/api/sessions/{sid}/report?format=docx&summary=1").content[:2] == b"PK"
    sh = client.get(f"/api/sessions/{sid}/report?format=html&summary=1").text
    assert "итоговое решение" in sh and 'id="voting"' not in sh and "Полный отчёт" in sh
    # «только решение»: вопрос и ответ, без обоснования и голосования
    dm = client.get(f"/api/sessions/{sid}/report?format=md&decision=1")
    assert dm.status_code == 200 and "Итог (демо)" in dm.text and "Почему" not in dm.text and "Голосование" not in dm.text
    assert "%D1%80%D0%B5%D1%88%D0%B5%D0%BD%D0%B8%D0%B5.md" in dm.headers["content-disposition"]
    assert client.get(f"/api/sessions/{sid}/report?format=pdf&decision=1").content[:5] == b"%PDF-"
    assert client.get(f"/api/sessions/{sid}/report?format=docx&decision=1").content[:2] == b"PK"
    dh = client.get(f"/api/sessions/{sid}/report?format=html&decision=1").text
    assert "решение совета" in dh and "Почему" not in dh and "Итог</a>" in dh and "decision=1" in dh
    html = client.get(f"/api/sessions/{sid}/report?format=html")
    assert html.status_code == 200 and 'id="proposal-A"' in html.text and "format=docx" in html.text
    assert client.get(f"/api/sessions/{sid}/report?format=exe").status_code == 400
    # поиск по архиву
    assert client.get("/api/sessions?q=кофейню").json()["total"] == 1
    assert client.get("/api/sessions?q=несуществующее").json()["total"] == 0
    assert client.delete(f"/api/sessions/{sid}").json() == {"ok": True}
    assert client.get(f"/api/sessions/{sid}/report").status_code == 404
    assert not list((tmp_runs(client)).glob(f"{sid}.*"))  # удалены и стенограмма, и все отчёты


def test_history_replay_from_file(client, tmp_path):
    sid = client.post("/api/sessions", json={"question": "Какую БД выбрать?", "mock": True, "mode": "evaluate",
                                             "options": ["Postgres", "Mongo", "SQLite"]}).json()["id"]
    live = read_events(client, sid)
    # новый сервер не знает о заседании в памяти — события восстанавливаются из файла
    app2 = create_app(tmp_path / "config.yaml", mock_delay=0)
    with TestClient(app2) as c2:
        replay = read_events(c2, sid)
    pick = lambda evs, t: {k: v for k, v in next(e for e in evs if e["type"] == t).items() if k != "ts"}  # noqa: E731
    assert pick(replay, "verdict") == pick(live, "verdict")
    assert pick(replay, "tally")["rows"] == pick(live, "tally")["rows"]
    assert pick(replay, "proposals")["proposals"] == pick(live, "proposals")["proposals"]
    assert replay[-1]["type"] == "done"


def test_path_traversal_rejected(client):
    assert client.get("/api/sessions/..%2Fconfig/report").status_code == 404
    assert client.get("/api/sessions/nope/events").status_code == 404


def test_password_protection(tmp_path, monkeypatch):
    import base64
    cfg = {"chair": "a", "runs_dir": str(tmp_path / "runs"), "members": [{"name": n, "model": f"m/{n}"} for n in "ab"]}
    (tmp_path / "c.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.setenv("WEB_USER", "boss")
    monkeypatch.setenv("TRUST_PROXY", "1")
    app = create_app(tmp_path / "c.yaml", mock_delay=0, password="s3cret-pass")
    auth = lambda u, p: {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}  # noqa: E731
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200                       # проверка здоровья без пароля
        r = c.get("/")
        assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
        assert c.get("/api/config", headers=auth("boss", "wrong")).status_code == 401
        assert c.get("/api/config", headers=auth("boss", "s3cret-pass")).status_code == 200
        assert c.get("/static/app.js", headers=auth("boss", "s3cret-pass")).status_code == 200
        # перебор: после 10 ошибок адрес блокируется, даже с верным паролем
        bad = {**auth("boss", "x"), "X-Forwarded-For": "6.6.6.6"}
        for _ in range(10):
            assert c.get("/", headers=bad).status_code == 401
        assert c.get("/", headers={**auth("boss", "s3cret-pass"), "X-Forwarded-For": "6.6.6.6"}).status_code == 429
        assert c.get("/", headers={**auth("boss", "s3cret-pass"), "X-Forwarded-For": "7.7.7.7"}).status_code == 200
        # SSE тоже за паролем
        sid = c.post("/api/sessions", json={"question": "Вопрос?", "mock": True},
                     headers=auth("boss", "s3cret-pass")).json()["id"]
        assert c.get(f"/api/sessions/{sid}/events").status_code == 401


def test_uploads_and_session_with_attachments(client, tmp_path):
    import io
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (800, 600), "blue").save(b, "JPEG")
    r = client.post("/api/uploads?name=схема.jpg", content=b.getvalue())
    assert r.status_code == 200
    img = r.json()
    assert img["kind"] == "image" and img["thumb"].startswith("data:image/jpeg") and len(img["id"]) == 16
    doc = client.post("/api/uploads?name=заметки.txt", content="Выручка: 10 млн".encode()).json()
    assert client.post("/api/uploads?name=x.exe", content=b"MZ").status_code == 400
    assert client.post("/api/sessions", json={"question": "Вопрос?", "mock": True,
                                              "attachments": ["0123456789abcdef"]}).status_code == 400
    est = client.post("/api/estimate", json={"question": "Вопрос?", "mock": True, "attachments": [img["id"], doc["id"]]}).json()
    assert est["calls"] > 0
    sid = client.post("/api/sessions", json={"question": "Что на схеме?", "mock": True, "mode": "quick",
                                             "attachments": [img["id"], doc["id"]]}).json()["id"]
    events = read_events(client, sid)
    att_ev = next(e for e in events if e["type"] == "attachments")
    assert [a["name"] for a in att_ev["items"]] == ["схема.jpg", "заметки.txt"]
    files = {a["name"]: a["file"] for a in att_ev["items"]}
    f = client.get(f"/api/sessions/{sid}/files/{files['схема.jpg']}")
    assert f.status_code == 200 and f.headers["content-type"] == "image/jpeg" and f.content[:2] == b"\xff\xd8"
    assert client.get(f"/api/sessions/{sid}/files/..%2F..%2Fconfig.yaml").status_code == 404
    assert client.get(f"/api/sessions/{sid}/files/not-listed.txt").status_code == 404
    # черновики удалены, вложения — в архиве заседания и в отчёте
    runs = tmp_path / "runs"
    assert not any((runs / ".uploads").iterdir())
    assert (runs / f"{sid}.files" / files["заметки.txt"]).read_text(encoding="utf-8") == "Выручка: 10 млн"
    md = client.get(f"/api/sessions/{sid}/report?format=md").text
    assert "Вложения (2)" in md and "схема.jpg" in md
    replay = read_events(client, sid)  # повторное открытие (из архива) тоже показывает вложения
    assert any(e["type"] == "attachments" for e in replay)
    assert client.delete(f"/api/sessions/{sid}").json() == {"ok": True}
    assert not (runs / f"{sid}.files").exists()


def test_very_long_question_is_accepted(client):
    q = "Проанализируй документ. " + "Очень длинное условие задачи. " * 3000  # ~90 000 символов
    r = client.post("/api/sessions", json={"question": q, "mock": True, "mode": "quick", "context": "к" * 80000})
    assert r.status_code == 200
    events = read_events(client, r.json()["id"])
    assert events[0]["question"] == q and events[-1]["type"] == "done"


def test_balance_endpoint(client, monkeypatch):
    assert client.get("/api/balance").json() == {"has_key": False, "balance": None}
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = []

    async def fake_balance(self):
        calls.append(1)
        return 12.5

    monkeypatch.setattr("council.web.app.OpenRouterClient.balance", fake_balance)
    assert client.get("/api/balance").json() == {"has_key": True, "balance": 12.5}
    client.get("/api/balance")
    assert len(calls) == 1  # кэш на минуту
    client.get("/api/balance?force=1")
    assert len(calls) == 2
