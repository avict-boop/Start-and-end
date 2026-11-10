"""Архив заседаний: список с поиском, отчёты в PDF/DOCX/MD, досоздание отчётов для старых заседаний.

Источник правды — стенограммы `<id>.json` в папке runs/. Отчёты строятся из них, поэтому
их можно пересобрать в любой момент (например, после обновления программы).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable

from .reports.docx import render_docx
from .reports.html import render_html
from .reports.md import render_md
from .reports.pdf import PDF_AVAILABLE, render_pdf
from .reports.view import MODE_NAME, View

log = logging.getLogger("council.archive")

REPORT_VERSION = "2"  # увеличить, когда меняется вид отчётов: старые пересоберутся при запуске
MARKER = ".reports-version"
ID_RE = re.compile(r"^[\w\-]{1,160}$", re.U)
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}
RENDER: dict[str, Callable[[View], bytes]] = {
    "pdf": render_pdf,
    "docx": render_docx,
    "md": lambda v: render_md(v).encode("utf-8"),
}


def available_formats() -> list[str]:
    return [f for f in ("pdf", "docx", "md") if f != "pdf" or PDF_AVAILABLE]


def key_of(stem: str) -> str:
    """Короткий ключ заседания (для кнопок Telegram, где callback_data ограничен 64 байтами)."""
    return hashlib.sha1(stem.encode("utf-8")).hexdigest()[:10]


class Archive:
    def __init__(self, runs_dir: str | Path) -> None:
        self.dir = Path(runs_dir)
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------ файлы
    def path(self, stem: str, ext: str) -> Path:
        if not ID_RE.match(stem):
            raise KeyError(stem)
        return self.dir / f"{stem}.{ext}"

    def exists(self, stem: str) -> bool:
        try:
            return self.path(stem, "json").is_file()
        except KeyError:
            return False

    def load(self, stem: str) -> dict[str, Any]:
        p = self.path(stem, "json")
        if not p.is_file():
            raise KeyError(stem)
        return json.loads(p.read_text(encoding="utf-8"))

    def view(self, stem: str) -> View:
        return View(self.load(stem), stem)

    def stems(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted((p.stem for p in self.dir.glob("*.json") if ID_RE.match(p.stem)), reverse=True)

    # ------------------------------------------------------------ список и поиск
    def summary(self, stem: str) -> dict[str, Any] | None:
        p = self.path(stem, "json")
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return None
        cached = self._cache.get(stem)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            v = View(json.loads(p.read_text(encoding="utf-8")), stem)
            w = v.winner
            s = {
                "id": stem, "key": key_of(stem), "question": v.question, "mode": v.d.get("mode"),
                "mode_name": MODE_NAME.get(v.d.get("mode", ""), ""), "date": v.date,
                "started_at": v.d.get("started_at"), "winner": w, "winner_title": v.title(w),
                "winner_author": v.author(w) if w else "", "cost": round(v.cost, 4),
                "members": v.members, "proposals": len(v.proposals), "status": "done",
            }
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            log.warning("не удалось прочитать %s: %s", p.name, e)
            return None
        self._cache[stem] = (mtime, s)
        return s

    def list(self, query: str = "", mode: str = "") -> list[dict[str, Any]]:
        q = query.casefold().strip()
        out = []
        for stem in self.stems():
            s = self.summary(stem)
            if not s:
                continue
            if mode and s["mode"] != mode:
                continue
            if q and q not in " ".join([s["question"], s["winner_title"], s["winner_author"],
                                        " ".join(s["members"])]).casefold():
                continue
            out.append(s)
        return out

    def find_key(self, key: str) -> str | None:
        return next((s for s in self.stems() if key_of(s) == key), None)

    # ------------------------------------------------------------ отчёты
    def _marker_ok(self) -> bool:
        try:
            return (self.dir / MARKER).read_text().strip() == REPORT_VERSION
        except OSError:
            return False

    def _fresh(self, stem: str, fmt: str) -> bool:
        rp, jp = self.path(stem, fmt), self.path(stem, "json")
        try:
            return self._marker_ok() and rp.stat().st_mtime >= jp.stat().st_mtime
        except OSError:
            return False

    def build(self, stem: str, formats: list[str] | None = None, data: dict | None = None) -> dict[str, str]:
        """Создаёт отчёты. Возвращает {формат: путь или 'ошибка: …'}."""
        view = View(data if data is not None else self.load(stem), stem)
        result = {}
        for fmt in formats or available_formats():
            target = self.path(stem, fmt)
            try:
                content = RENDER[fmt](view)
                tmp = target.with_suffix(target.suffix + ".tmp")
                tmp.write_bytes(content)
                os.replace(tmp, target)
                result[fmt] = str(target)
            except Exception as e:  # noqa: BLE001 — один формат не должен ломать остальные
                log.warning("отчёт %s.%s не создан: %s", stem, fmt, e)
                result[fmt] = f"ошибка: {e}"
        return result

    def report(self, stem: str, fmt: str) -> Path:
        """Путь к отчёту; создаёт или обновляет его при необходимости."""
        if fmt not in RENDER:
            raise KeyError(fmt)
        if not self.exists(stem):
            raise KeyError(stem)
        target = self.path(stem, fmt)
        if not self._fresh(stem, fmt):
            with self._lock:
                if not self._fresh(stem, fmt):
                    res = self.build(stem, [fmt])
                    if res[fmt].startswith("ошибка"):
                        raise RuntimeError(res[fmt])
        return target

    def html(self, stem: str, toolbar: str = "") -> str:
        return render_html(self.view(stem), toolbar=toolbar)

    def ensure_all(self, force: bool = False, progress: Callable[[str, dict], None] | None = None) -> tuple[int, int]:
        """Досоздаёт недостающие и устаревшие отчёты для всех заседаний. Возвращает (создано, ошибок)."""
        force = force or not self._marker_ok()
        built = failed = 0
        for stem in self.stems():
            need = [f for f in available_formats() if force or not self.path(stem, f).is_file()
                    or self.path(stem, f).stat().st_mtime < self.path(stem, "json").stat().st_mtime]
            if not need:
                continue
            try:
                res = self.build(stem, need)
            except (OSError, ValueError, KeyError) as e:
                log.warning("заседание %s пропущено: %s", stem, e)
                failed += 1
                continue
            bad = [f for f, r in res.items() if r.startswith("ошибка")]
            failed += bool(bad)
            built += 1
            if progress:
                progress(stem, res)
        if self.dir.is_dir():
            (self.dir / MARKER).write_text(REPORT_VERSION)
        return built, failed

    def ensure_all_in_background(self) -> threading.Thread:
        def run() -> None:
            try:
                built, failed = self.ensure_all()
                if built or failed:
                    log.info("отчёты для архива: обновлено %d заседаний, с ошибками %d", built, failed)
            except Exception:  # noqa: BLE001
                log.exception("не удалось обновить отчёты архива")

        t = threading.Thread(target=run, name="archive-reports", daemon=True)
        t.start()
        return t

    def delete(self, stem: str) -> bool:
        removed = False
        for ext in ("json", "md", "pdf", "docx"):
            try:
                self.path(stem, ext).unlink()
                removed = True
            except (OSError, KeyError):
                pass
        self._cache.pop(stem, None)
        return removed
