"""Сохранение результатов заседания: стенограмма (JSON) и отчёты (PDF, DOCX, MD)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .archive import Archive, all_reports
from .attachments import Attachment, save_files
from .reports.md import render_md
from .reports.view import View
from .session import SessionResult


def render_markdown(res: SessionResult) -> str:
    return render_md(View(res.to_dict()))


def make_stem(question: str) -> str:
    slug = re.sub(r"[^\w]+", "-", question.lower(), flags=re.U).strip("-")[:40] or "session"
    return f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"


def save(res: SessionResult, runs_dir: str | Path, stem: str | None = None,
         attachments: list[Attachment] | None = None) -> dict[str, Path]:
    """Сохраняет стенограмму, вложения (папка <id>.files) и все отчёты.

    Возвращает {"json"|"pdf"|"docx"|"md"|…: путь} (без форматов с ошибкой).
    """
    d = Path(runs_dir)
    d.mkdir(parents=True, exist_ok=True)
    stem = stem or make_stem(res.question)
    save_files(attachments or [], d / f"{stem}.files")
    data = res.to_dict()
    json_path = d / f"{stem}.json"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    built = Archive(d).build(stem, all_reports(), data=data)
    paths = {"json": json_path}
    paths.update({fmt: Path(p) for fmt, p in built.items() if not p.startswith("ошибка")})
    return paths
