"""Запросы в работе у всех идущих заседаний (сайт и Telegram-бот — разные процессы): сколько денег уже
«в полёте». Нужен для общего лимита ключа OpenRouter — один файл с блокировкой, без базы данных."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

STALE_S = 1800  # запись старше получаса — заседание, вероятно, прервалось: не учитываем


class InFlight:
    def __init__(self, folder: Any, session_id: str) -> None:
        self.path = Path(folder) / "inflight.json"
        self.sid = session_id

    def _update(self, fn: Any) -> Any:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
        except ImportError:  # без блокировок (Windows) — лучше приблизительно, чем никак
            fcntl = None
        with open(self.path, "a+", encoding="utf-8") as f:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                data = json.loads(f.read() or "{}")
            except ValueError:
                data = {}
            now = time.time()
            data = {k: v for k, v in data.items() if isinstance(v, dict) and now - v.get("t", 0) < STALE_S}
            out = fn(data, now)
            f.seek(0)
            f.truncate()
            f.write(json.dumps(data))
            f.flush()
            os.fsync(f.fileno())
            return out

    def set(self, reserved: float) -> None:
        def fn(data: dict[str, Any], now: float) -> None:
            if reserved > 0:
                data[self.sid] = {"usd": round(reserved, 4), "t": now}
            else:
                data.pop(self.sid, None)
        try:
            self._update(fn)
        except OSError:
            pass

    def others(self) -> float:
        try:
            return self._update(lambda data, now: sum(float(v.get("usd") or 0) for k, v in data.items() if k != self.sid))
        except OSError:
            return 0.0
