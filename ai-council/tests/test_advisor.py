"""«Подобрать состав»: кандидаты из каталога, проверка ответа советника, участники на заседание, поиск у участников."""
import asyncio
import json

import pytest
import yaml
from fastapi.testclient import TestClient

from council import advisor as A
from council.client import Completion
from council.config import CouncilConfig, OverrideError, with_overrides
from council.mock import MockClient
from council.session import Council


def model(i, ts, pin=1, pout=4, reasoning=True, ctx=200_000, out=64_000, desc=""):
    d = {"id": i, "created": ts, "context_length": ctx, "description": desc or f"{i} model",
         "pricing": {"prompt": str(pin / 1e6), "completion": str(pout / 1e6)},
         "supported_parameters": ["reasoning"] if reasoning else [], "top_provider": {"max_completion_tokens": out}}
    return d


CATALOG = [
    model("anthropic/claude-sonnet-5.5", 1790000000, 3, 15, out=128_000),
    model("anthropic/claude-haiku-5", 1789000000, 0.8, 4, out=32_000),
    model("deepseek/deepseek-v4-flash", 1780000000, 0.1, 0.4, out=16_000),
    model("deepseek/deepseek-v4-pro", 1785000000, 0.5, 2.0, out=64_000),
    model("google/gemini-4-flash", 1788000000, 0.2, 0.8, out=32_000),
    model("google/gemini-4-pro", 1789000000, 2, 10, out=64_000),
    model("qwen/qwen4-max", 1789500000, 1, 4, out=32_000),
    model("perplexity/sonar-pro", 1780000000, 3, 15, reasoning=False, out=8000),
]


def cfg(**over):
    data = {"chair": "claude", "members": [{"name": "claude", "model": "anthropic/claude-sonnet-5.5"},
                                           {"name": "deepseek", "model": "deepseek/deepseek-v4-flash"},
                                           {"name": "gemini", "model": "google/gemini-4-flash"}]}
    data.update(over)
    return CouncilConfig.model_validate(data)


def test_candidates_include_members_their_siblings_and_other_developers():
    ids = [c["id"] for c in A.candidates(cfg(), CATALOG)]
    assert {"anthropic/claude-sonnet-5.5", "deepseek/deepseek-v4-flash", "google/gemini-4-flash"} <= set(ids)
    assert "deepseek/deepseek-v4-pro" in ids and "google/gemini-4-pro" in ids  # старшие модели тех же разработчиков
    assert "qwen/qwen4-max" in ids  # «какие ещё модели»: известный разработчик, которого в совете нет
    assert set(ids[:3]) == {"anthropic/claude-sonnet-5.5", "deepseek/deepseek-v4-flash", "google/gemini-4-flash"}


def test_finalize_keeps_only_catalog_models_one_chair_and_two_searchers():
    c = cfg()
    cands = A.candidates(c, CATALOG)
    adv = A.Advice(mode="nonsense", picks=[
        A.Pick(model="anthropic/claude-sonnet-5.5", role="chair", basis=["catalog", "magic"]),
        A.Pick(model="openai/invented-9", role="member"),
        A.Pick(model="deepseek/deepseek-v4-pro", role="chair", web_search=True),
        A.Pick(model="google/gemini-4-pro", role="devil", web_search=True),
        A.Pick(model="qwen/qwen4-max", role="devil", web_search=True),
        A.Pick(model="perplexity/sonar-pro", web_search=True)])
    out = A.finalize(adv, cands, c, "debate")
    roles = {p.model: p.role for p in out.picks}
    assert "openai/invented-9" not in roles and any("invented-9" in x for x in out.caveats)
    assert list(roles.values()).count("chair") == 1 and roles["anthropic/claude-sonnet-5.5"] == "chair"
    assert list(roles.values()).count("devil") == 1
    assert sum(p.web_search for p in out.picks) <= 2
    assert "perplexity/sonar-pro" not in roles  # модели с поиском — для проверки фактов, не участники
    assert out.picks[0].basis == ["catalog"] and out.mode == "debate"


def test_assign_reuses_members_swaps_models_and_adds_new_ones():
    c = cfg()
    adv = A.Advice(picks=[A.Pick(model="anthropic/claude-sonnet-5.5", role="chair"),
                          A.Pick(model="deepseek/deepseek-v4-pro", web_search=True),
                          A.Pick(model="qwen/qwen4-max", role="devil")])
    plan = A.assign(adv, c)
    assert plan["members"] == ["claude", "deepseek", "qwen"] and plan["chair"] == "claude"
    assert plan["models"] == {"deepseek": "deepseek/deepseek-v4-pro"}
    assert plan["extra_members"] == [{"name": "qwen", "model": "qwen/qwen4-max"}]
    assert plan["devil_advocate"] == "qwen" and plan["web_search"] == ["deepseek"]
    session = with_overrides(c, members=plan["members"], chair=plan["chair"], devil_advocate=plan["devil_advocate"],
                             models=plan["models"], extra_members=plan["extra_members"], web_search=plan["web_search"])
    active = {m.name: m for m in session.active_members}
    assert active["deepseek"].model == "deepseek/deepseek-v4-pro" and active["deepseek"].web_search
    assert active["qwen"].model == "qwen/qwen4-max" and "gemini" not in active
    assert [m.model for m in c.members if m.name == "deepseek"] == ["deepseek/deepseek-v4-flash"]  # настройки не тронуты


def test_overrides_reject_bad_members():
    with pytest.raises(OverrideError):
        with_overrides(cfg(), extra_members=[{"name": "claude", "model": "x/y"}])
    with pytest.raises(OverrideError):
        with_overrides(cfg(), models={"nobody": "x/y"})
    with pytest.raises(OverrideError):
        with_overrides(cfg(), web_search=["nobody"])


def test_rules_pick_stronger_siblings_and_research_for_a_big_market_study():
    c = cfg()
    q = "Проведи исследование мирового рынка и найди производителей средств от гипергидроза. " * 20
    adv = A.finalize(A.rule_advice(q, "research", c, A.candidates(c, CATALOG), {}, []), A.candidates(c, CATALOG), c,
                     "research")
    ids = {p.model: p for p in adv.picks}
    assert adv.mode == "research" and adv.task.size == "large"
    assert "deepseek/deepseek-v4-pro" in ids and "google/gemini-4-pro" in ids  # pro вместо flash
    assert ids["anthropic/claude-sonnet-5.5"].role == "chair"
    assert all(p.basis[0] == "catalog" for p in adv.picks) and any("правилам" in x for x in adv.caveats)


def test_member_with_web_search_searches_on_proposal_and_revision_only():
    class Rec(MockClient):
        def __init__(self):
            super().__init__(delay=0)
            self.web: list[tuple[str, str]] = []

        async def complete(self, model, messages, **kw):
            if kw.get("web_search"):
                self.web.append((model, kw.get("meta", {}).get("stage", "")))
            return await super().complete(model, messages, **kw)

    c = CouncilConfig.model_validate({"chair": "a", "json_retries": 1, "members": [
        {"name": n, "model": f"m/{n}", "web_search": n == "b"} for n in "abcd"]})
    client = Rec()
    asyncio.run(Council(c, client, seed=1).run("Какие сейчас цены на кофе?"))
    assert {s for _, s in client.web} == {"proposal", "revision"} and {m for m, _ in client.web} == {"m/b"}


# ------------------------------------------------------------------ сайт

@pytest.fixture
def site(tmp_path, monkeypatch):
    from council.estimate import ModelCatalog

    async def fake_get(self):
        return CATALOG

    monkeypatch.setattr(ModelCatalog, "get", fake_get)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    conf = {"chair": "claude", "runs_dir": str(tmp_path / "runs"),
            "members": [{"name": "claude", "model": "anthropic/claude-sonnet-5.5"},
                        {"name": "deepseek", "model": "deepseek/deepseek-v4-flash"},
                        {"name": "gemini", "model": "google/gemini-4-flash"}]}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(conf), encoding="utf-8")
    with TestClient(create_app_(path)) as c:
        c.runs = tmp_path / "runs"
        yield c


def create_app_(path):
    from council.web.app import create_app
    return create_app(path, mock_delay=0)


def read_events(client, sid):
    with client.stream("GET", f"/api/sessions/{sid}/events") as r:
        return [json.loads(l[6:]) for l in r.iter_lines() if l.startswith("data: ")]


def test_advise_in_demo_then_run_with_the_recommended_lineup(site):
    r = site.post("/api/advise", json={"question": "Сравни три CRM для отдела продаж и найди актуальные цены", "mock": True})
    assert r.status_code == 200
    body = r.json()
    plan = body["plan"]
    assert body["source"] == "rules" and plan["chair"] in plan["members"]
    assert body["facts"] and all(p["basis"] for p in body["advice"]["picks"])
    plan["extra_members"] = [{"name": "qwen", "model": "qwen/qwen4-max"}]  # и участник не из настроек
    s = site.post("/api/sessions", json={"question": "Сравни три CRM", "mock": True, "members": plan["members"] + ["qwen"],
                                         "chair": plan["chair"], "models": plan["models"],
                                         "extra_members": plan["extra_members"], "web_search": ["qwen"]})
    assert s.status_code == 200, s.text
    sid = s.json()["id"]
    assert read_events(site, sid)[-1]["type"] == "done"
    lineup = site.get(f"/api/sessions/{sid}/request").json()  # для уточнения: тот же состав
    assert lineup["members"] == plan["members"] + ["qwen"] and lineup["chair"] == plan["chair"]
    assert site.get("/api/sessions/nope/request").status_code == 404
    req = site.post(f"/api/sessions/{sid}/repeat").json()["request"]
    assert req["extra_members"] == [{"name": "qwen", "model": "qwen/qwen4-max"}] and req["web_search"] == ["qwen"]
    assert req["models"]["deepseek"] == plan["models"].get("deepseek", "deepseek/deepseek-v4-flash")


def test_advise_with_key_builds_dossiers_once_and_uses_the_advisor(site, monkeypatch):
    import council.client as client_mod
    calls = {"dossier": 0, "advisor": 0}

    class Fake:
        def __init__(self, *a, **k):
            pass

        async def complete(self, model, messages, **kw):
            text = messages[-1]["content"]
            if kw.get("web_search"):
                calls["dossier"] += 1
                ids = [l[2:] for l in text.splitlines() if l.startswith("- ") and "/" in l]
                data = {"models": [{"id": i, "strengths": ["хорош в анализе"], "known_issues": ["бывает многословен"],
                                    "sources": ["https://real.example/review", "https://invented.example/x"]} for i in ids]}
                return Completion(text=json.dumps(data), model=model, cost=0.01,
                                  citations=["https://real.example/review"])
            calls["advisor"] += 1
            adv = {"task": {"kind": "сравнение", "size": "medium", "needs": ["свежие цены"]}, "mode": "debate",
                   "picks": [{"model": "anthropic/claude-sonnet-5.5", "role": "chair", "why": "длинный итог",
                              "basis": ["catalog", "reviews"]},
                             {"model": "google/gemini-4-pro", "role": "member", "web_search": True, "why": "свежие цены",
                              "basis": ["reviews"]},
                             {"model": "x-ai/nonexistent", "role": "member", "why": "?"}],
                   "avoid": [{"model": "deepseek/deepseek-v4-flash", "why": "облегчённая", "basis": ["catalog"]}]}
            return Completion(text=json.dumps(adv, ensure_ascii=False), model=model, cost=0.05)

        async def aclose(self):
            pass

    monkeypatch.setattr(client_mod, "OpenRouterClient", Fake)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    body = site.post("/api/advise", json={"question": "Какая CRM лучше по цене сейчас?"}).json()
    assert body["source"] == "advisor" and calls["advisor"] == 1 and calls["dossier"] >= 1
    assert body["dossiers_built"] == len(A.candidates(cfg(), CATALOG))
    models = [p["model"] for p in body["advice"]["picks"]]
    assert "x-ai/nonexistent" not in models and body["plan"]["web_search"] == ["gemini"]
    assert body["plan"]["models"] == {"gemini": "google/gemini-4-pro"}
    d = body["dossiers"]["google/gemini-4-pro"]
    assert d["sources"] == ["https://real.example/review"]  # ссылки, которых не было в поиске, отброшены
    first = calls["dossier"]
    body2 = site.post("/api/advise", json={"question": "Ещё вопрос про цены"}).json()
    assert calls["dossier"] == first and body2["dossiers_cached"] == body["dossiers_built"]  # досье из памяти
    assert not (site.runs / "model_dossiers.json").exists()  # не в корне архива — не примут за заседание


def test_research_settings_are_suggested_cleaned_and_applied_per_session():
    c = cfg()
    cands = A.candidates(c, CATALOG)
    q = "Проведи актуальное исследование мирового рынка и найди производителей средств от гипергидроза, цены 2026. " * 20
    adv = A.finalize(A.rule_advice(q, "research", c, cands, {}, []), cands, c, "research")
    rs = adv.research_settings
    assert rs["arbitration_claims"] == 25 and rs["fresh_scouts"] == 1 and "shorten_long" not in rs  # уже включено
    assert adv.research_cost["arbitration_claims"] > 0
    assert all(adv.research_why[k] for k in rs)
    raw = A.Advice(mode="research", research_settings={"arbitration_claims": 150, "missions": 1, "max_words": 1400,
                                                     "runs_dir": "/etc", "lenses": False},
                   research_why={"missions": "дешевле", "lenses": "один угол"})
    out = A.finalize(raw, cands, c, "research")
    assert out.research_settings == {"missions": 1, "lenses": False}  # вне пределов, чужие и неизменные — отброшены
    assert A.finalize(raw.model_copy(update={"mode": "debate"}), cands, c, "debate").research_settings == {}
    s = with_overrides(c, research={"arbitration_claims": 25, "fresh_scouts": 1})
    assert s.research.arbitration_claims == 25 and c.research.arbitration_claims == 20
    with pytest.raises(OverrideError):
        with_overrides(c, research={"runs_dir": "x"})


def test_research_overrides_are_recorded_and_restored(site):
    s = site.post("/api/sessions", json={"question": "Рынок кофеен у вузов?", "mock": True, "mode": "research",
                                         "research": {"arbitration_claims": 15, "missions": 1}})
    assert s.status_code == 200, s.text
    sid = s.json()["id"]
    assert read_events(site, sid)[-1]["type"] == "done"
    assert site.get(f"/api/sessions/{sid}/request").json()["research_overrides"] == {"arbitration_claims": 15, "missions": 1}
    bad = site.post("/api/sessions", json={"question": "Вопрос?", "mock": True, "mode": "research", "research": {"runs_dir": "x"}})
    assert bad.status_code == 400


def test_light_models_are_detected_by_whole_words():
    from council.catalog import is_light
    assert not is_light("google/gemini-4-pro") and not is_light("google/gemini-2.5-pro")
    assert is_light("google/gemini-4-flash") and is_light("openai/gpt-6.1-mini") and is_light("anthropic/claude-haiku-5")
    assert is_light("google/gemini-2.0-flash-lite") and not is_light("x-ai/grok-4.7")


def test_rules_pick_for_creative_mode_without_changing_it():
    c = cfg()
    q = "Придумай название и слоган для новой линейки средств от потливости — актуальные тренды 2026"
    cands = A.candidates(c, CATALOG)
    # режим не меняется: в «Подробном обсуждении» советник остаётся в нём, даже если вопрос творческий
    assert A.finalize(A.rule_advice(q, "debate", c, cands, {}, []), cands, c, "debate").mode == "debate"
    adv = A.finalize(A.rule_advice(q, "creative", c, cands, {}, []), cands, c, "creative")
    assert adv.mode == "creative" and adv.task.kind == "творческая задача" and not adv.mode_why
    assert sum(p.web_search for p in adv.picks) == 1  # «актуальные тренды» — ищет один автор, остальные независимо
    assert len({A.provider(p.model) for p in adv.picks}) == len(adv.picks)  # по одной модели от разработчика
    assert any("Креатив" in x for x in adv.caveats)
    plain = A.finalize(A.rule_advice("Придумай название для кофейни у моря", "creative", c, cands, {}, []), cands, c,
                       "creative")
    assert plain.mode == "creative" and not any(p.web_search for p in plain.picks)
    comp = A.finalize(A.rule_advice("Придумай название, сравни с конкурентами", "creative", c, cands, {}, []), cands, c,
                      "creative")
    assert sum(p.web_search for p in comp.picks) == 1
    # советнику-модели передаётся правило подбора для «Креатива»
    p = A.advise_prompt(question=q, mode="creative", attachments=[], followup=False, budget=3, current={},
                        cands=cands, history={}, dossiers={}, language="Russian", research_words=1400,
                        search_model="x/y")
    assert "DIVERSITY OF IMAGINATION" in p and "do NOT suggest another" in p and "research: long context" not in p


def test_history_is_split_by_mode():
    from council.model_notes import history
    def sess(mode, winner):
        return {"mode": mode, "authors": {"A": "k", "B": "c"}, "verdict": {"winner": winner},
                "tally": [{"label": "A", "avg_score": 8}, {"label": "B", "avg_score": 6}],
                "transcript": [{"member": "k", "model": "m/kimi"}, {"member": "c", "model": "m/claude"}],
                "stats": {"k": {}, "c": {}}}
    h = history([sess("research", "A")] * 4 + [sess("creative", "B")] * 3)
    assert h["m/kimi"]["by_mode"]["research"]["wins"] == 4 and h["m/kimi"]["by_mode"]["creative"]["wins"] == 0
    assert h["m/claude"]["by_mode"]["creative"]["win_rate"] == 1.0
