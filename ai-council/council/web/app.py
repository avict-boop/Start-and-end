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
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
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
from ..links import check_links
from ..checkpoint import Checkpoint, Journal, classify
from ..session import Council, CouncilError
from .auth import BasicAuth

log = logging.getLogger("council.web")
from .. import series as SR  # noqa: E402
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
                    if d.get("followup_of") else None),
         "series": (d.get("request") or {}).get("series") or None},
        {"type": "framing", "refined_question": d.get("refined_question", d["question"]), "criteria": d["criteria"],
         "requirements": d.get("requirements") or [], **{k: (d.get("research") or {}).get(k)
                                                         for k in ("scope", "as_of", "subquestions", "lenses")
                                                         if (d.get("research") or {}).get(k)}},
    ]
    lens_of = (d.get("research") or {}).get("lens_of") or {}
    if lens_of:  # метка варианта -> угол зрения; показываем по авторам
        authors = d.get("authors") or {}
        ev.append({"type": "lenses", "by": {authors.get(lab) or lab: t for lab, t in lens_of.items() if t}})
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
    led = (d.get("research") or {}).get("ledger")
    if led:
        ev.append({"type": "stage", "stage": "arbitration", "title": "Арбитраж доказательств"})
        ev.append({"type": "ledger", "ledger": led})
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
               "coverage": d.get("coverage"), "sections": (d.get("research") or {}).get("sections") or {}})
    if d.get("fact_check"):
        ev.append({"type": "stage", "stage": "factcheck", "title": "Проверка фактов"})
        ev.append({"type": "factcheck", "results": d["fact_check"], "model": d.get("fact_checker", "")})
    ev.append({"type": "stage", "stage": "verdict", "title": "Итоговое решение"})
    for name, s in d.get("stats", {}).items():
        for f in s.get("failures", []):
            stage, _, err = f.partition(": ")
            ev.append({"type": "member_failed", "stage": stage, "member": name, "error": err})
    from ..reports.view import chair_failure
    cf = chair_failure(d)
    if cf and not any(f.startswith("verdict:") for s in d.get("stats", {}).values() for f in s.get("failures", [])):
        ev.append({"type": "member_failed", "stage": "verdict", "member": cf[0], "error": cf[1]})
    ev.append({"type": "verdict", "verdict": d["verdict"], "ideas": d.get("ideas") or []})
    ev.append({"type": "done", "id": session_id, "total_cost": d.get("total_cost_usd", 0),
               "duration_s": d.get("duration_s"), "stats": d.get("stats", {}),
               "semantic": (d.get("research") or {}).get("semantic_audit")})
    return ev


class RatingRequest(BaseModel):
    rating: int = Field(ge=0, le=5)
    note: str = ""
    kind: str = ""  # "solo" — оценка ответа одной модели (пилот)


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
    research_words: int | None = Field(default=None, ge=0, le=5000)  # 0 — без ограничения длины  # длина варианта исследования
    research_shorten: bool | None = None  # возвращать слишком длинные варианты на сокращение
    research: dict[str, Any] | None = None  # другие настройки исследования на это заседание (из рекомендации)
    # состав на это заседание («Подобрать состав»): другая модель у участника, новые участники, поиск у участников
    models: dict[str, str] | None = None
    extra_members: list[dict[str, str]] | None = None
    web_search: list[str] | None = None
    budget: float | None = Field(default=None, gt=0, le=100)
    answer_size: Literal["full", "brief"] | None = None  # объём итога на это заседание
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


class SeriesPart(BaseModel):
    title: str = ""
    question: str = ""  # пусто — программа соберёт: полное задание + «в этой части только эти объекты»
    objects: list[str] = Field(default_factory=list)


class SeriesRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    task: str = ""
    parts: list[SeriesPart] = Field(min_length=1, max_length=20)
    request: dict[str, Any] = Field(default_factory=dict)  # настройки запуска, как у одного заседания
    max_parallel: int = Field(default=2, ge=1, le=5)
    auto_summary: bool = True


class SplitAsk(BaseModel):
    question: str = Field(min_length=3)
    request: dict[str, Any] = Field(default_factory=dict)
    per_part: int | None = Field(default=None, ge=3, le=40)
    facts: int | None = Field(default=None, ge=1, le=5)
    # пересмотр разбиения: уже найденные объекты + сколько частей и/или по какому принципу
    objects: list[str] | None = Field(default=None, max_length=300)
    parts: int | None = Field(default=None, ge=1, le=20)
    principle: str = Field(default="", max_length=1500)


class BudgetRequest(BaseModel):
    budget: float = Field(gt=0, le=1000)


class SearchTest(BaseModel):
    model: str | None = None


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


access = logging.getLogger("council.web.access")


async def _limited_body(request: Any, limit: int, too_big: str) -> bytes:
    """Тело запроса не больше limit байт: читается частями и обрывается сразу — огромный файл не займёт память
    сервера целиком до проверки размера."""
    try:
        if int(request.headers.get("content-length") or 0) > limit:
            raise HTTPException(413, too_big)
    except ValueError:
        pass
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise HTTPException(413, too_big)
    return bytes(data)


def _same_site(src: str, request: Any) -> bool:
    """Страница-источник запроса — этот же сайт (с учётом прокси: X-Forwarded-Host)."""
    from urllib.parse import urlsplit
    if src == "null":
        return False
    host = urlsplit(src).netloc.lower()
    own = {h.strip().lower() for h in (request.headers.get("host", ""), request.headers.get("x-forwarded-host", ""))
           if h.strip()}
    return bool(host) and host in own


def create_app(config_path: str | Path, *, mock_delay: float = 0.6, password: str | None = None) -> FastAPI:
    config_path = Path(config_path)
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            cfg0 = base_config()
            setup_logging(runs_dir(cfg0), "web", level=cfg0.log_level, file_mb=cfg0.log_file_mb, files=cfg0.log_files)
        except HTTPException:
            pass
        log.info("веб-интерфейс запущен")
        # отчёты PDF/DOCX/MD для заседаний, проведённых до обновления программы
        archive().ensure_all_in_background()
        cleanup_uploads()
        yield

    app = FastAPI(title="Совет нейросетей", docs_url="/api/docs", lifespan=lifespan)

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next: Any) -> Any:
        """Защита от подделки запросов с чужих сайтов (CSRF). Пароль сайта браузер подставляет сам, поэтому чужая
        страница, открытая у того же человека, могла бы отправить сюда действие — запустить платное заседание,
        обновить или перезапустить программу. Браузер всегда сообщает, с какой страницы идёт запрос (Origin или
        Referer): действия принимаются только со страниц самого сайта. Запросы не из браузера (curl, бот) — без
        этих заголовков — проходят как раньше."""
        t = time.monotonic()
        path = request.url.path
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            src = request.headers.get("origin") or request.headers.get("referer") or ""
            if src and not _same_site(src, request):
                access.warning("%s %s → 403: запрос со стороннего сайта (%s)", request.method, path, src[:120])
                return JSONResponse({"detail": "запрос со стороннего сайта отклонён"}, status_code=403)
        try:
            resp = await call_next(request)
        except Exception:
            access.exception("%s %s → сбой", request.method, path)
            raise
        # опрос хода заседания и статика — каждые секунды: только на самом подробном уровне
        quiet = path.startswith("/static") or path.endswith("/events") or path in ("/api/balance", "/healthz")
        (access.debug if quiet else access.info)("%s %s%s → %d, %.0f мс", request.method, path,
                                                 f"?{request.url.query}" if request.url.query else "",
                                                 resp.status_code, (time.monotonic() - t) * 1000)
        if resp.status_code >= 400 and not quiet:
            access.warning("%s %s → %d", request.method, path, resp.status_code)
        return resp

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
                                  chair_proposes=req.chair_proposes,
                                  # в «Креативе» проверки фактов нет — не тратить на неё и не показывать её этап
                                  fact_check=False if req.mode == "creative" else req.fact_check,
                                  compact=False if req.mode in ("research", "creative") else req.compact,
                                  research_words=req.research_words if req.mode == "research" else None,
                                  research_shorten=req.research_shorten if req.mode == "research" else None,
                                  # «Свежие данные» на экране вопроса — явный список; «разведчики» по умолчанию
                                  # (fresh_scouts) сверх него не добавляются, иначе ищут те, кого человек не выбрал
                                  research=({**(req.research or {}), "fresh_scouts": 0} if req.web_search is not None
                                            else req.research) if req.mode == "research" else None,
                                  models=req.models, extra_members=req.extra_members, web_search=req.web_search,
                                  answer_size=req.answer_size)
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
        data = await _limited_body(request, MAX_FILE_BYTES, f"«{name}» больше {MAX_FILE_BYTES // 1024 // 1024} МБ")
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
        data = await _limited_body(request, MAX_AUDIO_BYTES, "запись больше 20 МБ")
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
        from ..ratings import load
        from ..stats import compute
        return await asyncio.to_thread(lambda: compute(archive(), load(runs_dir(base_config()))))

    @app.get("/api/sessions/{sid}/rating")
    async def get_rating(sid: str) -> dict[str, Any]:
        from ..ratings import load
        return {"rating": (await asyncio.to_thread(load, runs_dir(base_config()))).get(sid)}

    @app.post("/api/sessions/{sid}/rating")
    async def set_rating(sid: str, body: RatingRequest) -> dict[str, Any]:
        """Ваша оценка пользы заседания (1–5) — для пилота; 0 — убрать оценку."""
        from ..ratings import save
        if not archive().exists(sid):
            raise HTTPException(404, "заседание не найдено")
        key = f"{sid}#solo" if body.kind == "solo" else sid
        item = await asyncio.to_thread(save, runs_dir(base_config()), key, body.rating or None, body.note)
        return {"rating": item}

    @app.get("/api/sessions/{sid}/pilot")
    async def pilot_get(sid: str) -> dict[str, Any]:
        """Пилот по заседанию: ответ одной модели с поиском и переигранный арбитраж (если делались)."""
        from .. import pilot
        from ..ratings import load
        d = runs_dir(base_config())
        data = await asyncio.to_thread(pilot.load, d, sid)
        data.get("replay", {}).pop("ledger", None)
        ratings = await asyncio.to_thread(load, d)
        return {**data, "solo_rating": (ratings.get(f"{sid}#solo") or {}).get("rating"),
                "solo_model": pilot.solo_model(base_config())}

    @app.get("/api/reports/memory")
    async def reports_memory() -> dict[str, Any]:
        """Пик памяти последних сборок отчётов — чтобы подобрать reports_memory_mb."""
        from ..archive import report_peaks
        return await asyncio.to_thread(report_peaks, runs_dir(base_config()))

    @app.post("/api/sessions/{sid}/solo")
    async def pilot_solo(sid: str) -> dict[str, Any]:
        """Контрольная группа: одна модель с поиском отвечает на тот же вопрос (обычно $0,2–0,5)."""
        from .. import pilot
        from ..attachments import documents_block, load_from_dir
        arch = archive()
        if not arch.exists(sid):
            raise HTTPException(404, "заседание не найдено")
        d = await asyncio.to_thread(arch.load, sid)
        cfg = base_config()
        mock = bool((d.get("request") or {}).get("mock")) or not os.environ.get("OPENROUTER_API_KEY")
        atts = await asyncio.to_thread(load_from_dir, arch.files_dir(sid), d.get("attachments") or [])
        client = None if mock else OpenRouterClient(os.environ["OPENROUTER_API_KEY"], timeout=cfg.timeout_s, max_retries=1)
        try:
            res = await pilot.solo_answer(cfg, client, d, documents_block(atts), mock=mock)
        except ProviderError as e:
            raise HTTPException(502, f"модель не ответила: {e}") from e
        finally:
            if client:
                await client.aclose()
        await asyncio.to_thread(pilot.save, runs_dir(cfg), sid, "solo", res)
        return res

    @app.post("/api/sessions/{sid}/replay")
    async def pilot_replay(sid: str) -> dict[str, Any]:
        """Переиграть арбитраж исследования с текущими настройками на сохранённых вариантах (центы)."""
        from .. import pilot
        arch = archive()
        if not arch.exists(sid):
            raise HTTPException(404, "заседание не найдено")
        d = await asyncio.to_thread(arch.load, sid)
        if d.get("mode") != "research":
            raise HTTPException(400, "переиграть арбитраж можно только у исследования")
        cfg = base_config()
        mock = bool((d.get("request") or {}).get("mock")) or not os.environ.get("OPENROUTER_API_KEY")
        client = make_client(cfg, mock)
        try:
            res = await pilot.replay_arbitration(cfg, client, d, None if mock else check_links)
        except (ProviderError, ValueError) as e:
            raise HTTPException(400, str(e)) from e
        finally:
            await client.aclose()
        await asyncio.to_thread(pilot.save, runs_dir(cfg), sid, "replay", res)
        return {k: v for k, v in res.items() if k != "ledger"}

    @app.get("/api/sessions/{sid}/costs")
    async def session_costs(sid: str) -> dict[str, Any]:
        """Куда ушли деньги заседания: по этапам и участникам, повторы, доля входа из кэша."""
        from ..costs import stage_costs
        arch = archive()
        if not arch.exists(sid):
            raise HTTPException(404, "заседание не найдено")
        d = await asyncio.to_thread(arch.load, sid)
        return stage_costs(d.get("transcript") or [])

    @app.get("/api/sessions/{sid}/request")
    async def session_request(sid: str) -> dict[str, Any]:
        """Настройки заседания (модели, роли, режим) — для уточнения: по умолчанию тот же состав."""
        arch = archive()
        if not arch.exists(sid):
            raise HTTPException(404, "заседание не найдено")
        return await asyncio.to_thread(arch.request_of, sid)

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
        # «песочница»: картинка (в том числе SVG со скриптом) или PDF из вложения не выполнит код от имени сайта
        return FileResponse(path, media_type=mime, filename=path.name.split("-", 1)[-1],
                            content_disposition_type="inline" if inline else "attachment",
                            headers={"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"})

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
            "members": [{"name": m.name, "model": m.model, "enabled": m.enabled, "web_search": m.web_search} for m in cfg.members],
            "chair": cfg.chair,
            "devil_advocate": cfg.devil_advocate,
            "chair_proposes": cfg.chair_proposes,
            "fact_check": {"enabled": cfg.fact_check.enabled, "model": cfg.fact_check.model, "top": cfg.fact_check.top},
            "compact": {"enabled": cfg.compact_proposals, "words": cfg.compact_words},
            "research": {"words": cfg.research.max_words, "shorten": cfg.research.shorten_long,
                         "factor": cfg.research.long_factor},
            "research_all": cfg.research.model_dump(),
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
                value, lim = await asyncio.gather(bc.balance(), bc.key_limit())
            finally:
                await bc.aclose()
            if value is not None or balance_cache["value"] is None:
                balance_cache["value"] = value
            balance_cache["key"] = lim
            balance_cache["at"] = time.time()
        return {"has_key": True, "balance": balance_cache["value"], "key_limit": balance_cache.get("key")}

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
        series_hint = None
        if mode_eff == "research" and cfg.research.auto_split and not req.parent:
            from ..splitter import heuristic_objects
            n_obj = len(heuristic_objects("\n".join([req.question, req.context or ""]
                                                     + [a.text for a in atts if a.kind == "document"])))
            if n_obj > cfg.research.objects_per_part:
                parts = -(-n_obj // cfg.research.objects_per_part)
                series_hint = {"objects": n_obj, "parts": parts, "per_part": cfg.research.objects_per_part,
                               "budget_total": round(cfg.max_cost_usd * (parts + 1), 2)}
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
            "balance": balance, "key_limit": balance_cache.get("key"),
            "members": [dataclasses.asdict(m) for m in est.members],
            "calls": est.calls,
            "low": est.low if pricing else None,
            "high": est.high if pricing else None,
            "budget": cfg.max_cost_usd,
            "unknown": missing,
            "warning": warning,
            "series": series_hint,
        }

    create_lock = asyncio.Lock()  # части серии создаются одновременно с похожими вопросами — номера не должны совпасть

    @app.post("/api/sessions")
    async def start_session(req: RunRequest) -> dict[str, Any]:
        # председатель может разбить на серию только новое заседание (не уточнение, не продолжение, не «доделать»)
        return {"id": await create_session(req, spec_extra={"allow_split": not req.parent})}

    async def create_session(req: RunRequest, *, extra_atts: list | None = None,
                             request_extra: dict[str, Any] | None = None,
                             spec_extra: dict[str, Any] | None = None) -> str:
        """Новое заседание по запросу страницы, серии или сводного отчёта; возвращает его номер."""
        cfg = build_config(req)
        opts = req.clean_options
        if req.mode == "evaluate" and (not opts or len(opts) < 2):
            raise HTTPException(400, "для оценки нужно минимум 2 варианта")
        atts = await asyncio.to_thread(load_uploads, req.attachments) if req.attachments else []
        atts += list(extra_atts or [])
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
        async with create_lock:
            return await _create_locked(req, cfg, opts, atts, history, parent_title, vision, client,
                                        request_extra or {}, spec_extra or {}, att_warnings)

    async def _create_locked(req: RunRequest, cfg: CouncilConfig, opts: list[str] | None, atts: list,
                             history: list[dict[str, Any]], parent_title: str, vision: dict[str, Any], client: Any,
                             request_extra: dict[str, Any], spec_extra: dict[str, Any], att_warnings: list[str]) -> str:
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
                   "compact_proposals": cfg.compact_proposals, "mock": req.mock, "answer_size": cfg.answer_size,
                   **({"research_words": cfg.research.max_words, "research_shorten": cfg.research.shorten_long,
                       "research_overrides": req.research or {}} if mode == "research" else {}),
                   "models": {m.name: m.model for m in cfg.active_members},
                   "extra_members": req.extra_members or [],
                   "web_search": [m.name for m in cfg.active_members if m.web_search], **request_extra}
        # «паспорт» заседания на диске: если оно прервётся, его можно будет продолжить без потери оплаченного
        cp = await asyncio.to_thread(
            Checkpoint.create, runs_dir(cfg), sid, cfg=cfg, question=req.question, mode=req.mode, rounds=rounds,
            criteria=req.clean_criteria, options=opts, context=req.context or None, attachments=atts,
            history=history, followup_of=req.parent or "", vision=vision, request=request, origin="web",
            mock=req.mock, extra={"uploads": req.attachments, "parent_title": parent_title,
                                  "att_warnings": att_warnings, **spec_extra})
        launch(cp, client)
        return sid

    os.environ.setdefault("COUNCIL_REPORTS_MEM_MB", str(base_config().reports_memory_mb))

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
                "series": (sp.get("request") or {}).get("series") or None,
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
                    pricing = None if sp.get("mock") else parse_pricing(await catalog.get() or [])
                    stage_out = await asyncio.to_thread(learn_lengths, runs_dir(cfg))
                    council = Council(cfg, client, on_event=on_event, vision=sp.get("vision") or {},
                                      seed=sp.get("seed"), journal=journal, labels=sp.get("labels"),
                                      link_checker=None if sp.get("mock") else check_links, pricing=pricing,
                                      stage_out=stage_out, session_id=sid, shared_dir=runs_dir(cfg) / "notes",
                                      key_limit=None if sp.get("mock") else getattr(client, "key_limit", None))
                    st.council = council
                    council.allow_split = bool(sp.get("allow_split")) and not (sp.get("request") or {}).get("series")
                    # часть серии: свои объекты; сводка: реестры частей (общее с ботом — council/series.apply_spec)
                    await asyncio.to_thread(SR.apply_spec, council, sp, archive())
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
                    ser = (sp.get("request") or {}).get("series") or {}
                    if ser.get("id") and not ser.get("summary"):
                        asyncio.create_task(maybe_summary(ser["id"]))
                    st.push({"type": "done", "id": sid, "total_cost": res.total_cost, "duration_s": res.duration_s,
                             "stats": jsonable(res.stats), "semantic": (res.research or {}).get("semantic_audit")})
                    cached = sum(s.cached_tokens for s in res.stats.values())
                    log.info("заседание %s: готово, $%.4f, %s с, из кэша %d входных токенов, из журнала %d ответов",
                             sid, res.total_cost, res.duration_s, cached, council.replayed)
                    failures = {n: s.failures for n, s in res.stats.items() if s.failures}
                    if failures:
                        log.warning("заседание %s: сбои участников: %s", sid, failures)
                except SR.SplitRequested as e:  # председатель разбил задание: дальше — серия частей
                    req0 = {k: v for k, v in (sp.get("request") or {}).items()
                            if k in RunRequest.model_fields and k not in ("question", "parent", "attachments")}
                    rs = {**(req0.get("research") or (sp.get("request") or {}).get("research_overrides") or {}),
                          "catalog": True, "auto_split": False, "facts_per_object": e.plan["facts_per_object"]}
                    req0 = {**req0, "research": rs, "mode": "research", "budget": council.budget if council else None}
                    s = await asyncio.to_thread(
                        SR.create, runs_dir(cfg), title=(sp["question"].strip().splitlines() or [""])[0][:120] or e.plan["title"], task=sp["question"],
                        parts=e.plan["parts"], request={k: v for k, v in req0.items() if v is not None},
                        max_parallel=2, auto_summary=True, origin=f"split:{sid}")
                    if sp.get("uploads"):  # файлы задания нужны каждой части
                        s["files"] = await asyncio.to_thread(keep_series_files, s["id"], sp["uploads"])
                        await asyncio.to_thread(SR.save, runs_dir(cfg), s)
                    kick_series(s["id"])
                    await asyncio.to_thread(cp.remove)
                    final = "done"
                    st.push({"type": "split_series", "series": s["id"], "parts": len(e.plan["parts"]),
                             "objects": len(e.plan["objects"]), "notes": e.plan.get("notes") or []})
                    log.info("заседание %s: разбито на серию %s (%d частей)", sid, s["id"], len(e.plan["parts"]))
                except (asyncio.CancelledError, Exception) as e:  # noqa: BLE001 — любая остановка = пауза
                    reason, message = classify(e)
                    # бюджет, лимит ключа, баланс и ручная пауза — штатные остановки, а не сбой программы
                    if isinstance(e, CouncilError) or reason in ("credits", "stopped", "key_limit", "budget"):
                        log.warning("заседание %s приостановлено (%s): %s", sid, reason, message)
                    else:
                        log.exception("заседание %s упало", sid)
                        message = f"внутренняя ошибка: {e!r}"
                    spent = council.total_cost if council else 0.0
                    cp.update(status="paused", reason=reason, message=message, spent=round(spent, 6),
                              config={**cp.spec["config"], "max_cost_usd": council.budget if council else cp.budget},
                              budget_at_pause=(council.budget if council else cp.budget) if reason == "budget" else 0)
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
        return await resume_session(sid, body.budget if body and body.budget else None)

    async def resume_session(sid: str, budget_in: float | None = None) -> dict[str, Any]:
        st = runs.get(sid)
        if st is not None and st.status == "running":
            raise HTTPException(409, "заседание уже идёт")
        d = runs_dir(base_config())
        cp = await asyncio.to_thread(Checkpoint.load, d, sid)
        if cp is None:
            raise HTTPException(404, "заседание не найдено или уже завершено")
        if await asyncio.to_thread(cp.is_live):
            raise HTTPException(409, "это заседание сейчас идёт в другом процессе (например, в Telegram-боте)")
        budget = budget_in
        spent = float(cp.spec.get("spent") or 0) or await asyncio.to_thread(lambda: Journal(cp.journal_path).spent)
        if budget:
            cp.set_budget(budget)
        if cp.budget_short(spent):
            raise HTTPException(400, f"бюджета ${cp.budget:g} не хватает (потрачено ${spent:.2f}, следующий запрос не "
                                     "помещается) — укажите бюджет больше")
        client = make_client(cp.config(), bool(cp.spec.get("mock")))
        cp.update(resumes=int(cp.spec.get("resumes") or 0) + 1)
        launch(cp, client)
        return {"id": sid}

    # ------------------------------------------------------------ серия заседаний (council/series.py)
    series_tasks: dict[str, asyncio.Task] = {}

    def series_files_dir(series_id: str) -> Path:
        return SR.series_dir(runs_dir(base_config())) / "files" / series_id

    def keep_series_files(series_id: str, uids: list[str]) -> list[str]:
        """Копирует загрузки в папку серии: части идут долго, а черновые загрузки удаляются."""
        import shutil
        kept = []
        for uid in uids or []:
            src = uploads_dir() / uid
            if UPLOAD_ID.match(uid) and (src / "meta.json").is_file():
                dst = series_files_dir(series_id) / uid
                if not dst.exists():
                    shutil.copytree(src, dst)
                kept.append(uid)
        return kept

    def series_atts(s: dict[str, Any]) -> list:
        atts = []
        for uid in s.get("files") or []:
            folder = series_files_dir(s["id"]) / uid
            try:
                meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
                atts.append(load_attachment((folder / "data").read_bytes(), meta["name"]))
            except (OSError, ValueError, KeyError, AttachmentError) as e:
                log.warning("серия %s: файл %s не прочитан: %s", s["id"], uid, e)
        return atts

    async def maybe_summary(series_id: str) -> None:
        """Часть готова вне очереди серии (её продолжили сами) — все готовы? Совет сводит."""
        t = series_tasks.get(series_id)
        if t is not None and not t.done():
            return  # очередь серии сама запустит сводку
        s = await asyncio.to_thread(SR.load, runs_dir(base_config()), series_id)
        if not s or not s.get("auto_summary") or s.get("summary_id"):
            return
        if all(p["session_id"] and archive().exists(p["session_id"]) for p in s["parts"]):
            try:
                await start_summary(series_id)
            except HTTPException as e:
                log.warning("серия %s: сводный отчёт не запустился: %s", series_id, e.detail)

    def part_state(sid: str) -> dict[str, Any]:
        """Что с заседанием части сейчас: ждёт / идёт / готово / на паузе — по самому заседанию."""
        if not sid:
            return {"status": "queued", "cost": 0.0}
        st = runs.get(sid)
        if st is not None and st.status == "running":
            c = st.council.total_cost if getattr(st, "council", None) else 0.0
            return {"status": "running", "cost": round(c, 4)}
        s = archive().summary(sid) if archive().exists(sid) else None
        if s:
            return {"status": "done", "cost": s.get("cost") or 0.0, "winner_title": s.get("winner_title", "")}
        cp = Checkpoint.load(runs_dir(base_config()), sid)
        if cp is not None:
            info = cp.summary()
            return {"status": info["status"], "reason": info.get("reason_text", ""), "cost": info.get("spent") or 0.0}
        return {"status": "missing", "cost": 0.0}

    def series_view(s: dict[str, Any]) -> dict[str, Any]:
        parts = [{**{k: p[k] for k in ("n", "title", "session_id")}, "objects": len(p.get("objects") or []),
                  **part_state(p["session_id"])} for p in s["parts"]]
        summ = {"session_id": s.get("summary_id", ""), **part_state(s.get("summary_id", ""))} if s.get("summary_id") else None
        done = sum(1 for p in parts if p["status"] == "done")
        busy = s["id"] in series_tasks and not series_tasks[s["id"]].done()
        status = ("done" if summ and summ["status"] == "done"
                  else "summary" if summ and summ["status"] in ("running", "queued")
                  else "running" if busy
                  else "stopped" if summ  # сводка на паузе / прервана / удалена — «Продолжить серию» или «Свести»
                  else "ready" if done == len(parts) else "stopped")
        return {"id": s["id"], "title": s["title"], "created": s["created"], "max_parallel": s["max_parallel"],
                "auto_summary": s["auto_summary"], "origin": s.get("origin", ""), "parts": parts, "summary": summ,
                "done": done, "total": len(parts), "status": status,
                "cost": round(sum(p["cost"] or 0 for p in parts) + ((summ or {}).get("cost") or 0), 4),
                "mode": (s.get("request") or {}).get("mode", "research"), "waiting": series_wait.get(s["id"], "")}

    series_wait: dict[str, str] = {}  # серия → почему следующая часть ждёт (показывается на странице серии)

    async def funds_left(mock: bool = False) -> float | None:
        """Сколько можно потратить сейчас: меньшее из баланса и остатка недельного лимита ключа (None — неизвестно)."""
        probe = getattr(app.state, "funds_probe", None)  # тесты подставляют свой «баланс»
        if probe is not None:
            return await probe()
        if mock or not os.environ.get("OPENROUTER_API_KEY"):
            return None
        try:
            b = await get_balance()
        except Exception:  # noqa: BLE001 — не узнали баланс: части не держим
            return None
        vals = [b.get("balance")] + [((b.get("key_limit") or {}).get("remaining"))]
        vals = [float(v) for v in vals if v is not None]
        return min(vals) if vals else None

    async def run_series(series_id: str) -> None:
        """Части — не больше max_parallel одновременно; готовые пропускаются, приостановленные продолжаются.
        Все готовы и включена сводка — совет сводит их в один отчёт."""
        rd = runs_dir(base_config())
        s = SR.load(rd, series_id)
        if not s:
            return
        series_wait.pop(series_id, None)  # прежняя причина ожидания / сбоя части — с нового запуска не актуальна
        sem = asyncio.Semaphore(s["max_parallel"])
        log.info("серия %s: старт (%d частей, одновременно до %d)", series_id, len(s["parts"]), s["max_parallel"])
        active: set[int] = set()
        part_budget = float((s.get("request") or {}).get("budget") or base_config().max_cost_usd)

        mock = bool((s.get("request") or {}).get("mock"))

        async def wait_funds(n: int) -> None:
            """Денег меньше, чем лимиты идущих частей + этой, — ждём, пока идущие закончат (части по одной)."""
            told = False
            while active:
                left = await funds_left(mock)
                if left is None or left >= part_budget * (len(active) + 1):
                    break
                if not told:
                    told = True
                    series_wait[series_id] = (f"на балансе ${left:.2f} — меньше лимитов идущих частей и следующей "
                                              f"(${part_budget:g} каждая): следующая часть ждёт, пока идущая закончит")
                    log.info("серия %s, часть %d: ждёт — %s", series_id, n, series_wait[series_id])
                await asyncio.sleep(getattr(app.state, "funds_poll", 30))
            if told:
                series_wait.pop(series_id, None)

        async def one(n: int) -> None:
            async with sem:
                await wait_funds(n)
                active.add(n)
                try:
                    await one_part(n)
                except Exception as e:  # noqa: BLE001 — одна часть не остановит остальные, но причина — в журнал
                    log.warning("серия %s, часть %d: не запустилась: %s", series_id, n,
                                getattr(e, "detail", None) or e)
                    series_wait[series_id] = f"часть {n} не запустилась: {getattr(e, 'detail', None) or e}"[:300]
                finally:
                    active.discard(n)

        async def one_part(n: int) -> None:
            if True:  # тело — прежнее «async with sem:» (отступ сохранён)
                cur = SR.load(rd, series_id) or s
                p = next(x for x in cur["parts"] if x["n"] == n)
                sid = p["session_id"]
                if sid and archive().exists(sid):
                    return
                if sid and sid in runs and runs[sid].status == "running":
                    pass
                elif sid and Checkpoint.load(rd, sid):
                    try:
                        await resume_session(sid)
                    except HTTPException as e:
                        log.warning("серия %s, часть %d: не продолжилась: %s", series_id, n, e.detail)
                        return
                else:
                    req = RunRequest(**{**cur["request"], "question": p["question"], "attachments": []})
                    files = await asyncio.to_thread(series_atts, cur)
                    sid = await create_session(req, extra_atts=files, spec_extra={"objects": p.get("objects") or []},
                                               request_extra={"series": {
                        "id": series_id, "part": n, "total": len(cur["parts"]), "title": cur["title"]}})
                    fresh = SR.load(rd, series_id) or cur
                    for x in fresh["parts"]:
                        if x["n"] == n:
                            x["session_id"] = sid
                    SR.save(rd, fresh)
                st = runs.get(sid)
                if st is not None and st.task is not None:
                    try:
                        await asyncio.shield(st.task)
                    except BaseException:  # noqa: BLE001 — пауза или сбой части не останавливает остальные
                        pass

        await asyncio.gather(*(one(p["n"]) for p in s["parts"]), return_exceptions=True)
        s = SR.load(rd, series_id) or s
        v = series_view(s)
        log.info("серия %s: части готовы %d из %d", series_id, v["done"], v["total"])
        summ_id = s.get("summary_id") or ""
        if summ_id and summ_id not in runs and await asyncio.to_thread(Checkpoint.load, rd, summ_id):
            try:  # сводка встала на паузу — «Продолжить серию» продолжает и её (оплаченное не повторяется)
                await resume_session(summ_id)
            except HTTPException as e:
                log.warning("серия %s: сводный отчёт не продолжился: %s", series_id, e.detail)
            return
        if s["auto_summary"] and not summ_id and v["done"] == v["total"]:
            try:
                await start_summary(series_id)
            except HTTPException as e:
                log.warning("серия %s: сводный отчёт не запустился: %s", series_id, e.detail)

    def kick_series(series_id: str) -> None:
        t = series_tasks.get(series_id)
        if t is None or t.done():
            series_tasks[series_id] = asyncio.create_task(run_series(series_id))

    SUMMARY_TASK = """Сводный отчёт серии «{title}». Задание ниже проверялось по частям ({n} заседаний с одинаковыми
настройками); итоги частей и их реестры проверенных утверждений приложены (файлы «chast-N.md»), а проверенные в
частях утверждения уже стоят в общем реестре с номерами P1-…, P2-… и своими статусами.

Собери ОДИН отчёт по всему заданию в его формате:
- охвати все объекты всех частей — ни один не пропусти;
- общий ранг и приоритеты выстрой заново по всем объектам сразу: ранги частей считались внутри своей части и
  напрямую не сравнимы;
- статусы фактов переноси как есть: что не подтверждено в части, не становится подтверждённым в сводке;
- отметь противоречия между частями (одна компания — разные цифры) и не выбирай число произвольно;
- новых объектов не добавляй.

Объекты всех частей ({count}):
{objects}

## Исходное задание
{task}"""

    def part_doc(n: int, title: str, objects: list[str], d: dict[str, Any], room: int = 60_000) -> str:
        v = d.get("verdict") or {}
        led = (d.get("research") or {}).get("ledger") or {}
        ev = {e.get("id"): e.get("url", "") for e in led.get("evidence") or []}
        rows = []
        for c in led.get("claims") or []:
            if c.get("duplicate_of"):
                continue
            urls = " ".join(ev.get(e, "") for e in c.get("evidence") or [] if ev.get(e))
            rows.append(f"| P{n}-{c.get('id')} | {c.get('status')} | {str(c.get('text', '')).replace('|', '/')} | {urls} |")
        oc = led.get("objects") or {}
        head = (f"# Часть {n} серии: {title}\n\nОбъекты части ({len(objects)}): " + "; ".join(objects)
                + (f"\n\nОхват части: проверены {oc.get('checked')} из {oc.get('total')}; не проверены: "
                   + ", ".join(oc.get("missing") or []) if oc.get("total") else "") + "\n\n## Итог части\n\n")
        table = ("\n\n## Реестр проверенных утверждений части\n\n| № | статус | утверждение | источники |\n|---|---|---|---|\n"
                 + "\n".join(rows) + "\n")
        answer = str(v.get("final_answer") or "")
        room = max(2000, room - len(head))
        if len(answer) + len(table) <= room:
            return head + answer + table
        # не помещается: итог части важнее — реестр части и так целиком в общем реестре сводки (номера P{n}-…)
        note = f"\n\n(Реестр части не приложен — он целиком в общем реестре сводки, номера P{n}-….)\n"
        if len(answer) + len(note) > room:
            answer = answer[:room - len(note) - 80].rsplit("\n", 1)[0] + "\n\n…(итог части сокращён по объёму — полный в заседании части)"
        return head + answer + note

    async def start_summary(series_id: str) -> str:
        """Сводный отчёт серии: совет (обычное исследование) по итогам частей и общему реестру."""
        from ..attachments import load_attachment
        rd = runs_dir(base_config())
        s = SR.load(rd, series_id)
        if not s:
            raise HTTPException(404, "серия не найдена")
        if s.get("summary_id") and part_state(s["summary_id"])["status"] not in ("missing", "error", "cancelled"):
            return s["summary_id"]  # сводка уже есть (идёт, на паузе или готова) — вторую не создаём
        done = [(p, p["session_id"]) for p in s["parts"] if p["session_id"] and archive().exists(p["session_id"])]
        if not done:
            raise HTTPException(400, "нет ни одной готовой части")
        from ..attachments import MAX_DOC_CHARS, MAX_TOTAL_DOC_CHARS
        atts, objects = [], []
        room = min(MAX_DOC_CHARS, MAX_TOTAL_DOC_CHARS // len(done) - 200)  # поровну: ни одна часть не обрезается целиком
        for p, sid in done:
            d = await asyncio.to_thread(archive().load, sid)
            objects += [o for o in p.get("objects") or [] if o not in objects]
            atts.append(load_attachment(part_doc(p["n"], p["title"], p.get("objects") or [], d, room).encode("utf-8"),
                                        f"chast-{p['n']}.md"))
        question = SUMMARY_TASK.format(title=s["title"], n=len(done), count=len(objects),
                                       objects="\n".join(f"- {o}" for o in objects) or "—", task=s.get("task") or "")
        base_rs = dict((s["request"] or {}).get("research") or {})
        rs = {**base_rs, "catalog": False, "facts_per_object": 1, "arbitration_claims": 10,
              "max_claims": max(int(base_rs.get("max_claims") or 8), min(80, len(objects) + 3))}
        req = RunRequest(**{**s["request"], "question": question, "mode": "research", "research": rs,
                            "research_words": 0, "web_search": []})
        sid = await create_session(req, extra_atts=atts, spec_extra={"inherit": [[p["n"], x] for p, x in done],
                                                                     "objects": objects},
                                   request_extra={"series": {"id": series_id, "summary": True, "title": s["title"],
                                                             "total": len(s["parts"])}})
        s = SR.load(rd, series_id) or s
        s["summary_id"] = sid
        SR.save(rd, s)
        log.info("серия %s: сводный отчёт %s (частей %d, объектов %d)", series_id, sid, len(done), len(objects))
        return sid

    @app.post("/api/split")
    async def split_plan(body: SplitAsk) -> dict[str, Any]:
        """Советник: анализ задания (объекты, факты, пределы режима) и разбиение на части — без запуска."""
        from .. import splitter as SP
        req = RunRequest(**{**{k: v for k, v in body.request.items() if k != "question"}, "question": body.question})
        cfg = build_config(req)
        client = make_client(cfg, req.mock)
        model = None if req.mock else (cfg.advisor.model or cfg.member(cfg.chair).model)
        rc = cfg.research
        try:
            if body.objects:  # пересмотр: объекты те же, группировка — по просьбе пользователя
                out = await SP.replan(body.question, client, model, body.objects, n_parts=body.parts,
                                      principle=body.principle, per_part=body.per_part or rc.objects_per_part,
                                      facts=body.facts or rc.facts_per_object, base_checks=rc.arbitration_claims,
                                      base_claims=rc.max_claims)
            else:
                out = await SP.plan(body.question, client, model, per_part=body.per_part or rc.objects_per_part,
                                    facts=body.facts or rc.facts_per_object, base_checks=rc.arbitration_claims,
                                    base_claims=rc.max_claims)
        finally:
            close = getattr(client, "aclose", None)
            if close:
                await close()
        out["model"] = model or "по правилам (демо)"
        log.info("разбиение задания: объектов %d, частей %d, проверка %s", len(out["objects"]), len(out["parts"]),
                 "ок" if out["checks"]["ok"] else out["checks"])
        return out

    @app.post("/api/series")
    async def series_create(body: SeriesRequest) -> dict[str, Any]:
        req0 = {k: v for k, v in body.request.items() if k not in ("question", "parent", "attachments")}
        uids = list(body.request.get("attachments") or [])
        if uids:
            await asyncio.to_thread(load_uploads, uids)  # файлы есть и читаются — иначе ошибка сразу, а не в части
        parts = [p.model_dump() for p in body.parts]
        for i, p in enumerate(parts):
            if not p["question"].strip():
                if not body.task.strip() or not p["objects"]:
                    raise HTTPException(400, f"часть {i + 1}: нет ни текста, ни объектов")
                p["question"] = SR.part_text(body.task, p["objects"], i + 1, len(parts))
        RunRequest(**{**req0, "question": parts[0]["question"]})  # те же проверки настроек, что у заседания
        s = await asyncio.to_thread(SR.create, runs_dir(base_config()), title=body.title, task=body.task,
                                    parts=parts, request=req0,
                                    max_parallel=body.max_parallel, auto_summary=body.auto_summary, origin="web")
        if uids:
            s["files"] = await asyncio.to_thread(keep_series_files, s["id"], uids)
            await asyncio.to_thread(SR.save, runs_dir(base_config()), s)
        kick_series(s["id"])
        return {"id": s["id"]}

    @app.get("/api/series")
    async def series_list() -> dict[str, Any]:
        items = await asyncio.to_thread(SR.all_series, runs_dir(base_config()))
        return {"items": [series_view(s) for s in items]}

    @app.get("/api/series/{series_id}")
    async def series_get(series_id: str) -> dict[str, Any]:
        s = await asyncio.to_thread(SR.load, runs_dir(base_config()), series_id)
        if not s:
            raise HTTPException(404, "серия не найдена")
        return series_view(s)

    @app.post("/api/series/{series_id}/continue")
    async def series_continue(series_id: str) -> dict[str, Any]:
        if not SR.load(runs_dir(base_config()), series_id):
            raise HTTPException(404, "серия не найдена")
        kick_series(series_id)
        return {"ok": True}

    @app.post("/api/series/{series_id}/summary")
    async def series_summary(series_id: str) -> dict[str, Any]:
        s = SR.load(runs_dir(base_config()), series_id)
        if not s:
            raise HTTPException(404, "серия не найдена")
        st0 = part_state(s["summary_id"])["status"] if s.get("summary_id") else ""
        if st0 in ("running", "done"):
            raise HTTPException(409, "сводный отчёт уже есть")
        if st0 in ("paused", "interrupted"):
            raise HTTPException(409, "сводный отчёт на паузе — нажмите «Продолжить серию» (оплаченное не повторится)")
        return {"id": await start_summary(series_id)}

    @app.post("/api/search-test")
    async def search_test(body: SearchTest | None = None) -> dict[str, Any]:
        """Один короткий запрос с поиском: работает ли поиск, цена, вернул ли он ссылки."""
        cfg = base_config()
        model = (body.model if body and body.model else "") or cfg.fact_check.model
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise HTTPException(400, "нет OPENROUTER_API_KEY")
        client = OpenRouterClient(key, timeout=cfg.timeout_s, max_retries=1)
        q = ("Find the current official price per 1M input tokens of one popular LLM API and cite the source. "
             'Reply as JSON: {"answer": "...", "sources": [{"url": "...", "date": "..."}]}')
        try:
            comp = await client.complete(model, [{"role": "user", "content": q}], temperature=0.1, max_tokens=800,
                                         web_search=True)
        except ProviderError as e:
            return {"ok": False, "model": model, "cost": float(getattr(e.spent, "cost", 0) or 0), "citations": [],
                    "error": str(e)[:400]}
        finally:
            await client.aclose()
        return {"ok": True, "model": comp.model or model, "cost": comp.cost, "citations": comp.citations[:10],
                "answer": comp.text[:600]}

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
        # Номер события — «запуск:номер». После паузы и продолжения события нумеруются заново, а браузер при
        # переподключении присылает номер из прошлого запуска: тогда отдаём всё с начала, а не ждём, пока новых
        # событий наберётся столько же (12.11 страница так висела на «нет связи», хотя заседание шло)
        epoch = str(int((st.checkpoint.spec.get("resumes") if st and st.checkpoint else 0) or 0))
        # браузер при переподключении шлёт Last-Event-ID; страница, переоткрывающая зависший поток сама, — ?after=
        last = request.headers.get("last-event-id", "") or request.query_params.get("after", "")
        run_id, _, num = last.rpartition(":")
        try:
            start = int(num) + 1 if last and run_id in (epoch, "") else 0
        except ValueError:
            start = 0
        if st is not None and start > len(st.events):
            start = 0

        def fmt(i: int, ev: dict[str, Any]) -> str:
            return f"id: {epoch}:{i}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

        async def gen():
            # первая строка — комментарий ~2 КБ: промежуточные буферы (прокси, сжатие) сразу пропускают поток дальше
            yield ":" + " " * 2048 + "\n\n"
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
                                 headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})

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
