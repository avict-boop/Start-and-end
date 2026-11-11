"""Приостановка и продолжение заседаний.

Пока заседание идёт, на диске лежит его «паспорт» (`runs/paused/<id>/session.json`: настройки, вопрос, вложения,
зерно случайности) и журнал (`journal.jsonl`): каждый готовый ответ модели и каждый потраченный цент записываются
сразу. Если заседание прервалось — кончился бюджет или деньги на OpenRouter, сбой, перезапуск сервера при
обновлении, — его можно продолжить: оно проходит те же шаги заново, но уже полученные ответы берутся из журнала
бесплатно и мгновенно, а платные запросы идут только туда, где ответа ещё нет. Потраченные деньги не теряются.

После успешного завершения папка удаляется: заседание уже в архиве.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .attachments import Attachment, load_from_dir, save_files
from .config import CouncilConfig

log = logging.getLogger("council.checkpoint")

PAUSED_DIR = "paused"
SPEC = "session.json"
JOURNAL = "journal.jsonl"
LOCK = ".lock"

# почему заседание стоит
REASON_TEXT = {
    "budget": "достигнут лимит бюджета заседания",
    "credits": "на балансе OpenRouter кончились деньги",
    "key_limit": "API-ключ OpenRouter не пускает дальше (лимит расходов ключа или неверный ключ)",
    "stopped": "остановлено вручную",
    "error": "сбой",
    "interrupted": "прервано перезапуском программы (например, при обновлении)",
}


def call_key(*parts: Any) -> str:
    """Отпечаток запроса к модели: тот же запрос при продолжении — тот же ответ из журнала."""
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Journal:
    """Журнал заседания: потраченные деньги, записи стенограммы, готовые ответы и окончательные сбои участников."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.results: dict[str, dict[str, Any]] = {}
        self.failures: dict[str, str] = {}
        self.costs: list[dict[str, Any]] = []
        self.transcript: list[dict[str, Any]] = []
        # Ответы из стенограммы архивного заседания (для «доделать»): (этап, участник, номер обращения) -> ответ
        self.loose: dict[tuple[str, str, int], dict[str, Any]] = {}
        if path is not None and path.is_file():
            self._load()
        loose = (path.parent / "archive_transcript.json") if path is not None else None
        if loose is not None and loose.is_file():
            try:
                self.load_transcript(json.loads(loose.read_text(encoding="utf-8")))
            except ValueError:
                pass
        # Ответы из собственной стенограммы журнала — запасной путь, если точный отпечаток запроса не совпал:
        # после обновления программы тексты запросов меняются, и без этого продолжение начиналось бы заново.
        if self.transcript:
            self.load_transcript(self.transcript)

    def load_transcript(self, transcript: list[dict[str, Any]]) -> None:
        """Готовые ответы из стенограммы: по каждому участнику и этапу — по порядку обращений."""
        count: dict[tuple[str, str], int] = {}
        for e in transcript:
            # принятый ответ — запись с «parsed» (в том числе склеенный из продолжений длинного ответа)
            if not isinstance(e, dict) or e.get("parsed") is None:
                continue
            k = (e.get("stage", ""), e.get("member", ""))
            n = e["call"] if isinstance(e.get("call"), int) else count.get(k, 0)  # номер обращения, если записан
            self.loose[(*k, n)] = e["parsed"]
            count[k] = n + 1

    def _load(self) -> None:
        assert self.path is not None
        fail_stage: dict[str, str] = {}
        last_stage = ""
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue  # недописанная строка при аварийной остановке
            kind = r.get("t")
            if kind == "ok":
                self.results[r["key"]] = r["parsed"]
                self.failures.pop(r["key"], None)
                last_stage = r.get("stage", last_stage)
            elif kind == "fail":
                self.failures[r["key"]] = r.get("error", "")
                fail_stage[r["key"]] = last_stage = r.get("stage", "")
            elif kind == "cost":
                self.costs.append(r)
            elif kind == "entry":
                self.transcript.append(r["entry"])
        # Отказы последнего этапа — вероятная причина остановки (например, нет кворума): при продолжении их
        # спрашиваем заново. Отказы более ранних этапов повторяем как были, чтобы ход заседания (метки
        # вариантов, кто что оценивал) не изменился и оплаченные ответы пригодились.
        for key, stage in fail_stage.items():
            if stage == last_stage:
                self.failures.pop(key, None)

    @property
    def replayable(self) -> int:
        return len(self.results)

    def _write(self, rec: dict[str, Any]) -> None:
        if self.path is None:
            return
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except OSError as e:  # журнал не должен ронять заседание
            log.warning("журнал заседания не записан: %s", e)

    def ok(self, key: str, parsed: dict[str, Any], stage: str = "") -> None:
        self.results[key] = parsed
        self._write({"t": "ok", "key": key, "stage": stage, "parsed": parsed})

    def fail(self, key: str, error: str, stage: str = "") -> None:
        self.failures[key] = error
        self._write({"t": "fail", "key": key, "stage": stage, "error": error})

    def cost(self, member: str, calls: int, prompt: int, completion: int, cached: int, cost: float) -> None:
        rec = {"t": "cost", "member": member, "calls": calls, "prompt": prompt, "completion": completion,
               "cached": cached, "cost": cost}
        self.costs.append(rec)
        self._write(rec)

    def entry(self, entry: dict[str, Any]) -> None:
        self._write({"t": "entry", "entry": entry})

    @property
    def spent(self) -> float:
        return sum(float(c.get("cost") or 0) for c in self.costs)


@dataclass
class Checkpoint:
    """Сохранённое заседание, которое можно продолжить."""

    dir: Path
    spec: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.dir.name

    @property
    def journal_path(self) -> Path:
        return self.dir / JOURNAL

    # ---------------------------------------------------------------- создание и чтение
    @classmethod
    def create(cls, runs_dir: Path, sid: str, *, cfg: CouncilConfig, question: str, mode: str, rounds: int,
               criteria: list[str] | None, options: list[str] | None, context: str | None,
               attachments: list[Attachment], history: list[dict[str, Any]], followup_of: str,
               vision: dict[str, Any], request: dict[str, Any], origin: str, mock: bool = False,
               chat_id: int | None = None, extra: dict[str, Any] | None = None) -> Checkpoint:
        d = Path(runs_dir) / PAUSED_DIR / sid
        d.mkdir(parents=True, exist_ok=True)
        save_files(attachments, d / "files")
        spec = {
            "id": sid, "created": time.time(), "updated": time.time(), "status": "running", "reason": "",
            "message": "", "origin": origin, "chat_id": chat_id, "mock": mock,
            "seed": random.randrange(1 << 30), "config": cfg.model_dump(mode="json"),
            "question": question, "mode": mode, "rounds": rounds, "criteria": criteria, "options": options,
            "context": context, "attachments": [a.meta() for a in attachments], "history": history,
            "followup_of": followup_of, "vision": vision, "request": request, "spent": 0.0, "resumes": 0,
            **(extra or {}),
        }
        cp = cls(d, spec)
        cp._save()
        return cp

    @classmethod
    def from_archive(cls, runs_dir: Path, sid: str, data: dict[str, Any], base: CouncilConfig, *, budget: float,
                     vision: dict[str, Any], origin: str = "web", chat_id: int | None = None) -> Checkpoint:
        """«Доделать» заседание из архива: оплаченные ответы берутся из его стенограммы, заново спрашиваются
        только шаги, где модели не ответили (например, кончился лимит ключа). Итог заменит архивную запись."""
        from .config import with_overrides
        req = data.get("request") or {}
        known = {m.name for m in base.members}
        members = [m for m in req.get("members") or [] if m in known] or None
        chair = req.get("chair") if req.get("chair") in known else None
        cfg = with_overrides(base, members=members, chair=chair,
                             devil_advocate=req.get("devil_advocate") if req.get("devil_advocate") in known else "",
                             budget=budget, chair_proposes=req.get("chair_proposes"),
                             fact_check=req.get("fact_check"), compact=req.get("compact_proposals"),
                             research_words=req.get("research_words"), research_shorten=req.get("research_shorten"))
        folder = Path(runs_dir) / f"{sid}.files"
        authors = {k: v for k, v in (data.get("authors") or {}).items() if v}
        cp = cls.create(runs_dir, sid, cfg=cfg, question=data.get("question", ""), mode=req.get("mode") or data.get("mode"),
                        rounds=req.get("rounds") or 1, criteria=req.get("criteria") or None,
                        options=req.get("options") or None, context=req.get("context") or None, attachments=[],
                        history=data.get("thread") or [], followup_of=data.get("followup_of") or "", vision=vision,
                        request={**req, "budget": budget}, origin=origin, chat_id=chat_id,
                        extra={"redo_of": sid, "labels": authors, "resumes": 1})
        if folder.is_dir():  # вложения копируются как есть: разбираются один раз, при запуске заседания
            shutil.copytree(folder, cp.dir / "files", dirs_exist_ok=True)
            cp.spec["attachments"] = data.get("attachments") or []
        transcript = data.get("transcript") or []
        (cp.dir / "archive_transcript.json").write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
        j = Journal(cp.journal_path)
        for name, st in (data.get("stats") or {}).items():  # уже потраченное остаётся в счёте заседания
            j.cost(name, int(st.get("calls") or 0), int(st.get("prompt_tokens") or 0), int(st.get("completion_tokens") or 0),
                   int(st.get("cached_tokens") or 0), float(st.get("cost") or 0))
        for e in transcript:
            j.entry(e)
        cp.update(spent=round(j.spent, 6))
        return cp

    @classmethod
    def load(cls, runs_dir: Path, sid: str) -> Checkpoint | None:
        d = Path(runs_dir) / PAUSED_DIR / sid
        try:
            cp = cls(d, json.loads((d / SPEC).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
        if cp.stale(runs_dir):
            cp.remove()
            return None
        return cp

    def stale(self, runs_dir: Path) -> bool:
        """Итог этого заседания уже в архиве и он новее паузы (процесс прервался, не успев её снять)."""
        try:
            done = (Path(runs_dir) / f"{self.id}.json").stat().st_mtime
        except OSError:
            return False
        return done >= float(self.spec.get("updated") or 0) and not self.is_live()

    @classmethod
    def all(cls, runs_dir: Path) -> list[Checkpoint]:
        root = Path(runs_dir) / PAUSED_DIR
        if not root.is_dir():
            return []
        out = []
        for d in root.iterdir():
            cp = cls.load(runs_dir, d.name) if d.is_dir() else None
            if cp is None:
                continue
            if cp.stale(runs_dir):
                cp.remove()
                continue
            out.append(cp)
        return sorted(out, key=lambda c: c.spec.get("created", 0), reverse=True)

    def _save(self) -> None:
        self.spec["updated"] = time.time()
        tmp = self.dir / (SPEC + ".tmp")
        tmp.write_text(json.dumps(self.spec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.dir / SPEC)

    def update(self, **kw: Any) -> None:
        self.spec.update(kw)
        try:
            self._save()
        except OSError as e:
            log.warning("состояние заседания %s не записано: %s", self.id, e)

    def remove(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---------------------------------------------------------------- для запуска
    def config(self, budget: float | None = None) -> CouncilConfig:
        cfg = CouncilConfig.model_validate(self.spec["config"])
        if budget:
            cfg = cfg.model_copy(update={"max_cost_usd": float(budget)})
        return cfg

    def attachments(self) -> list[Attachment]:
        return load_from_dir(self.dir / "files", self.spec.get("attachments") or [])

    def run_args(self) -> dict[str, Any]:
        s = self.spec
        return {"criteria": s.get("criteria"), "options": s.get("options"), "context": s.get("context"),
                "mode": s.get("mode"), "rounds": s.get("rounds"), "history": s.get("history") or [],
                "followup_of": s.get("followup_of") or ""}

    @property
    def budget(self) -> float:
        return float(self.spec.get("config", {}).get("max_cost_usd") or 0)

    def set_budget(self, budget: float) -> None:
        self.spec.setdefault("config", {})["max_cost_usd"] = float(budget)
        self.update()

    def lock(self) -> Any:
        """Блокировка «заседание сейчас идёт» между процессами (сайт и бот), снимается при выходе процесса."""
        from .archive import _ProcessLock
        return _ProcessLock(self.dir / LOCK)

    def is_live(self) -> bool:
        """Идёт ли это заседание прямо сейчас в каком-либо процессе."""
        with self.lock() as got:
            return not got

    @property
    def state(self) -> str:
        """running (идёт) | paused (стоит, причина в reason) — «running» без живого процесса значит «прервано»."""
        st = self.spec.get("status", "paused")
        if st == "running" and not self.is_live():
            return "interrupted"
        return st

    def spent_from_journal(self) -> float:
        """Сколько потрачено — по журналу (для заседания, прерванного перезапуском, пока оно не записало итог)."""
        return Journal(self.journal_path).spent if self.journal_path.is_file() else 0.0

    def summary(self) -> dict[str, Any]:
        s = self.spec
        state = self.state
        reason = "interrupted" if state == "interrupted" else s.get("reason", "")
        return {"id": self.id, "question": s.get("question", ""), "mode": s.get("mode", ""), "status": state,
                "reason": reason, "reason_text": REASON_TEXT.get(reason, reason), "message": s.get("message", ""),
                "spent": round(float(s.get("spent") or 0) or self.spent_from_journal(), 4), "budget": self.budget,
                "stage": s.get("stage", ""),
                "origin": s.get("origin", ""), "chat_id": s.get("chat_id"), "mock": bool(s.get("mock")),
                "created": s.get("created"), "updated": s.get("updated"), "resumes": s.get("resumes", 0),
                "members": [m.get("name") for m in s.get("config", {}).get("members", [])
                            if m.get("enabled", True)]}


def classify(exc: BaseException) -> tuple[str, str]:
    """Причина остановки и текст для человека."""
    from .client import KeyLimit, OutOfCredits
    from .session import BudgetExceeded
    import asyncio
    if isinstance(exc, BudgetExceeded):
        return "budget", str(exc)
    if isinstance(exc, KeyLimit):
        return "key_limit", str(exc)
    if isinstance(exc, OutOfCredits):
        return "credits", str(exc)
    if isinstance(exc, asyncio.CancelledError):
        return "stopped", "остановлено вручную"
    return "error", str(exc) or type(exc).__name__
