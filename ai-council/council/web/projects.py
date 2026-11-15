"""Сайт: «Проекты закупки» (council/procurement.py) — создание из «Отбора», правка, письма-запросы, разбор ответов,
сравнение, предупреждения, перепроверка; раз в несколько часов — предупреждения в Telegram (если бот настроен)."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .. import procurement as P
from ..client import ProviderError
from ..config import with_overrides
from ..session import Council, InvalidResponse

log = logging.getLogger("council.web")
ACTION_BUDGET = 0.5      # одно действие (письмо, разбор ответа) — не дороже
ALERT_EVERY_S = 6 * 3600  # как часто проверять предупреждения проектов


class NewProject(BaseModel):
    session: str
    ids: list[str] | None = None
    top: int = 0
    title: str = ""


class ProjectEdit(BaseModel):
    title: str | None = None
    company: str | None = None
    skus: list[dict[str, str]] | None = None
    documents_required: list[str] | None = None
    rates: dict[str, float] | None = None
    base_currency: str | None = None
    suppliers: dict[str, dict[str, Any]] | None = None   # id -> {contact, status, notes}
    remove: list[str] | None = None                       # убрать поставщиков
    letters: dict[str, str] | None = None                 # правка текста писем


class RfqBody(BaseModel):
    mock: bool = False


class ReplyBody(BaseModel):
    text: str = ""
    attachments: list[str] = Field(default_factory=list)
    mock: bool = False


def register(app: FastAPI, *, base_config: Callable, runs_dir: Callable, archive: Callable, load_uploads: Callable,
             make_client: Callable, create_session: Callable, jsonable: Callable) -> None:
    def rdir():
        return runs_dir(base_config())

    locks: dict[str, asyncio.Lock] = {}

    def lock(pid: str) -> asyncio.Lock:
        """Правки одного проекта — по очереди: два ответа заводов подряд не должны затереть друг друга."""
        return locks.setdefault(pid, asyncio.Lock())

    async def show(p: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(view, p)  # читает архив перепроверки — не в основном потоке сайта

    def get(pid: str) -> dict[str, Any]:
        try:
            return P.load(rdir(), pid)
        except (KeyError, OSError, ValueError) as e:
            raise HTTPException(404, "проект не найден") from e

    def view(p: dict[str, Any]) -> dict[str, Any]:
        out = dict(p)
        out["alerts"] = P.alerts(p)
        out["compare"] = P.compare(p)
        out["status_names"] = P.STATUS_RU
        for s in out.get("suppliers") or []:
            for d in s.get("documents") or []:
                d["flags"] = P.doc_flags(d, s)
            s["letter_en"] = P.letter_for(p, s, "en") if p.get("letters") else None
            s["letter_zh"] = P.letter_for(p, s, "zh") if p.get("letters") else None
        last = (p.get("rechecks") or [])[-1:] or []
        if last:
            try:
                d = archive().load(last[0]["session"])
                out["recheck_diff"] = P.diff(p, d) if d.get("selection") else None
                out["recheck_done"] = bool(d.get("selection"))
            except (KeyError, OSError, ValueError):
                out["recheck_done"] = False
        return jsonable(out)

    async def ask(prompt: str, schema: type, task: str, mock: bool, images: list | None = None) -> Any:
        """Один запрос модели председателя с маленьким бюджетом (письмо или разбор ответа)."""
        cfg = with_overrides(base_config(), budget=ACTION_BUDGET)
        client = make_client(cfg, mock)
        c = Council(cfg, client, session_id=f"project-{task}")
        chair = cfg.member(cfg.chair)
        content = None
        if images:
            from ..attachments import image_parts
            content = [{"type": "text", "text": prompt}, *image_parts(images)]
        try:
            return await c._ask(chair, f"procure_{task}", prompt, schema, meta={"procurement": True, "task": task},
                                content=content, max_tokens=6000), c.total_cost
        except (ProviderError, InvalidResponse) as e:
            raise HTTPException(502, f"модель не ответила: {e}") from e
        finally:
            await client.aclose()

    async def load_session(sid: str) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(archive().load, sid)
        except (KeyError, OSError, ValueError):
            return {}

    @app.get("/api/projects")
    async def projects_list() -> dict[str, Any]:
        return {"items": await asyncio.to_thread(P.list_all, rdir())}

    @app.post("/api/projects")
    async def project_new(body: NewProject) -> dict[str, Any]:
        d = await load_session(body.session)
        if not d or not d.get("selection"):
            raise HTTPException(400, "проект создаётся из заседания «Отбора»")
        try:
            p = P.create_from_selection(d, body.session, ids=body.ids, top=body.top, title=body.title)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        await asyncio.to_thread(P.save, rdir(), p)
        return {"id": p["id"]}

    @app.get("/api/projects/{pid}")
    async def project_get(pid: str) -> dict[str, Any]:
        return await show(await asyncio.to_thread(get, pid))

    @app.put("/api/projects/{pid}")
    async def project_edit(pid: str, body: ProjectEdit) -> dict[str, Any]:
        async with lock(pid):
            return await _edit(pid, body)

    async def _edit(pid: str, body: ProjectEdit) -> dict[str, Any]:
        p = await asyncio.to_thread(get, pid)
        for k in ("title", "company", "skus", "documents_required", "rates", "base_currency"):
            v = getattr(body, k)
            if v is not None:
                p[k] = v
        if body.letters:
            p["letters"] = {**(p.get("letters") or {}), **{k: v for k, v in body.letters.items()
                                                         if k in P.RfqLetters.model_fields}}
        for sid, upd in (body.suppliers or {}).items():
            try:
                s = P.supplier(p, sid)
            except KeyError:
                continue
            for k in ("contact", "notes"):
                if k in upd:
                    s[k] = str(upd[k])[:2000]
            if upd.get("status") in P.STATUS_RU:
                if upd["status"] == "sent" and s.get("status") != "sent":
                    s["sent_at"] = P.now()
                s["status"] = upd["status"]
        if body.remove:
            p["suppliers"] = [s for s in p["suppliers"] if s["id"] not in set(body.remove)]
        await asyncio.to_thread(P.save, rdir(), p)
        return await show(p)

    @app.delete("/api/projects/{pid}")
    async def project_delete(pid: str) -> dict[str, Any]:
        await asyncio.to_thread(get, pid)
        await asyncio.to_thread(P.delete, rdir(), pid)
        return {"ok": True}

    @app.post("/api/projects/{pid}/rfq")
    async def project_rfq(pid: str, body: RfqBody) -> dict[str, Any]:
        """Письмо-запрос цен (английский и китайский) по SKU и списку документов проекта — одним запросом модели."""
        p = await asyncio.to_thread(get, pid)
        letters, cost = await ask(P.rfq_prompt(p, p.get("company", "")), P.RfqLetters, "rfq", body.mock)
        async with lock(pid):  # пока модель писала, проект могли править — сохраняем поверх свежей версии
            p = await asyncio.to_thread(get, pid)
            p["letters"] = letters.model_dump()
            p["spent"] = round(float(p.get("spent") or 0) + cost, 4)
            await asyncio.to_thread(P.save, rdir(), p)
        return await show(p)

    @app.post("/api/projects/{pid}/suppliers/{sup}/reply")
    async def project_reply(pid: str, sup: str, body: ReplyBody) -> dict[str, Any]:
        """Ответ завода (текст письма и/или файлы: прайс, PDF, фото сертификата) → цены по SKU и документы."""
        p = await asyncio.to_thread(get, pid)
        try:
            s = P.supplier(p, sup)
        except KeyError as e:
            raise HTTPException(404, "поставщик не найден") from e
        atts = await asyncio.to_thread(load_uploads, body.attachments) if body.attachments else []
        docs = "\n\n".join(f"=== {a.name} ===\n{a.text}" for a in atts if a.kind == "document" and a.text)
        images = [a for a in atts if a.kind == "image"]
        text = "\n\n".join(x for x in (body.text.strip(), docs) if x)
        if not text and not images:
            raise HTTPException(400, "вставьте текст ответа или приложите файл")
        parsed, cost = await ask(P.reply_prompt(p, s, text or "(see images)"), P.ReplyParsed, "reply", body.mock,
                                 images=images)
        async with lock(pid):  # ответы двух заводов разбираются одновременно — дописываем каждый в свежую версию
            p = await asyncio.to_thread(get, pid)
            try:
                P.add_reply(p, sup, parsed, source=", ".join(a.name for a in atts) or "текст письма", text=text)
            except KeyError as e:
                raise HTTPException(404, "поставщика уже убрали из проекта") from e
            p["spent"] = round(float(p.get("spent") or 0) + cost, 4)
            await asyncio.to_thread(P.save, rdir(), p)
        return await show(p)

    @app.post("/api/projects/{pid}/recheck")
    async def project_recheck(pid: str, body: RfqBody) -> dict[str, Any]:
        """Перепроверка: новое заседание «Отбора» по поставщикам проекта (проверить список, без остановок);
        когда оно закончится, проект покажет, что изменилось."""
        p = await asyncio.to_thread(get, pid)
        src = await load_session(p["source"]) if p.get("source") else {}
        req0 = (src or {}).get("request") or {}
        from .app import RunRequest
        req = RunRequest(question=P.recheck_question(p), mode="selection", mock=body.mock or bool(req0.get("mock")),
                         members=req0.get("members") or None, chair=req0.get("chair") or None,
                         selection={"start": "check", "review": []}, budget=req0.get("budget") or None)
        sid = await create_session(req)
        async with lock(pid):
            p = await asyncio.to_thread(get, pid)
            p.setdefault("rechecks", []).append({"session": sid, "at": P.now()})
            await asyncio.to_thread(P.save, rdir(), p)
        return {"session": sid}

    async def alerts_loop() -> None:
        """Раз в несколько часов: новые предупреждения проектов — сообщением в Telegram владельцу бота."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        users = [u.strip() for u in os.environ.get("TELEGRAM_ALLOWED_USERS", "").replace(";", ",").split(",") if u.strip()]
        if not token or not users:
            return
        base = os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
        import httpx
        while True:
            try:
                for item in await asyncio.to_thread(P.list_all, rdir()):
                    p = await asyncio.to_thread(P.load, rdir(), item["id"])
                    new = [a for a in P.alerts(p) if a["key"] not in set(p.get("notified") or [])]
                    if not new:
                        continue
                    text = f"📦 Проект закупки «{p['title'][:80]}»:\n" + "\n".join(f"• {a['text']}" for a in new[:15])
                    async with httpx.AsyncClient(timeout=20) as h:
                        for u in users:
                            await h.post(f"{base}/bot{token}/sendMessage", json={"chat_id": u, "text": text})
                    async with lock(p["id"]):
                        fresh = await asyncio.to_thread(P.load, rdir(), p["id"])
                        fresh["notified"] = list(dict.fromkeys((fresh.get("notified") or []) + [a["key"] for a in new]))[-500:]
                        await asyncio.to_thread(P.save, rdir(), fresh)
            except Exception as e:  # noqa: BLE001 — уведомления не должны ронять сайт
                log.warning("предупреждения проектов не отправлены: %r", e)
            await asyncio.sleep(ALERT_EVERY_S)

    app.state.startup = [*getattr(app.state, "startup", []), alerts_loop]
