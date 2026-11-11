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
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .reports.docx import render_docx
from .reports.html import render_html
from .reports.md import render_md
from .reports.pdf import PDF_AVAILABLE, render_pdf
from .reports.view import MODE_NAME, View

log = logging.getLogger("council.archive")

REPORT_VERSION = "11"  # 11 — «Только решение» без вопроса; 10 — новое оформление: решение первым, стили Word, PDF/UA  # увеличить, когда меняется вид отчётов: старые пересоберутся при запуске
MARKER = ".reports-version"  # номер версии; время изменения файла — момент смены версии
LOCK = ".reports-lock"  # фоновую пересборку ведёт один процесс (сайт и бот делят папку архива)
URGENT = ".reports-urgent-"  # пока такие файлы есть, фоновая пересборка ждёт: человек ждёт свой отчёт
URGENT_STALE_S = 600
WORKER_TIMEOUT_S = 900
WORKER_LOCK = ".reports-worker.lock"  # на всём сервере верстается один отчёт за раз: вёрстка PDF — сотни МБ памяти  # сборка всех отчётов одного очень длинного заседания на слабом сервере  # файл старше — остался от упавшего процесса, не ждём его
ID_RE = re.compile(r"^[\w\-]{1,160}$", re.U)
MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "md": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
}
# Виды отчёта: «pdf» — полный отчёт, «summary.pdf» — итог (решение + почему, особые мнения, риски),
# «decision.pdf» — только решение совета (то же для docx и md)
RENDER: dict[str, Callable[[View], bytes]] = {
    "pdf": render_pdf,
    "docx": render_docx,
    "md": lambda v: render_md(v).encode("utf-8"),
    "summary.pdf": lambda v: render_pdf(v, summary=True),
    "summary.docx": lambda v: render_docx(v, summary=True),
    "summary.md": lambda v: render_md(v, summary=True).encode("utf-8"),
    "decision.pdf": lambda v: render_pdf(v, decision=True),
    "decision.docx": lambda v: render_docx(v, decision=True),
    "decision.md": lambda v: render_md(v, decision=True).encode("utf-8"),
}
PARTS = ("summary", "decision")  # варианты сокращённого отчёта


def available_formats() -> list[str]:
    """Форматы файлов, которые можно получить на этой машине."""
    return [f for f in ("pdf", "docx", "md") if f != "pdf" or PDF_AVAILABLE]


def report_kind(fmt: str, summary: bool = False, decision: bool = False) -> str:
    return f"decision.{fmt}" if decision else (f"summary.{fmt}" if summary else fmt)


def all_reports() -> list[str]:
    """Все файлы отчётов одного заседания: полные, «итог» и «только решение»."""
    base = available_formats()
    return base + [f"{part}.{f}" for part in PARTS for f in base]


def download_name(stem: str, kind: str) -> str:
    fmt = kind.rsplit(".", 1)[-1]
    suffix = {"summary": "-итог", "decision": "-решение"}.get(kind.split(".", 1)[0] if "." in kind else "", "")
    return f"{stem}{suffix}.{fmt}"


def key_of(stem: str) -> str:
    """Короткий ключ заседания (для кнопок Telegram, где callback_data ограничен 64 байтами)."""
    return hashlib.sha1(stem.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


class Archive:
    def __init__(self, runs_dir: str | Path, isolate: bool = False) -> None:
        """isolate=True — отчёты собираются в отдельном процессе (сайт и бот): тяжёлая вёрстка PDF
        не подвешивает сервер, а занятая ею память возвращается системе после каждого заседания."""
        self.dir = Path(runs_dir)
        self.isolate = isolate
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
        return View(self.load(stem), stem, files_dir=self.files_dir(stem))

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
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or "question" not in data or "verdict" not in data:
                return None  # служебный файл в папке архива, а не заседание
            v = View(data, stem)
            w = v.winner
            s = {
                "id": stem, "key": key_of(stem), "question": v.question, "mode": v.d.get("mode"),
                "mode_name": MODE_NAME.get(v.d.get("mode", ""), ""), "date": v.date,
                "started_at": v.d.get("started_at"), "winner": w, "winner_title": v.title(w),
                "winner_author": v.author(w) if w else "", "cost": round(v.cost, 4),
                "members": v.members, "proposals": len(v.proposals), "status": "done",
                "attachments": len(v.attachments),
                "followup_of": v.d.get("followup_of", ""),
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
    def _version_time(self) -> float | None:
        """Когда включилась текущая версия отчётов; None — архив ещё не переведён на неё."""
        try:
            m = self.dir / MARKER
            return m.stat().st_mtime if m.read_text().strip() == REPORT_VERSION else None
        except OSError:
            return None

    def _fresh(self, stem: str, fmt: str) -> bool:
        """Отчёт собран текущей версией и после последнего изменения стенограммы."""
        vt = self._version_time()
        try:
            rt = self.path(stem, fmt).stat().st_mtime
            return vt is not None and rt >= vt and rt >= self.path(stem, "json").stat().st_mtime
        except OSError:
            return False

    def _wait_urgent(self) -> None:
        """Фоновая пересборка уступает процессор отчёту, который сейчас ждёт человек."""
        while True:
            now = time.time()
            try:
                busy = [p for p in self.dir.glob(URGENT + "*") if now - p.stat().st_mtime < URGENT_STALE_S]
            except OSError:
                busy = []
            if not busy:
                return
            time.sleep(0.5)

    def build(self, stem: str, formats: list[str] | None = None, data: dict | None = None) -> dict[str, str]:
        """Создаёт отчёты. Возвращает {формат: путь или 'ошибка: …'}."""
        view = View(data if data is not None else self.load(stem), stem, files_dir=self.files_dir(stem))
        result = {}
        for fmt in formats or all_reports():
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

    def _build(self, stem: str, formats: list[str], background: bool = False) -> dict[str, str]:
        """Как build(), но при isolate — в отдельном процессе; фоновая сборка — с низким приоритетом."""
        if not self.isolate:
            return self.build(stem, formats)
        nice = (lambda: os.nice(15)) if background and hasattr(os, "nice") else None
        pkg_root = str(Path(__file__).resolve().parent.parent)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [pkg_root, os.environ.get("PYTHONPATH")]))}
        try:
            with _WaitLock(self.dir / WORKER_LOCK):  # сайт, бот, пересборка архива — по очереди, не одновременно
                p = subprocess.run([sys.executable, "-m", "council.reports_worker", str(self.dir), stem, *formats],
                                   capture_output=True, text=True, check=False, timeout=WORKER_TIMEOUT_S, preexec_fn=nice,
                                   env=env, cwd=pkg_root)
            if p.returncode == 0:
                return json.loads(p.stdout.strip().splitlines()[-1])
            err = (p.stderr.strip().splitlines() or [f"код {p.returncode}"])[-1]
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
            err = str(e) or type(e).__name__
        log.warning("отчёты %s не собраны: %s", stem, err)
        return {f: f"ошибка: {err}" for f in formats}

    def report(self, stem: str, fmt: str) -> Path:
        """Путь к отчёту; создаёт или обновляет его при необходимости."""
        if fmt not in RENDER:
            raise KeyError(fmt)
        if not self.exists(stem):
            raise KeyError(stem)
        target = self.path(stem, fmt)
        if self._fresh(stem, fmt):
            return target
        flag = self.dir / f"{URGENT}{os.getpid()}-{threading.get_ident()}"
        try:
            flag.touch()
        except OSError:
            flag = None
        try:
            with self._lock:
                if not self._fresh(stem, fmt):
                    res = self._build(stem, [fmt])
                    if res[fmt].startswith("ошибка"):
                        raise RuntimeError(res[fmt])
        finally:
            if flag is not None:
                flag.unlink(missing_ok=True)
        return target

    def html(self, stem: str, toolbar: str = "", summary: bool = False, decision: bool = False) -> str:
        return render_html(self.view(stem), toolbar=toolbar, summary=summary, decision=decision)

    def ensure_all(self, force: bool = False, progress: Callable[[str, dict], None] | None = None) -> tuple[int, int]:
        """Досоздаёт недостающие и устаревшие отчёты для всех заседаний. Возвращает (создано, ошибок).

        Пока пересборка идёт, уже пересобранные отчёты считаются свежими, а отчёт, который открыл человек,
        собирается вне очереди. Если пересборку уже ведёт другой процесс (сайт или бот), второй не запускается."""
        if not self.dir.is_dir():
            return 0, 0
        with _ProcessLock(self.dir / LOCK) as got:
            if not got:
                log.info("отчёты архива уже пересобирает другой процесс")
                return 0, 0
            if force or self._version_time() is None:
                (self.dir / MARKER).write_text(REPORT_VERSION)  # с этого момента всё собранное раньше — устарело
            built = failed = 0
            for stem in self.stems():
                need = [f for f in all_reports() if not self._fresh(stem, f)]
                if not need:
                    continue
                res: dict[str, str] = {}
                try:
                    self._wait_urgent()
                    if self.isolate:  # отдельный процесс с низким приоритетом, по одному формату: открытый человеком
                        for f in need:  # отчёт ждёт не дольше одного формата
                            self._wait_urgent()
                            if not self._fresh(stem, f):
                                res.update(self._build(stem, [f], background=True))
                    else:
                        for f in need:
                            self._wait_urgent()
                            with self._lock:
                                if not self._fresh(stem, f):
                                    res.update(self.build(stem, [f]))
                except (OSError, ValueError, KeyError) as e:
                    log.warning("заседание %s пропущено: %s", stem, e)
                    failed += 1
                    continue
                if not res:
                    continue
                bad = [f for f, r in res.items() if r.startswith("ошибка")]
                failed += bool(bad)
                built += 1
                if progress:
                    progress(stem, res)
            return built, failed

    def ensure_all_in_background(self, force: bool = False) -> threading.Thread:
        def run() -> None:
            try:
                built, failed = self.ensure_all(force=force)
                if built or failed:
                    log.info("отчёты для архива: обновлено %d заседаний, с ошибками %d", built, failed)
            except Exception:  # noqa: BLE001
                log.exception("не удалось обновить отчёты архива")

        t = threading.Thread(target=run, name="archive-reports", daemon=True)
        t.start()
        return t

    MAX_THREAD = 4
    MAX_ANSWER_CHARS = 4000

    def followup(self, parent: str) -> tuple[list[dict[str, Any]], list]:
        """Для уточняющего вопроса: цепочка «вопрос — решение» (последние 4) и вложения исходного заседания."""
        from .attachments import load_from_dir

        d = self.load(parent)
        v = View(d, parent)
        answer = (d.get("verdict") or {}).get("final_answer", "")
        if len(answer) > self.MAX_ANSWER_CHARS:
            answer = answer[:self.MAX_ANSWER_CHARS] + "…"
        item = {"id": parent, "title": v.title_text, "question": v.question, "answer": answer,
                "winner": v.title(v.winner)}
        if v.is_research:  # исследование: подтверждённые факты с источниками и открытые вопросы — с датой поиска
            by = v.evidence_by_id
            known = [f"- {k.get('statement', '')} ({', '.join(by[i]['url'] for i in k.get('evidence_ids') or [] if i in by)})"
                     for k in v.known][:8]
            unknown = [f"- {u.get('gap', '')}" for u in v.unknown][:6]
            ev = "\n".join((["Confirmed by sources (search date " + v.date + "):"] + known if known else [])
                           + (["Still unknown:"] + unknown if unknown else []))
            item["evidence"] = ev[:1500]
        thread = list(d.get("thread") or []) + [item]
        atts = load_from_dir(self.files_dir(parent), d.get("attachments") or [])
        return thread[-self.MAX_THREAD:], atts

    def request_of(self, stem: str) -> dict[str, Any]:
        """Настройки, с которыми заседание запускали (для «Повторить»); для старых заседаний — восстановленные."""
        d = self.load(stem)
        req = dict(d.get("request") or {})
        req.setdefault("question", d.get("question", ""))
        req.setdefault("mode", d.get("mode", "debate"))
        req.setdefault("members", list(d.get("stats") or {}))
        if d.get("mode") == "evaluate" and "options" not in req:
            req["options"] = [p.get("summary", "") for _, p in sorted((d.get("initial_proposals") or {}).items())]
        return req

    def files_dir(self, stem: str) -> Path:
        return self.path(stem, "files")

    def attachment(self, stem: str, file: str) -> Path:
        """Путь к сохранённому вложению (только файлы из стенограммы, без выхода за папку)."""
        metas = self.load(stem).get("attachments") or []
        if file not in {m.get("file") for m in metas}:
            raise KeyError(file)
        p = self.files_dir(stem) / file
        if not p.is_file() or p.resolve().parent != self.files_dir(stem).resolve():
            raise KeyError(file)
        return p

    def delete(self, stem: str) -> bool:
        import shutil

        removed = False
        try:
            if self.files_dir(stem).is_dir():
                shutil.rmtree(self.files_dir(stem))
                removed = True
        except (OSError, KeyError):
            pass
        for ext in ("json", *RENDER):
            try:
                self.path(stem, ext).unlink()
                removed = True
            except (OSError, KeyError):
                pass
        for extra in (self.dir / f"{stem}.events.jsonl", self.dir / "failed" / f"{stem}.json"):  # журналы для разбора
            try:
                extra.unlink()
            except OSError:
                pass
        self._cache.pop(stem, None)
        return removed


class _ProcessLock:
    """Неблокирующая блокировка файла между процессами; где её нет (Windows) — всегда «получена»."""

    def __init__(self, path: Path) -> None:
        self.path, self.fh = path, None

    def __enter__(self) -> bool:
        try:
            import fcntl
        except ImportError:
            return True
        try:
            self.fh = open(self.path, "a")  # закрывается в __exit__
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if self.fh:
                self.fh.close()
                self.fh = None
            return False

    def __exit__(self, *exc: object) -> None:
        if self.fh:
            self.fh.close()  # закрытие файла снимает блокировку


class _WaitLock:
    """Блокировка файла между процессами с ожиданием (где её нет — без блокировки)."""

    def __init__(self, path: Path) -> None:
        self.path, self.fh = path, None

    def __enter__(self) -> None:
        try:
            import fcntl
            self.fh = open(self.path, "a")  # закрывается в __exit__
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except (ImportError, OSError):
            if self.fh:
                self.fh.close()
            self.fh = None

    def __exit__(self, *exc: object) -> None:
        if self.fh:
            self.fh.close()
