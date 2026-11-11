"""Страница настроек: сохранение config.yaml, API, обновление программы через службу на сервере."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from council.config import load_config
from council.settings import SettingsError, editable, save

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cfg_file(tmp_path):
    p = tmp_path / "config.yaml"
    shutil.copy(ROOT / "config.yaml", p)
    text = p.read_text(encoding="utf-8").replace("runs_dir: runs", f"runs_dir: {tmp_path / 'runs'}")
    p.write_text(text, encoding="utf-8")
    return p


def test_save_keeps_comments_and_changes_only_values(cfg_file, tmp_path):
    before = cfg_file.read_text(encoding="utf-8")
    d = editable(load_config(cfg_file))
    d["max_cost_usd"] = 3.5
    d["members"][1]["model"] = "openai/gpt-new"
    d["members"].append({"name": "newbie", "model": "x/new", "enabled": True, "reasoning_effort": "high"})
    d["fact_check"]["enabled"] = True
    d["branding"]["title"] = "ООО Ромашка"
    save(cfg_file, d, backups=tmp_path / "bk")
    after = cfg_file.read_text(encoding="utf-8")
    assert "# жёсткий лимит стоимости одного заседания" in after and "max_cost_usd: 3.5" in after
    assert "# Фирменное оформление отчётов" in after and 'title: "ООО Ромашка"' in after
    assert "model: openai/gpt-new" in after and "name: newbie" in after and "reasoning_effort: high" in after
    changed = [ln for ln in after.splitlines() if ln not in before.splitlines()]
    assert len(changed) <= 8, changed  # остальной файл не тронут
    cfg = load_config(cfg_file)
    assert cfg.max_cost_usd == 3.5 and cfg.fact_check.enabled and cfg.member("newbie").reasoning_effort == "high"
    assert len(list((tmp_path / "bk").glob("config-*.yaml"))) == 1


def test_save_rejects_bad_settings(cfg_file):
    d = editable(load_config(cfg_file))
    original = cfg_file.read_text(encoding="utf-8")
    bad = json.loads(json.dumps(d))
    bad["chair"] = "nobody"
    with pytest.raises(SettingsError, match="председатель"):
        save(cfg_file, bad)
    bad = json.loads(json.dumps(d))
    bad["members"][0]["model"] = ""
    with pytest.raises(SettingsError, match="имя и модель"):
        save(cfg_file, bad)
    bad = json.loads(json.dumps(d))
    bad["max_cost_usd"] = -1
    with pytest.raises(SettingsError):
        save(cfg_file, bad)
    assert cfg_file.read_text(encoding="utf-8") == original


def test_remove_and_disable_members(cfg_file):
    d = editable(load_config(cfg_file))
    d["members"] = [m for m in d["members"] if m["name"] != "kimi"]
    for m in d["members"]:
        if m["name"] == "deepseek":
            m["enabled"] = False
    d["devil_advocate"] = None
    save(cfg_file, d)
    cfg = load_config(cfg_file)
    assert "kimi" not in [m.name for m in cfg.members] and not cfg.member("deepseek").enabled
    assert cfg.devil_advocate is None


@pytest.fixture
def web(cfg_file, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from council.estimate import ModelCatalog

    async def fake_get(self):
        return [{"id": "anthropic/claude-sonnet-4.5", "created": 1759000000, "context_length": 200000,
                 "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                 "architecture": {"input_modalities": ["text", "image"]}},
                {"id": "anthropic/claude-sonnet-5", "created": 1780000000, "context_length": 1000000,
                 "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                 "architecture": {"input_modalities": ["text", "image"]}},
                {"id": "openai/gpt-5", "created": 1754000000, "pricing": {}}]
    monkeypatch.setattr(ModelCatalog, "get", fake_get)
    from council.web.app import create_app
    with TestClient(create_app(cfg_file, mock_delay=0)) as c:
        yield c, cfg_file


def test_settings_api(web):
    c, cfg_file = web
    r = c.get("/api/settings").json()
    assert r["writable"] and r["settings"]["chair"] == "claude"
    s = r["settings"]
    s["max_cost_usd"] = 1.25
    r = c.put("/api/settings", json=s)
    assert r.status_code == 200 and r.json()["settings"]["max_cost_usd"] == 1.25
    assert any("нет на OpenRouter" in w for w in r.json()["warnings"])  # gemini и др. нет в тестовом каталоге
    assert c.get("/api/config").json()["budget"] == 1.25  # форма вопроса видит новые значения
    s["chair"] = "nobody"
    assert c.put("/api/settings", json=s).status_code == 400
    backups = c.get("/api/settings/backups").json()["items"]
    assert backups and c.post(f"/api/settings/restore/{backups[0]}").json()["ok"]
    assert load_config(cfg_file).max_cost_usd == 2.0  # вернулась версия до сохранения
    assert c.post("/api/settings/restore/..%2Fconfig.yaml").status_code in (400, 404)


def test_models_and_upgrades_api(web):
    c, _ = web
    items = c.get("/api/models?q=claude").json()["items"]
    assert [m["id"] for m in items] == ["anthropic/claude-sonnet-5", "anthropic/claude-sonnet-4.5"]
    assert items[0]["image"] and items[0]["prompt"] == 3.0
    u = {x["name"]: x for x in c.get("/api/models/upgrades").json()["items"]}
    assert [m["id"] for m in u["claude"]["same_family"]] == ["anthropic/claude-sonnet-5"]
    assert u["gemini"]["found"] is False


def test_logo_upload(web):
    c, cfg_file = web
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (40, 20), "red").save(buf, "PNG")
    r = c.post("/api/branding/logo", content=buf.getvalue())
    assert r.status_code == 200 and (cfg_file.parent / "branding" / "logo.png").is_file()
    assert load_config(cfg_file).branding.logo == "branding/logo.png"
    assert c.get("/api/branding/logo").status_code == 200
    assert c.post("/api/branding/logo", content=b"not an image").status_code == 400


def test_system_update_request_and_check(web, monkeypatch):
    c, cfg_file = web
    control = cfg_file.parent / "control"
    s = c.get("/api/system").json()
    assert s["control"] is False and s["version"] is None
    assert c.post("/api/system/update").status_code == 409  # служба не установлена
    control.mkdir()
    (control / "version.json").write_text(json.dumps({
        "commit": "a" * 40, "date": "2026-11-01T10:00:00+00:00", "subject": "Старое", "branch": "main",
        "remote": "https://github.com/owner/repo.git"}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/owner/repo/compare/{'a' * 40}...main"
        return httpx.Response(200, json={"ahead_by": 2, "status": "ahead", "commits": [
            {"sha": "b" * 40, "commit": {"message": "Первое\n\nподробно", "committer": {"date": "2026-11-02T10:00:00Z"}}},
            {"sha": "c" * 40, "commit": {"message": "Второе", "committer": {"date": "2026-11-03T10:00:00Z"}}}]})

    real = httpx.AsyncClient
    monkeypatch.setattr("council.web.admin.httpx.AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    chk = c.get("/api/system/check").json()
    assert chk["behind"] == 2 and [x["title"] for x in chk["commits"]] == ["Второе", "Первое"]
    r = c.post("/api/system/update")
    assert r.status_code == 200 and (control / "request").read_text().strip() == "update"
    assert c.post("/api/system/restart").status_code == 409  # заявка уже ждёт
    assert c.get("/api/system").json()["pending"] is True


# ---------------------------------------------------------------- серверный скрипт обновления


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t"})


@pytest.mark.skipif(shutil.which("git") is None, reason="нужен git")
def test_web_control_update_keeps_user_config(tmp_path):
    # «GitHub»: репозиторий с программой
    origin = tmp_path / "origin"
    (origin / "ai-council" / "deploy").mkdir(parents=True)
    shutil.copy(ROOT / "deploy" / "web-control.sh", origin / "ai-council" / "deploy" / "web-control.sh")
    (origin / "ai-council" / "config.yaml").write_text("chair: a\nmax_cost_usd: 2.0\n", encoding="utf-8")
    (origin / "ai-council" / "app.txt").write_text("v1\n", encoding="utf-8")
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "add", ".")
    _git(origin, "commit", "-q", "-m", "v1")
    # сервер: клон, пользователь поменял настройки
    server = tmp_path / "server"
    _git(tmp_path, "clone", "-q", str(origin), str(server))
    app = server / "ai-council"
    (app / "config.yaml").write_text("chair: a\nmax_cost_usd: 7.0  # моё\n", encoding="utf-8")
    # в репозитории вышла новая версия, в том числе с изменённым config.yaml
    (origin / "ai-council" / "app.txt").write_text("v2\n", encoding="utf-8")
    (origin / "ai-council" / "config.yaml").write_text("chair: a\nmax_cost_usd: 2.0\nnew_option: 1\n", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "v2: новая версия")
    # docker подменён: записывает, что его вызвали
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/docker.log\n', encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    (app / "control").mkdir()
    (app / "control" / "request").write_text("update\n", encoding="utf-8")
    r = subprocess.run(["bash", str(app / "deploy" / "web-control.sh"), "run"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (app / "app.txt").read_text() == "v2\n"                       # код обновлён
    assert "max_cost_usd: 7.0  # моё" in (app / "config.yaml").read_text()  # настройки пользователя сохранены
    assert not (app / "control" / "request").exists()
    st = json.loads((app / "control" / "status.json").read_text())
    assert st["state"] == "done" and st["action"] == "update"
    ver = json.loads((app / "control" / "version.json").read_text())
    assert ver["subject"] == "v2: новая версия" and ver["branch"] == "main"
    calls = (tmp_path / "docker.log").read_text()
    assert "compose build -q" in calls and "compose up -d --force-recreate" in calls
    # без заявки скрипт ничего не делает
    assert subprocess.run(["bash", str(app / "deploy" / "web-control.sh"), "run"], env=env).returncode == 0


def test_pick_any_model_of_the_same_developer(web, monkeypatch):
    """DeepSeek Pro вышла раньше v4.1-flash и считается другой линейкой — но выбрать её можно."""
    c, _ = web
    from council.estimate import ModelCatalog

    async def fake_get(self):
        return [{"id": "deepseek/deepseek-v4.1-flash", "created": 1790000000, "context_length": 128000,
                 "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}, "supported_parameters": ["reasoning"]},
                {"id": "deepseek/deepseek-v4-pro", "created": 1785000000, "context_length": 128000,
                 "pricing": {"prompt": "0.0000005", "completion": "0.000002"}, "supported_parameters": ["reasoning"],
                 "top_provider": {"max_completion_tokens": 64000}},
                {"id": "deepseek/deepseek-v4-flash", "created": 1780000000, "pricing": {}},
                {"id": "deepseek/deepseek-embed", "created": 1790000001, "pricing": {}},
                {"id": "openai/gpt-5", "created": 1754000000, "pricing": {}}]
    monkeypatch.setattr(ModelCatalog, "get", fake_get)
    r = c.get("/api/models/family", params={"model": "deepseek/deepseek-v4.1-flash"}).json()
    groups = {g["family"]: [m["id"] for m in g["items"]] for g in r["groups"]}
    assert r["provider"] == "deepseek" and r["groups"][0]["family"] == "deepseek-flash"  # своя линейка первой
    assert groups["deepseek-flash"] == ["deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4-flash"]
    assert groups["deepseek-pro"] == ["deepseek/deepseek-v4-pro"] and not any("embed" in i for v in groups.values() for i in v)
    pro = r["groups"][1]["items"][0]
    assert pro["reasoning"] and pro["max_out"] == 64000 and pro["prompt"] == 0.5


def test_model_notes_tags_roles_and_history():
    from council.model_notes import history, roles, tags
    pro = {"id": "deepseek/deepseek-v4-pro", "completion": 2.0, "reasoning": True, "context": 128000, "max_out": 64000}
    flash = {"id": "deepseek/deepseek-v4.1-flash", "completion": 0.6, "reasoning": True, "context": 128000}
    search = {"id": "perplexity/sonar-pro", "completion": 15, "context": 200000}
    assert ["ok", "дешёвая"] in tags(flash) and ["plain", "облегчённая версия"] in tags(flash)
    assert any("председатель" in r for r in roles(pro)) and not any("председатель:" in r for r in roles(flash))
    assert any("проверка фактов" in r for r in roles(search)) and ["ok", "ищет в интернете"] in tags(search)
    sessions = [{"transcript": [{"member": "ds", "model": "deepseek/deepseek-v4-pro"}],
                 "stats": {"ds": {"cost": 0.2, "failures": ["x"], "reasoning_off": True}},
                 "authors": {"A": "ds"}, "verdict": {"winner": "A"}, "tally": [{"label": "A", "avg_score": 7.5}]}]
    h = history(sessions)["deepseek/deepseek-v4-pro"]
    assert h["wins"] == 1 and h["win_rate"] == 1.0 and h["failures"] == 1 and h["reasoning_off"] == 1


def test_model_card_api(web, monkeypatch):
    c, _ = web
    r = c.get("/api/models/card", params={"model": "anthropic/claude-sonnet-5"}).json()
    assert r["found"] and r["tags"] and r["roles"] and r["history"] is None
    assert c.get("/api/models/card", params={"model": "nope/x"}).json()["found"] is False
    assert c.post("/api/models/card/summary", json={"model": "anthropic/claude-sonnet-5"}).status_code == 400  # нет ключа
    items = c.get("/api/models?q=claude").json()["items"]
    assert items[0]["tags"] and "desc" in items[0]
