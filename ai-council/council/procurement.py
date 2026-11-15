"""Проект закупки: что делать с результатом «Отбора» дальше.

Поставщики — из таблицы отбора (первая группа или выбранные). Здесь: письма-запросы цен (RFQ) на английском и
китайском, разбор ответов заводов (письмо, прайс, PDF, фото сертификата) в цены по SKU и документы, сверка документов
с тем, что найдено при отборе (держатель, срок), сравнение котировок, предупреждения (документ истёк или истекает,
завод молчит), перепроверка таблицы и что в ней изменилось. Хранится в runs/projects/<id>.json.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from . import selection as S
from .schemas import to_list, to_text

STATUS_RU = {"new": "не писали", "sent": "запрос отправлен", "replied": "ответил", "declined": "отказался",
             "paused": "отложен"}
DOC_KINDS = ("SC", "GMP", "ISO 22000", "FSSC 22000", "BRCGS", "HACCP", "NSF", "Halal", "Kosher", "ISO 9001", "other")
EXPIRY_DAYS = 60       # документ истекает в ближайшие N дней — предупреждение
SILENCE_DAYS = 7       # запрос отправлен N дней назад, ответа нет — напомнить


def _t(v: Any) -> str:
    return to_text(v).strip()


# ------------------------------------------------------------------------------------------------ схемы ответов

class RfqLetters(BaseModel):
    subject_en: str = ""
    body_en: str = ""
    subject_zh: str = ""
    body_zh: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return _t(v)


class QuoteItem(BaseModel):
    sku: str = ""
    price: str = ""
    currency: str = ""
    unit: str = ""            # за что цена: упаковка, 1000 капсул, кг…
    moq: str = ""
    incoterms: str = ""
    lead_time: str = ""       # срок изготовления
    note: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return _t(v)


class DocItem(BaseModel):
    kind: str = ""            # SC, GMP, ISO 22000, NSF…
    number: str = ""
    issuer: str = ""          # кто выдал
    holder: str = ""          # на кого выдан (юрлицо)
    address: str = ""
    valid_until: str = ""
    scope: str = ""           # область: формы, продукты
    quote: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return _t(v)


class ReplyParsed(BaseModel):
    items: list[QuoteItem] = Field(default_factory=list)
    documents: list[DocItem] = Field(default_factory=list)
    contact: str = ""
    summary: str = ""
    questions: list[str] = Field(default_factory=list)   # что завод не ответил или спросил сам

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            for a, b in (("prices", "items"), ("quotes", "items"), ("docs", "documents"), ("certificates", "documents")):
                if a in data and b not in data:
                    data[b] = data.pop(a)
        return data

    @field_validator("contact", "summary", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return _t(v)

    @field_validator("questions", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return [x for x in to_list(v) if x.strip()]


# ------------------------------------------------------------------------------------------------ хранение

def projects_dir(runs_dir: Path | str) -> Path:
    return Path(runs_dir) / "projects"


def _path(runs_dir: Path | str, pid: str) -> Path:
    if not re.fullmatch(r"[0-9a-z-]{4,60}", pid or ""):
        raise KeyError(pid)
    return projects_dir(runs_dir) / f"{pid}.json"


def load(runs_dir: Path | str, pid: str) -> dict[str, Any]:
    p = _path(runs_dir, pid)
    if not p.is_file():
        raise KeyError(pid)
    return json.loads(p.read_text(encoding="utf-8"))


def save(runs_dir: Path | str, project: dict[str, Any]) -> dict[str, Any]:
    project["updated"] = now()
    p = _path(runs_dir, project["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(project, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return project


def list_all(runs_dir: Path | str) -> list[dict[str, Any]]:
    out = []
    for p in sorted(projects_dir(runs_dir).glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"id": d["id"], "title": d.get("title", ""), "suppliers": len(d.get("suppliers") or []),
                    "replied": sum(1 for x in d.get("suppliers") or [] if x.get("status") == "replied"),
                    "alerts": len(alerts(d)), "updated": d.get("updated", ""), "source": d.get("source", "")})
    return out


def delete(runs_dir: Path | str, pid: str) -> None:
    _path(runs_dir, pid).unlink(missing_ok=True)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def snapshot_cells(sel: dict[str, Any], oid: str) -> dict[str, dict[str, Any]]:
    """Ячейки объекта из отбора — по названию признака (для сверки и «что изменилось»)."""
    names = {a["id"]: a["name"] for a in sel.get("attributes") or []}
    row = (sel.get("table") or {}).get(oid) or {}
    return {names.get(aid, aid): {k: c.get(k, "") for k in ("value", "unit", "status", "url", "as_of")}
            for aid, c in row.items() if c.get("status") not in (None, "none")}


def create_from_selection(d: dict[str, Any], session_id: str, *, ids: list[str] | None = None, top: int = 0,
                          title: str = "") -> dict[str, Any]:
    """Новый проект из заседания «Отбора»: поставщики — выбранные или первая группа ранга."""
    sel = d.get("selection") or {}
    if not sel:
        raise ValueError("это не заседание «Отбора»")
    by_id = {o["id"]: o for o in sel.get("objects") or []}
    rank = {r["object"]: r for r in sel.get("ranking") or []}
    order = [r["object"] for r in sel.get("ranking") or []]
    chosen = [i for i in ids or [] if i in by_id] or order[:top or sel.get("top_n") or len(order)]
    suppliers = []
    for oid in chosen:
        o = by_id[oid]
        suppliers.append({"id": oid, "name": o["name"], "names": o.get("names") or [o["name"]],
                          "website": o.get("website", ""), "location": o.get("location", ""),
                          "rank": rank.get(oid, {}).get("rank"), "score": rank.get(oid, {}).get("score"),
                          "cells": snapshot_cells(sel, oid), "contact": "", "status": "new", "notes": "",
                          "sent_at": "", "replies": [], "documents": []})
    concl = sel.get("conclusion") or {}
    return {"id": f"p{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(3)}",
            "title": title or (d.get("refined_question") or d.get("question") or "Проект закупки")[:120],
            "source": session_id, "created": now(), "updated": now(), "suppliers": suppliers,
            "skus": [], "documents_required": ["SC (лицензия на производство) с приложением",
                                               "GMP / ISO 22000 / FSSC 22000 — номер, орган, срок",
                                               "COA на типовую партию"],
            "rates": {}, "base_currency": "USD", "letters": {}, "rechecks": [], "notified": [],
            "next_steps": concl.get("next_steps") or [], "attributes": [a["name"] for a in sel.get("attributes") or []]}


# ------------------------------------------------------------------------------------------------ задания моделям

def rfq_prompt(p: dict[str, Any], company: str) -> str:
    skus = "\n".join(f"- {x.get('name', '')}: {x.get('spec', '')}" + (f"; volumes: {x['volumes']}" if x.get("volumes") else "")
                     for x in p.get("skus") or []) or "- (the buyer will attach the SKU list)"
    docs = "\n".join(f"- {x}" for x in p.get("documents_required") or [])
    return f"""Write a request for quotation (RFQ) letter from a buyer to contract manufacturers. One template, sent to each
supplier separately: use the placeholder {{supplier}} for the supplier's name. Buyer: {company or "{company}"}.
Purpose: private-label products; the buyer compares several suppliers on the SAME specification.

SKUs (identical for every supplier — prices must be comparable):
{skus}

Ask for: price per unit for each SKU at each volume, currency, Incoterms, MOQ, lead time for the first and repeat
batches, shelf life, packaging options; what is included (raw materials, packaging, tests). Documents BEFORE price talks:
{docs}
Ask to state the exact legal entity and address of the factory that will produce, and whether production is
outsourced. Ask to reply in a table. Polite, short, business tone. No invented facts about the buyer.

Return JSON: {{"subject_en": "...", "body_en": "...", "subject_zh": "...", "body_zh": "... (natural business Chinese)"}}"""


def reply_prompt(p: dict[str, Any], sup: dict[str, Any], text: str) -> str:
    skus = "\n".join(f"- {x.get('name', '')}: {x.get('spec', '')}" for x in p.get("skus") or []) or "- (not set)"
    return f"""A supplier answered our request for quotation. Extract prices and documents EXACTLY as written — no
conversion, no guesses. The text may be an e-mail, a price list or an OCR of a certificate (any language).

Supplier: {sup['name']} (also: {', '.join(sup.get('names') or [])}); site {sup.get('website') or '—'}
Our SKUs:
{skus}

Supplier's reply (and attached files, if any):
\"\"\"
{text[:60000]}
\"\"\"

Return JSON: {{"items": [{{"sku": "<our SKU name it refers to, or their product name>", "price": "number as written",
"currency": "USD|CNY|EUR|RUB…", "unit": "per bottle / per 1000 capsules / per kg…", "moq": "", "incoterms": "",
"lead_time": "", "note": "conditions, volume tier"}}], "documents": [{{"kind": "SC|GMP|ISO 22000|FSSC 22000|BRCGS|HACCP|
NSF|Halal|Kosher|ISO 9001|other", "number": "", "issuer": "", "holder": "legal entity it is issued to", "address": "",
"valid_until": "YYYY-MM-DD if given", "scope": "", "quote": "verbatim fragment"}}], "contact": "name, e-mail, phone",
"summary": "2–3 lines in Russian: what they offered and on what terms", "questions": ["what they did not answer or asked us"]}}"""


def letter_for(p: dict[str, Any], sup: dict[str, Any], lang: str = "en") -> dict[str, str]:
    L = p.get("letters") or {}
    subj, body = L.get(f"subject_{lang}", ""), L.get(f"body_{lang}", "")
    name = sup.get("names", [sup["name"]])[1] if lang == "zh" and len(sup.get("names") or []) > 1 else sup["name"]
    return {"subject": subj.replace("{supplier}", name), "body": body.replace("{supplier}", name)}


# ------------------------------------------------------------------------------------------------ ответы и проверки

def doc_flags(doc: dict[str, Any], sup: dict[str, Any], today: date | None = None) -> list[str]:
    """Что не так с документом: другое юрлицо, истёк или скоро истечёт, нет номера или срока."""
    today = today or date.today()
    out = []
    if doc.get("holder"):
        h = S.norm_name(doc["holder"])
        names = [S.norm_name(x) for x in sup.get("names") or [sup["name"]] if S.norm_name(x)]
        if h and names and not any(n in h or h in n for n in names):
            out.append(f"выдан на другое юрлицо: {doc['holder']}")
    v = S.parse_date(doc.get("valid_until", ""))
    if v is not None:
        t = today.year + (today.month - 1) / 12 + (today.day - 1) / 365
        if v < t:
            out.append(f"истёк {doc['valid_until']}")
        elif v - t < EXPIRY_DAYS / 365:
            out.append(f"истекает {doc['valid_until']}")
    elif doc.get("kind") in ("SC", "GMP", "ISO 22000", "FSSC 22000", "BRCGS", "NSF", "HACCP"):
        out.append("срок действия не указан")
    if not doc.get("number") and doc.get("kind") not in ("", "other"):
        out.append("нет номера")
    return out


def add_reply(p: dict[str, Any], sup_id: str, parsed: ReplyParsed, *, source: str, text: str) -> dict[str, Any]:
    sup = supplier(p, sup_id)
    entry = {"at": now(), "source": source, "summary": parsed.summary, "contact": parsed.contact,
             "items": [i.model_dump() for i in parsed.items], "questions": parsed.questions, "text": text[:20000]}
    sup["replies"].append(entry)
    for d in parsed.documents:
        doc = d.model_dump()
        doc["flags"] = doc_flags(doc, sup)
        doc["from"] = entry["at"]
        sup["documents"] = [x for x in sup["documents"] if not (x.get("kind") == doc["kind"] and x.get("number") and
                                                              x.get("number") == doc["number"])] + [doc]
    if parsed.contact and not sup.get("contact"):
        sup["contact"] = parsed.contact
    if sup.get("status") in ("new", "sent", "paused"):
        sup["status"] = "replied"
    return entry


def supplier(p: dict[str, Any], sup_id: str) -> dict[str, Any]:
    for s in p.get("suppliers") or []:
        if s["id"] == sup_id:
            return s
    raise KeyError(sup_id)


def _price(item: dict[str, Any]) -> float | None:
    return S.parse_number(item.get("price", ""))


def compare(p: dict[str, Any]) -> list[dict[str, Any]]:
    """Сравнение котировок по SKU: последняя цена каждого поставщика; в базовой валюте, если задан курс;
    лучшая цена отмечена. Разные единицы («за флакон» и «за 1000 капсул») не сравниваются — показываются как есть."""
    rates = {k.upper(): float(v) for k, v in (p.get("rates") or {}).items() if v}
    base = (p.get("base_currency") or "USD").upper()
    by_sku: dict[str, list[dict[str, Any]]] = {}
    sku_names = [x.get("name", "") for x in p.get("skus") or []]
    for sup in p.get("suppliers") or []:
        latest: dict[str, dict[str, Any]] = {}
        for r in sup.get("replies") or []:
            for it in r.get("items") or []:
                key = _match_sku(it.get("sku", ""), sku_names)
                latest[key] = it
        for key, it in latest.items():
            pr = _price(it)
            cur = (it.get("currency") or "").upper()
            conv = None
            if pr is not None:
                if cur == base:
                    conv = pr
                elif cur in rates:
                    conv = pr * rates[cur]
            by_sku.setdefault(key, []).append({"supplier": sup["id"], "name": sup["name"], **it, "value": pr,
                                               "base": round(conv, 4) if conv is not None else None})
    out = []
    for key, rows in by_sku.items():
        units = {S.plain(r.get("unit", "")) for r in rows if r.get("base") is not None}
        comparable = len(units) <= 1
        vals = [r["base"] for r in rows if r.get("base") is not None]
        best = min(vals) if vals and comparable else None
        for r in rows:
            r["best"] = best is not None and r.get("base") == best
        rows.sort(key=lambda r: (r.get("base") is None, r.get("base") or 0))
        out.append({"sku": key, "rows": rows, "comparable": comparable, "base_currency": base})
    return out


def _match_sku(name: str, skus: list[str]) -> str:
    n = S.plain(name)
    for s in skus:
        ps = S.plain(s)
        if ps and (ps == n or ps in n or n in ps):
            return s
    return name or "—"


def alerts(p: dict[str, Any], today: date | None = None) -> list[dict[str, Any]]:
    """Предупреждения проекта: документы истекли или истекают, выданы на другое юрлицо, завод молчит."""
    today = today or date.today()
    out = []
    for sup in p.get("suppliers") or []:
        for d in sup.get("documents") or []:
            for f in doc_flags(d, sup, today):
                if f.startswith(("истёк", "истекает", "выдан на другое")):
                    out.append({"key": f"{sup['id']}|{d.get('kind')}|{d.get('number')}|{f}", "supplier": sup["name"],
                                "level": "bad" if f.startswith(("истёк", "выдан")) else "warn",
                                "text": f"{sup['name']}: {d.get('kind') or 'документ'} {d.get('number') or ''} — {f}".replace("  ", " ")})
        if sup.get("status") == "sent" and sup.get("sent_at"):
            try:
                sent = datetime.fromisoformat(sup["sent_at"]).date()
            except ValueError:
                sent = today
            days = (today - sent).days
            if days >= SILENCE_DAYS:
                out.append({"key": f"{sup['id']}|silence|{sup['sent_at'][:10]}", "supplier": sup["name"], "level": "warn",
                            "text": f"{sup['name']}: нет ответа {days} дн. после запроса — напомнить"})
        # срок из таблицы отбора (например, «сертификат действует до»)
        for attr, c in (sup.get("cells") or {}).items():
            if re.search(r"срок|до|valid|expir|действ", attr, re.I) and c.get("value"):
                v = S.parse_date(c["value"])
                t = today.year + (today.month - 1) / 12 + (today.day - 1) / 365
                if v is not None and v < t + EXPIRY_DAYS / 365:
                    word = "истёк" if v < t else "истекает"
                    out.append({"key": f"{sup['id']}|cell|{attr}|{c['value']}", "supplier": sup["name"], "level": "warn",
                                "text": f"{sup['name']}: «{attr}» {word} ({c['value']}, по данным отбора — запросить актуальный)"})
    return out


def recheck_question(p: dict[str, Any]) -> str:
    """Задание для перепроверки таблицы: те же признаки, только поставщики проекта."""
    rows = "\n".join(f"| {i} | {s['name']} |" + (f" {s['website']} |" if s.get("website") else " |")
                     for i, s in enumerate(p.get("suppliers") or [], 1))
    attrs = ", ".join(p.get("attributes") or [])
    return (f"Перепроверка проекта закупки «{p.get('title', '')}»: сравнить объекты по тем же признакам ({attrs}), "
            f"обновить значения с источниками.\n\n| № | Объект | Сайт |\n|---|---|---|\n{rows}")


def diff(p: dict[str, Any], d: dict[str, Any]) -> list[dict[str, Any]]:
    """Что изменилось при перепроверке: новые, пропавшие и другие значения ячеек (по названию объекта и признака)."""
    sel = d.get("selection") or {}
    by_name = {S.norm_name(o["name"]): o["id"] for o in sel.get("objects") or []}
    out = []
    for sup in p.get("suppliers") or []:
        oid = by_name.get(S.norm_name(sup["name"]))
        if not oid:
            continue
        new = snapshot_cells(sel, oid)
        old = sup.get("cells") or {}
        kinds = {a["name"]: a.get("kind", "text") for a in sel.get("attributes") or []}
        for attr in sorted(set(old) | set(new)):
            a, b = old.get(attr), new.get(attr)
            if a and b and S.values_agree(a, b, kinds.get(attr, "text")):
                continue
            if not a and not b:
                continue
            out.append({"supplier": sup["name"], "attr": attr, "old": (a or {}).get("value", ""),
                        "new": (b or {}).get("value", ""), "status": (b or {}).get("status", "none"),
                        "url": (b or {}).get("url", "")})
    return out
