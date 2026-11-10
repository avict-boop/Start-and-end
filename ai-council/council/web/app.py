"""Веб-интерфейс совета: FastAPI + поток событий заседания по SSE.

Запуск: `python -m council web` и открыть http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..client import OpenRouterClient
from ..config import CouncilConfig, Mode, OverrideError, load_config, with_overrides
from ..estimate import ModelCatalog, estimate, parse_pricing, unknown_models
from ..mock import MockClient
from ..report import make_stem, save
from ..session import Council, CouncilError
from .auth import BasicAuth

log = logging.getLogger("council.web")
STATIC = Path(__file__).parent / "static"
ID_RE = re.compile(r"^[\w\-]{1,120}$", re.U)
MODELS_TTL = 600
KEEP_FINISHED = 20


# ---------------------------------------------------------------- helpers


def jsonable(obj: Any) -> Any:
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj


def events_from_result(d: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    """Восстанавливает ленту событий из сохранённого заседания (для истории)."""
    ev: list[dict[str, Any]] = [
        {"type": "start", "id": session_id, "question": d["question"], "mode": d["mode"],
         "members": list(d.get("stats", {})), "rounds": None},
        {"type": "framing", "refined_question": d.get("refined_question", d["question"]), "criteria": d["criteria"]},
    ]
    if d["mode"] != "evaluate":
        ev.append({"type": "stage", "stage": "proposal", "title": "Независимые предложения"})
    ev.append({"type": "proposals", "proposals": d["initial_proposals"], "authors": d["authors"]})
    if d.get("critiques"):
        ev.append({"type": "stage", "stage": "critique", "title": "Критика"})
        ev += [{"type": "member_done", "stage": "critique", "member": m, "result": cs}
               for m, cs in d["critiques"].items()]
    revised = {l: p for l, p in d["proposals"].items() if p != d["initial_proposals"].get(l)}
    if revised:
        ev.append({"type": "stage", "stage": "revision", "title": "Доработка"})
        ev.append({"type": "revisions", "revised": revised})
    ev.append({"type": "stage", "stage": "vote", "title": "Голосование"})
    ev += [{"type": "member_done", "stage": "vote", "member": m, "result": b} for m, b in d["ballots"].items()]
    ev.append({"type": "tally", "rows": d["tally"], "authors": d["authors"]})
    ev.append({"type": "stage", "stage": "verdict", "title": "Итоговое решение"})
    for name, s in d.get("stats", {}).items():
        for f in s.get("failures", []):
            stage, _, err = f.partition(": ")
            ev.append({"type": "member_failed", "stage": stage, "member": name, "error": err})
    ev.append({"type": "verdict", "verdict": d["verdict"]})
    ev.append({"type": "done", "id": session_id, "total_cost": d.get("total_cost_usd", 0),
               "duration_s": d.get("duration_s"), "stats": d.get("stats", {})})
    return ev


class RunRequest(BaseModel):
    question: str = Field(min_length=3, max_length=20000)
    mode: Mode = "debate"
    rounds: int | None = Field(default=None, ge=0, le=5)
    criteria: list[str] | None = None
    options: list[str] | None = None
    context: str | None = Field(default=None, max_length=50000)
    members: list[str] | None = None
    chair: str | None = None
    devil_advocate: str | None = None  # "" — без адвоката дьявола
    budget: float | None = Field(default=None, gt=0, le=100)
    mock: bool = False

    @property
    def clean_options(self) -> list[str] | None:
        if self.mode != "evaluate":
            return None
        return [o.strip() for o in (self.options or []) if o.strip()]

    @property
    def clean_criteria(self) -> list[str] | None:
        c = [x.strip() for x in (self.criteria or []) if x.strip()]
        return c or None


class RunState:
    def __init__(self, sid: str, question: str) -> None:
        self.id = sid
        self.question = question
        self.events: list[dict[str, Any]] = []
        self.status = "running"
        self.task: asyncio.Task | None = None
        self.started = time.time()
        self._changed = asyncio.Event()

    def push(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        self._changed.set()
        self._changed = asyncio.Event()

    async def wait(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._changed.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False


# ---------------------------------------------------------------- app


def create_app(config_path: str | Path, *, mock_delay: float = 0.6, password: str | None = None) -> FastAPI:
    config_path = Path(config_path)
    app = FastAPI(title="Совет нейросетей", docs_url="/api/docs")
    runs: dict[str, RunState] = {}
    catalog = ModelCatalog(MODELS_TTL)

    def base_config() -> CouncilConfig:
        try:
            return load_config(config_path)
        except (OSError, ValueError) as e:
            raise HTTPException(500, f"ошибка в {config_path.name}: {e}") from e

    def runs_dir(cfg: CouncilConfig) -> Path:
        d = Path(cfg.runs_dir)
        return d if d.is_absolute() else config_path.parent / d

    def build_config(req: RunRequest) -> CouncilConfig:
        try:
            return with_overrides(base_config(), members=req.members, chair=req.chair,
                                  devil_advocate=req.devil_advocate, budget=req.budget)
        except OverrideError as e:
            raise HTTPException(400, str(e)) from e

    def find_file(sid: str, ext: str) -> Path:
        if not ID_RE.match(sid):
            raise HTTPException(404, "заседание не найдено")
        path = runs_dir(base_config()) / f"{sid}.{ext}"
        if not path.is_file():
            raise HTTPException(404, "заседание не найдено")
        return path

    # ------------------------------------------------------------ API

    @app.get("/api/config")
    async def get_config() -> dict[str, Any]:
        cfg = base_config()
        return {
            "members": [{"name": m.name, "model": m.model, "enabled": m.enabled} for m in cfg.members],
            "chair": cfg.chair,
            "devil_advocate": cfg.devil_advocate,
            "mode": cfg.mode,
            "rounds": cfg.debate_rounds,
            "budget": cfg.max_cost_usd,
            "language": cfg.language,
            "has_key": bool(os.environ.get("OPENROUTER_API_KEY")),
        }

    @app.post("/api/estimate")
    async def post_estimate(req: RunRequest) -> dict[str, Any]:
        cfg = build_config(req)
        warning = None
        pricing: dict = {}
        missing: dict[str, list[str]] = {}
        if not req.mock:
            models = await catalog.get()
            if models is None:
                warning = "Не удалось получить список моделей OpenRouter — проверка и цены недоступны."
            else:
                pricing = parse_pricing(models)
                missing = unknown_models(cfg, set(pricing))
        opts = req.clean_options
        rounds = cfg.debate_rounds if req.rounds is None else req.rounds
        est = estimate(cfg, pricing, mode="evaluate" if opts else req.mode, rounds=rounds,
                       n_options=len(opts) if opts else None, has_criteria=bool(req.clean_criteria))
        return {
            "members": [dataclasses.asdict(m) for m in est.members],
            "calls": est.calls,
            "low": est.low if pricing else None,
            "high": est.high if pricing else None,
            "budget": cfg.max_cost_usd,
            "unknown": missing,
            "warning": warning,
        }

    @app.post("/api/sessions")
    async def start_session(req: RunRequest) -> dict[str, Any]:
        cfg = build_config(req)
        opts = req.clean_options
        if req.mode == "evaluate" and (not opts or len(opts) < 2):
            raise HTTPException(400, "для оценки нужно минимум 2 варианта")
        if req.mock:
            client = MockClient(delay=mock_delay)
        else:
            key = os.environ.get("OPENROUTER_API_KEY")
            if not key:
                raise HTTPException(400, "нет OPENROUTER_API_KEY: добавьте ключ в .env и перезапустите сервер "
                                         "или включите демо-режим")
            client = OpenRouterClient(key, timeout=cfg.timeout_s, max_retries=cfg.max_retries,
                            max_tokens_cap=cfg.max_tokens_cap)
        sid = make_stem(req.question)
        while sid in runs or (runs_dir(cfg) / f"{sid}.json").exists():
            sid += "-1"
        st = RunState(sid, req.question)
        rounds = cfg.debate_rounds if req.rounds is None else req.rounds
        st.push({"type": "start", "id": sid, "question": req.question, "mode": "evaluate" if opts else req.mode,
                 "rounds": rounds, "members": [m.name for m in cfg.active_members], "chair": cfg.chair,
                 "budget": cfg.max_cost_usd, "mock": req.mock})

        def on_event(event: str, **data: Any) -> None:
            st.push({"type": event, **jsonable(data)})

        async def execute() -> None:
            try:
                council = Council(cfg, client, on_event=on_event)
                res = await council.run(req.question, criteria=req.clean_criteria, options=opts,
                                        context=req.context or None, mode=req.mode, rounds=rounds)
                save(res, runs_dir(cfg), stem=sid)
                st.status = "done"
                st.push({"type": "done", "id": sid, "total_cost": res.total_cost, "duration_s": res.duration_s,
                         "stats": jsonable(res.stats)})
            except asyncio.CancelledError:
                st.status = "cancelled"
                st.push({"type": "error", "message": "Заседание остановлено пользователем"})
            except CouncilError as e:
                st.status = "error"
                st.push({"type": "error", "message": str(e)})
            except Exception as e:  # noqa: BLE001
                log.exception("заседание %s упало", sid)
                st.status = "error"
                st.push({"type": "error", "message": f"внутренняя ошибка: {e!r}"})
            finally:
                await client.aclose()

        finished = [r for r in runs.values() if r.status != "running"]
        for old in sorted(finished, key=lambda r: r.started)[:-KEEP_FINISHED or None]:
            runs.pop(old.id, None)
        runs[sid] = st
        st.task = asyncio.create_task(execute())
        return {"id": sid}

    @app.get("/api/sessions")
    async def list_sessions() -> list[dict[str, Any]]:
        items: dict[str, dict[str, Any]] = {}
        d = runs_dir(base_config())
        for path in sorted(d.glob("*.json"), reverse=True)[:100] if d.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                v = data["verdict"]
                items[path.stem] = {
                    "id": path.stem, "question": data["question"], "mode": data["mode"],
                    "started_at": data.get("started_at"), "status": "done",
                    "winner": data["proposals"].get(v["winner"], {}).get("title"),
                    "cost": data.get("total_cost_usd"),
                }
            except (OSError, ValueError, KeyError, AttributeError):
                continue
        for st in runs.values():
            if st.status != "done" or st.id not in items:
                items[st.id] = {"id": st.id, "question": st.question, "status": st.status,
                                "started_at": None, "winner": None, "cost": None, "mode": None}
        return sorted(items.values(), key=lambda x: x["id"], reverse=True)

    @app.get("/api/sessions/{sid}/events")
    async def stream(sid: str, request: Request) -> StreamingResponse:
        st = runs.get(sid)
        if st is None:
            data = json.loads(find_file(sid, "json").read_text(encoding="utf-8"))
            static_events = events_from_result(data, sid)
        try:
            start = int(request.headers.get("last-event-id", "-1")) + 1
        except ValueError:
            start = 0

        def fmt(i: int, ev: dict[str, Any]) -> str:
            return f"id: {i}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

        async def gen():
            if st is None:
                for i, ev in enumerate(static_events):
                    if i >= start:
                        yield fmt(i, ev)
                return
            i = start
            while True:
                while i < len(st.events):
                    yield fmt(i, st.events[i])
                    i += 1
                if st.status != "running":
                    return
                if await request.is_disconnected():
                    return
                if not await st.wait(15):
                    yield ": ping\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/sessions/{sid}/cancel")
    async def cancel(sid: str) -> dict[str, Any]:
        st = runs.get(sid)
        if st is None or st.status != "running" or st.task is None:
            raise HTTPException(409, "заседание не идёт")
        st.task.cancel()
        return {"ok": True}

    @app.get("/api/sessions/{sid}/report")
    async def report(sid: str) -> FileResponse:
        path = find_file(sid, "md")
        return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=path.name)

    @app.delete("/api/sessions/{sid}")
    async def delete(sid: str) -> dict[str, Any]:
        st = runs.get(sid)
        if st and st.status == "running":
            raise HTTPException(409, "сначала остановите заседание")
        runs.pop(sid, None)
        removed = 0
        for ext in ("json", "md"):
            try:
                find_file(sid, ext).unlink()
                removed += 1
            except HTTPException:
                pass
        if not removed and st is None:
            raise HTTPException(404, "заседание не найдено")
        return {"ok": True}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    password = os.environ.get("WEB_PASSWORD", "") if password is None else password
    if password:
        user = os.environ.get("WEB_USER", "").strip() or "admin"
        trust = os.environ.get("TRUST_PROXY", "") == "1"
        return BasicAuth(app, user=user, password=password, trust_proxy=trust)  # type: ignore[return-value]
    return app
