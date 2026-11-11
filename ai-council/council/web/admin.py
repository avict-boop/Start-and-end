"""Страница настроек веб-интерфейса: участники и модели, параметры заседания, оформление, версия программы.

Настройки пишутся в config.yaml (с сохранением комментариев). Обновление и перезапуск программы
веб-интерфейс сам не выполняет — он оставляет заявку в папке control/, её выполняет служба на сервере
(deploy/web-control.sh). Так контейнеру не нужен доступ к Docker и серверу.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from ..catalog import created, date, family, is_text_model, modalities, provider, sort_newest, upgrades
from ..config import CouncilConfig
from ..estimate import ModelCatalog, unknown_models
from ..settings import SettingsError, editable, save

GITHUB_API = os.environ.get("GITHUB_API", "https://api.github.com")
CHECK_TTL = 300
LOGO_MAX = 2 * 1024 * 1024


def _model_row(m: dict[str, Any]) -> dict[str, Any]:
    pr = m.get("pricing") or {}
    mod = modalities(m)
    params = set(m.get("supported_parameters") or [])
    return {"id": m["id"], "name": m.get("name") or "", "date": date(m), "context": m.get("context_length"),
            "prompt": round(float(pr.get("prompt") or 0) * 1e6, 3),
            "completion": round(float(pr.get("completion") or 0) * 1e6, 3),
            "image": "image" in mod, "audio": "audio" in mod,
            "reasoning": bool(params & {"reasoning", "include_reasoning"}),
            "max_out": (m.get("top_provider") or {}).get("max_completion_tokens"),
            "desc": _short(m.get("description") or "")}


class _SummaryReq(BaseModel):
    model: str


class _TranslateReq(BaseModel):
    ids: list[str]
    full: bool = False


class _AdviseReq(BaseModel):
    question: str
    mode: str = "debate"
    members: list[str] | None = None
    chair: str | None = None
    devil_advocate: str | None = None
    budget: float | None = None
    attachments: list[dict[str, Any]] = []   # {name, kind, chars} — только размеры, не содержимое
    followup: bool = False
    mock: bool = False
    refresh: bool = False  # обновить досье моделей, даже если они свежие


def _short(text: str, limit: int = 220) -> str:
    """Первое предложение-два описания разработчика (для списков)."""
    t = " ".join(str(text).split())
    if len(t) <= limit:
        return t
    cut = t[:limit]
    dot = cut.rfind(". ")
    return (cut[:dot + 1] if dot > 80 else cut.rstrip() + "…")


def _with_tags(row: dict[str, Any]) -> dict[str, Any]:
    from ..model_notes import tags
    return {**row, "tags": tags(row)}


def created_of(models: list[dict[str, Any]], mid: str) -> int:
    return next((created(m) for m in models if m.get("id") == mid), 0)


def _repo_of(remote: str) -> str | None:
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?/?$", remote or "")
    return m.group(1) if m else None


def register(app: FastAPI, config_path: Path, base_config: Callable[[], CouncilConfig],
             runs_dir: Callable[[CouncilConfig], Path], catalog: ModelCatalog) -> None:
    control = config_path.parent / "control"
    branding_dir = config_path.parent / "branding"
    check_cache: dict[str, Any] = {}

    def writable(p: Path) -> bool:
        return p.exists() and os.access(p, os.W_OK)

    # ------------------------------------------------------------ настройки

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        cfg = base_config()
        return {"settings": editable(cfg), "writable": writable(config_path),
                "logo": (cfg.branding.logo or ""), "logo_writable": writable(branding_dir)}

    @app.put("/api/settings")
    async def put_settings(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError as e:
            raise HTTPException(400, "ожидался JSON с настройками") from e
        if not isinstance(data, dict):
            raise HTTPException(400, "ожидался объект с настройками")
        backups = runs_dir(base_config()) / "config-backups"
        try:
            cfg = await asyncio.to_thread(save, config_path, data, backups=backups)
        except SettingsError as e:
            raise HTTPException(400, str(e)) from e
        warnings = []
        models = await catalog.get()
        if models:
            for model, near in unknown_models(cfg, {m["id"] for m in models}).items():
                warnings.append(f"модели «{model}» нет на OpenRouter" + (f" — похожие: {', '.join(near)}" if near else ""))
            if cfg.fact_check.enabled and cfg.fact_check.model not in {m["id"] for m in models}:
                warnings.append(f"модели проверки фактов «{cfg.fact_check.model}» нет на OpenRouter")
        return {"ok": True, "settings": editable(cfg), "warnings": warnings}

    @app.get("/api/settings/backups")
    async def list_backups() -> dict[str, Any]:
        d = runs_dir(base_config()) / "config-backups"
        items = sorted(d.glob("config-*.yaml"), reverse=True) if d.is_dir() else []
        return {"items": [p.name for p in items]}

    @app.post("/api/settings/restore/{name}")
    async def restore_backup(name: str) -> dict[str, Any]:
        if not re.fullmatch(r"config-\d{8}-\d{6}\.yaml", name):
            raise HTTPException(400, "неверное имя копии")
        src = runs_dir(base_config()) / "config-backups" / name
        if not src.is_file():
            raise HTTPException(404, "копия не найдена")
        import yaml
        from ..settings import validate

        def restore() -> None:
            text = src.read_text(encoding="utf-8")
            data = yaml.safe_load(text) or {}
            validate(data, CouncilConfig.model_validate(data))
            current = config_path.read_text(encoding="utf-8")
            (src.parent / f"config-{time.strftime('%Y%m%d-%H%M%S')}.yaml").write_text(current, encoding="utf-8")
            with open(config_path, "w", encoding="utf-8") as f:  # поверх: файл подключён в контейнер отдельным томом
                f.write(text)

        try:
            await asyncio.to_thread(restore)
        except (SettingsError, ValueError) as e:
            raise HTTPException(400, f"копия не подходит: {e}") from e
        except OSError as e:
            raise HTTPException(500, f"не удалось записать config.yaml: {e}") from e
        return {"ok": True}

    # ------------------------------------------------------------ модели

    @app.get("/api/models")
    async def list_models(q: str = "", limit: int = 400) -> dict[str, Any]:
        models = await catalog.get()
        if models is None:
            return {"available": False, "items": []}
        q = q.lower().strip()
        items = [_with_tags(_model_row(m)) for m in sort_newest(models) if is_text_model(m)
                 and (not q or q in m["id"].lower() or q in (m.get("name") or "").lower())]
        return {"available": True, "items": items[:max(1, min(limit, 2000))]}

    @app.get("/api/models/family")
    async def model_family(model: str) -> dict[str, Any]:
        """Все доступные сейчас модели того же разработчика, что и model (deepseek/…, anthropic/…), по линейкам,
        новые сверху: чтобы выбрать любую — Pro вместо Flash, старшую или младшую версию."""
        models = await catalog.get()
        if models is None:
            return {"available": False, "groups": []}
        prov = provider(model)
        from ..model_notes import load_cache
        cache = load_cache(runs_dir(base_config()))
        lang = base_config().language
        groups: dict[str, list[dict[str, Any]]] = {}
        for m in sort_newest(models):
            if is_text_model(m) and provider(m["id"]) == prov:
                groups.setdefault(family(m["id"]), []).append(_tr(_with_tags(_model_row(m)), cache, lang))
        cur_fam = family(model)
        order = sorted(groups, key=lambda f: (f != cur_fam, -max(created_of(models, r["id"]) for r in groups[f]), f))
        return {"available": True, "provider": prov, "current": model,
                "groups": [{"family": f.split("/", 1)[-1], "items": groups[f]} for f in order]}

    history_cache: dict[str, Any] = {}

    def model_history() -> dict[str, dict[str, Any]]:
        """Ваша статистика по моделям; пересчитывается, только когда в архиве что-то изменилось."""
        from ..archive import Archive
        from ..model_notes import history
        arc = Archive(runs_dir(base_config()))
        stems = arc.stems()
        key = f"{len(stems)}:{max((arc.path(s, 'json').stat().st_mtime for s in stems), default=0)}"
        if history_cache.get("key") != key:
            sessions = []
            for s in stems:
                try:
                    sessions.append(arc.load(s))
                except (KeyError, OSError, ValueError):
                    continue
            history_cache.update(key=key, data=history(sessions))
        return history_cache["data"]

    @app.get("/api/models/card")
    async def model_card(model: str) -> dict[str, Any]:
        """Всё о модели для выбора: метки, роли в совете, ваша история, описание разработчика и пересказ."""
        from ..model_notes import history_for, load_cache, roles, tags, translate_key
        models = await catalog.get()
        info = next((m for m in models or [] if m.get("id") == model), None)
        row = _model_row(info) if info else {"id": model}
        hist = history_for(await asyncio.to_thread(model_history), model)
        return {"available": models is not None, "found": info is not None, "row": row,
                "tags": tags(row) if info else [], "roles": roles(row) if info else [],
                "history": hist, "description": (info or {}).get("description") or "",
                "description_tr": load_cache(runs_dir(base_config())).get(
                    translate_key(base_config().language, model, True), ""),
                "summary": load_cache(runs_dir(base_config())).get(model, "")}

    @app.post("/api/models/card/summary")
    async def model_summary(body: _SummaryReq) -> dict[str, Any]:
        """Кратко по-русски по описанию разработчика (дешёвая модель, доли цента; запоминается)."""
        from ..client import OpenRouterClient, ProviderError
        from ..model_notes import load_cache, save_summary, summary_prompt
        cfg = base_config()
        cached = load_cache(runs_dir(cfg)).get(body.model)
        if cached:
            return {"summary": cached, "cached": True}
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise HTTPException(400, "нет OPENROUTER_API_KEY — пересказ недоступен в демо-режиме")
        models = await catalog.get()
        info = next((m for m in models or [] if m.get("id") == body.model), None)
        if not info or not info.get("description"):
            raise HTTPException(404, "у этой модели нет описания в каталоге OpenRouter")
        client = OpenRouterClient(key, timeout=60, max_retries=1)
        try:
            comp = await client.complete(cfg.transcribe_model, [{"role": "user", "content": summary_prompt(
                _model_row(info), info["description"])}], temperature=0.2, max_tokens=700)
        except ProviderError as e:
            raise HTTPException(502, f"не удалось получить пересказ: {e}") from e
        finally:
            await client.aclose()
        text = comp.text.strip()
        await asyncio.to_thread(save_summary, runs_dir(cfg), body.model, text)
        return {"summary": text, "cached": False, "cost": comp.cost}

    def _tr(row: dict[str, Any], cache: dict[str, str], language: str) -> dict[str, Any]:
        """Подставляет переведённое описание из кэша (если уже переводили)."""
        from ..model_notes import translate_key
        t = cache.get(translate_key(language, row["id"], False))
        return {**row, "desc_tr": t} if t else row

    @app.post("/api/models/translate")
    async def translate_models(body: _TranslateReq) -> dict[str, Any]:
        """Перевод описаний моделей на язык приложения (дешёвая модель, пакетом; переводы запоминаются)."""
        from ..client import OpenRouterClient, ProviderError
        from ..model_notes import load_cache, save_many, translate_key, translate_prompt
        from ..voting import extract_json
        cfg = base_config()
        lang = cfg.language
        cache = load_cache(runs_dir(cfg))
        out = {i: cache[translate_key(lang, i, body.full)] for i in body.ids if translate_key(lang, i, body.full) in cache}
        need = [i for i in body.ids[:40] if i not in out]
        if not need:
            return {"translations": out}
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            return {"translations": out, "unavailable": "нет OPENROUTER_API_KEY — описания остаются на языке разработчика"}
        models = {m["id"]: m for m in (await catalog.get() or [])}
        texts = {}
        for i in need:
            d = (models.get(i) or {}).get("description") or ""
            if d:
                texts[i] = d[:4000] if body.full else _short(d)
        if not texts:
            return {"translations": out}
        client = OpenRouterClient(key, timeout=90, max_retries=1)
        try:
            comp = await client.complete(cfg.transcribe_model, [{"role": "user", "content": translate_prompt(texts, lang)}],
                                         temperature=0.1, max_tokens=6000 if body.full else 4000)
            got = extract_json(comp.text)
        except (ProviderError, ValueError) as e:
            return {"translations": out, "unavailable": f"перевод не удался: {str(e)[:200]}"}
        finally:
            await client.aclose()
        new = {i: str(t).strip() for i, t in (got or {}).items() if i in texts and str(t).strip()}
        await asyncio.to_thread(save_many, runs_dir(cfg), {translate_key(lang, i, body.full): t for i, t in new.items()})
        return {"translations": {**out, **new}, "cost": comp.cost}

    @app.post("/api/advise")
    async def advise(body: _AdviseReq) -> dict[str, Any]:
        """«Подобрать состав»: какие модели, роли, режим и поиск лучше для этого вопроса (см. council/advisor.py)."""
        from .. import advisor as A
        from ..client import OpenRouterClient, ProviderError
        from ..config import RESEARCH_TUNABLE
        from ..voting import extract_json
        if len(body.question.strip()) < 3:
            raise HTTPException(400, "сначала напишите вопрос")
        cfg = base_config()
        models = await catalog.get()
        if models is None:
            raise HTTPException(503, "каталог OpenRouter недоступен — подбор невозможен")
        cands = A.candidates(cfg, models)
        hist_all = await asyncio.to_thread(model_history)
        from ..model_notes import history_for
        lim = cfg.advisor
        # история — довод советника, только если заседаний с моделью достаточно (иначе это шум)
        hist = {c["id"]: h for c in cands if (h := history_for(hist_all, c["id"])) and h.get("sessions", 0) >= lim.history_min_sessions}
        rdir = runs_dir(cfg)
        key = os.environ.get("OPENROUTER_API_KEY")
        cost, info = 0.0, {"dossiers_built": 0, "dossiers_cached": 0, "source": "rules"}

        def finish(adv: A.Advice) -> dict[str, Any]:
            adv = A.finalize(adv, cands, cfg, body.mode)
            plan = A.assign(adv, cfg)
            by_id = {c["id"]: c for c in cands}
            return {"advice": adv.model_dump(), "plan": plan, "cost": round(cost, 4), **info,
                    "facts": {p.model: by_id.get(p.model) for p in adv.picks},
                    "dossiers": {i: d for i, d in A.load_dossiers(rdir).items() if i in {p.model for p in adv.picks}}}

        if body.mock or not key:
            adv = A.rule_advice(body.question, body.mode, cfg, cands, hist, body.attachments)
            if not key and not body.mock:
                adv.caveats.append("Нет ключа OpenRouter — без анализа вопроса моделью и без обзоров моделей.")
            return finish(adv)

        client = OpenRouterClient(key, timeout=150, max_retries=1)
        try:
            dossiers = await asyncio.to_thread(A.load_dossiers, rdir)
            ids = [c["id"] for c in cands]
            need = ids if body.refresh else A.stale(dossiers, ids, cfg.advisor.dossier_days)
            info["dossiers_cached"] = len(ids) - len(need)
            notes: list[str] = []
            if cfg.advisor.dossier and need:  # досье: обзоры и известные проблемы моделей — поиском, пачками
                today = time.strftime("%Y-%m-%d")

                async def one(batch: list[str]) -> tuple[dict[str, Any], float, str]:
                    try:
                        comp = await client.complete(cfg.fact_check.model, [{"role": "user", "content": A.dossier_prompt(batch, today)}],
                                                     temperature=0.1, max_tokens=5000, web_search=True)
                        ds = A.DossierSet.model_validate(extract_json(comp.text))
                        return A.clean_dossiers(ds, batch, comp.citations, time.time()), comp.cost, ""
                    except (ProviderError, ValueError) as e:
                        return {}, float(getattr(getattr(e, "spent", None), "cost", 0) or 0), str(e)[:160]

                batches = [need[i:i + A.DOSSIER_BATCH] for i in range(0, len(need), A.DOSSIER_BATCH)]
                for got, c, err in await asyncio.gather(*(one(b) for b in batches)):
                    cost += c
                    dossiers.update(got)
                    info["dossiers_built"] += len(got)
                    if err:
                        notes.append(f"Часть обзоров моделей не собрана: {err}")
                await asyncio.to_thread(A.save_dossiers, rdir, {i: dossiers[i] for i in need if i in dossiers})
            current = {"members": body.members, "chair": body.chair, "devil_advocate": body.devil_advocate,
                       "models": {m.name: m.model for m in cfg.members if not body.members or m.name in body.members}}
            prompt = A.advise_prompt(question=body.question, mode=body.mode, attachments=body.attachments,
                                     followup=body.followup, budget=body.budget or cfg.max_cost_usd, current=current,
                                     cands=cands, history=hist, dossiers=dossiers, language=cfg.language,
                                     research_words=cfg.research.max_words, search_model=cfg.fact_check.model,
                                     research_now={k: v for k, v in cfg.research.model_dump().items()
                                                   if k in RESEARCH_TUNABLE},
                                     question_chars=lim.question_chars, max_claims_cap=lim.max_arbitration_claims,
                                     max_words_cap=lim.max_words_cap, history_min=lim.history_min_sessions)
            model = cfg.advisor.model or next((m.model for m in cfg.members if m.name == cfg.chair), cfg.members[0].model)
            messages = [{"role": "user", "content": prompt}]
            adv = None
            for attempt in range(2):
                try:
                    comp = await client.complete(model, messages, temperature=0.2, max_tokens=8000, reasoning_effort="medium")
                except ProviderError as e:
                    notes.append(f"Советник ({model}) не ответил: {str(e)[:200]}")
                    break
                cost += comp.cost
                try:
                    adv = A.Advice.model_validate(extract_json(comp.text))
                    break
                except ValueError as e:
                    messages = [*messages, {"role": "assistant", "content": comp.text},
                                {"role": "user", "content": f"Your reply is not valid JSON of the requested format ({str(e)[:200]}). "
                                                            "Reply with the JSON object only."}]
            if adv is None:
                adv = A.rule_advice(body.question, body.mode, cfg, cands, hist, body.attachments)
            else:
                info["source"] = "advisor"
                info["advisor_model"] = model
            adv.caveats.extend(notes)
            return finish(adv)
        finally:
            await client.aclose()

    @app.get("/api/models/upgrades")
    async def model_upgrades() -> dict[str, Any]:
        models = await catalog.get()
        if models is None:
            return {"available": False, "items": []}
        out = []
        for u in upgrades(base_config(), models):
            out.append({"name": u["name"], "enabled": u["enabled"], "model": u["model"], "found": u["found"],
                        "current": _model_row(u["current"]) if u["current"] else None,
                        "same_family": [_model_row(m) for m in u["same_family"]],
                        "newest": [_model_row(m) for m in u["newest"]]})
        return {"available": True, "items": out}

    # ------------------------------------------------------------ логотип

    @app.post("/api/branding/logo")
    async def upload_logo(request: Request) -> dict[str, Any]:
        data = await request.body()
        if not data or len(data) > LOGO_MAX:
            raise HTTPException(400, "логотип: PNG или JPG до 2 МБ")
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            img.thumbnail((800, 800))
            out = io.BytesIO()
            img.save(out, "PNG")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, "не удалось прочитать картинку (нужен PNG или JPG)") from e
        try:
            branding_dir.mkdir(exist_ok=True)
            (branding_dir / "logo.png").write_bytes(out.getvalue())
        except OSError as e:
            raise HTTPException(500, "нет прав на запись в папку branding. На сервере один раз выполните: "
                                     "sudo bash /opt/ai-council/ai-council/deploy/web-control.sh install") from e
        cfg = base_config()
        try:
            await asyncio.to_thread(save, config_path, {"branding": {**cfg.branding.model_dump(), "logo": "branding/logo.png"}},
                                    backups=runs_dir(cfg) / "config-backups")
        except SettingsError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "logo": "branding/logo.png"}

    @app.get("/api/branding/logo")
    async def get_logo():
        from fastapi.responses import FileResponse
        cfg = base_config()
        p = (config_path.parent / cfg.branding.logo) if cfg.branding.logo else None
        if not p or not p.is_file() or branding_dir.resolve() not in p.resolve().parents:
            raise HTTPException(404, "логотипа нет")
        return FileResponse(p)

    # ------------------------------------------------------------ версия и обновление

    def read_json(name: str) -> dict[str, Any] | None:
        try:
            return json.loads((control / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def log_tail(n: int = 40) -> str:
        try:
            return "\n".join((control / "update.log").read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
        except OSError:
            return ""

    @app.get("/api/system")
    async def system() -> dict[str, Any]:
        return {"version": read_json("version.json"), "status": read_json("status.json"), "log": log_tail(),
                "control": writable(control), "pending": (control / "request").exists(),
                "server_time": time.time()}

    @app.get("/api/system/check")
    async def check_updates(force: bool = False) -> dict[str, Any]:
        ver = read_json("version.json") or {}
        repo, branch, commit = _repo_of(ver.get("remote", "")), ver.get("branch"), ver.get("commit")
        if not (repo and branch and commit and commit != "unknown"):
            raise HTTPException(409, "версия программы неизвестна — на сервере один раз выполните: "
                                     "sudo bash /opt/ai-council/ai-council/deploy/web-control.sh install")
        key = f"{repo}@{branch}@{commit}"
        if not force and check_cache.get("key") == key and time.time() - check_cache.get("at", 0) < CHECK_TTL:
            return check_cache["data"]
        url = f"{GITHUB_API}/repos/{repo}/compare/{commit}...{branch}"
        try:
            async with httpx.AsyncClient(timeout=15, headers={"Accept": "application/vnd.github+json"}) as c:
                r = await c.get(url)
        except httpx.HTTPError as e:
            raise HTTPException(502, f"нет связи с GitHub: {e.__class__.__name__}") from e
        if r.status_code == 404:
            raise HTTPException(502, "GitHub не нашёл установленную версию или ветку — возможно, ветку переименовали. "
                                     "Обновите программу один раз из терминала: ai-council update")
        if r.status_code in (403, 429):
            raise HTTPException(502, "GitHub временно ограничил запросы — попробуйте через несколько минут")
        if r.status_code != 200:
            raise HTTPException(502, f"GitHub ответил {r.status_code}")
        d = r.json()
        commits = [{"sha": c.get("sha", "")[:7], "date": ((c.get("commit") or {}).get("committer") or {}).get("date", ""),
                    "title": ((c.get("commit") or {}).get("message") or "").splitlines()[0][:200]}
                   for c in d.get("commits") or []]
        data = {"behind": int(d.get("ahead_by") or 0), "commits": list(reversed(commits))[:30],
                "status": d.get("status", ""), "checked_at": time.time()}
        check_cache.update(key=key, at=time.time(), data=data)
        return data

    def request_action(action: str) -> dict[str, Any]:
        if not writable(control):
            raise HTTPException(409, "обновление из веб-интерфейса ещё не включено — на сервере один раз выполните: "
                                     "sudo bash /opt/ai-council/ai-council/deploy/web-control.sh install")
        st = read_json("status.json") or {}
        if (control / "request").exists() or st.get("state") == "running":
            raise HTTPException(409, "предыдущая операция ещё выполняется")
        (control / "request").write_text(action + "\n", encoding="utf-8")
        check_cache.clear()
        return {"ok": True, "requested": action}

    @app.post("/api/system/update")
    async def update() -> dict[str, Any]:
        return request_action("update")

    @app.post("/api/system/restart")
    async def restart() -> dict[str, Any]:
        return request_action("restart")
