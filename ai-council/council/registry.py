"""Реестр компаний по рынку — память «Отбора» между заседаниями (runs/registry/<рынок>.json).

Запись — одна компания: её сайт (ключ, если есть свой) или полное название, все встреченные названия, в каких
заданиях и заседаниях она была и чем закончилась её последняя проверка (статус, причина, «да» экспертов, дата).

Зачем:
- однажды найденная компания не теряется: новое заседание с тем же заданием берёт её из реестра (бесплатно);
- известный сайт не ищется заново (Sonar не платится второй раз);
- у любой компании видна история: «20.11: подходит», «19.11: не выяснено — нет города».
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from . import selection as S

log = logging.getLogger(__name__)

REGISTRY_DIR = "registry"
MAX_NAMES = 12
MAX_SESSIONS = 30

MARKETS = (
    ("china", r"кита[йе]|\bкнр\b|\bchin|中国|大陆"),
    ("turkey", r"турц|турецк|turkey|türkiye|turkish|türk"),
    ("russia", r"росси[ия]|\bрф\b|russia"),
    ("india", r"инди[яи]|индийск|india"),
    ("vietnam", r"вьетнам|vietnam"),
    ("korea", r"коре[яи]|корейск|korea"),
    ("japan", r"япони|японск|japan"),
    ("europe", r"европ|\beu\b|europe"),
    ("usa", r"\bсша\b|америк|\busa\b|united states"),
)


def market_of(text: str) -> str:
    """Рынок задания по его словам (первое совпадение); нет — «general»."""
    for key, rx in MARKETS:
        if re.search(rx, text or "", re.I):
            return key
    return "general"


def _path(runs_dir: str | Path, market: str) -> Path:
    return Path(runs_dir) / REGISTRY_DIR / f"{re.sub(r'[^a-z0-9_-]', '', market.lower()) or 'general'}.json"


def load(runs_dir: str | Path, market: str) -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_path(runs_dir, market).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(runs_dir: str | Path, market: str, reg: dict[str, dict[str, Any]]) -> None:
    p = _path(runs_dir, market)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(reg, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        log.warning("реестр компаний не сохранён: %s", e)


def _own(w: str) -> bool:
    return bool(S.site_of(w or "")) and not S.weak_source(w) and S.site_like(w)


def key_of(e: dict[str, Any]) -> str:
    """Ключ записи: свой сайт (бренд домена), иначе полное название без форм собственности."""
    w = e.get("website") or ""
    if _own(w):
        return "s:" + S.site_brand(S.site_of(w))
    return "n:" + S.full_name_key(e.get("name") or "")


def update(runs_dir: str | Path, market: str, tasks: set[str], session: str, entities: list[dict[str, Any]]) -> int:
    """Дописать в реестр всех кандидатов заседания с их статусом. Возвращает, сколько записей обновлено."""
    reg = load(runs_dir, market)
    # уже известные по названию — та же запись, даже если сайт появился только сейчас
    by_name = {S.full_name_key(n): k for k, r in reg.items() for n in r.get("names") or []}
    day = time.strftime("%Y-%m-%d")
    n = 0
    for e in entities:
        if e.get("seed") and not e.get("website"):
            continue
        k = key_of(e)
        if k == "n:":
            continue
        k = k if k in reg else next((by_name[x] for x in (S.full_name_key(y) for y in e.get("names") or [e["name"]])
                                     if x in by_name), k)
        r = reg.setdefault(k, {"name": e["name"], "names": [], "website": "", "tasks": [], "sessions": []})
        r["names"] = list(dict.fromkeys([*r["names"], e["name"], *(e.get("names") or [])]))[:MAX_NAMES]
        if _own(e.get("website") or "") and not r.get("website"):
            r["website"] = e["website"]
        r["tasks"] = sorted(set(r.get("tasks") or []) | tasks)[:20]
        r["sessions"] = list(dict.fromkeys([*(r.get("sessions") or []), session]))[-MAX_SESSIONS:]
        sc = e.get("screen") or {}
        if e.get("status") or sc:
            r["last"] = {"status": e.get("status") or "", "reason": (sc.get("reason") or "")[:200],
                         "expert_yes": list((e.get("expert") or {}).get("yes") or [])[:5], "date": day,
                         "session": session}
        n += 1
    _save(runs_dir, market, reg)
    return n


def finds_for(runs_dir: str | Path, market: str, tasks: set[str]) -> list[tuple[str, S.Candidate]]:
    """Компании реестра из заданий с тем же ключом — как находки «реестр рынка» (только название и сайт)."""
    out = []
    for r in load(runs_dir, market).values():
        if not tasks & set(r.get("tasks") or []):
            continue
        names = [x for x in r.get("names") or [] if x]
        if not names:
            continue
        lat = next((x for x in names if not S.CJK.search(x)), "")
        loc = next((x for x in names if S.CJK.search(x)), "")
        try:
            out.append((f"реестр рынка{S.BY}{(r.get('sessions') or [''])[-1][:15]}",
                        S.Candidate(name=lat or loc, local_name=loc if lat else "", website=r.get("website") or "")))
        except ValueError:
            continue
    return out


def fill_sites(runs_dir: str | Path, market: str, entities: list[dict[str, Any]]) -> int:
    """Карточкам без своего сайта — сайт из реестра (по названию): не ищем его второй раз."""
    reg = load(runs_dir, market)
    site = {S.full_name_key(n): r["website"] for r in reg.values() if r.get("website") for n in r.get("names") or []}
    n = 0
    for e in entities:
        if _own(e.get("website") or ""):
            continue
        hit = next((site[k] for k in (S.full_name_key(x) for x in [e["name"], *(e.get("names") or [])]) if len(k) >= 6
                    and k in site), "")
        if hit:
            e["website"] = hit
            n += 1
    return n


def history(runs_dir: str | Path, market: str, entities: list[dict[str, Any]]) -> None:
    """Последняя проверка компании в прежних заседаниях — в e["history"] («20.11: подходит»)."""
    reg = load(runs_dir, market)
    by = {S.full_name_key(n): r for r in reg.values() for n in r.get("names") or []}
    ru = {"fit": "подходит", "likely": "вероятно подходит", "unfit": "не подходит", "unknown": "неизвестно"}
    for e in entities:
        r = by.get(S.full_name_key(e["name"])) or next((by[k] for k in (S.full_name_key(x) for x in e.get("names") or [])
                                                          if k in by), None)
        last = (r or {}).get("last") or {}
        if last.get("status"):
            e["history"] = f"{last.get('date', '')}: {ru.get(last['status'], last['status'])}"
