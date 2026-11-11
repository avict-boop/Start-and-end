"""Веб-интерфейс совета: FastAPI + поток событий заседания по SSE.

Запуск: `python -m council web` и открыть http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import dataclasses
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..client import OpenRouterClient, ProviderError
from ..config import CouncilConfig, Mode, OverrideError, load_config, with_overrides
from ..estimate import ModelCatalog, estimate, learn_lengths, parse_pricing, similar_actual, unknown_models, vision_map
from ..mock import MockClient
from ..archive import MIME, Archive, available_formats, download_name, report_kind
from ..attachments import (ACCEPT, MAX_FILE_BYTES, MAX_FILES, AttachmentError, assign_files, human_size,
                           limit_total, load_attachment, merge, safe_name)
from ..report import make_stem, save
from ..reports.brand import set_brand
from ..checkpoint import Checkpoint, Journal, classify
from ..session import Council, CouncilError
from .auth import BasicAuth

log = logging.getLogger("council.web")
from ..diagnostics import bundle, list_failed, save_events, save_failed, setup_logging, stamp  # noqa: E402
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
         "members": list(d.get("stats", {})), "rounds": None,
         "parent": ({"id": d["followup_of"], "title": (d.get("thread") or [{}])[-1].get("title", "")}
                    if d.get("followup_of") else None)},
        {"type": "framing", "refined_question": d.get("refined_question", d["question"]), "criteria": d["criteria"],
         "requirements": d.get("requirements") or []},
    ]
    if d.get("attachments"):
        ev.insert(1, {"type": "attachments", "items": d["attachments"], "blind": []})
        if d.get("image_notes"):
            ev.insert(2, {"type": "image_notes", "by": d.get("image_notes_by", ""), "notes": d["image_notes"]})
    if d["mode"] != "evaluate":
        ev.append({"type": "stage", "stage": "proposal", "title": "Независимые предложения",
                   "chair_proposes": d.get("chair_proposed", True)})
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
    from ..voting import agreement
    agree = d.get("agreement") or agreement({m: b.get("ranking", []) for m, b in (d.get("ballots") or {}).items()})
    ev.append({"type": "tally", "rows": d["tally"], "authors": d["authors"], "agreement": agree,
               "ballot_flags": d.get("ballot_flags") or {}, "leader_without_flagged": d.get("leader_without_flagged"),
               "co_leaders": d.get("co_leaders") or [], "decisive": d.get("decisive") or {},
               "coverage": d.get("coverage")})
    if d.get("fact_check"):
        ev.append({"type": "stage", "stage": "factcheck", "title": "Проверка фактов"})
        ev.append({"type": "factcheck", "results": d["fact_check"], "model": d.get("fact_checker", "")})
    ev.append({"type": "stage", "stage": "verdict", "title": "Итоговое решение"})
    for name, s in d.get("stats", {}).items():
        for f in s.get("failures", []):
            stage, _, err = f.partition(": ")
            ev.append({"type": "member_failed", "stage": stage, "member": name, "error": err})
    ev.append({"type": "verdict", "verdict": d["verdict"], "ideas": d.get("ideas") or []})
    ev.append({"type": "done", "id": session_id, "total_cost": d.get("total_cost_usd", 0),
               "duration_s": d.get("duration_s"), "stats": d.get("stats", {})})
    return ev


class RunRequest(BaseModel):
    question: str = Field(min_length=3)  # без ограничения длины
    mode: Mode = "debate"
    rounds: int | None = Field(default=None, ge=0, le=5)
    criteria: list[str] | None = None
    options: list[str] | None = None
    context: str | None = None
    members: list[str] | None = None
    chair: str | None = None
    devil_advocate: str | None = None  # "" — без адвоката дьявола
    chair_proposes: bool | None = None  # None — как в config.yaml
    fact_check: bool | None = None      # проверка фактов лидеров; None — как в config.yaml
    compact: bool | None = None         # компактные варианты; None — как в config.yaml
    budget: float | None = Field(default=None, gt=0, le=100)
    mock: bool = False
    attachments: list[str] = Field(default_factory=list, max_length=MAX_FILES)  # id загрузок
    parent: str | None = None  # уточнение к этому заседанию

    @property
    def clean_options(self) -> list[str] | None:
        if self.mode != "evaluate":
            return None
        return [o.strip() for o in (self.options or []) if o.strip()]

    @property
    def clean_criteria(self) -> list[str] | None:
        c = [x.strip() for x in (self.criteria or []) if x.strip()]
        return c or None


class BudgetRequest(BaseModel):
    budget: float = Field(gt=0, le=1000)


class ResumeRequest(BaseModel):
    budget: float | None = Field(default=None, gt=0, le=1000)


class RunState:
    def __init__(self, sid: str, question: str, mode: str = "") -> None:
        self.id = sid
        self.question = question
        self.mode = mode
        self.events: list[dict[str, Any]] = []
        self.status = "running"
        self.task: asyncio.Task | None = None
        self.started = time.time()
        self.council: Any = None  # идущее заседание — чтобы поднять бюджет на ходу
        self.checkpoint: Checkpoint | None = None
        self._changed = asyncio.Event()

    def push(self, event: dict[str, Any]) -> None:
        self.events.append(stamp(event))
        self._changed.set()
        self._changed = asyncio.Event()

    def finish(self, status: str) -> None:
        """Отметить заседание завершённым и разбудить тех, кто ждёт событий."""
        self.status = status
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
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            setup_logging(runs_dir(base_config()), "web")
        except HTTPException:
            pass
        log.info("веб-интерфейс запущен")
        # отчёты PDF/DOCX/MD для заседаний, проведённых до обновления программы
        archive().ensure_all_in_background()
        cleanup_uploads()
        yield

    app = FastAPI(title="Совет нейросетей", docs_url="/api/docs", lifespan=lifespan)
    runs: dict[str, RunState] = {}
    catalog = ModelCatalog(MODELS_TTL)

    def base_config() -> CouncilConfig:
        try:
            cfg = load_config(config_path)
        except (OSError, ValueError) as e:
            raise HTTPException(500, f"ошибка в {config_path.name}: {e}") from e
        set_brand(cfg.branding, config_path.parent)  # правки оформления подхватываются без перезапуска
        return cfg

    def runs_dir(cfg: CouncilConfig) -> Path:
        d = Path(cfg.runs_dir)
        return d if d.is_absolute() else config_path.parent / d

    def build_config(req: RunRequest) -> CouncilConfig:
        try:
            return with_overrides(base_config(), members=req.members, chair=req.chair,
                                  devil_advocate=req.devil_advocate, budget=req.budget,
                                  chair_proposes=req.chair_proposes, fact_check=req.fact_check,
                                  compact=req.compact)
        except OverrideError as e:
            raise HTTPException(400, str(e)) from e

    from .admin import register as register_admin
    register_admin(app, config_path, base_config, runs_dir, catalog)

    archives: dict[Path, Archive] = {}

    @app.post("/api/reports/rebuild")
    async def rebuild_reports() -> dict[str, Any]:
        """Пересобрать отчёты всего архива (например, после смены оформления) — в фоне."""
        arch = archive()
        arch.ensure_all_in_background(force=True)
        return {"ok": True, "sessions": len(arch.stems())}

    def archive() -> Archive:
        d = runs_dir(base_config())
        if d not in archives:
            archives[d] = Archive(d, isolate=True)
        return archives[d]

    UPLOAD_ID = re.compile(r"^[0-9a-f]{16}$")

    def uploads_dir() -> Path:
        return runs_dir(base_config()) / ".uploads"

    def cleanup_uploads(max_age: float = 24 * 3600) -> None:
        """Черновые загрузки, к которым так и не запустили заседание, удаляются через сутки."""
        import shutil
        d = uploads_dir()
        if not d.is_dir():
            return
        for p in d.iterdir():
            try:
                if time.time() - p.stat().st_mtime > max_age:
                    shutil.rmtree(p, ignore_errors=True)
            except OSError:
                pass

    def upload_meta(att, uid: str) -> dict[str, Any]:
        m = att.meta()
        m.update(id=uid, label=f"{att.name} — {human_size(att.size)}", tokens=att.tokens)
        if att.kind == "image":
            from io import BytesIO
            import base64
            from PIL import Image
            img = Image.open(BytesIO(base64.b64decode(att.image_b64)))
            img.thumbnail((160, 160))
            buf = BytesIO()
            img.convert("RGB").save(buf, "JPEG", quality=75)
            m["thumb"] = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        return m

    def load_uploads(ids: list[str]) -> list:
        atts = []
        for uid in ids:
            if not UPLOAD_ID.match(uid):
                raise HTTPException(400, "неверный идентификатор вложения")
            folder = uploads_dir() / uid
            meta_p = folder / "meta.json"
            if not meta_p.is_file():
                raise HTTPException(400, "вложение не найдено — загрузите файл ещё раз")
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            try:
                atts.append(load_attachment((folder / "data").read_bytes(), meta["name"]))
            except AttachmentError as e:
                raise HTTPException(400, str(e)) from e
        return atts

    @app.post("/api/uploads")
    async def upload(request: Request, name: str = "file") -> dict[str, Any]:
        """Загрузка одного файла (тело запроса — содержимое файла). Возвращает описание для формы."""
        import secrets
        name = safe_name(name)
        data = await request.body()
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, f"«{name}» больше {MAX_FILE_BYTES // 1024 // 1024} МБ")
        try:
            att = await asyncio.to_thread(load_attachment, data, name)
        except AttachmentError as e:
            raise HTTPException(400, str(e)) from e
        uid = secrets.token_hex(8)
        folder = uploads_dir() / uid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "data").write_bytes(data)
        (folder / "meta.json").write_text(json.dumps({"name": att.name}, ensure_ascii=False), encoding="utf-8")
        return await asyncio.to_thread(upload_meta, att, uid)

    @app.delete("/api/uploads/{uid}")
    async def delete_upload(uid: str) -> dict[str, Any]:
        import shutil
        if UPLOAD_ID.match(uid):
            shutil.rmtree(uploads_dir() / uid, ignore_errors=True)
        return {"ok": True}

    @app.post("/api/transcribe")
    async def transcribe_audio(request: Request, mock: bool = False) -> dict[str, Any]:
        """Голосовой ввод: тело запроса — запись из браузера (WebM/MP4/OGG/WAV). Возвращает распознанный текст."""
        from ..transcribe import MAX_AUDIO_BYTES, TranscribeError, transcribe
        data = await request.body()
        if len(data) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "запись больше 20 МБ")
        cfg = base_config()
        if mock:
            client = MockClient(delay=0.3)
        else:
            key = os.environ.get("OPENROUTER_API_KEY")
            if not key:
                raise HTTPException(400, "нет OPENROUTER_API_KEY — расшифровка голоса доступна в демо-режиме или с ключом")
            client = OpenRouterClient(key, timeout=cfg.timeout_s, max_retries=1)
        try:
            text, seconds, cost = await transcribe(client, cfg.transcribe_model, data)
        except TranscribeError as e:
            raise HTTPException(400, str(e)) from e
        except ProviderError as e:
            raise HTTPException(502, f"модель расшифровки не ответила: {e}") from e
        finally:
            await client.aclose()
        return {"text": text, "seconds": round(seconds, 1), "cost": cost}

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        from ..stats import compute
        return await asyncio.to_thread(compute, archive())

    @app.post("/api/sessions/{sid}/repeat")
    async def repeat(sid: str) -> dict[str, Any]:
        """Данные для «Повторить»: исходные настройки и вложения (скопированы в черновики формы)."""
        import secrets
        arch = archive()
        if not arch.exists(sid):
            raise HTTPException(404, "заседание не найдено")
        req = await asyncio.to_thread(arch.request_of, sid)
        d = await asyncio.to_thread(arch.load, sid)
        uploads = []
        for m in d.get("attachments") or []:
            try:
                data = await asyncio.to_thread(arch.attachment(sid, m["file"]).read_bytes)
                att = await asyncio.to_thread(load_attachment, data, m["name"])
            except (KeyError, OSError, AttachmentError):
                continue
            uid = secrets.token_hex(8)
            folder = uploads_dir() / uid
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "data").write_bytes(data)
            (folder / "meta.json").write_text(json.dumps({"name": att.name}, ensure_ascii=False), encoding="utf-8")
            uploads.append(await asyncio.to_thread(upload_meta, att, uid))
        title = await asyncio.to_thread(lambda: arch.view(sid).title_text)
        return {"request": req, "uploads": uploads, "title": title}

    @app.get("/api/sessions/{sid}/files/{file}")
    async def session_file(sid: str, file: str) -> FileResponse:
        try:
            path = archive().attachment(sid, file)
        except KeyError as e:
            raise HTTPException(404, "файл не найден") from e
        import mimetypes
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        inline = mime.startswith("image/") or mime == "application/pdf"
        return FileResponse(path, media_type=mime, filename=path.name.split("-", 1)[-1],
                            content_disposition_type="inline" if inline else "attachment")

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
            "upload": {"accept": ACCEPT, "max_files": MAX_FILES, "max_mb": MAX_FILE_BYTES // 1024 // 1024},
            "members": [{"name": m.name, "model": m.model, "enabled": m.enabled} for m in cfg.members],
            "chair": cfg.chair,
            "devil_advocate": cfg.devil_advocate,
            "chair_proposes": cfg.chair_proposes,
            "fact_check": {"enabled": cfg.fact_check.enabled, "model": cfg.fact_check.model, "top": cfg.fact_check.top},
            "compact": {"enabled": cfg.compact_proposals, "words": cfg.compact_words},
            "mode": cfg.mode,
            "rounds": cfg.debate_rounds,
            "budget": cfg.max_cost_usd,
            "language": cfg.language,
            "has_key": bool(os.environ.get("OPENROUTER_API_KEY")),
        }

    balance_cache: dict[str, float | None] = {"at": 0.0, "value": None}

    @app.get("/api/balance")
    async def get_balance(force: bool = False) -> dict[str, Any]:
        """Остаток на балансе OpenRouter (кэш на минуту, чтобы не дёргать OpenRouter при каждом обновлении)."""
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            return {"has_key": False, "balance": None}
        if force or time.time() - (balance_cache["at"] or 0) > 60:
            bc = OpenRouterClient(key, timeout=15)
            try:
                value = await bc.balance()
            finally:
                await bc.aclose()
            if value is not None or balance_cache["value"] is None:
                balance_cache["value"] = value
            balance_cache["at"] = time.time()
        return {"has_key": True, "balance": balance_cache["value"]}

    @app.post("/api/estimate")
    async def post_estimate(req: RunRequest) -> dict[str, Any]:
        cfg = build_config(req)
        warning = None
        pricing: dict = {}
        missing: dict[str, list[str]] = {}
        models = None
        if not req.mock:
            models = await catalog.get()
            if models is None:
                warning = "Не удалось получить список моделей OpenRouter — проверка и цены недоступны."
            else:
                pricing = parse_pricing(models)
                missing = unknown_models(cfg, set(pricing))
        opts = req.clean_options
        rounds = cfg.debate_rounds if req.rounds is None else req.rounds
        atts = await asyncio.to_thread(load_uploads, req.attachments) if req.attachments else []
        vision = vision_map(cfg, models)
        lengths = await asyncio.to_thread(learn_lengths, runs_dir(cfg), compact=cfg.compact_proposals)
        mode_eff = "evaluate" if opts else req.mode
        est = estimate(cfg, pricing, mode=mode_eff, rounds=rounds,
                       n_options=len(opts) if opts else None, has_criteria=bool(req.clean_criteria),
                       doc_tokens=sum(a.tokens for a in atts if a.kind == "document"),
                       images=sum(1 for a in atts if a.kind == "image"), vision=vision,
                       question_chars=len(req.question) + len(req.context or ""),
                       options_chars=sum(len(o) for o in opts or []), lengths=lengths)
        blind = [m.name for m in cfg.active_members if vision.get(m.model) is False]
        if any(a.kind == "image" for a in atts) and blind:
            note = (f"Модели без зрения ({', '.join(blind)}) получат описание картинок, "
                    f"которое заранее сделает одна из «зрячих» моделей.")
            warning = f"{warning} {note}" if warning else note
        balance = None
        key = os.environ.get("OPENROUTER_API_KEY")
        if not req.mock and key:
            bc = OpenRouterClient(key, timeout=15)
            try:
                balance = await bc.balance()
            finally:
                await bc.aclose()
        return {
            "basis": est.basis,
            "similar": similar_actual(lengths, mode_eff, len(cfg.active_members)),
            "balance": balance,
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
        atts = await asyncio.to_thread(load_uploads, req.attachments) if req.attachments else []
        history: list[dict[str, Any]] = []
        parent_title = ""
        if req.parent:
            if not archive().exists(req.parent):
                raise HTTPException(400, "заседание, к которому задаётся уточнение, не найдено")
            history, parent_atts = await asyncio.to_thread(archive().followup, req.parent)
            parent_title = history[-1]["title"]
            atts = merge(parent_atts, atts)  # вложения исходного заседания тоже видны совету (без повторов)
        assign_files(atts)
        att_warnings = limit_total(atts)
        vision = vision_map(cfg, None if req.mock else await catalog.get())
        client = make_client(cfg, req.mock)
        sid = make_stem(req.question)
        while sid in runs or (runs_dir(cfg) / f"{sid}.json").exists() or Checkpoint.load(runs_dir(cfg), sid):
            sid += "-1"
        rounds = cfg.debate_rounds if req.rounds is None else req.rounds
        mode = "evaluate" if opts else req.mode
        request = {"question": req.question, "mode": mode, "rounds": rounds,
                   "criteria": req.clean_criteria or [], "options": opts or [], "context": req.context or "",
                   "members": [m.name for m in cfg.active_members], "chair": cfg.chair,
                   "devil_advocate": cfg.devil_advocate or "", "budget": cfg.max_cost_usd,
                   "chair_proposes": cfg.chair_proposes, "fact_check": cfg.fact_check.enabled,
                   "compact_proposals": cfg.compact_proposals, "mock": req.mock}
        # «паспорт» заседания на диске: если оно прервётся, его можно будет продолжить без потери оплаченного
        cp = await asyncio.to_thread(
            Checkpoint.create, runs_dir(cfg), sid, cfg=cfg, question=req.question, mode=req.mode, rounds=rounds,
            criteria=req.clean_criteria, options=opts, context=req.context or None, attachments=atts,
            history=history, followup_of=req.parent or "", vision=vision, request=request, origin="web",
            mock=req.mock, extra={"uploads": req.attachments, "parent_title": parent_title,
                                  "att_warnings": att_warnings})
        launch(cp, client)
        return {"id": sid}

    def make_client(cfg: CouncilConfig, mock: bool):
        if mock:
            return MockClient(delay=mock_delay)
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise HTTPException(400, "нет OPENROUTER_API_KEY: добавьте ключ в .env и перезапустите сервер "
                                     "или включите демо-режим")
        return OpenRouterClient(key, timeout=cfg.timeout_s, max_retries=cfg.max_retries,
                                max_tokens_cap=cfg.max_tokens_cap)

    def start_event(cp: Checkpoint) -> dict[str, Any]:
        sp = cp.spec
        cfg = cp.config()
        mode = "evaluate" if sp.get("options") else sp.get("mode")
        parent = sp.get("followup_of")
        return {"type": "start", "id": cp.id, "question": sp["question"], "mode": mode, "rounds": sp.get("rounds"),
                "members": [m.name for m in cfg.active_members], "chair": cfg.chair, "budget": cfg.max_cost_usd,
                "mock": bool(sp.get("mock")), "fact_check": cfg.fact_check.enabled,
                "parent": {"id": parent, "title": sp.get("parent_title", "")} if parent else None,
                "resumed": int(sp.get("resumes") or 0)}

    def paused_event(cp: Checkpoint) -> dict[str, Any]:
        info = cp.summary()
        return {"type": "paused", "id": cp.id, "reason": info["reason"], "reason_text": info["reason_text"],
                "message": info["message"], "spent": info["spent"], "budget": info["budget"], "stage": info["stage"]}

    def launch(cp: Checkpoint, client: Any) -> RunState:
        """Запускает новое или продолженное заседание по его «паспорту» на диске."""
        sid = cp.id
        sp = cp.spec
        cfg = cp.config()
        st = RunState(sid, sp["question"], "evaluate" if sp.get("options") else sp.get("mode", ""))
        st.checkpoint = cp
        st.push(start_event(cp))

        def on_event(event: str, **data: Any) -> None:
            st.push({"type": event, **jsonable(data)})
            if event == "stage":
                cp.update(stage=data.get("title", ""))

        council: Council | None = None

        def keep_failed(status: str, error: str) -> None:
            """Прерванное заседание тоже сохраняется — с событиями и частичной стенограммой, для разбора."""
            save_failed(runs_dir(cfg), sid, question=sp["question"], status=status, error=error, events=st.events,
                        transcript=council.transcript if council else [], stats=council.stats if council else {},
                        request={"mode": sp.get("mode"), "members": [m.name for m in cfg.active_members],
                                 "chair": cfg.chair})

        async def execute() -> None:
            nonlocal council
            final = "error"
            resumed = int(sp.get("resumes") or 0)
            log.info("заседание %s: %s (%s, участников %d)", sid, "продолжение" if resumed else "старт",
                     sp.get("mode"), len(cfg.active_members))
            with cp.lock():
                try:
                    cp.update(status="running", reason="", message="")
                    atts = await asyncio.to_thread(cp.attachments)
                    journal = await asyncio.to_thread(Journal, cp.journal_path)
                    council = Council(cfg, client, on_event=on_event, vision=sp.get("vision") or {},
                                      seed=sp.get("seed"), journal=journal, labels=sp.get("labels"))
                    st.council = council
                    if journal.replayable or journal.loose:
                        st.push({"type": "resumed", "replayable": journal.replayable or len(journal.loose),
                                 "spent": round(journal.spent, 4)})
                    for w in sp.get("att_warnings") or []:
                        st.push({"type": "member_failed", "stage": "attachments", "member": "вложения", "error": w})
                    res = await council.run(sp["question"], attachments=atts, **cp.run_args())
                    res.request = {**(sp.get("request") or {}), "budget": council.budget}
                    st.push({"type": "saving"})
                    await asyncio.to_thread(save, res, runs_dir(cfg), sid, atts, True, cp.remove)
                    import shutil
                    for uid in sp.get("uploads") or []:  # черновики больше не нужны: файлы уже в архиве заседания
                        shutil.rmtree(uploads_dir() / uid, ignore_errors=True)
                    await asyncio.to_thread(cp.remove)
                    final = "done"
                    st.push({"type": "done", "id": sid, "total_cost": res.total_cost, "duration_s": res.duration_s,
                             "stats": jsonable(res.stats)})
                    cached = sum(s.cached_tokens for s in res.stats.values())
                    log.info("заседание %s: готово, $%.4f, %s с, из кэша %d входных токенов, из журнала %d ответов",
                             sid, res.total_cost, res.duration_s, cached, council.replayed)
                    failures = {n: s.failures for n, s in res.stats.items() if s.failures}
                    if failures:
                        log.warning("заседание %s: сбои участников: %s", sid, failures)
                except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001 — любая остановка = пауза
                    reason, message = classify(e)
                    if isinstance(e, CouncilError) or reason in ("credits", "stopped"):
                        log.warning("заседание %s приостановлено (%s): %s", sid, reason, message)
                    else:
                        log.exception("заседание %s упало", sid)
                        message = f"внутренняя ошибка: {e!r}"
                    spent = council.total_cost if council else 0.0
                    cp.update(status="paused", reason=reason, message=message, spent=round(spent, 6),
                              config={**cp.spec["config"], "max_cost_usd": council.budget if council else cp.budget})
                    final = "paused"
                    st.push(paused_event(cp))
                    if reason in ("error", "stopped"):
                        import traceback
                        expected = reason == "stopped" or isinstance(e, CouncilError)
                        await asyncio.to_thread(keep_failed, "cancelled" if reason == "stopped" else
                                                "error" if expected else "crash",
                                                message if expected else traceback.format_exc())
                finally:
                    st.council = None
                    await asyncio.to_thread(save_events, runs_dir(cfg), sid, st.events)
                    st.finish(final)  # статус «завершено» — только когда журнал уже на диске
                    await client.aclose()

        finished = [r for r in runs.values() if r.status != "running"]
        for old in sorted(finished, key=lambda r: r.started)[:-KEEP_FINISHED or None]:
            runs.pop(old.id, None)
        runs[sid] = st
        st.task = asyncio.create_task(execute())
        return st

    @app.get("/api/paused")
    async def paused_list() -> dict[str, Any]:
        """Приостановленные и прерванные заседания, которые можно продолжить."""
        items = await asyncio.to_thread(lambda: [c.summary() for c in Checkpoint.all(runs_dir(base_config()))])
        return {"items": [i for i in items if i["status"] != "running"]}

    @app.post("/api/sessions/{sid}/budget")
    async def raise_budget(sid: str, body: BudgetRequest) -> dict[str, Any]:
        """Поднять бюджет заседания — прямо на ходу или перед продолжением."""
        st = runs.get(sid)
        if st is not None and st.status == "running" and st.council is not None:
            if body.budget <= st.council.total_cost:
                raise HTTPException(400, f"уже потрачено ${st.council.total_cost:.2f} — новый бюджет должен быть больше")
            st.council.set_budget(body.budget)
            if st.checkpoint:
                await asyncio.to_thread(st.checkpoint.set_budget, body.budget)
            return {"ok": True, "budget": body.budget, "running": True}
        cp = await asyncio.to_thread(Checkpoint.load, runs_dir(base_config()), sid)
        if cp is None:
            raise HTTPException(404, "заседание не найдено или уже завершено")
        await asyncio.to_thread(cp.set_budget, body.budget)
        return {"ok": True, "budget": body.budget, "running": False}

    @app.post("/api/sessions/{sid}/resume")
    async def resume(sid: str, body: ResumeRequest | None = None) -> dict[str, Any]:
        """Продолжить приостановленное заседание: уже полученные ответы берутся из журнала, без повторной оплаты."""
        st = runs.get(sid)
        if st is not None and st.status == "running":
            raise HTTPException(409, "заседание уже идёт")
        d = runs_dir(base_config())
        cp = await asyncio.to_thread(Checkpoint.load, d, sid)
        if cp is None:
            raise HTTPException(404, "заседание не найдено или уже завершено")
        if await asyncio.to_thread(cp.is_live):
            raise HTTPException(409, "это заседание сейчас идёт в другом процессе (например, в Telegram-боте)")
        budget = body.budget if body and body.budget else None
        spent = float(cp.spec.get("spent") or 0) or await asyncio.to_thread(lambda: Journal(cp.journal_path).spent)
        if budget:
            cp.set_budget(budget)
        if cp.budget <= spent:
            raise HTTPException(400, f"бюджет ${cp.budget:g} уже потрачен (${spent:.2f}) — укажите бюджет больше")
        client = make_client(cp.config(), bool(cp.spec.get("mock")))
        cp.update(resumes=int(cp.spec.get("resumes") or 0) + 1)
        launch(cp, client)
        return {"id": sid}

    @app.post("/api/sessions/{sid}/redo")
    async def redo(sid: str, body: ResumeRequest | None = None) -> dict[str, Any]:
        """«Доделать» заседание из архива, где модели не ответили (лимит ключа, деньги, сбой): оплаченные ответы
        берутся из стенограммы, заново спрашиваются только недостающие шаги. Итог заменит архивную запись."""
        st = runs.get(sid)
        if st is not None and st.status == "running":
            raise HTTPException(409, "заседание уже идёт")
        cfg = base_config()
        d = runs_dir(cfg)
        if await asyncio.to_thread(Checkpoint.load, d, sid):
            raise HTTPException(409, "заседание уже на паузе — нажмите «Продолжить»")
        path = find_file(sid, "json")
        data = await asyncio.to_thread(lambda: json.loads(path.read_text(encoding="utf-8")))  # бывает большим
        spent = float(data.get("total_cost_usd") or 0)
        budget = body.budget if body and body.budget else round(spent + max(1.0, cfg.max_cost_usd), 2)
        if budget <= spent:
            raise HTTPException(400, f"на заседание уже потрачено ${spent:.2f} — бюджет должен быть больше")
        mock = bool((data.get("request") or {}).get("mock"))  # демо-заседание доделывается тоже в демо
        client = make_client(cfg, mock)
        vision = vision_map(cfg, None if mock else await catalog.get())
        try:
            cp = await asyncio.to_thread(Checkpoint.from_archive, d, sid, data, cfg, budget=budget, vision=vision)
        except OverrideError as e:
            raise HTTPException(400, str(e)) from e
        launch(cp, client)
        return {"id": sid}

    @app.delete("/api/sessions/{sid}/paused")
    async def drop_paused(sid: str) -> dict[str, Any]:
        """Отказаться от приостановленного заседания (потраченное уже не вернуть)."""
        st = runs.get(sid)
        if st is not None and st.status == "running":
            raise HTTPException(409, "заседание идёт — сначала остановите его")
        cp = await asyncio.to_thread(Checkpoint.load, runs_dir(base_config()), sid)
        if cp is None:
            raise HTTPException(404, "заседание не найдено")
        if await asyncio.to_thread(cp.is_live):
            raise HTTPException(409, "это заседание сейчас идёт в другом процессе")
        await asyncio.to_thread(cp.remove)
        runs.pop(sid, None)
        return {"ok": True}

    # ------------------------------------------------------------ диагностика

    def diag_response(stem: str | None) -> Response:
        cfg = base_config()
        data = bundle(runs_dir(cfg), config_path, stem=stem, control_dir=config_path.parent / "control")
        name = f"diagnostics-{stem or time.strftime('%Y%m%d-%H%M%S')}.zip"
        ascii_name = f"diagnostics-{(stem or '')[:15] or time.strftime('%Y%m%d-%H%M%S')}.zip"  # дата и время заседания
        from urllib.parse import quote
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"})

    @app.get("/api/diagnostics")
    async def diagnostics() -> Response:
        """Журналы программы и прерванные заседания одним архивом — для разбора ошибок."""
        return await asyncio.to_thread(diag_response, None)

    @app.get("/api/diagnostics/failed")
    async def failed_sessions() -> dict[str, Any]:
        return {"items": await asyncio.to_thread(list_failed, runs_dir(base_config()))}

    @app.get("/api/sessions/{sid}/diagnostics")
    async def session_diagnostics(sid: str) -> Response:
        """Пакет для разбора одного заседания: журнал событий, стенограмма с сырыми ответами, журналы программы."""
        if not re.fullmatch(r"[\w\-]{1,160}", sid):
            raise HTTPException(400, "неверный id")
        st = runs.get(sid)
        if st is not None and st.status == "running":
            await asyncio.to_thread(save_events, runs_dir(base_config()), sid, st.events)
        d = runs_dir(base_config())
        if not (st or (d / f"{sid}.json").exists() or (d / f"{sid}.events.jsonl").exists()
                or (d / "failed" / f"{sid}.json").exists()):
            raise HTTPException(404, "заседание не найдено")
        return await asyncio.to_thread(diag_response, sid)

    @app.get("/api/sessions")
    async def list_sessions(q: str = "", mode: str = "") -> dict[str, Any]:
        items = await asyncio.to_thread(archive().list, q, mode)
        known = {i["id"] for i in items}
        from ..reports.view import MODE_NAME
        # идущие, остановленные и упавшие заседания (их нет в архиве на диске) — с теми же фильтрами
        live = [{"id": st.id, "question": st.question, "status": st.status,
                 "date": time.strftime("%d.%m.%Y %H:%M", time.localtime(st.started)),
                 "mode": st.mode, "mode_name": MODE_NAME.get(st.mode, ""), "winner": "", "winner_title": "",
                 "winner_author": "", "cost": None, "members": [], "proposals": 0, "attachments": 0}
                for st in runs.values()
                if st.id not in known and (not q or q.casefold() in st.question.casefold()) and (not mode or st.mode == mode)]
        # приостановленные и прерванные (например, перезапуском при обновлении) — их можно продолжить
        live_ids = {x["id"] for x in live}
        cps = await asyncio.to_thread(lambda: [(c, c.summary()) for c in Checkpoint.all(runs_dir(base_config()))])
        for cp, info in cps:
            if cp.id in known or (q and q.casefold() not in info["question"].casefold()) or (mode and info["mode"] != mode):
                continue
            row = {"id": cp.id, "question": info["question"], "status": info["status"], "reason": info["reason"],
                   "reason_text": info["reason_text"], "spent": info["spent"], "budget": info["budget"],
                   "date": time.strftime("%d.%m.%Y %H:%M", time.localtime(info["created"] or time.time())),
                   "mode": info["mode"], "mode_name": MODE_NAME.get(info["mode"], ""), "winner": "", "winner_title": "",
                   "winner_author": "", "cost": info["spent"], "members": info["members"], "proposals": 0, "attachments": 0}
            if cp.id in live_ids:
                for x in live:
                    if x["id"] == cp.id and x["status"] != "running":
                        x.update(row)
            else:
                live.append(row)
        return {"items": sorted(live + items, key=lambda x: x["id"], reverse=True),
                "formats": available_formats(), "total": len(items) + len(live)}

    @app.get("/api/sessions/{sid}/events")
    async def stream(sid: str, request: Request) -> StreamingResponse:
        st = runs.get(sid)
        if st is None:
            cp = await asyncio.to_thread(Checkpoint.load, runs_dir(base_config()), sid) if ID_RE.match(sid) else None
            if cp is not None:  # приостановлено или прервано перезапуском — показываем, что можно продолжить
                static_events = [start_event(cp), paused_event(cp)]
            else:
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
                if not await st.wait(10):
                    # «сердцебиение» без id: страница видит, что связь есть, даже пока модели молча думают
                    yield f"data: {json.dumps({'type': 'ping', 'server_time': time.time()})}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/sessions/{sid}/cancel")
    async def cancel(sid: str) -> dict[str, Any]:
        st = runs.get(sid)
        if st is None or st.status != "running" or st.task is None:
            raise HTTPException(409, "заседание не идёт")
        st.task.cancel()
        return {"ok": True}

    @app.get("/api/sessions/{sid}/report", response_model=None)
    async def report(sid: str, format: str = "pdf", download: bool = False,
                     summary: bool = False, decision: bool = False) -> FileResponse | HTMLResponse:
        """Отчёт заседания. summary=1 — итоговое решение с обоснованием, рисками и особыми мнениями;
        decision=1 — только решение совета, без вопроса."""
        arch = archive()
        if not arch.exists(sid):
            raise HTTPException(404, "заседание не найдено")
        if format == "html":
            return HTMLResponse(await asyncio.to_thread(arch.html, sid, report_toolbar(sid, summary, decision),
                                                        summary, decision))
        if format not in MIME:
            raise HTTPException(400, "формат: pdf, docx, md или html")
        if format not in available_formats():
            raise HTTPException(501, "PDF недоступен на этом сервере (нет WeasyPrint) — скачайте DOCX или MD")
        kind = report_kind(format, summary, decision)
        try:
            path = await asyncio.to_thread(arch.report, sid, kind)
        except RuntimeError as e:
            raise HTTPException(500, f"не удалось создать отчёт: {e}") from e
        inline = format == "pdf" and not download
        return FileResponse(path, media_type=MIME[format], filename=download_name(sid, kind),
                            content_disposition_type="inline" if inline else "attachment")

    def report_toolbar(sid: str, summary: bool = False, decision: bool = False) -> str:
        from urllib.parse import quote
        q = quote(sid)
        s = "&decision=1" if decision else ("&summary=1" if summary else "")
        base = f"/api/sessions/{q}/report?format=html"
        links = [f'<a href="/#/s/{q}">← В приложение</a>']
        for name, extra, on in (("Полный отчёт", "", not (summary or decision)), ("Итог", "&summary=1", summary and not decision),
                                ("Только решение", "&decision=1", decision)):
            if not on:
                links.append(f'<a href="{base}{extra}">{name}</a>')
        links.append('<span class="sp"></span>')
        names = {"pdf": "PDF", "docx": "Word", "md": "Markdown"}
        links += [f'<a href="/api/sessions/{q}/report?format={f}&download=1{s}">{names[f]}</a>' for f in available_formats()]
        links.append('<button onclick="window.print()">Печать</button>')
        return '<div class="toolbar noprint">' + "".join(links) + "</div>"

    @app.delete("/api/sessions/{sid}")
    async def delete(sid: str) -> dict[str, Any]:
        st = runs.get(sid)
        if st and st.status == "running":
            raise HTTPException(409, "сначала остановите заседание")
        runs.pop(sid, None)
        removed = archive().delete(sid) if ID_RE.match(sid) else False
        cp = await asyncio.to_thread(Checkpoint.load, runs_dir(base_config()), sid) if ID_RE.match(sid) else None
        if cp is not None:  # приостановленное заседание
            if await asyncio.to_thread(cp.is_live):
                raise HTTPException(409, "это заседание сейчас идёт в другом процессе")
            await asyncio.to_thread(cp.remove)
            removed = True
        if not removed and st is None:
            raise HTTPException(404, "заседание не найдено")
        return {"ok": True}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # метка версии статики: после обновления программы браузер сразу берёт новые app.js и style.css, а не старые из кэша
    import hashlib
    static_tag = hashlib.sha1(b"".join((STATIC / n).read_bytes() for n in ("app.js", "settings.js", "style.css"))).hexdigest()[:10]
    index_html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "settings.js", "style.css"):
        index_html = index_html.replace(f'"/static/{name}"', f'"/static/{name}?v={static_tag}"')

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(index_html, headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    password = os.environ.get("WEB_PASSWORD", "") if password is None else password
    if password:
        user = os.environ.get("WEB_USER", "").strip() or "admin"
        trust = os.environ.get("TRUST_PROXY", "") == "1"
        return BasicAuth(app, user=user, password=password, trust_proxy=trust)  # type: ignore[return-value]
    return app
