"""Единое представление заседания для всех форматов отчёта.

Строится из словаря стенограммы (SessionResult.to_dict() или сохранённый .json), поэтому
отчёты можно в любой момент пересобрать и для старых заседаний.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from statistics import mean
from typing import Any

import markdown as _markdown

MODE_NAME = {"debate": "Дебаты", "quick": "Быстрый", "evaluate": "Оценка вариантов"}
STAGE_NAME = {"framing": "Постановка", "proposal": "Предложения", "critique": "Критика",
              "revision": "Доработка", "vote": "Голосование", "verdict": "Вердикт"}


@dataclass
class View:
    d: dict[str, Any]
    stem: str = ""

    # ------------------------------------------------------------ основное
    @property
    def question(self) -> str:
        return self.d.get("question", "")

    @property
    def title_text(self) -> str:
        """Короткий заголовок отчёта (первая строка запроса)."""
        return short_title(self.question)

    @property
    def question_is_long(self) -> bool:
        return self.question.strip() != self.title_text

    @property
    def refined(self) -> str:
        r = self.d.get("refined_question") or ""
        return r if r and r != self.question else ""

    @property
    def criteria(self) -> list[str]:
        return list(self.d.get("criteria") or [])

    @property
    def mode(self) -> str:
        return MODE_NAME.get(self.d.get("mode", ""), self.d.get("mode", ""))

    @property
    def date(self) -> str:
        raw = self.d.get("started_at") or ""
        try:
            return datetime.fromisoformat(raw).astimezone().strftime("%d.%m.%Y %H:%M")
        except ValueError:
            m = re.match(r"(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})", self.stem)
            return f"{m[3]}.{m[2]}.{m[1]} {m[4]}:{m[5]}" if m else ""

    @property
    def cost(self) -> float:
        return float(self.d.get("total_cost_usd") or sum((s or {}).get("cost", 0) for s in self.stats.values()))

    @property
    def duration(self) -> str:
        s = float(self.d.get("duration_s") or 0)
        return f"{int(s // 60)} мин {int(s % 60)} с" if s >= 60 else f"{s:.0f} с"

    @property
    def stats(self) -> dict[str, dict]:
        return self.d.get("stats") or {}

    @property
    def members(self) -> list[str]:
        return list(self.stats)

    # ------------------------------------------------------------ варианты и голосование
    @property
    def verdict(self) -> dict[str, Any]:
        return self.d.get("verdict") or {}

    @property
    def winner(self) -> str:
        return self.verdict.get("winner", "")

    @property
    def proposals(self) -> dict[str, dict]:
        return dict(sorted((self.d.get("proposals") or {}).items()))

    def initial(self, label: str) -> dict | None:
        return (self.d.get("initial_proposals") or {}).get(label)

    def revised(self, label: str) -> bool:
        ini = self.initial(label)
        return bool(ini) and ini != self.proposals.get(label)

    def author(self, label: str) -> str:
        a = (self.d.get("authors") or {}).get(label)
        return a or "пользователь"

    def title(self, label: str) -> str:
        return (self.proposals.get(label) or {}).get("title", "")

    @property
    def tally(self) -> list[dict]:
        return list(self.d.get("tally") or [])

    def place(self, label: str) -> int | None:
        for i, r in enumerate(self.tally, 1):
            if r.get("label") == label:
                return i
        return None

    @property
    def ballots(self) -> dict[str, dict]:
        return self.d.get("ballots") or {}

    @property
    def critiques(self) -> dict[str, dict]:
        return self.d.get("critiques") or {}

    def reviews_for(self, label: str) -> list[tuple[str, dict]]:
        out = []
        for reviewer, cs in self.critiques.items():
            for r in cs.get("reviews", []):
                if r.get("label") == label:
                    out.append((reviewer, r))
        return out

    def matrix(self) -> tuple[list[str], list[tuple[str, list[float | None]]], list[float | None]]:
        """(метки, [(рецензент, [средняя оценка по каждой метке])], средние по столбцам)."""
        labels = list(self.proposals)
        rows = []
        cols: dict[str, list[float]] = {l: [] for l in labels}
        for reviewer, cs in self.critiques.items():
            by = {r.get("label"): r for r in cs.get("reviews", [])}
            vals: list[float | None] = []
            for l in labels:
                r = by.get(l)
                if r and r.get("scores"):
                    v = mean(float(x) for x in r["scores"].values())
                    cols[l].append(v)
                    vals.append(v)
                else:
                    vals.append(None)
            rows.append((reviewer, vals))
        return labels, rows, [mean(cols[l]) if cols[l] else None for l in labels]

    def failures(self) -> list[tuple[str, str, str]]:
        out = []
        for name, s in self.stats.items():
            for f in (s or {}).get("failures", []):
                stage, _, err = f.partition(": ")
                out.append((name, STAGE_NAME.get(stage, stage), err))
        return out


# ------------------------------------------------------------------ Markdown → HTML

_LIST = re.compile(r"^(\s*)([-*+•]|\d+[.)])\s+")
_TABLE = re.compile(r"^\s*\|")
_HEAD = re.compile(r"^\s{0,3}#{1,6}\s")


def normalize_md(text: str) -> str:
    """Приводит Markdown от моделей к виду, который понимает Python-Markdown.

    Модели часто пишут список или таблицу сразу после абзаца (без пустой строки)
    и делают вложенность в 2 пробела; маркер «•» — тоже частый гость.
    """
    lines = str(text or "").replace("\r\n", "\n").replace("\t", "    ").split("\n")
    indents = [len(m.group(1)) for m in map(_LIST.match, lines) if m and m.group(1)]
    unit = min(indents) if indents else 4  # шаг вложенности, которым пользовалась модель
    out: list[str] = []
    in_code = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_code = not in_code
            if in_code and out and out[-1].strip():
                out.append("")
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        m = _LIST.match(line)
        if m:
            level = round(len(m.group(1)) / unit)
            marker = "-" if m.group(2) == "•" else m.group(2)
            line = "    " * level + marker + " " + line[m.end():]
        prev = out[-1] if out else ""
        block_start = bool(m) or _TABLE.match(line) or _HEAD.match(line)
        pm = _LIST.match(prev)
        same_list = bool(pm and m) and not (
            not pm.group(1) and not m.group(1) and pm.group(2)[0].isdigit() != m.group(2)[0].isdigit()
        )  # смена «- …» на «1. …» на верхнем уровне — это новый список
        prev_same = same_list or (_TABLE.match(prev) and _TABLE.match(line))
        if block_start and prev.strip() and not prev_same:
            out.append("")
        if prev.strip() and _HEAD.match(prev) and line.strip():
            out.append("")
        elif pm and not m and line.strip() and not line[:1].isspace() and not _TABLE.match(line):
            out.append("")  # обычный абзац сразу после списка — конец списка, а не продолжение пункта
        out.append(line)
    return "\n".join(out)


_TABLE_HTML = re.compile(r"<table>(.*?)</table>", re.S)


def _wrap_table(m: re.Match) -> str:
    """Оборачивает таблицу и помечает её ширину: широким — мельче шрифт (PDF) и прокрутка (экран)."""
    body = m.group(1)
    first_row = re.search(r"<tr>(.*?)</tr>", body, re.S)
    cols = len(re.findall(r"<t[hd][ >]", first_row.group(1))) if first_row else 0
    size = "xwide" if cols >= 8 else "wide" if cols >= 6 else "normal"
    return f'<div class="tbl"><table class="mdt {size}" data-cols="{cols}">{body}</table></div>'


def short_title(text: str, limit: int = 110) -> str:
    """Первая содержательная строка без Markdown-разметки, не длиннее limit (обрезка по слову)."""
    for raw in str(text or "").splitlines():
        line = re.sub(r"^\s*(#+|[-*+•>]|\d+[.)])\s+", "", raw)
        line = re.sub(r"[*_`]+", "", line).strip()
        if line:
            break
    else:
        return ""
    many_lines = len([x for x in str(text).splitlines() if x.strip()]) > 1
    # длинный запрос: в заголовок — первое предложение, если оно короткое
    m = re.match(r"(.{12,90}?[.!?…])(\s|$)", line)
    if m and (many_lines or len(line) > limit):
        return m.group(1)
    if len(line) <= limit:
        return line
    cut = line[:limit].rsplit(" ", 1)[0] if " " in line[:limit] else line[:limit]
    return cut.rstrip(",;:—-– ") + "…"


def md_to_html(text: str, *, breaks: bool = False) -> str:
    """Безопасно: сырой HTML из ответа модели не исполняется, а выводится как текст.

    breaks=True сохраняет одиночные переносы строк (для текста запроса, написанного человеком).
    """
    ext = ["tables", "fenced_code", "sane_lists"] + (["nl2br"] if breaks else [])
    md = _markdown.Markdown(extensions=ext, output_format="html")
    md.preprocessors.deregister("html_block")
    md.inlinePatterns.deregister("html")
    return _TABLE_HTML.sub(_wrap_table, md.convert(normalize_md(text)))
