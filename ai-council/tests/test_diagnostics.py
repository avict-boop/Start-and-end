"""Журналы для разбора ошибок: сохранение событий и прерванных заседаний, пакет zip, скрытие ключей."""
import io
import json
import zipfile

import pytest
import yaml
from fastapi.testclient import TestClient

from council.diagnostics import bundle, events_text, redact, save_events, save_failed, stamp


def test_redact_hides_keys():
    text = "key sk-or-v1-abcdef1234567890abcdef token 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw Bearer abc.def.ghi"
    out = redact(text)
    assert "1234567890abcdef" not in out and "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in out and "abc.def.ghi" not in out
    assert "sk-or-v1-a" in out and "скрыто" in out


def test_events_text_is_readable():
    t0 = 1_700_000_000.0
    evs = [{"type": "stage", "stage": "proposal", "title": "Независимые предложения", "ts": t0},
           {"type": "call", "member": "claude", "stage": "proposal", "phase": "thinking", "ts": t0 + 1},
           {"type": "call", "member": "claude", "stage": "proposal", "phase": "retry",
            "detail": "ответ длинный, упёрся в лимит 8000 токенов — прошу продолжить", "ts": t0 + 50},
           {"type": "member_failed", "member": "mistral", "stage": "proposal", "error": "field 'title': Field required", "ts": t0 + 60},
           {"type": "ping"}]
    txt = events_text(evs)
    assert "▶ Независимые предложения" in txt and "+   50.0s" in txt and "упёрся в лимит" in txt
    assert "СБОЙ: field 'title'" in txt and "thinking" not in txt  # шумные обновления пропущены


def test_bundle_contains_session_and_logs(tmp_path):
    runs = tmp_path / "runs"
    (runs / "logs").mkdir(parents=True)
    (runs / "logs" / "web.log").write_text("ERROR сбой с ключом sk-or-v1-secretsecretsecret\n", encoding="utf-8")
    save_events(runs, "s1", [stamp({"type": "stage", "stage": "vote", "title": "Голосование"})])
    save_failed(runs, "s1", question="Вопрос?", status="error", error="нет кворума",
                transcript=[{"stage": "proposal", "member": "a", "raw": "обрыв {", "error": "JSON"}])
    cfg = tmp_path / "config.yaml"
    cfg.write_text("chair: a\n", encoding="utf-8")
    z = zipfile.ZipFile(io.BytesIO(bundle(runs, cfg, stem="s1")))
    names = set(z.namelist())
    assert {"README.txt", "config.yaml", "logs/web.log", "failed/s1.json", "session/events.txt",
            "session/events.jsonl", "session/failed.json", "session/transcript.json"} <= names
    assert "secretsecret" not in z.read("logs/web.log").decode()
    assert json.loads(z.read("session/transcript.json"))[0]["raw"] == "обрыв {"


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    cfg = {"chair": "a", "runs_dir": str(tmp_path / "runs"), "min_quorum": 2,
           "members": [{"name": n, "model": f"m/{n}"} for n in "abc"]}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    from council.web.app import create_app
    with TestClient(create_app(tmp_path / "config.yaml", mock_delay=0)) as c:
        yield c, tmp_path


def _events(c, sid):
    with c.stream("GET", f"/api/sessions/{sid}/events") as r:
        return [json.loads(line[6:]) for line in r.iter_lines() if line.startswith("data: ")]


def test_web_saves_events_and_failed_sessions(web, monkeypatch):
    c, tmp = web
    sid = c.post("/api/sessions", json={"question": "Как назвать кофейню?", "mock": True, "mode": "quick"}).json()["id"]
    evs = _events(c, sid)
    assert all("ts" in e for e in evs if e["type"] != "ping")
    assert (tmp / "runs" / f"{sid}.events.jsonl").is_file()
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/sessions/{sid}/diagnostics").content))
    assert "session/events.txt" in z.namelist() and "session/transcript.json" in z.namelist()
    assert "logs/web.log" in z.namelist()  # журнал программы ведётся

    # заседание, которое упало: все модели недоступны -> нет кворума
    from council.mock import MockClient
    real_init = MockClient.__init__

    def failing(self, *a, **kw):
        real_init(self, *a, **kw)
        self.fail_models = {"m/a", "m/b", "m/c"}
    monkeypatch.setattr(MockClient, "__init__", failing)
    sid2 = c.post("/api/sessions", json={"question": "Упадёт?", "mock": True, "mode": "quick"}).json()["id"]
    evs = _events(c, sid2)
    assert evs[-1]["type"] == "error"
    failed = c.get("/api/diagnostics/failed").json()["items"]
    assert failed and failed[0]["id"] == sid2 and "кворума" in failed[0]["error"]
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/api/sessions/{sid2}/diagnostics").content))
    tr = json.loads(z.read("session/transcript.json"))
    assert "session/failed.json" in z.namelist() and isinstance(tr, list)
    assert "СБОЙ" in z.read("session/events.txt").decode()
    all_zip = zipfile.ZipFile(io.BytesIO(c.get("/api/diagnostics").content))
    assert f"failed/{sid2}.json" in all_zip.namelist()
    assert c.get("/api/sessions/nope/diagnostics").status_code == 404
