"""Ваша оценка пользы заседания (1–5) — для пилота «исследование против обсуждения».

Хранится отдельно от архива (runs/notes/ratings.json), чтобы не переписывать большие файлы заседаний.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

FILE = "ratings.json"


def load(runs_dir: Path) -> dict[str, dict[str, Any]]:
    try:
        return json.loads((Path(runs_dir) / "notes" / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(runs_dir: Path, stem: str, rating: int | None, note: str = "") -> dict[str, Any] | None:
    data = load(runs_dir)
    if rating is None:
        data.pop(stem, None)
        item = None
    else:
        item = {"rating": int(rating), "note": note.strip()[:500], "ts": time.time()}
        data[stem] = item
    p = Path(runs_dir) / "notes" / FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return item
