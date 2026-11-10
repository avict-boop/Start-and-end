"""Отчёт в DOCX (Word). Форматирование ответов моделей переносится из того же HTML, что и в PDF."""
from __future__ import annotations

import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor
from lxml import html as LH

from .view import View, md_to_html

MONO = "Consolas"
ACCENT = RGBColor(0x45, 0x45, 0xC8)
MUTED = RGBColor(0x6B, 0x6B, 0x80)
GREEN = RGBColor(0x1F, 0x7F, 0x55)
RED = RGBColor(0xB4, 0x23, 0x3A)


# ------------------------------------------------------------------ низкоуровневые помощники

def _shade(cell_or_par, fill: str) -> None:
    """Заливка ячейки таблицы или абзаца (hex без #)."""
    el = cell_or_par._tc if hasattr(cell_or_par, "_tc") else cell_or_par._p
    pr = el.get_or_add_tcPr() if hasattr(cell_or_par, "_tc") else el.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    pr.append(shd)


def _hyperlink(par, text: str, url: str) -> None:
    rel = par.part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                             is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "4545C8")
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rpr.append(color)
    rpr.append(u)
    run.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    par._p.append(link)


def _page_number_footer(doc) -> None:
    par = doc.sections[0].footer.paragraphs[0]
    par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = par.add_run("Совет нейросетей · стр. ")
    run.font.size = Pt(8)
    run.font.color.rgb = MUTED
    for instr in ("PAGE",):
        fld = OxmlElement("w:fldSimple")
        fld.set(qn("w:instr"), instr)
        r = OxmlElement("w:r")
        t = OxmlElement("w:t")
        t.text = "1"
        r.append(t)
        fld.append(r)
        par._p.append(fld)


def _widths(table, cm: list[float]) -> None:
    """Ширина колонок (Word учитывает ширину ячеек, а не колонок)."""
    from docx.shared import Cm
    table.autofit = False
    for row in table.rows:
        for cell, w in zip(row.cells, cm):
            cell.width = Cm(w)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text)


# ------------------------------------------------------------------ HTML (из Markdown) → DOCX

class _Writer:
    def __init__(self, container) -> None:
        self.c = container  # Document или ячейка таблицы

    # --- строчные элементы
    def inline(self, par, el, fmt: dict | None = None) -> None:
        fmt = dict(fmt or {})
        if el.text:
            self._run(par, el.text, fmt)
        for child in el:
            tag = child.tag if isinstance(child.tag, str) else ""
            f = dict(fmt)
            if tag in ("strong", "b"):
                f["bold"] = True
            elif tag in ("em", "i"):
                f["italic"] = True
            elif tag == "code":
                f["code"] = True
            if tag == "br":
                par.add_run().add_break()
            elif tag == "a" and (child.get("href") or "").startswith(("http://", "https://")):
                _hyperlink(par, child.text_content(), child.get("href"))
            elif tag in ("ul", "ol", "table", "pre", "p", "blockquote"):
                pass  # блочные элементы внутри пункта списка обрабатываются отдельно
            else:
                self.inline(par, child, f)
            if child.tail:
                # после переноса строки не начинаем новую строку с пробела
                self._run(par, child.tail.lstrip() if tag == "br" else child.tail, fmt)

    def _run(self, par, text: str, fmt: dict) -> None:
        text = text if fmt.get("pre") else _clean(text)
        if not text:
            return
        run = par.add_run(text)
        run.bold = fmt.get("bold") or None
        run.italic = fmt.get("italic") or None
        if fmt.get("code"):
            run.font.name = MONO
            run.font.size = Pt(9)

    # --- блочные элементы
    def block(self, el, level: int = 0) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        if tag == "p":
            self.inline(self.c.add_paragraph(), el)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            par = self.c.add_paragraph()
            run = par.add_run(_clean(el.text_content()))
            run.bold = True
            run.font.size = Pt(12 if tag in ("h1", "h2") else 11)
        elif tag in ("ul", "ol"):
            self.list(el, level)
        elif tag == "pre":
            par = self.c.add_paragraph()
            _shade(par, "F3F3F8")
            run = par.add_run(el.text_content().rstrip("\n"))
            run.font.name = MONO
            run.font.size = Pt(9)
        elif tag == "blockquote":
            for child in el:
                par = self.c.add_paragraph()
                par.paragraph_format.left_indent = Pt(14)
                self.inline(par, child, {"italic": True})
        elif tag == "table":
            self.table(el)
        elif tag == "hr":
            self.c.add_paragraph("—" * 20)
        elif tag in ("div", "section"):  # обёртки (например, блок прокрутки таблицы) — заходим внутрь
            for child in el:
                self.block(child, level)
        elif tag:
            self.inline(self.c.add_paragraph(), el)

    def list(self, el, level: int) -> None:
        ordered = el.tag == "ol"
        for n, li in enumerate([x for x in el if getattr(x, "tag", "") == "li"], 1):
            par = self.c.add_paragraph()
            par.paragraph_format.left_indent = Pt(18 + 16 * level)
            par.paragraph_format.first_line_indent = Pt(-12)
            par.paragraph_format.space_after = Pt(1)
            par.add_run(f"{n}. " if ordered else ("•  " if level == 0 else "◦  "))
            inner_p = [x for x in li if getattr(x, "tag", "") == "p"]
            if inner_p:  # «рыхлый» список: текст пункта в <p>
                self.inline(par, inner_p[0])
            else:
                self.inline(par, li)
            for child in li:
                if getattr(child, "tag", "") in ("ul", "ol"):
                    self.list(child, level + 1)
                elif getattr(child, "tag", "") in ("pre", "table", "blockquote"):
                    self.block(child, level)

    def table(self, el) -> None:
        rows = el.xpath(".//tr")
        if not rows:
            return
        ncols = max(len(r.xpath("./th|./td")) for r in rows)
        t = self.c.add_table(rows=len(rows), cols=ncols)
        t.style = "Table Grid"
        t.autofit = True
        # широкие таблицы — мельче шрифт, чтобы колонки поместились на страницу
        size = Pt(7.5) if ncols >= 8 else Pt(8.5) if ncols >= 6 else None
        for i, r in enumerate(rows):
            for j, cell_el in enumerate(r.xpath("./th|./td")):
                cell = t.cell(i, j)
                par = cell.paragraphs[0]
                self.inline(par, cell_el, {"bold": cell_el.tag == "th"})
                if size:
                    for run in par.runs:
                        run.font.size = size
                if cell_el.tag == "th":
                    _shade(cell, "F2F2F8")


def inline_markdown(par, text: str, fmt: dict | None = None) -> None:
    """Короткий Markdown-текст (пункт, обоснование) — в уже созданный абзац."""
    html = md_to_html(text).strip()
    if not html:
        return
    frag = LH.fragment_fromstring(html, create_parent="div")
    blocks = list(frag)
    target = blocks[0] if len(blocks) == 1 and blocks[0].tag in ("p", "h1", "h2", "h3", "h4") else None
    if target is not None:
        _Writer(par).inline(par, target, fmt)
    else:
        par.add_run(_clean(frag.text_content()))


def add_markdown(container, text: str, *, breaks: bool = False) -> None:
    """Добавляет Markdown-текст модели в документ или ячейку с сохранением форматирования."""
    html = md_to_html(text, breaks=breaks).strip()
    if not html:
        return
    frag = LH.fragment_fromstring(html, create_parent="div")
    w = _Writer(container)
    for el in frag:
        w.block(el)


# ------------------------------------------------------------------ сам отчёт

def _heading(doc, text: str, level: int):
    h = doc.add_heading(text, level=level)
    return h


def _meta_table(doc, rows: list[tuple[str, str]]) -> None:
    t = doc.add_table(rows=len(rows), cols=2)
    t.style = "Table Grid"
    for i, (k, v) in enumerate(rows):
        a, b = t.cell(i, 0), t.cell(i, 1)
        a.paragraphs[0].add_run(k).bold = True
        _shade(a, "F2F2F8")
        b.paragraphs[0].add_run(v)


def _bullets(doc, items: list[str], color: RGBColor | None = None, prefix: str = "") -> None:
    for x in items:
        par = doc.add_paragraph()
        par.paragraph_format.left_indent = Pt(18)
        par.paragraph_format.first_line_indent = Pt(-12)
        par.paragraph_format.space_after = Pt(1)
        r = par.add_run("•  " + prefix)
        if color:
            r.font.color.rgb = color
        inline_markdown(par, x)


def _score_fill(v: float) -> str:
    import colorsys
    t = max(0.0, min(1.0, (v - 1) / 9))
    r, g, b = colorsys.hls_to_rgb(t * 130 / 360, 0.85, 0.6)
    return f"{int(r * 255):02X}{int(g * 255):02X}{int(b * 255):02X}"


def _save(doc) -> bytes:
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def render_docx(view: View, summary: bool = False) -> bytes:
    """Полный отчёт или (summary=True) только итоговое решение."""
    v = view
    vd = v.verdict
    w = v.winner
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Calibri"
    st.font.size = Pt(10.5)
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Pt(50)
    sec.top_margin = sec.bottom_margin = Pt(48)
    _page_number_footer(doc)
    doc.core_properties.title = v.title_text
    doc.core_properties.subject = "Отчёт «Совета нейросетей»"

    brand = doc.add_paragraph()
    r = brand.add_run("СОВЕТ НЕЙРОСЕТЕЙ · " + ("ИТОГОВОЕ РЕШЕНИЕ" if summary else "ОТЧЁТ О ЗАСЕДАНИИ"))
    r.font.size = Pt(8)
    r.font.color.rgb = MUTED
    r.bold = True
    title = _heading(doc, v.title_text, 1)
    for run in title.runs:
        run.font.size = Pt(16)
    if v.question_is_long:
        lab = doc.add_paragraph()
        r = lab.add_run("ЗАПРОС")
        r.bold = True
        r.font.size = Pt(8)
        r.font.color.rgb = MUTED
        add_markdown(doc, v.question, breaks=True)
    if v.refined:
        p = doc.add_paragraph()
        p.add_run("Уточнённая постановка: ").bold = True
        p.add_run(v.refined)
    meta = [("Дата", v.date), ("Режим", v.mode), ("Участники", ", ".join(v.members)),
            ("Критерии", ", ".join(v.criteria) or "—"), ("Стоимость", f"${v.cost:.4f}"),
            ("Длительность", v.duration)]
    _meta_table(doc, meta[:3] if summary else meta)

    # Решение
    if summary:
        _heading(doc, "Решение совета", 1)
        if w:
            rr = doc.add_paragraph().add_run(f"Лучший вариант: {v.title(w)} (предложил {v.author(w)})")
            rr.font.color.rgb = MUTED
            rr.font.size = Pt(9)
    else:
        _heading(doc, f"Решение совета: вариант {w} — {v.title(w)} ({v.author(w)})" if w else "Решение совета", 1)
    add_markdown(doc, vd.get("final_answer", ""))
    if vd.get("why"):
        _heading(doc, "Почему", 3)
        add_markdown(doc, vd["why"])
    if vd.get("dissent"):
        _heading(doc, "Особые мнения", 3)
        _bullets(doc, vd["dissent"])
    if vd.get("risks"):
        _heading(doc, "Риски", 3)
        _bullets(doc, vd["risks"])
    if summary:
        note = doc.add_paragraph().add_run("Это краткая версия: только итоговое решение. Полный отчёт с вариантами "
                                           "всех моделей, критикой и голосованием скачивается отдельно.")
        note.font.size = Pt(8.5)
        note.font.color.rgb = MUTED
        return _save(doc)

    # Голосование
    _heading(doc, "Голосование", 1)
    t = doc.add_table(rows=1, cols=6)
    t.style = "Table Grid"
    for j, h in enumerate(["#", "Вариант", "Автор", "Борда (0–1)", "Оценка критики", "1-х мест"]):
        t.cell(0, j).paragraphs[0].add_run(h).bold = True
        _shade(t.cell(0, j), "F2F2F8")
    for i, row in enumerate(v.tally, 1):
        l = row.get("label", "")
        avg = row.get("avg_score")
        cells = t.add_row().cells
        vals = [str(i), f"{l}: {v.title(l)}", v.author(l), f"{float(row.get('borda') or 0):.2f}",
                "—" if avg is None else f"{avg:.1f}", f"{row.get('first_places', 0)}/{row.get('ballots', 0)}"]
        for c, val in zip(cells, vals):
            c.paragraphs[0].add_run(val).bold = (l == w) or None
            if l == w:
                _shade(c, "FFF6D8")
    _widths(t, [0.8, 6.6, 2.6, 2.2, 2.2, 1.6])
    if v.ballots:
        _heading(doc, "Бюллетени", 3)
        for voter, b in v.ballots.items():
            par = doc.add_paragraph()
            par.add_run(f"{voter}: ").bold = True
            par.add_run(" › ".join(b.get("ranking", []))).bold = True
            if b.get("rationale"):
                par.add_run(" — ")
                inline_markdown(par, b["rationale"])

    # Варианты
    _heading(doc, "Варианты", 1)
    for l, p in v.proposals.items():
        title = f"Вариант {l}: {p.get('title', '')}"
        if l == w:
            title += "  — победитель"
        _heading(doc, title, 2)
        sub = [f"Автор: {v.author(l)}"]
        if v.place(l):
            sub.append(f"место в голосовании: {v.place(l)}")
        if p.get("confidence") is not None and v.author(l) != "пользователь":
            sub.append(f"уверенность автора: {round(float(p['confidence']) * 100)}%")
        if v.revised(l):
            sub.append("доработан после критики")
        rr = doc.add_paragraph().add_run(" · ".join(sub))
        rr.font.color.rgb = MUTED
        rr.font.size = Pt(9)
        add_markdown(doc, p.get("summary", ""))
        if p.get("reasoning"):
            _heading(doc, "Обоснование", 3)
            add_markdown(doc, p["reasoning"])
        if p.get("pros"):
            _heading(doc, "Плюсы", 3)
            _bullets(doc, p["pros"])
        if p.get("risks"):
            _heading(doc, "Риски", 3)
            _bullets(doc, p["risks"])
        if p.get("changes"):
            par = doc.add_paragraph()
            _shade(par, "F6F6FA")
            par.add_run("Что изменено после критики: ").bold = True
            par.add_run(_clean(p["changes"]))
        ini = v.initial(l)
        if v.revised(l) and ini:
            _heading(doc, f"Первоначальная версия: {ini.get('title', '')}", 3)
            add_markdown(doc, ini.get("summary", ""))
        reviews = v.reviews_for(l)
        if reviews:
            _heading(doc, "Оценки других участников", 3)
            for reviewer, r in reviews:
                par = doc.add_paragraph()
                par.add_run(reviewer).bold = True
                scores = ", ".join(f"{k}: {float(s):g}" for k, s in (r.get("scores") or {}).items())
                sr = par.add_run(f"  ({scores})")
                sr.font.color.rgb = MUTED
                _bullets(doc, r.get("strengths", []), GREEN, "+ ")
                _bullets(doc, r.get("weaknesses", []), RED, "− ")

    # Матрица
    if v.critiques:
        labels, rows, avgs = v.matrix()
        _heading(doc, "Критика: средние оценки (1–10)", 1)
        t = doc.add_table(rows=1, cols=len(labels) + 1)
        t.style = "Table Grid"
        t.cell(0, 0).paragraphs[0].add_run("Рецензент").bold = True
        for j, l in enumerate(labels, 1):
            t.cell(0, j).paragraphs[0].add_run(l).bold = True
            t.cell(0, j).paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        for reviewer, vals in rows + [("Среднее", avgs)]:
            cells = t.add_row().cells
            cells[0].paragraphs[0].add_run(reviewer).bold = reviewer == "Среднее" or None
            for c, l, x in zip(cells[1:], labels, vals):
                c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
                if x is None:
                    c.paragraphs[0].add_run("свой" if v.author(l) == reviewer else "—").font.color.rgb = MUTED
                else:
                    c.paragraphs[0].add_run(f"{x:.1f}").bold = True
                    _shade(c, _score_fill(x))

    # Участники
    _heading(doc, "Участники и расходы", 1)
    t = doc.add_table(rows=1, cols=4)
    t.style = "Table Grid"
    for j, h in enumerate(["Участник", "Вызовов", "Токены (вход / выход)", "Стоимость"]):
        t.cell(0, j).paragraphs[0].add_run(h).bold = True
        _shade(t.cell(0, j), "F2F2F8")
    for name, s in v.stats.items():
        s = s or {}
        cells = t.add_row().cells
        for c, val in zip(cells, [name, str(s.get("calls", 0)),
                                  f"{s.get('prompt_tokens', 0)} / {s.get('completion_tokens', 0)}",
                                  f"${float(s.get('cost', 0)):.4f}"]):
            c.paragraphs[0].add_run(val)
    cells = t.add_row().cells
    _widths(t, [5.0, 2.2, 5.0, 3.8])
    cells[0].paragraphs[0].add_run("Итого").bold = True
    cells[3].paragraphs[0].add_run(f"${v.cost:.4f}").bold = True
    fails = v.failures()
    if fails:
        _heading(doc, "Сбои", 3)
        _bullets(doc, [f"**{n}** ({st}): {err}" for n, st, err in fails])
    return _save(doc)
