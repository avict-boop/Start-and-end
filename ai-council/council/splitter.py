"""Разбиение большого задания на части серии — без потерь.

Модель (советник или председатель) только находит объекты задания и группирует их по смыслу. Тексты частей
собирает программа: в каждую часть идёт ПОЛНОЕ задание без сокращений плюс рамка «в этой части — только эти
объекты» (series.part_text). Поэтому из задания ничего не может потеряться; пропасть мог бы только объект, который
модель не заметила, — это проверяют программа (каждый объект ровно в одной части) и вторая модель («каких объектов
задания нет в списке»).
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from . import series as SR

MAX_OBJECTS = 300  # столько объектов задания программа держит без потерь (постановка задачи — столько же)

# Пределы режима «Исследование», которые советник учитывает (держать в согласии с config.ResearchConfig)
LIMITS = {"checks": 100, "claims": 80, "requirements": 40, "subquestions": 5, "per_part_default": 8}


class Group(BaseModel):
    title: str = ""
    objects: list[str] = Field(default_factory=list)


class SplitPlan(BaseModel):
    objects: list[str] = Field(default_factory=list)
    facts_per_object: int = 0  # 0 — модель не сказала: берётся из настроек («Фактов на объект»)
    one_session: bool = False
    reason: str = ""
    groups: list[Group] = Field(default_factory=list)

    @field_validator("objects", mode="before")
    @classmethod
    def _objs(cls, v: Any) -> list[str]:
        out = []
        for x in v if isinstance(v, list) else []:
            t = (x.get("name") or x.get("object") or "") if isinstance(x, dict) else str(x)
            t = t.strip()
            if t and t not in out:
                out.append(t[:200])
        return out[:MAX_OBJECTS]


class Missing(BaseModel):
    missing: list[str] = Field(default_factory=list)


def split_prompt(task: str, per_part: int, facts: int) -> str:
    return f"""You plan how to run a large research task in an "AI council" app. One council session can check at most
{LIMITS['checks']} claims by web search, each author lists at most {LIMITS['claims']} claims, quality drops when one
answer must cover many objects. A session works well with about {per_part} objects at {facts} facts each.

Task:
<<<
{task}
>>>

1. "objects": every specific object the task GIVES to cover (companies, products, sites, brands, categories) — copy
   each as a short identifying name exactly as in the task (keep the original-language and the English name, e.g.
   "山东禹王制药 / Shandong Yuwang"). Only objects listed in the task, never new ones. If the task asks to FIND
   objects rather than listing them, return an empty list.
2. "facts_per_object": how many facts the task needs checked about each object (1-5).
3. "one_session": true if the whole task fits one session well (few objects), else false; "reason": one sentence.
4. "groups": if not one session — split the objects into parts of about {per_part}, grouped by meaning (category,
   product type, role in the task) so that each part can be compared inside itself; every object in exactly one
   group; a short "title" per group in the task's language.

Reply with ONE JSON object:
{{"objects": ["..."], "facts_per_object": 3, "one_session": false, "reason": "...",
  "groups": [{{"title": "...", "objects": ["..."]}}]}}"""


def regroup_prompt(task: str, objects: list[str], n_parts: int | None, per_part: int, principle: str) -> str:
    """Пересмотр разбиения по просьбе пользователя: объекты уже найдены — только сгруппировать заново."""
    listed = "\n".join(f"- {o}" for o in objects)
    size = (f"into EXACTLY {n_parts} parts (about {-(-len(objects) // n_parts)} objects each)" if n_parts
            else f"into parts of about {per_part} objects")
    rule = (f"\nThe user's grouping principle (follow it first, then balance the sizes):\n<<<\n{principle.strip()}\n>>>\n"
            if principle.strip() else "\nGroup by meaning (category, product type, role in the task) so that each part "
                                      "can be compared inside itself.\n")
    return f"""You re-split a large research task into parts for an "AI council" app. Each part becomes a separate
session that gets the whole task plus its own objects.

Task (for context):
<<<
{task}
>>>

Objects (use EXACTLY these strings, every object in exactly one part, no new objects):
{listed}

Split them {size}.{rule}
Give each part a short "title" in the task's language that names its principle (e.g. a province or a category).
Reply with ONE JSON object: {{"reason": "one sentence: how you grouped", "groups": [{{"title": "...", "objects": ["..."]}}]}}"""


def _parts_word(n: int) -> str:
    return "часть" if n % 10 == 1 and n % 100 != 11 else "части" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "частей"


def rebalance(groups: list[list[str]], n_parts: int) -> list[list[str]]:
    """Модель сделала не столько частей, сколько просили: те же объекты в том же порядке (соседи по смыслу остаются
    рядом) — ровно на n_parts частей."""
    flat = [o for g in groups for o in g]
    n = max(1, min(n_parts, len(flat)))
    size, extra = divmod(len(flat), n)
    out, i = [], 0
    for k in range(n):
        step = size + (1 if k < extra else 0)
        out.append(flat[i:i + step])
        i += step
    return [g for g in out if g]


def audit_prompt(task: str, objects: list[str]) -> str:
    listed = "\n".join(f"- {o}" for o in objects)
    return f"""Here is a task and the list of objects extracted from it. Which specific objects that the task GIVES to
cover (companies, products, sites, brands, categories) are NOT in the list? Only objects present in the task text;
never invent. Reply with ONE JSON object: {{"missing": ["..."]}} (empty list if none).

Task:
<<<
{task}
>>>

Extracted objects:
{listed}"""


_ROW_RE = re.compile(r"^\|\s*(\d{1,3})\s*\|\s*([^|]{3,})\|", re.M)
_ITEM_RE = re.compile(r"^\s*(?:\d{1,3}[.)]|[-*•])\s+(.{3,160})$", re.M)


def heuristic_objects(task: str) -> list[str]:
    """Без модели (демо, сбой): строки нумерованной таблицы «| 1 | Компания |», иначе пункты списка."""
    rows = [m.group(2).strip() for m in _ROW_RE.finditer(task)]
    if len(rows) >= 2:
        return list(dict.fromkeys(rows))[:200]
    return []


def _norm(s: str) -> str:
    return re.sub(r"[\s\"'«»“”()（）/|·,，.]+", " ", s.casefold()).strip()


def audit_added(task: str, objects: list[str], missing: list[str]) -> list[str]:
    """Что вторая проверка нашла пропущенным: только то, что есть в тексте задания и чего нет в списке (в любом
    написании)."""
    low = task.casefold()
    have = {_norm(o) for o in objects}
    out: list[str] = []
    for m in missing:
        m = (m or "").strip()
        if m and _norm(m) not in have and any(w in low for w in re.findall(r"\w{4,}", m.casefold())):
            out.append(m[:200])
            have.add(_norm(m))
    return out[:50]


def need_split(n_objects: int, per_part: int) -> bool:
    """Делить или нет — одно правило для советника и председателя: объектов больше, чем «объектов в части»."""
    return n_objects > per_part


def fix_groups(objects: list[str], groups: list[list[str]], per_part: int) -> tuple[list[list[str]], list[str]]:
    """Программа доводит группировку модели до «каждый объект ровно в одной части»; что поправлено — в notes."""
    notes: list[str] = []
    seen: set[str] = set()
    out: list[list[str]] = []
    canon = {_norm(o): o for o in objects}  # модель могла чуть иначе написать название: пробелы, регистр, кавычки
    for g in groups:
        keep = []
        for o in g:
            o = o if o in objects else canon.get(_norm(o), o)
            if o not in objects:
                notes.append(f"убран объект, которого нет в задании: {o}")
            elif o in seen:
                notes.append(f"убран повтор: {o}")
            else:
                keep.append(o)
                seen.add(o)
        if keep:
            out.append(keep)
    lost = [o for o in objects if o not in seen]
    if lost:
        notes.append("модель не распределила " + str(len(lost)) + " объектов — добавлены отдельной частью")
        out += SR.chunk(lost, per_part)
    if not out:
        out = SR.chunk(objects, per_part)
    return out, notes


def cap_groups(groups: list[list[str]], titles: list[str], per_part: int) -> tuple[list[list[str]], list[str], list[str]]:
    """Часть больше «объектов в части» (модель сгруппировала крупнее, чем задано) — делится на ровные куски с тем же
    названием («… (1/2)»): настройка пользователя соблюдается, смысловая группа остаётся рядом."""
    out_g: list[list[str]] = []
    out_t: list[str] = []
    notes: list[str] = []
    for i, g in enumerate(groups):
        t = titles[i] if i < len(titles) else ""
        if len(g) <= per_part:
            out_g.append(g)
            out_t.append(t)
            continue
        pieces = SR.chunk(g, per_part)
        notes.append(f"часть «{t or i + 1}» — {len(g)} объектов, больше {per_part}: разделена на {len(pieces)}")
        for k, piece in enumerate(pieces):
            out_g.append(piece)
            out_t.append(f"{t} ({k + 1}/{len(pieces)})" if t else "")
    return out_g, out_t, notes


def build_parts(task: str, groups: list[list[str]], titles: list[str], facts: int,
                base_checks: int, base_claims: int) -> list[dict[str, Any]]:
    total = len(groups)
    return [{"title": titles[i] if i < len(titles) and titles[i] else f"Часть {i + 1}", "objects": g,
             "question": SR.part_text(task, g, i + 1, total),
             "settings": SR.settings_for(len(g), facts, base_checks, base_claims)}
            for i, g in enumerate(groups)]


def parse_json(text: str) -> dict[str, Any]:
    from .voting import extract_json
    try:
        return extract_json(text)
    except ValueError:
        return {}


async def replan(task: str, client: Any, model: str | None, objects: list[str], *, n_parts: int | None = None,
                 principle: str = "", per_part: int = 8, facts: int = 3, base_checks: int = 20,
                 base_claims: int = 8) -> dict[str, Any]:
    """Пересмотр: объекты уже найдены (и проверены второй моделью) — заново только группировка: столько частей,
    сколько попросил пользователь, и/или по его принципу. Полноту по-прежнему проверяет программа."""
    notes: list[str] = []
    objects = list(dict.fromkeys(o.strip() for o in objects if o and o.strip()))
    if n_parts:
        n_parts = max(1, min(n_parts, len(objects), 20))
        per_part = -(-len(objects) // n_parts)
    reason, raw, titles = "", [], []
    if model and client is not None:
        try:
            comp = await client.complete(model, [{"role": "user", "content": regroup_prompt(task, objects, n_parts, per_part,
                                                                                            principle)}],
                                         temperature=0.2, max_tokens=12000, json_mode=True)
            p = SplitPlan.model_validate(parse_json(comp.text))
            raw, titles, reason = [g.objects for g in p.groups], [g.title for g in p.groups], p.reason
        except Exception as e:  # noqa: BLE001
            notes.append(f"модель не ответила ({type(e).__name__}) — части ровные по порядку")
    elif principle.strip():
        notes.append("демо-режим: принцип разбиения учитывает только настоящая модель — части ровные по порядку")
    groups, fixes = fix_groups(objects, raw or SR.chunk(objects, per_part), per_part)
    notes += fixes
    if fixes:
        titles = []
    if not n_parts:  # просили «по принципу» без числа частей — части не крупнее настройки
        groups, titles, big = cap_groups(groups, titles, per_part)
        notes += big
    if n_parts and len(groups) != n_parts:
        notes.append(f"модель сделала {len(groups)} частей вместо {n_parts} — части выровнены до {n_parts}, порядок "
                     "объектов сохранён")
        groups, titles = rebalance(groups, n_parts), []
    parts = build_parts(task, groups, titles, facts, base_checks, base_claims)
    return {"objects": objects, "facts_per_object": facts, "need_split": len(groups) > 1,
            "reason": reason or (f"по просьбе: {len(groups)} {_parts_word(len(groups))}" + (f", принцип — {principle.strip()[:120]}"
                                                                      if principle.strip() else "")),
            "parts": parts, "notes": notes,
            "checks": {**SR.check_split(objects, groups), "audit_added": []}, "revised": True}


async def plan(task: str, client: Any, model: str | None, *, per_part: int = 8, facts: int = 3,
               base_checks: int = 20, base_claims: int = 8) -> dict[str, Any]:
    """Анализ задания и разбиение: объекты, нужно ли делить, части (тексты собирает программа), проверки полноты."""
    notes: list[str] = []
    p = SplitPlan()
    if model and client is not None:
        try:
            comp = await client.complete(model, [{"role": "user", "content": split_prompt(task, per_part, facts)}],
                                         temperature=0.2, max_tokens=12000, json_mode=True)
            p = SplitPlan.model_validate(parse_json(comp.text))
        except Exception as e:  # noqa: BLE001 — модель не ответила: разбиваем по правилам
            notes.append(f"модель не ответила ({type(e).__name__}) — объекты найдены по таблице задания")
    objects = p.objects or heuristic_objects(task)
    if not p.objects and objects and not notes:
        notes.append("объекты найдены по нумерованной таблице задания")
    added: list[str] = []
    if objects and model and client is not None:  # вторая модель: каких объектов задания нет в списке
        try:
            comp = await client.complete(model, [{"role": "user", "content": audit_prompt(task, objects)}],
                                         temperature=0.0, max_tokens=4000, json_mode=True)
            miss = Missing.model_validate(parse_json(comp.text)).missing
            added = audit_added(task, objects, miss)  # «пропущенный» объект в другом написании — не новый
            objects += added
        except Exception:  # noqa: BLE001
            notes.append("проверка второй моделью не удалась")
    facts = max(1, min(int(p.facts_per_object or facts), 5))
    # то же правило, что у председателя: иначе советник сказал бы «делить не нужно», а председатель разбил бы
    need = need_split(len(objects), per_part)
    if not objects:
        return {"objects": [], "need_split": False, "facts_per_object": facts, "parts": [], "notes": notes,
                "reason": p.reason or "в задании нет списка объектов — делить нечего: совет ищет объекты сам",
                "checks": {"ok": True, "missing": [], "twice": [], "extra": [], "audit_added": []}}
    if need:
        raw = [[o for o in g.objects] for g in p.groups] or SR.chunk(objects, per_part)
        groups, fixes = fix_groups(objects, raw, per_part)
        titles = [g.title for g in p.groups] if p.groups and not fixes else []
        notes += fixes
        groups, titles, big = cap_groups(groups, titles, per_part)
        notes += big
    else:
        groups, titles = [objects], ["Всё задание"]
    check = SR.check_split(objects, groups)
    parts = build_parts(task, groups, titles, facts, base_checks, base_claims)
    auto = (f"{len(objects)} объектов — больше, чем «объектов в части» ({per_part}): так каждая часть проверяется глубже"
            if need else f"{len(objects)} объектов — не больше {per_part}: задание помещается в одно заседание")
    reason = p.reason if p.reason and bool(p.one_session) != need else auto  # довод модели — если совпал с решением
    if added and any(set(g) <= set(added) for g in groups):
        notes = [n for n in notes if "не распределила" not in n] + [
            "объекты, найденные второй проверкой, — отдельной частью (их можно перенести в другие части)"]
    return {"objects": objects, "facts_per_object": facts, "need_split": need, "reason": reason, "parts": parts,
            "notes": notes, "checks": {**check, "audit_added": added}}


def plan_json(d: dict[str, Any]) -> str:
    return json.dumps(d, ensure_ascii=False)
