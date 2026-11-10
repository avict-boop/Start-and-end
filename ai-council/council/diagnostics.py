"""Журналы для разбора ошибок.

- Журнал программы: runs/logs/<web|bot|cli>.log с ротацией — ошибки, предупреждения, ход работы.
- Журнал событий заседания: runs/<id>.events.jsonl — каждое событие с временем (запросы, повторы, сбои).
- Упавшие и остановленные заседания: runs/failed/<id>.json — вопрос, ошибка, события и частичная
  стенограмма с сырыми ответами моделей (обычные заседания сохраняются только при успехе).
- Пакет для разбора: zip со всем этим, версией программы и настройками — без ключей и паролей.
"""
from __future__ import annotations

import io
import json
import logging
import logging.handlers
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

LOG_DIR = "logs"
FAILED_DIR = "failed"
MAX_FAILED = 50
SECRET_RE = re.compile(r"(sk-or-[\w-]{6})[\w-]+|(\d{6,}:)[\w-]{20,}|(Bearer\s+)[\w.-]{8,}", re.I)


def redact(text: str) -> str:
    """Прячет ключи OpenRouter, токены Telegram и заголовки авторизации, если они случайно попали в текст."""
    return SECRET_RE.sub(lambda m: (m.group(1) or m.group(2) or m.group(3) or "") + "…скрыто…", text)


class _Redacting(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(runs_dir: str | Path, name: str, *, console: bool = True) -> Path:
    """Пишет журнал программы в runs/logs/<name>.log (5 файлов по 2 МБ). Возвращает путь к журналу."""
    d = Path(runs_dir) / LOG_DIR
    path = d / f"{name}.log"
    root = logging.getLogger()
    if any(getattr(h, "_council_log", None) == str(path) for h in root.handlers):
        return path
    try:
        d.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=4, encoding="utf-8")
    except OSError:
        return path
    h._council_log = str(path)  # type: ignore[attr-defined]
    h.setFormatter(_Redacting("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    h.setLevel(logging.INFO)
    root.addHandler(h)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    if console and not any(isinstance(x, logging.StreamHandler) and not isinstance(x, logging.FileHandler)
                           for x in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(_Redacting("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        sh.setLevel(logging.WARNING)
        root.addHandler(sh)
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # WeasyPrint предупреждает о CSS для экрана (sticky, @media) и о способе встраивания шрифтов —
    # на PDF это не влияет, а журнал засоряет. Настоящие ошибки сборки PDF остаются.
    for noisy in ("weasyprint", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    return path


# ------------------------------------------------------------------ события заседания

def stamp(event: dict[str, Any]) -> dict[str, Any]:
    """Добавляет время к событию (секунды Unix), если его нет."""
    if "ts" not in event:
        event = {**event, "ts": round(time.time(), 3)}
    return event


def _jsonable(x: Any) -> Any:
    if hasattr(x, "model_dump"):
        return x.model_dump()
    if hasattr(x, "__dict__") and not isinstance(x, type):
        return {k: _jsonable(v) for k, v in vars(x).items() if not k.startswith("_")}
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def save_events(runs_dir: str | Path, stem: str, events: list[dict[str, Any]]) -> Path | None:
    path = Path(runs_dir) / f"{stem}.events.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(_jsonable(ev), ensure_ascii=False, default=str) + "\n")
        return path
    except OSError:
        logging.getLogger("council.diagnostics").exception("не удалось сохранить журнал событий %s", stem)
        return None


def save_failed(runs_dir: str | Path, stem: str, *, question: str, status: str, error: str,
                events: list[dict[str, Any]] | None = None, transcript: list[dict[str, Any]] | None = None,
                stats: dict[str, Any] | None = None, request: dict[str, Any] | None = None) -> Path | None:
    """Сохраняет заседание, которое не дошло до конца (ошибка или остановка), для разбора."""
    d = Path(runs_dir) / FAILED_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
        data = {"id": stem, "question": question, "status": status, "error": redact(error or ""),
                "saved_at": datetime.now().isoformat(timespec="seconds"), "request": request or {},
                "stats": _jsonable(stats or {}), "transcript": _jsonable(transcript or []),
                "events": _jsonable(events or [])}
        path = d / f"{stem}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        for old in sorted(d.glob("*.json"))[:-MAX_FAILED]:
            old.unlink(missing_ok=True)
        return path
    except OSError:
        logging.getLogger("council.diagnostics").exception("не удалось сохранить упавшее заседание %s", stem)
        return None


def list_failed(runs_dir: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    d = Path(runs_dir) / FAILED_DIR
    out = []
    for p in sorted(d.glob("*.json"), reverse=True)[:limit] if d.is_dir() else []:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({"id": p.stem, "question": (data.get("question") or "")[:200], "status": data.get("status", ""),
                    "error": (data.get("error") or "")[:300], "saved_at": data.get("saved_at", "")})
    return out


# ------------------------------------------------------------------ читаемый журнал

STAGE_RU = {"framing": "постановка", "proposal": "предложения", "critique": "критика", "revision": "доработка",
            "vote": "голосование", "verdict": "итог", "factcheck": "проверка фактов", "describe": "описание картинок",
            "attachments": "вложения"}


def events_text(events: list[dict[str, Any]]) -> str:
    """Журнал событий заседания в виде текста: время, кто, этап, что произошло."""
    lines = []
    t0 = next((e.get("ts") for e in events if e.get("ts")), None)
    for e in events:
        typ = e.get("type", "")
        if typ in ("ping",):
            continue
        ts = e.get("ts")
        when = datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "--:--:--"
        rel = f"+{ts - t0:7.1f}s" if ts and t0 else "         "
        who = e.get("member", "")
        st = STAGE_RU.get(e.get("stage", ""), e.get("stage", ""))
        if typ == "call":
            msg = f"{e.get('phase', '')}" + (f" — {e.get('detail')}" if e.get("detail") else "")
            if e.get("phase") in ("thinking", "writing") and not e.get("detail"):
                continue  # частые обновления «думает/пишет» — без пользы для разбора
        elif typ == "stage":
            msg = f"▶ {e.get('title', '')}"
        elif typ == "member_failed":
            msg = f"СБОЙ: {e.get('error', '')}"
        elif typ == "warning":
            msg = f"ПРЕДУПРЕЖДЕНИЕ: {e.get('text', '')}"
        elif typ == "error":
            msg = f"ОШИБКА ЗАСЕДАНИЯ: {e.get('message', '')}"
        elif typ == "member_done":
            msg = "готово"
        elif typ == "cost":
            continue
        elif typ == "tally":
            rows = e.get("rows") or []
            msg = "итоги: " + ", ".join(f"{r.get('label')}" for r in rows)
        elif typ == "done":
            msg = f"заседание завершено: ${e.get('total_cost', 0):.4f}, {e.get('duration_s', '')} с"
        else:
            msg = typ
        lines.append(f"{when} {rel}  {who:<12} {st:<14} {msg}")
    return redact("\n".join(lines)) + "\n"


# ------------------------------------------------------------------ пакет для разбора

README = """Пакет для разбора ошибок «Совета нейросетей»
============================================

Что внутри:
  version.json           — версия программы (если служба управления установлена)
  config.yaml            — настройки совета (в нём нет ключей; ключи хранятся в .env и сюда не попадают)
  logs/*.log             — журнал программы: ошибки, предупреждения, ход работы
  failed/*.json          — заседания, которые прервались (ошибка или остановка), с частичной стенограммой
  session/…              — выбранное заседание:
      events.txt         — журнал событий: время, модель, этап, что произошло (повторы, сбои, продолжения)
      events.jsonl       — то же в машинном виде
      transcript.json    — стенограмма: каждый запрос к модели, сырой ответ, ошибка разбора, стоимость
      failed.json        — если заседание прервалось

Ключи OpenRouter и токены Telegram в журналах скрываются автоматически.
Вопросы и ответы моделей в пакете есть — не пересылайте его, если они конфиденциальны.
"""


def bundle(runs_dir: str | Path, config_path: str | Path | None = None, *, stem: str | None = None,
           control_dir: str | Path | None = None, max_log_bytes: int = 3_000_000) -> bytes:
    """Zip-пакет для разбора: журналы программы, упавшие заседания, выбранное заседание, версия, настройки."""
    runs = Path(runs_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README)
        if config_path and Path(config_path).is_file():
            z.writestr("config.yaml", redact(Path(config_path).read_text(encoding="utf-8", errors="replace")))
        if control_dir:
            for name in ("version.json", "status.json", "update.log"):
                p = Path(control_dir) / name
                if p.is_file():
                    z.writestr(f"program/{name}", redact(p.read_text(encoding="utf-8", errors="replace")))
        logs = sorted((runs / LOG_DIR).glob("*.log*"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if (runs / LOG_DIR).is_dir() else []
        used = 0
        for p in logs:
            size = p.stat().st_size
            if used + size > max_log_bytes:
                continue
            used += size
            z.writestr(f"logs/{p.name}", redact(p.read_text(encoding="utf-8", errors="replace")))
        failed = sorted((runs / FAILED_DIR).glob("*.json"), reverse=True)[:10] if (runs / FAILED_DIR).is_dir() else []
        for p in failed:
            z.writestr(f"failed/{p.name}", p.read_text(encoding="utf-8", errors="replace"))
        if stem:
            ev = runs / f"{stem}.events.jsonl"
            events: list[dict[str, Any]] = []
            if ev.is_file():
                raw = ev.read_text(encoding="utf-8", errors="replace")
                z.writestr("session/events.jsonl", redact(raw))
                events = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
            fp = runs / FAILED_DIR / f"{stem}.json"
            if fp.is_file():
                fdata = json.loads(fp.read_text(encoding="utf-8"))
                z.writestr("session/failed.json", json.dumps(fdata, ensure_ascii=False, indent=1))
                events = events or fdata.get("events") or []
                z.writestr("session/transcript.json", json.dumps(fdata.get("transcript") or [], ensure_ascii=False, indent=1))
            js = runs / f"{stem}.json"
            if js.is_file():
                data = json.loads(js.read_text(encoding="utf-8"))
                z.writestr("session/transcript.json", json.dumps(data.get("transcript") or [], ensure_ascii=False, indent=1))
                z.writestr("session/result.json", json.dumps({k: v for k, v in data.items() if k != "transcript"},
                                                            ensure_ascii=False, indent=1))
            if events:
                z.writestr("session/events.txt", events_text(events))
    return buf.getvalue()
