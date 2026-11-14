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
    assert "Итог заседания" in sh and 'id="voting"' not in sh and "Полный отчёт" in sh
    # «только решение»: вопрос и ответ, без обоснования и голосования
    dm = client.get(f"/api/sessions/{sid}/report?format=md&decision=1")
    assert dm.status_code == 200 and "Итог (демо)" in dm.text and "Почему" not in dm.text and "Голосование" not in dm.text
    assert "%D1%80%D0%B5%D1%88%D0%B5%D0%BD%D0%B8%D0%B5.md" in dm.headers["content-disposition"]
    assert client.get(f"/api/sessions/{sid}/report?format=pdf&decision=1").content[:5] == b"%PDF-"
    assert client.get(f"/api/sessions/{sid}/report?format=docx&decision=1").content[:2] == b"PK"
    dh = client.get(f"/api/sessions/{sid}/report?format=html&decision=1").text
    assert "решение совета" in dh.lower() and "Почему" not in dh and "Итог</a>" in dh and "decision=1" in dh
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
    assert "### Вложения" in md and "схема.jpg" in md
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
    assert client.get("/api/balance").json() == {"has_key": True, "balance": 12.5, "key_limit": None}
    client.get("/api/balance")
    assert len(calls) == 1  # кэш на минуту
    client.get("/api/balance?force=1")
    assert len(calls) == 2


def test_index_busts_static_cache(tmp_path):
    from council.web.app import create_app
    from fastapi.testclient import TestClient
    import re
    cfg = tmp_path / "config.yaml"
    cfg.write_text("chair: a\nmembers:\n  - {name: a, model: m/a}\n  - {name: b, model: m/b}\nruns_dir: runs\n", encoding="utf-8")
    c = TestClient(create_app(cfg, password=""))
    r = c.get("/")
    assert r.headers["cache-control"] == "no-cache"
    assert re.search(r'/static/app\.js\?v=[0-9a-f]{10}"', r.text) and re.search(r'/static/style\.css\?v=[0-9a-f]{10}"', r.text)
    assert c.get(re.search(r'"(/static/app\.js\?v=[0-9a-f]+)"', r.text).group(1)).status_code == 200


def test_budget_pause_resume_and_raise(client):
    sid = client.post("/api/sessions", json={"question": "Вопрос с маленьким бюджетом?", "mock": True, "budget": 0.01}).json()["id"]
    events = read_events(client, sid)
    paused = events[-1]
    assert paused["type"] == "paused" and paused["reason"] == "budget"
    listed = client.get("/api/sessions").json()["items"]
    assert any(i["id"] == sid and i["status"] == "paused" for i in listed)
    assert [i["id"] for i in client.get("/api/paused").json()["items"]] == [sid]
    assert client.post(f"/api/sessions/{sid}/resume", json={}).status_code == 400  # бюджет уже потрачен
    assert client.post(f"/api/sessions/{sid}/resume", json={"budget": 3}).status_code == 200
    events = read_events(client, sid)
    types = [e["type"] for e in events]
    assert "resumed" in types and types[-1] == "done"
    assert client.get("/api/paused").json()["items"] == []
    assert client.get(f"/api/sessions/{sid}/report?format=md").status_code == 200

    # на ходу: бюджет можно поднять, пока заседание идёт; удалить приостановленное
    sid2 = client.post("/api/sessions", json={"question": "Второй вопрос?", "mock": True, "budget": 0.01}).json()["id"]
    read_events(client, sid2)
    assert client.post(f"/api/sessions/{sid2}/budget", json={"budget": 0.5}).json()["running"] is False
    assert client.delete(f"/api/sessions/{sid2}").status_code == 200
    assert client.get("/api/paused").json()["items"] == []


def test_redo_archived_session_with_failures(client, monkeypatch):
    from council.mock import MockClient
    real_init = MockClient.__init__

    def flaky(self, *a, **kw):
        real_init(self, *a, **kw)
        self.fail_models = {"m/a"}  # председатель не отвечает — итог «по голосованию», без синтеза
    monkeypatch.setattr(MockClient, "__init__", flaky)
    sid = client.post("/api/sessions", json={"question": "Вопрос со сбоем модели?", "mock": True}).json()["id"]
    assert read_events(client, sid)[-1]["type"] == "done"
    monkeypatch.setattr(MockClient, "__init__", real_init)
    assert client.post(f"/api/sessions/{sid}/redo", json={"budget": 0.001}).status_code == 400  # меньше потраченного
    assert client.post(f"/api/sessions/{sid}/redo", json={"budget": 5}).status_code == 200
    events = read_events(client, sid)
    assert "resumed" in [e["type"] for e in events] and events[-1]["type"] == "done"
    data = client.get(f"/api/sessions/{sid}/report?format=md").text
    assert "Решение совета" in data and "Председатель не ответил" not in data
    assert client.get("/api/paused").json()["items"] == []


def test_research_length_is_set_per_question_and_compact_ignored(client):
    r = client.post("/api/sessions", json={"question": "Рынок кофеен у вузов?", "mock": True, "mode": "research",
                                           "members": ["a", "b", "c"], "compact": True,
                                           "research_words": 2200, "research_shorten": False})
    sid = r.json()["id"]
    assert read_events(client, sid)[-1]["type"] == "done"
    req = client.post(f"/api/sessions/{sid}/repeat").json()["request"]
    assert req["research_words"] == 2200 and req["research_shorten"] is False and req["compact_proposals"] is False
    cfg = client.get("/api/config").json()["research"]
    assert cfg["words"] == 1400 and cfg["shorten"] is True  # общие настройки не изменились


def test_rating_and_pilot_table(client):
    q = "Рынок кофеен у вузов?"
    ids = []
    for mode in ("research", "debate"):
        r = client.post("/api/sessions", json={"question": q, "mock": True, "mode": mode, "members": ["a", "b", "c"]})
        ids.append(r.json()["id"])
        assert read_events(client, ids[-1])[-1]["type"] == "done"
    assert client.post(f"/api/sessions/{ids[0]}/rating", json={"rating": 4, "note": "полезно"}).json()["rating"]["rating"] == 4
    assert client.get(f"/api/sessions/{ids[0]}/rating").json()["rating"]["rating"] == 4
    assert client.post("/api/sessions/nope/rating", json={"rating": 3}).status_code == 404
    s = client.get("/api/stats").json()
    row = s["research"][0]
    assert row["id"] == ids[0] and row["rating"] == 4 and row["checked"] > 0 and row["subq"] == 3
    assert len(s["pairs"]) == 1 and {r["mode"] for r in s["pairs"][0]["runs"]} == {"research", "debate"}
    assert client.post(f"/api/sessions/{ids[0]}/rating", json={"rating": 0}).json()["rating"] is None


def test_costs_pilot_solo_and_replay_in_demo(client):
    r = client.post("/api/sessions", json={"question": "Рынок кофеен у вузов?", "mock": True, "mode": "research",
                                           "members": ["a", "b", "c"]})
    sid = r.json()["id"]
    assert read_events(client, sid)[-1]["type"] == "done"
    c = client.get(f"/api/sessions/{sid}/costs").json()
    assert c["total"] > 0 and {s["stage"] for s in c["stages"]} >= {"proposal", "arbitration", "verdict"}
    solo = client.post(f"/api/sessions/{sid}/solo").json()
    assert solo["mock"] and solo["answer"]
    rep = client.post(f"/api/sessions/{sid}/replay").json()
    assert rep["after"]["checked"] > 0 and "ledger" not in rep
    assert client.post(f"/api/sessions/{sid}/rating", json={"rating": 3, "kind": "solo"}).status_code == 200
    p = client.get(f"/api/sessions/{sid}/pilot").json()
    assert p["solo"]["answer"] and p["replay"]["after"]["checked"] and p["solo_rating"] == 3
    s = client.get("/api/stats").json()
    assert s["stages"] and s["research"][-1]["solo_rating"] == 3


def test_actions_from_another_site_are_refused(client):
    r = client.post("/api/system/restart", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/api/estimate", json={"question": "Вопрос?", "mock": True}, headers={"Origin": "http://testserver"})
    assert r.status_code == 200
    assert client.post("/api/estimate", json={"question": "Вопрос?", "mock": True},
                       headers={"Referer": "https://evil.example/page"}).status_code == 403
    assert client.post("/api/estimate", json={"question": "Вопрос?", "mock": True}).status_code == 200  # не браузер


def test_report_links_cannot_run_code():
    from council.reports.view import md_to_html
    h = md_to_html("[a](javascript:alert(1)) ![b](https://t.example/x.png) [c](https://ok.example)")
    assert "javascript:" not in h and "<img" not in h and 'href="https://ok.example"' in h


def test_event_ids_from_previous_run_do_not_stall_the_stream(client):
    # после паузы и продолжения события нумеруются заново: номер из прошлого запуска не должен «прятать» события
    r = client.post("/api/sessions", json={"question": "Как назвать кофейню?", "mock": True, "rounds": 1,
                                           "members": ["a", "b", "c"], "chair": "b", "devil_advocate": ""})
    sid = r.json()["id"]
    events = read_events(client, sid)
    for stale in ("7:5", "0:99999", "99999"):
        with client.stream("GET", f"/api/sessions/{sid}/events", headers={"Last-Event-ID": stale}) as r2:
            got = [json.loads(l[6:]) for l in r2.iter_lines() if l.startswith("data: ")]
        assert [e["type"] for e in got] == [e["type"] for e in events], stale


def test_page_can_reopen_stream_after_last_event(client):
    # страница сама переоткрывает зависший поток: ?after=<номер> — только продолжение, без повторов
    r = client.post("/api/sessions", json={"question": "Как назвать кофейню?", "mock": True, "rounds": 1,
                                           "members": ["a", "b", "c"], "chair": "b", "devil_advocate": ""})
    sid = r.json()["id"]
    events = read_events(client, sid)
    with client.stream("GET", f"/api/sessions/{sid}/events?after=0:{len(events) - 2}") as r2:
        tail = [json.loads(l[6:]) for l in r2.iter_lines() if l.startswith("data: ")]
    assert [e["type"] for e in tail] == ["done"]


def test_series_runs_parts_in_parallel_then_council_summary(client):
    import time as _t
    task = "Проверь компании: Alphacorp Foods, Betamax Nutrition, Gammaline Labs, Deltaform Pharma"
    parts = [{"title": "Пакет A", "question": task + "\n\nЧасть 1: Alphacorp Foods, Betamax Nutrition",
              "objects": ["Alphacorp Foods", "Betamax Nutrition"]},
             {"title": "Пакет B", "question": task + "\n\nЧасть 2: Gammaline Labs", "objects": ["Gammaline Labs"]},
             {"title": "Пакет C", "question": task + "\n\nЧасть 3: Deltaform Pharma", "objects": ["Deltaform Pharma"]}]
    r = client.post("/api/series", json={"title": "Проверка компаний", "task": task, "parts": parts,
                                          "request": {"mode": "research", "mock": True, "members": ["a", "b", "c"],
                                                      "chair": "a"}, "max_parallel": 2, "auto_summary": True})
    assert r.status_code == 200, r.text
    sid = r.json()["id"]
    v = None
    for _ in range(240):
        v = client.get(f"/api/series/{sid}").json()
        if v["status"] == "done":
            break
        _t.sleep(0.25)
    assert v["status"] == "done", v
    assert v["done"] == 3 and len({p["session_id"] for p in v["parts"]}) == 3  # номера частей не совпали
    import json as _json
    summ = _json.loads((tmp_runs(client) / f"{v['summary']['session_id']}.json").read_text(encoding="utf-8"))
    assert summ["mode"] == "research" and summ["request"]["series"]["summary"] is True
    led = (summ.get("research") or {}).get("ledger") or {}
    assert led.get("inherited_parts") == [1, 2, 3] and any(c["id"].startswith("P2-") for c in led["claims"])
    assert client.get("/api/sessions").json()["items"][0].get("series")  # части и сводка помечены серией
    assert any(x["id"] == sid for x in client.get("/api/series").json()["items"])


def _catalog_question(n):
    rows = "\n".join(f"| {i} | Company{i:02d} Foods Ltd | Провинция {i} | https://c{i}.example |" for i in range(1, n + 1))
    return ("Проверь каждую компанию из таблицы по трём фактам.\n\n| № | Компания | Провинция | Сайт |\n|---|---|---|---|\n"
            + rows)


def test_split_plan_keeps_everything_and_checks_coverage(client):
    q = _catalog_question(22)
    r = client.post("/api/split", json={"question": q, "request": {"mode": "research", "mock": True}, "per_part": 8,
                                         "facts": 3})
    assert r.status_code == 200, r.text
    d = r.json()
    assert len(d["objects"]) == 22 and d["need_split"] and d["checks"]["ok"]
    assert sum(len(p["objects"]) for p in d["parts"]) == 22 and len(d["parts"]) == 3
    for p in d["parts"]:
        assert p["question"].startswith(q.rstrip()) and "Рамка этой части серии" in p["question"]  # задание целиком
        assert p["settings"]["arbitration_claims"] >= len(p["objects"]) * 3


def test_chair_splits_a_big_list_into_a_series(client):
    import time as _t
    r = client.post("/api/sessions", json={"question": _catalog_question(23), "mode": "research", "mock": True,
                                           "members": ["a", "b", "c"], "chair": "a"})
    sid = r.json()["id"]
    ev = read_events(client, sid)
    sp = [e for e in ev if e["type"] == "split_series"]
    assert sp and sp[0]["parts"] == 3 and sp[0]["objects"] == 23
    series_id = sp[0]["series"]
    v = None
    for _ in range(400):
        v = client.get(f"/api/series/{series_id}").json()
        if v["status"] == "done":
            break
        _t.sleep(0.25)
    assert v["status"] == "done" and v["done"] == 3 and v["summary"]["status"] == "done", v


def _wait_series(client, series_id, tries=400):
    import time as _t
    v = None
    for _ in range(tries):
        v = client.get(f"/api/series/{series_id}").json()
        if v["status"] == "done":
            return v
        _t.sleep(0.25)
    return v


def test_series_parts_get_files_and_only_their_objects(client):
    import json as _json
    q = _catalog_question(14)
    up = client.post("/api/uploads?name=zametki.txt", content="Справка по рынку".encode()).json()
    objs = [f"Company{i:02d} Foods Ltd" for i in range(1, 15)]
    r = client.post("/api/series", json={
        "title": "Файлы", "task": q, "parts": [{"title": "A", "objects": objs[:7]}, {"title": "B", "objects": objs[7:]}],
        "request": {"mode": "research", "mock": True, "members": ["a", "b", "c"], "chair": "a",
                    "attachments": [up["id"]], "research": {"catalog": True}},
        "max_parallel": 1, "auto_summary": True})
    assert r.status_code == 200, r.text
    v = _wait_series(client, r.json()["id"])
    assert v["status"] == "done", v
    for p in v["parts"]:  # вторая часть тоже получила файл (первая готовая часть удаляет черновые загрузки)
        d = _json.loads((tmp_runs(client) / f"{p['session_id']}.json").read_text(encoding="utf-8"))
        assert "zametki.txt" in _json.dumps(d.get("attachments"), ensure_ascii=False)
        led = d["research"]["ledger"]
        assert led["objects"]["total"] == 7  # охват — только объекты своей части, а не вся таблица задания
    summ = _json.loads((tmp_runs(client) / f"{v['summary']['session_id']}.json").read_text(encoding="utf-8"))
    assert summ["research"]["ledger"]["objects"]["total"] == 14


def test_estimate_warns_about_series_and_followup_never_splits(client):
    e = client.post("/api/estimate", json={"question": _catalog_question(23), "mode": "research", "mock": True}).json()
    assert e["series"] and e["series"]["parts"] == 3 and e["series"]["objects"] == 23
    e = client.post("/api/estimate", json={"question": _catalog_question(5), "mode": "research", "mock": True}).json()
    assert e["series"] is None
    r = client.post("/api/sessions", json={"question": "Короткий вопрос про рынок", "mode": "research", "mock": True,
                                           "members": ["a", "b", "c"], "chair": "a"})
    base = r.json()["id"]
    assert any(x["type"] == "done" for x in read_events(client, base))
    r = client.post("/api/sessions", json={"question": _catalog_question(23), "mode": "research", "mock": True,
                                           "members": ["a", "b", "c"], "chair": "a", "parent": base})
    ev = read_events(client, r.json()["id"])
    assert not any(x["type"] == "split_series" for x in ev) and any(x["type"] == "done" for x in ev)


def test_series_runs_parts_one_by_one_when_funds_are_short(client):
    calls = []

    async def probe():
        calls.append(1)
        return 5.0  # меньше, чем лимиты двух частей по $10

    client.app.state.funds_probe = probe
    client.app.state.funds_poll = 0.05
    objs = [f"Company{i:02d} Foods Ltd" for i in range(1, 9)]
    r = client.post("/api/series", json={
        "title": "Мало денег", "task": _catalog_question(8),
        "parts": [{"title": "A", "objects": objs[:4]}, {"title": "B", "objects": objs[4:]}],
        "request": {"mode": "research", "mock": True, "members": ["a", "b", "c"], "chair": "a", "budget": 10},
        "max_parallel": 2, "auto_summary": False})
    sid = r.json()["id"]
    v = None
    import time as _t
    for _ in range(400):
        v = client.get(f"/api/series/{sid}").json()
        if v["done"] == 2:
            break
        if v["parts"][1]["status"] == "queued" and v["parts"][0]["status"] == "running":
            assert "ждёт, пока идущая закончит" in v["waiting"]  # страница серии объясняет, почему вторая часть ждёт
        _t.sleep(0.1)
    assert v["done"] == 2 and calls  # вторая часть ждала, пока первая закончит


def test_split_can_be_revised_with_more_parts(client):
    q = _catalog_question(22)
    d = client.post("/api/split", json={"question": q, "request": {"mode": "research", "mock": True}}).json()
    r = client.post("/api/split", json={"question": q, "request": {"mode": "research", "mock": True},
                                         "objects": d["objects"], "parts": 5, "principle": "по провинциям"})
    assert r.status_code == 200, r.text
    x = r.json()
    assert len(x["parts"]) == 5 and x["checks"]["ok"] and sum(len(p["objects"]) for p in x["parts"]) == 22


def test_series_summary_is_never_duplicated(client):
    objs = ["Alphacorp Foods", "Betamax Nutrition"]
    r = client.post("/api/series", json={
        "title": "Одна сводка", "task": "Проверь компании Alphacorp Foods и Betamax Nutrition",
        "parts": [{"title": "A", "objects": objs[:1]}, {"title": "B", "objects": objs[1:]}],
        "request": {"mode": "research", "mock": True, "members": ["a", "b", "c"], "chair": "a"},
        "max_parallel": 2, "auto_summary": True})
    v = _wait_series(client, r.json()["id"])
    assert v["status"] == "done"
    sid = v["summary"]["session_id"]
    assert client.post(f"/api/series/{v['id']}/summary").status_code == 409  # готовая сводка — вторую не создаём
    client.post(f"/api/series/{v['id']}/continue")
    v2 = _wait_series(client, v["id"])
    assert v2["summary"]["session_id"] == sid and v2["status"] == "done"


def test_live_thoughts_are_shown_but_never_saved(client, tmp_path):
    import json as _json
    import time as _t
    r = client.post("/api/sessions", json={"question": "Каков рынок кофеен у вузов?", "mode": "quick", "mock": True,
                                           "members": ["a", "b", "c"], "chair": "a"})
    sid = r.json()["id"]
    ev = read_events(client, sid)
    assert any(e["type"] == "done" for e in ev)
    # в событиях заседания (они сохраняются) текста рассуждений нет — только объём
    assert not any("thought" in e for e in ev) and not any("(демо) Читаю" in _json.dumps(e, ensure_ascii=False) for e in ev)
    assert client.get(f"/api/sessions/{sid}/thoughts").json() == {"running": False, "items": {}}
    for p in tmp_runs(client).rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl", ".md", ".txt"):
            assert "(демо) Читаю" not in p.read_text(encoding="utf-8", errors="ignore"), p


def test_run_state_keeps_only_tail_of_current_thought():
    from council.web.app import RunState
    st = RunState("x", "q")
    n = RunState.THOUGHT_KEEP + 1000
    st.add_thought("a", "proposal", "я" * n)
    v = st.thought_view("a")
    assert len(v["text"]) == RunState.THOUGHT_KEEP and v["chars"] == n
    st.add_thought("a", "critique", "новый этап")  # новый этап — новое рассуждение
    assert st.thought_view("a")["text"] == "новый этап"
    st.add_thought("a", "critique", None)  # обращение закончилось — рассуждение удалено
    assert st.thought_view("a") is None and "a" not in st.thoughts
    # проверка утверждений: несколько пачек одной модели сразу — не перемешиваются, конец одной не стирает другие
    st.add_thought("s", "arbitration", "пачка один", call=0)
    st.add_thought("s", "arbitration", "пачка два", call=1)
    v = st.thought_view("s")
    assert v["parallel"] == 2 and v["text"].index("пачка один") < v["text"].index("пачка два")
    st.add_thought("s", "arbitration", None, call=0)
    assert st.thought_view("s")["text"] == "пачка два"


def test_retry_failed_members_after_stage_keeps_done_answers(client, monkeypatch):
    """Модель не ответила на «вариантах» → «Повторить не ответивших» → пауза после этапа → само продолжается:
    готовые ответы — из сохранённого, не ответившая модель спрашивается заново и в этот раз отвечает."""
    import time as _t
    from council import mock as M
    state = {"fail": True, "asked": []}
    orig = M.MockClient.complete

    async def flaky(self, model, messages, **kw):
        st = (kw.get("meta") or {}).get("stage")
        if st == "proposal":
            state["asked"].append(model)
            if model == "m/b" and state["fail"]:
                from council.client import ProviderError
                raise ProviderError("m/b: ответ идёт слишком долго (более 540 с)")
        if st == "critique" and state["fail"]:  # «Повторить» нажали, пока шли варианты — ждём начала критики
            import asyncio as _a
            await _a.sleep(0.4)
        return await orig(self, model, messages, **kw)

    monkeypatch.setattr(M.MockClient, "complete", flaky)
    r = client.post("/api/sessions", json={"question": "Каков рынок кофеен у вузов?", "mode": "debate", "mock": True,
                                           "members": ["a", "b", "c"], "chair": "a", "rounds": 1,
                                           "chair_proposes": True})
    sid = r.json()["id"]
    for _ in range(100):  # дождаться сбоя b
        if "m/b" in state["asked"]:
            break
        _t.sleep(0.05)
    rr = client.post(f"/api/sessions/{sid}/retry_failed", json={"on": True})
    assert rr.status_code == 200 and rr.json()["planned"] is True, rr.text
    state["fail"] = False  # к повтору модель «починилась»
    asked_before = len(state["asked"])
    done = None
    for _ in range(300):
        d = client.get("/api/sessions").json()["items"]
        done = next((x for x in d if x["id"] == sid and x.get("status") == "done"), None)
        if done:
            break
        _t.sleep(0.1)
    assert done, "заседание не завершилось после повтора"
    again = state["asked"][asked_before:]
    assert again == ["m/b"]  # заново спросили только не ответившую модель, остальные варианты — из сохранённого
    import json as _json
    res = _json.loads((tmp_runs(client) / f"{sid}.json").read_text(encoding="utf-8"))
    assert len(res["proposals"]) == 3  # в итоге — варианты всех трёх


def test_summary_checks_conflicts_between_parts_and_keeps_recheck_flag(client, monkeypatch):
    import json as _json
    import re as _re
    from council import mock as M
    orig = M.MockClient.complete
    seen = {}

    async def with_conflict(self, model, messages, **kw):
        st = (kw.get("meta") or {}).get("stage")
        if st == "conflicts":
            text = str(messages[-1]["content"])
            ids = _re.findall(r"^- (P\d+-\S+) \[", text, _re.M)
            p1 = next(i for i in ids if i.startswith("P1-"))
            p2 = next(i for i in ids if i.startswith("P2-"))
            seen["ids"] = [p1, p2]
            from council.client import Completion
            return Completion(text=_json.dumps({"conflicts": [{"ids": [p1, p2], "question": "Что верно?"}]}),
                              prompt_tokens=10, completion_tokens=10, cost=0.0001, model=model)
        return await orig(self, model, messages, **kw)

    monkeypatch.setattr(M.MockClient, "complete", with_conflict)
    objs = ["Alphacorp Foods", "Betamax Nutrition"]
    r = client.post("/api/series", json={
        "title": "Противоречия", "task": "Проверь компании Alphacorp Foods и Betamax Nutrition",
        "parts": [{"title": "A", "objects": objs[:1]}, {"title": "B", "objects": objs[1:]}],
        "request": {"mode": "research", "mock": True, "members": ["a", "b", "c"], "chair": "a"},
        "max_parallel": 2, "auto_summary": True, "summary_recheck": True})
    v = _wait_series(client, r.json()["id"])
    assert v["status"] == "done" and v["summary_recheck"] is True
    d = _json.loads((tmp_runs(client) / f"{v['summary']['session_id']}.json").read_text(encoding="utf-8"))
    assert d["request"]["research_overrides"]["summary_recheck"] is True  # галочка серии дошла до сводки
    led = d["research"]["ledger"]
    x = next(c for c in led["claims"] if c["id"] == "X-C1")  # противоречие проверено поиском
    assert x["conflict_of"] == seen["ids"] and x["status"]
    assert all(next(c for c in led["claims"] if c["id"] == i).get("conflict") == "X-C1" for i in seen["ids"])


def test_summary_waits_for_part_being_redone_and_goes_stale_after(client, monkeypatch):
    import asyncio as _a
    import time as _t
    from council import mock as M
    objs = ["Alphacorp Foods", "Betamax Nutrition"]
    r = client.post("/api/series", json={
        "title": "Устаревшая сводка", "task": "Проверь компании Alphacorp Foods и Betamax Nutrition",
        "parts": [{"title": "A", "objects": objs[:1]}, {"title": "B", "objects": objs[1:]}],
        "request": {"mode": "research", "mock": True, "members": ["a", "b", "c"], "chair": "a"},
        "max_parallel": 2, "auto_summary": True})
    v = _wait_series(client, r.json()["id"])
    assert v["status"] == "done" and not v["summary_stale"]
    first_summary = v["summary"]["session_id"]
    part2 = v["parts"][1]["session_id"]
    orig = M.MockClient.complete

    async def slow(self, model, messages, **kw):
        await _a.sleep(0.05)
        return await orig(self, model, messages, **kw)

    monkeypatch.setattr(M.MockClient, "complete", slow)
    _t.sleep(1.1)  # время файла части должно отличаться от записанного в сводке
    assert client.post(f"/api/sessions/{part2}/redo", json={"budget": 5}).status_code == 200
    busy = client.post(f"/api/series/{v['id']}/summary")
    assert busy.status_code == 409  # пока часть 2 переделывается — новой сводки нет
    # сводки ещё нет, а часть идёт — сводка не берёт её старый итог
    rd = tmp_runs(client)
    import json as _json
    sf = rd / "series" / f"{v['id']}.json"
    d = _json.loads(sf.read_text(encoding="utf-8"))
    d["summary_id"] = ""
    sf.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
    busy = client.post(f"/api/series/{v['id']}/summary")
    assert busy.status_code == 409 and "ещё идёт" in busy.json()["detail"], busy.text
    d["summary_id"] = first_summary
    sf.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
    for _ in range(300):
        st = client.get(f"/api/series/{v['id']}").json()
        if st["parts"][1]["status"] == "done" and st["summary_stale"]:
            break
        _t.sleep(0.1)
    assert st["summary_stale"] == [2]  # сводка устарела: часть 2 переделана после неё
    again = client.post(f"/api/series/{v['id']}/summary")
    assert again.status_code == 200 and again.json()["id"] != first_summary


def test_redo_of_summary_and_part_keeps_inherited_ledgers_and_objects(client):
    import json as _json
    objs = ["Alphacorp Foods", "Betamax Nutrition"]
    r = client.post("/api/series", json={
        "title": "Доделать сводку", "task": "Проверь компании Alphacorp Foods и Betamax Nutrition",
        "parts": [{"title": "A", "objects": objs[:1]}, {"title": "B", "objects": objs[1:]}],
        "request": {"mode": "research", "mock": True, "members": ["a", "b", "c"], "chair": "a"},
        "max_parallel": 2, "auto_summary": True})
    v = _wait_series(client, r.json()["id"])
    rd = tmp_runs(client)
    for sid, want_parts, want_objects in [(v["summary"]["session_id"], [1, 2], 2), (v["parts"][1]["session_id"], None, 1)]:
        assert client.post(f"/api/sessions/{sid}/redo", json={"budget": 5}).status_code == 200
        assert any(e["type"] == "done" for e in read_events(client, sid))
        led = _json.loads((rd / f"{sid}.json").read_text(encoding="utf-8"))["research"]["ledger"]
        if want_parts:  # 13.11: после «Доделать» сводка шла без реестров частей
            assert led.get("inherited_parts") == want_parts
        assert led["objects"]["total"] == want_objects  # объекты части / все объекты серии — не вся таблица задания
    # старая запись (без частей в request.series) — части берутся из файла серии
    sid = v["summary"]["session_id"]
    f = rd / f"{sid}.json"
    d = _json.loads(f.read_text(encoding="utf-8"))
    d["request"]["series"].pop("inherit", None)
    d["request"]["series"].pop("objects", None)
    f.write_text(_json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert client.post(f"/api/sessions/{sid}/redo", json={"budget": 5}).status_code == 200
    assert any(e["type"] == "done" for e in read_events(client, sid))
    led = _json.loads(f.read_text(encoding="utf-8"))["research"]["ledger"]
    assert led.get("inherited_parts") == [1, 2] and led["objects"]["total"] == 2


def test_user_can_forbid_split_for_one_request(client):
    q = _catalog_question(23)
    base = {"question": q, "mode": "research", "mock": True, "members": ["a", "b", "c"], "chair": "a"}
    assert client.post("/api/estimate", json=base).json()["series"]  # по умолчанию — предупреждение о серии
    off = {**base, "research": {"auto_split": False}}
    assert client.post("/api/estimate", json=off).json()["series"] is None
    sid = client.post("/api/sessions", json=off).json()["id"]
    ev = read_events(client, sid)
    kinds = [e["type"] for e in ev]
    assert "split_series" not in kinds and "done" in kinds  # одним заседанием, несмотря на длинный список
    obj = next(e for e in ev if e["type"] == "objects")
    assert obj["count"] == 23


def test_light_summary_two_authors_whole_council_critiques_and_votes(client):
    import json as _json
    objs = ["Alphacorp Foods", "Betamax Nutrition"]
    r = client.post("/api/series", json={
        "title": "Лёгкая сводка", "task": "Проверь компании Alphacorp Foods и Betamax Nutrition",
        "parts": [{"title": "A", "objects": objs[:1]}, {"title": "B", "objects": objs[1:]}],
        "request": {"mode": "research", "mock": True, "members": ["a", "b", "c"], "chair": "a"},
        "max_parallel": 2, "auto_summary": True})
    v = _wait_series(client, r.json()["id"])
    d = _json.loads((tmp_runs(client) / f"{v['summary']['session_id']}.json").read_text(encoding="utf-8"))
    authors = set(d["authors"].values())
    assert len(authors) == 2 and "a" in authors  # пишут председатель и лучший по частям
    critics = {c.get("reviewer") or c.get("member") for c in d["critiques"]} if isinstance(d["critiques"], list) else set(d["critiques"])
    assert {"a", "b", "c"} <= set(d["ballots"]) and len(critics) == 3  # критикуют и голосуют все трое
    assert d["research"]["ledger"]["inherited_parts"] == [1, 2]
