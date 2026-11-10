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
    files_dir: Any = None  # папка с вложениями заседания (Path) — для картинок в отчёте

    @property
    def followup_title(self) -> str:
        """Название заседания, к которому это уточнение (или пусто)."""
        thread = self.d.get("thread") or []
        return (thread[-1].get("title") or thread[-1].get("question", "")[:100]) if thread and self.d.get("followup_of") else ""

    @property
    def attachments(self) -> list[dict[str, Any]]:
        return list(self.d.get("attachments") or [])

    @property
    def image_notes(self) -> dict[str, str]:
        return dict(self.d.get("image_notes") or {})

    def attachment_label(self, m: dict[str, Any]) -> str:
        """«отчёт.pdf — PDF, 12 стр., 340 КБ» и т. п."""
        from ..attachments import human_size
        bits = []
        if m.get("kind") == "image":
            bits.append(f"картинка {m.get('width')}×{m.get('height')}" if m.get("width") else "картинка")
        else:
            bits.append(str(m.get("name", "")).rsplit(".", 1)[-1].upper())
            if m.get("pages"):
                bits.append(f"{m['pages']} стр.")
            if m.get("chars"):
                bits.append(f"{m['chars']:,} симв.".replace(",", " ") + (" (обрезан)" if m.get("truncated") else ""))
        bits.append(human_size(int(m.get("size") or 0)))
        return f"{m.get('name', '')} — " + ", ".join(bits)

    def thumb_data_url(self, meta: dict[str, Any], max_side: int = 900) -> str:
        """Уменьшенная копия картинки для отчёта (data URL) или пустая строка."""
        data = self.attachment_bytes(meta)
        if not data:
            return ""
        import base64
        import io
        try:
            from PIL import Image, ImageOps
            img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
            img.thumbnail((max_side, max_side))
            out = io.BytesIO()
            img.convert("RGB").save(out, "JPEG", quality=82)
            return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")
        except Exception:  # noqa: BLE001 — битая картинка не должна ломать отчёт
            return ""

    def attachment_bytes(self, meta: dict[str, Any]) -> bytes | None:
        if not self.files_dir:
            return None
        from pathlib import Path
        p = Path(self.files_dir) / str(meta.get("file", ""))
        try:
            return p.read_bytes() if p.is_file() else None
        except OSError:
            return None

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
    def agreement(self) -> dict | None:
        """Согласие совета; для старых заседаний считается по сохранённым бюллетеням."""
        from ..voting import agreement
        if self.d.get("agreement"):
            return self.d["agreement"]
        return agreement({m: (b or {}).get("ranking", []) for m, b in (self.d.get("ballots") or {}).items()})

    @property
    def agreement_line(self) -> str:
        """«Согласие совета: высокое (τ = 0.67) — пояснение» или пустая строка."""
        from ..voting import AGREEMENT_HINT, agreement_text
        a = self.agreement
        return f"{agreement_text(a)} — {AGREEMENT_HINT[a['level']]}" if a else ""

    @property
    def agreement_low(self) -> bool:
        a = self.agreement
        return bool(a and a.get("level") == "low")

    @property
    def tally(self) -> list[dict]:
        return list(self.d.get("tally") or [])

    # ------------------------------------------------------------ проверка бюллетеней и фактов

    @property
    def ballot_flags(self) -> dict[str, dict]:
        return dict(self.d.get("ballot_flags") or {})

    @property
    def flag_notes(self) -> list[str]:
        """«kimi — ставит на последнее место вариант, который остальные считают лучшим (τ = −0.33)»."""
        from ..voting import FLAG_NAME
        out = []
        for voter, f in self.ballot_flags.items():
            tau = f" (τ = {f['tau']:.2f})" if f.get("tau") is not None else ""
            out.append(f"{voter} — " + "; ".join(FLAG_NAME.get(k, k) for k in f.get("flags", [])) + tau)
        return out

    @property
    def robust_note(self) -> str:
        """Меняется ли лидер без помеченных бюллетеней."""
        if not self.ballot_flags:
            return ""
        lead = self.tally[0].get("label") if self.tally else None
        alt = self.d.get("leader_without_flagged")
        if alt and alt != lead:
            return f"Без этих бюллетеней лидером был бы вариант {alt} — победа зависит от спорных голосов."
        return "Без этих бюллетеней лидер тот же — результат устойчив."

    @property
    def fact_check(self) -> dict[str, dict]:
        return dict(self.d.get("fact_check") or {})

    FACT_ICON = {"confirmed": "✅", "refuted": "❌", "unclear": "❔"}
    FACT_NAME = {"confirmed": "подтверждено", "refuted": "опровергнуто", "unclear": "не удалось проверить"}

    @property
    def fact_counts(self) -> dict[str, int]:
        c = {"confirmed": 0, "refuted": 0, "unclear": 0}
        for r in self.fact_check.values():
            for cl in r.get("claims", []):
                c[cl.get("status") if cl.get("status") in c else "unclear"] += 1
        return c

    @property
    def fact_line(self) -> str:
        """«проверены варианты B, A: ✅ 4 · ❌ 1 · ❔ 2» или пустая строка."""
        if not self.fact_check:
            return ""
        c = self.fact_counts
        return (f"проверены варианты {', '.join(self.fact_check)}: ✅ {c['confirmed']} подтверждено · "
                f"❌ {c['refuted']} опровергнуто · ❔ {c['unclear']} не удалось проверить")

    @staticmethod
    def safe_url(u: str) -> str:
        u = str(u or "").strip()
        return u if u.lower().startswith(("http://", "https://")) else ""

    @staticmethod
    def pairwise_str(row: dict) -> str:
        """Попарный счёт (побед минус поражений) или «—» для старых заседаний."""
        return "—" if row.get("pairwise") is None else f"{float(row['pairwise']):+.0f}"

    @property
    def tie_notes(self) -> list[str]:
        """Пояснения, чем решены ничьи по баллу Борда."""
        from ..voting import TIE_NAME
        rows = self.tally
        return [f"{a.get('label')} выше {b.get('label')}: при равном балле Борда решило "
                f"{TIE_NAME.get(a['tie_break'], a['tie_break'])}"
                for a, b in zip(rows, rows[1:]) if a.get("tie_break")]

    @property
    def chair_note(self) -> str:
        """«Председатель claude — без своего варианта» (только для новых заседаний, где это так)."""
        if self.d.get("chair_proposed", True) or self.d.get("mode") == "evaluate":
            return ""
        chair = (self.d.get("request") or {}).get("chair") or ""
        return f"председатель {chair} — без своего варианта (только постановка, оценка и итог)" if chair else \
            "председатель — без своего варианта (только постановка, оценка и итог)"

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
