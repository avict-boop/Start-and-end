"""Серия заседаний: одно большое задание, разбитое на части, — части идут с одинаковыми настройками (не больше
max_parallel одновременно), потом совет сводит их в один отчёт.

Серия хранится в runs/series/<id>.json: части (текст, номер заседания, объекты части), общие настройки запуска,
сводное заседание. Что с каждой частью сейчас (ждёт, идёт, готова, на паузе), сайт узнаёт по самим заседаниям —
в файле серии только номера заседаний, поэтому серия переживает перезапуск программы: «Продолжить серию» запускает
то, что ещё не запускалось, и продолжает приостановленное.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

SERIES_DIR = "series"
ID_RE = re.compile(r"^s-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")


def series_dir(runs: Path) -> Path:
    return Path(runs) / SERIES_DIR


def new_id() -> str:
    return time.strftime("s-%Y%m%d-%H%M%S-") + secrets.token_hex(2)


def path_of(runs: Path, sid: str) -> Path:
    if not ID_RE.match(sid):
        raise ValueError("неверный номер серии")
    return series_dir(runs) / f"{sid}.json"


def save(runs: Path, s: dict[str, Any]) -> None:
    p = path_of(runs, s["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def load(runs: Path, sid: str) -> dict[str, Any] | None:
    try:
        p = path_of(runs, sid)
    except ValueError:
        return None
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def all_series(runs: Path) -> list[dict[str, Any]]:
    d = series_dir(runs)
    out = []
    if d.is_dir():
        for p in sorted(d.glob("s-*.json"), reverse=True):
            s = load(runs, p.stem)
            if s:
                out.append(s)
    return out


def create(runs: Path, *, title: str, task: str, parts: list[dict[str, Any]], request: dict[str, Any],
           max_parallel: int = 2, auto_summary: bool = True, origin: str = "") -> dict[str, Any]:
    """Новая серия. parts: [{"title": "...", "question": "...", "objects": [...]}]."""
    s = {"id": new_id(), "title": title.strip()[:200] or "Серия", "task": task, "created": time.time(),
         "max_parallel": max(1, min(int(max_parallel or 2), 5)), "auto_summary": bool(auto_summary),
         "request": request, "origin": origin, "summary_id": "", "summary_full": True,
         "parts": [{"n": i + 1, "title": (p.get("title") or f"Часть {i + 1}")[:200], "question": p["question"],
                    "objects": list(p.get("objects") or []), "session_id": ""} for i, p in enumerate(parts)]}
    save(runs, s)
    return s


class SplitRequested(Exception):
    """Председатель решил разбить большое задание на части серии (исследование останавливается до вариантов)."""

    def __init__(self, plan: dict[str, Any]):
        super().__init__(f"задание разбито на {len(plan.get('parts') or [])} частей")
        self.plan = plan


# ------------------------------------------------------------------ разбиение без потерь

SCOPE_NOTE = """

---
## Рамка этой части серии ({n} из {total})

Это задание проверяется по частям: каждая часть — отдельное заседание с одинаковыми настройками, потом совет сводит
части в один отчёт. **В этой части работай только с объектами ниже** ({count}); остальные объекты задания
проверяются в других частях серии — не включай их и не добавляй новых. Все остальные требования, правила, статусы и
формат задания выше действуют полностью.

Объекты этой части:
{objects}
"""


def part_text(task: str, objects: list[str], n: int, total: int) -> str:
    """Текст части: полное задание без сокращений + рамка «только эти объекты» — ничего из задания не теряется."""
    return task.rstrip() + SCOPE_NOTE.format(n=n, total=total, count=len(objects),
                                             objects="\n".join(f"- {o}" for o in objects))


def chunk(objects: list[str], per_part: int) -> list[list[str]]:
    """Ровные пачки по порядку (запасной вариант, если группировка по смыслу не удалась)."""
    per_part = max(1, per_part)
    n = max(1, -(-len(objects) // per_part))
    size = -(-len(objects) // n)
    return [objects[i:i + size] for i in range(0, len(objects), size)]


def check_split(objects: list[str], groups: list[list[str]]) -> dict[str, Any]:
    """Каждый объект — ровно в одной части; лишних нет. Это проверяет программа, а не модель."""
    seen: dict[str, int] = {}
    for g in groups:
        for o in g:
            seen[o] = seen.get(o, 0) + 1
    missing = [o for o in objects if o not in seen]
    twice = [o for o, k in seen.items() if k > 1]
    extra = [o for o in seen if o not in objects]
    return {"ok": not missing and not twice and not extra, "missing": missing, "twice": twice, "extra": extra}


def settings_for(n_objects: int, facts: int, base_checks: int, base_claims: int) -> dict[str, int]:
    """Настройки части: проверок = объекты × фактов на объект (до 100), утверждений у автора — столько же + 3."""
    want = n_objects * max(1, facts)
    return {"arbitration_claims": max(base_checks, min(100, want)), "max_claims": max(base_claims, min(80, want + 3))}


def apply_spec(council: Any, spec: dict[str, Any], archive: Any) -> None:
    """Что заседание серии берёт из своего «паспорта» — одинаково на сайте и в Telegram-боте (бот тоже умеет
    продолжать приостановленные заседания сайта): объекты части задаёт программа, сводке — реестры частей."""
    council.fixed_objects = [str(o) for o in spec.get("objects") or []]
    inherited = []
    for n, sid in spec.get("inherit") or []:
        try:
            d = archive.load(sid)
        except (KeyError, OSError, ValueError):
            continue
        led = (d.get("research") or {}).get("ledger") or {}
        if led:
            inherited.append((int(n), led))
    council.inherited = inherited
