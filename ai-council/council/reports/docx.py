"""Отчёт в Word (DOCX) — настоящий редактируемый документ.

Стили Word (Заголовок 1–4, Код, Цитата, Плашка), списки Word с собственной нумерацией, таблицы Word со своим
стилем и повтором шапки на каждой странице, поле «Оглавление», колонтитулы с номерами страниц, свойства
документа и язык. Порядок разделов — как в PDF: сначала решение и обоснование, затем подробности.
"""
from __future__ import annotations

import base64
import io
import re

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Pt, RGBColor
from lxml import html as LH

from . import brand
from .html import AGREE_DOTS, AGREE_SENT, AGREE_WORD, FACT_SIGN, FACT_WORD, IDEAS_INTRO, KIND, SUMMARY_NOTE, TURQUOISE, _accent_soft, _plural
from .view import View, md_to_html, readable_flaw

FONT = "Calibri"           # есть в Word на Windows и Mac; LibreOffice подставляет метрически совместимый Carlito
MONO = "Consolas"
MATH = "Cambria Math"
INK = "18233D"
MUTED = "586379"
LINE = "DFE3EC"
TINT = "F5F6FA"
WARN, WARN_SOFT = "8A5200", "FCF1DA"
BAD, BAD_SOFT = "B8283A", "FCE8EA"
OK = "13773F"


def _hex(c: str) -> str:
    return c.lstrip("#").upper()


# ------------------------------------------------------------------ стили документа

def _set_lang(style_el, lang: str = "ru-RU") -> None:
    rpr = style_el.get_or_add_rPr()
    el = rpr.find(qn("w:lang"))
    if el is None:
        el = OxmlElement("w:lang")
        rpr.append(el)
    el.set(qn("w:val"), lang)
    el.set(qn("w:eastAsia"), lang)


def _font(style, name: str = FONT, size: float | None = None, color: str | None = None, bold: bool | None = None,
          italic: bool | None = None) -> None:
    f = style.font
    f.name = name
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    for a in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(a), name)
    for a in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
        fonts.attrib.pop(qn(a), None)
    if size:
        f.size = Pt(size)
    if color:
        f.color.rgb = RGBColor.from_string(color)
    if bold is not None:
        f.bold = bold
    if italic is not None:
        f.italic = italic


def _para_border(pf_el, side: str, color: str, size: int = 8, space: int = 4) -> None:
    ppr = pf_el.get_or_add_pPr()
    bdr = ppr.find(qn("w:pBdr"))
    if bdr is None:
        bdr = OxmlElement("w:pBdr")
        ppr.append(bdr)
    el = OxmlElement(f"w:{side}")
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size))
    el.set(qn("w:space"), str(space))
    el.set(qn("w:color"), color)
    bdr.append(el)


def _shade_el(pr, fill: str) -> None:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    pr.append(shd)


def _style(doc, name: str, kind=WD_STYLE_TYPE.PARAGRAPH, base: str | None = "Normal"):
    try:
        return doc.styles[name]
    except KeyError:
        st = doc.styles.add_style(name, kind)
        if base and kind == WD_STYLE_TYPE.PARAGRAPH:
            st.base_style = doc.styles[base]
        return st


TABLE_STYLE = f"""<w:style {nsdecls('w')} w:type="table" w:customStyle="1" w:styleId="CouncilTable">
  <w:name w:val="Council Table"/><w:basedOn w:val="TableNormal"/><w:uiPriority w:val="40"/><w:qFormat/>
  <w:pPr><w:spacing w:before="0" w:after="0" w:line="252" w:lineRule="auto"/></w:pPr>
  <w:rPr><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>
  <w:tblPr><w:tblBorders>
    <w:top w:val="single" w:sz="8" w:space="0" w:color="B9C0CF"/>
    <w:bottom w:val="single" w:sz="4" w:space="0" w:color="{LINE}"/>
    <w:insideH w:val="single" w:sz="4" w:space="0" w:color="{LINE}"/></w:tblBorders>
    <w:tblCellMar><w:top w:w="70" w:type="dxa"/><w:left w:w="100" w:type="dxa"/><w:bottom w:w="70" w:type="dxa"/>
      <w:right w:w="100" w:type="dxa"/></w:tblCellMar></w:tblPr>
  <w:tblStylePr w:type="firstRow"><w:rPr><w:b/><w:color w:val="{MUTED}"/><w:sz w:val="16"/><w:szCs w:val="16"/></w:rPr>
    <w:tcPr><w:tcBorders><w:bottom w:val="single" w:sz="8" w:space="0" w:color="B9C0CF"/></w:tcBorders>
      <w:shd w:val="clear" w:color="auto" w:fill="{TINT}"/></w:tcPr></w:tblStylePr>
</w:style>"""


def _setup(doc, kind: str, title: str) -> dict[str, str]:
    """Стили, поля A4, свойства документа. Возвращает цвета темы."""
    b = brand.current()
    custom = b.color.lower() != brand.DEFAULT_COLOR.lower()
    theme = {"accent": _hex(b.color), "accent_soft": _hex(_accent_soft(b.color)),
             "dec": _hex(b.color if custom else TURQUOISE), "dec_soft": _hex(_accent_soft(b.color)) if custom else "E3F6F3"}
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2)
    sec.top_margin, sec.bottom_margin = Cm(2.2), Cm(2)
    sec.header_distance = sec.footer_distance = Cm(1)

    normal = doc.styles["Normal"]
    _font(normal, FONT, 10.5, INK)
    _set_lang(normal.element)
    pf = normal.paragraph_format
    pf.space_after = Pt(5)
    pf.line_spacing = 1.15
    for name, size, before, after in (("Title", 22, 0, 6), ("Heading 1", 15, 18, 6), ("Heading 2", 12.5, 14, 4),
                                      ("Heading 3", 11, 10, 3), ("Heading 4", 10.5, 8, 2)):
        st = doc.styles[name]
        _font(st, FONT, size, INK if name != "Heading 4" else "2C3852", True, False)
        st.paragraph_format.space_before = Pt(before)
        st.paragraph_format.space_after = Pt(after)
        st.paragraph_format.keep_with_next = True
        st.paragraph_format.line_spacing = 1.1
    ppr = doc.styles["Title"].element.get_or_add_pPr()
    for el in ppr.findall(qn("w:pBdr")):  # у встроенного «Заголовка» синяя линия снизу — убираем
        ppr.remove(el)
    for name in ("Title", "Subtitle"):  # и разрядка букв
        rp = doc.styles[name].element.get_or_add_rPr()
        for tag in ("w:spacing", "w:kern"):
            for el in rp.findall(qn(tag)):
                rp.remove(el)
    for name in ("Header", "Footer"):
        try:
            tabs = doc.styles[name].element.get_or_add_pPr().find(qn("w:tabs"))
            if tabs is not None:
                tabs.getparent().remove(tabs)
        except KeyError:
            pass
    _para_border(doc.styles["Heading 1"].element, "top", LINE, 6, 8)

    st = _style(doc, "Subtitle")
    _font(st, FONT, 11, "2C3852", False, False)
    st.paragraph_format.space_after = Pt(8)
    st = _style(doc, "Council Meta")
    _font(st, FONT, 8.5, MUTED)
    st.paragraph_format.space_after = Pt(4)
    st = _style(doc, "Council Label")
    _font(st, FONT, 7.5, MUTED, True)
    st.paragraph_format.space_after = Pt(1)
    st = _style(doc, "Council Value")
    _font(st, FONT, 10.5, INK, True)
    st.paragraph_format.space_after = Pt(1)
    st = _style(doc, "Code")
    _font(st, MONO, 8.5, INK)
    st.paragraph_format.space_before = Pt(3)
    st.paragraph_format.space_after = Pt(6)
    st.paragraph_format.line_spacing = 1.0
    st.paragraph_format.left_indent = Cm(0.25)
    _shade_el(st.element.get_or_add_pPr(), TINT)
    _para_border(st.element, "left", "C8CEDA", 12, 6)
    st = _style(doc, "Quote")
    _font(st, FONT, 10.5, "2C3852", False, False)
    st.paragraph_format.left_indent = Cm(0.4)
    _para_border(st.element, "left", theme["accent_soft"], 18, 8)
    st = _style(doc, "Council Callout")
    _font(st, FONT, 9.5, INK)
    st.paragraph_format.left_indent = Cm(0.2)
    st.paragraph_format.space_before = Pt(4)
    st.paragraph_format.space_after = Pt(6)
    st = _style(doc, "Council Box")
    _font(st, FONT, 9.5, INK)
    _shade_el(st.element.get_or_add_pPr(), TINT)
    st = _style(doc, "Council Query")
    _font(st, FONT, 9.5, INK)
    _shade_el(st.element.get_or_add_pPr(), TINT)
    st.paragraph_format.left_indent = st.paragraph_format.right_indent = Cm(0.15)
    st.paragraph_format.space_after = Pt(3)
    st = _style(doc, "Council Formula")
    _font(st, MATH, 11, INK, False, True)
    st.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
    st = _style(doc, "TOC Heading")
    _font(st, FONT, 8, MUTED, True)
    st = _style(doc, "Caption")
    _font(st, FONT, 8, MUTED, False, False)
    st = _style(doc, "Code Char", WD_STYLE_TYPE.CHARACTER, None)
    _font(st, MONO, 9, INK)
    st = _style(doc, "Hyperlink", WD_STYLE_TYPE.CHARACTER, None)
    st.font.color.rgb = RGBColor.from_string(theme["accent"])
    st.font.underline = True
    for name in ("List Bullet", "List Bullet 2", "List Bullet 3", "List Number", "List Number 2", "List Number 3"):
        doc.styles[name].paragraph_format.space_after = Pt(2)
    if "CouncilTable" not in [s.style_id for s in doc.styles]:
        doc.styles.element.append(parse_xml(TABLE_STYLE))

    cp = doc.core_properties
    cp.title = title
    cp.subject = f"{KIND[kind]} «{b.title}»"
    cp.author = b.title
    cp.keywords = "совет нейросетей; решение; отчёт"
    cp.language = "ru-RU"
    cp.comments = "Создано программой «Совет нейросетей»"
    return theme


# ------------------------------------------------------------------ поля, колонтитулы, оглавление

def _field(par, instr: str, placeholder: str = "1", size: float | None = None, color: str | None = None) -> None:
    def run(*children):
        r = OxmlElement("w:r")
        if size or color:
            rpr = OxmlElement("w:rPr")
            if color:
                c = OxmlElement("w:color")
                c.set(qn("w:val"), color)
                rpr.append(c)
            if size:
                s = OxmlElement("w:sz")
                s.set(qn("w:val"), str(int(size * 2)))
                rpr.append(s)
            r.append(rpr)
        for ch in children:
            r.append(ch)
        par._p.append(r)

    def fld(t):
        e = OxmlElement("w:fldChar")
        e.set(qn("w:fldCharType"), t)
        return e
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = f" {instr} "
    t = OxmlElement("w:t")
    t.text = placeholder
    run(fld("begin"))
    run(it)
    run(fld("separate"))
    run(t)
    run(fld("end"))


def _small(par, text: str, color: str = MUTED, size: float = 7.5, bold: bool = False):
    r = par.add_run(text)
    r.font.size = Pt(size)
    r.font.color.rgb = RGBColor.from_string(color)
    r.bold = bold or None
    return r


def _headers(doc, title: str) -> None:
    """Первая страница — со своей шапкой в тексте; на остальных: бренд и заголовок сверху, номер страницы снизу."""
    b = brand.current()
    sec = doc.sections[0]
    sec.different_first_page_header_footer = True
    width = sec.page_width - sec.left_margin - sec.right_margin
    for hdr in (sec.header,):
        p = hdr.paragraphs[0]
        p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
        _small(p, b.title, bold=True)
        _small(p, "\t" + (title if len(title) <= 80 else title[:78].rsplit(" ", 1)[0] + "…"))
    for ftr in (sec.footer, sec.first_page_footer):
        p = ftr.paragraphs[0]
        p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
        _small(p, (b.footer_text or b.title) + "\tстр. ")
        _field(p, "PAGE", "1", 7.5, MUTED)
        _small(p, " из ")
        _field(p, "NUMPAGES", "1", 7.5, MUTED)


def _bookmark(par, name: str, idx: int) -> None:
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(idx))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(idx))
    par._p.insert(0, start)
    par._p.append(end)


def _internal_link(par, text: str, anchor: str) -> None:
    link = OxmlElement("w:hyperlink")
    link.set(qn("w:anchor"), anchor)
    link.set(qn("w:history"), "1")
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.set(qn("xml:space"), "preserve")
    t.text = text
    r.append(t)
    link.append(r)
    par._p.append(link)


def _toc(doc, entries: list[tuple[str, str, int]]) -> None:
    """Поле Word «Оглавление» (заголовки 1–2 уровня). До обновления поля внутри — готовый список разделов-ссылок,
    поэтому оглавление видно сразу, в том числе в LibreOffice и на телефоне; номера страниц Word проставит по F9."""
    p = doc.add_paragraph("Содержание", style="TOC Heading")
    width = doc.sections[0].page_width - doc.sections[0].left_margin - doc.sections[0].right_margin
    first = doc.add_paragraph()
    _field_begin(first, 'TOC \\o "1-2" \\h \\z \\u')
    for i, (text, anchor, level) in enumerate(entries):
        par = first if i == 0 else doc.add_paragraph()
        par.paragraph_format.space_after = Pt(1)
        par.paragraph_format.left_indent = Cm(0.5 * (level - 1))
        par.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS)
        _internal_link(par, text, anchor)
        for r in par.runs:
            r.font.size = Pt(9)
    end = doc.add_paragraph()
    e = OxmlElement("w:r")
    fc = OxmlElement("w:fldChar")
    fc.set(qn("w:fldCharType"), "end")
    e.append(fc)
    end._p.append(e)
    end.paragraph_format.space_after = Pt(6)
    del p


def _field_begin(par, instr: str) -> None:
    for t, val in (("begin", None), ("instr", instr), ("separate", None)):
        r = OxmlElement("w:r")
        if t == "instr":
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = f" {val} "
            r.append(it)
        else:
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), t)
            r.append(fc)
        par._p.append(r)


# ------------------------------------------------------------------ таблицы

def _table(doc_or_cell, rows: int, cols: int, *, header: bool = True, widths: list[float] | None = None, style: str = "Council Table"):
    t = doc_or_cell.add_table(rows=rows, cols=cols)
    t.style = style
    t.alignment = WD_TABLE_ALIGNMENT.LEFT
    tblPr = t._tbl.tblPr
    look = tblPr.find(qn("w:tblLook"))
    if look is None:
        look = OxmlElement("w:tblLook")
        tblPr.append(look)
    for k, v in (("w:val", "0420" if header else "0000"), ("w:firstRow", "1" if header else "0"), ("w:lastRow", "0"),
                 ("w:firstColumn", "0"), ("w:lastColumn", "0"), ("w:noHBand", "1"), ("w:noVBand", "1")):
        look.set(qn(k), v)
    tw = tblPr.find(qn("w:tblW"))
    if tw is None:
        tw = OxmlElement("w:tblW")
        tblPr.append(tw)
    tw.set(qn("w:type"), "pct")
    tw.set(qn("w:w"), "5000")
    if header and rows:
        trpr = t.rows[0]._tr.get_or_add_trPr()
        trpr.append(OxmlElement("w:tblHeader"))  # шапка повторяется на каждой странице
    for r in t.rows:
        r._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    if widths:
        _set_widths(t, widths)
    return t


def _set_widths(t, widths: list[float], total_cm: float = 17.0) -> None:
    """Ширины столбцов: и в сетке таблицы (её читает LibreOffice), и в ячейках (их читает Word)."""
    total = sum(widths) or 1
    tw = [int(total_cm * w / total * 567) for w in widths]  # 1 см = 567 twips
    grid = t._tbl.tblGrid
    for col, w in zip(grid.findall(qn("w:gridCol")), tw):
        col.set(qn("w:w"), str(w))
    for r in t.rows:
        for c, w in zip(r.cells, tw):
            c.width = w * 635  # twips → EMU


def _new_row(t):
    row = t.add_row()
    row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    return row


def _cell_shade(cell, fill: str) -> None:
    _shade_el(cell._tc.get_or_add_tcPr(), fill)


def _cell_text(cell, text: str, *, bold: bool = False, color: str | None = None, size: float | None = None,
               align=None) -> None:
    p = cell.paragraphs[0]
    r = p.add_run(text)
    r.bold = bold or None
    if color:
        r.font.color.rgb = RGBColor.from_string(color)
    if size:
        r.font.size = Pt(size)
    if align is not None:
        p.alignment = align


def _no_borders(t) -> None:
    tblPr = t._tbl.tblPr
    b = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{side}")
        e.set(qn("w:val"), "nil")
        b.append(e)
    tblPr.append(b)


def _box_borders(t, color: str = LINE) -> None:
    tblPr = t._tbl.tblPr
    b = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        e = OxmlElement(f"w:{side}")
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), "6")
        e.set(qn("w:color"), color)
        b.append(e)
    tblPr.append(b)


# ------------------------------------------------------------------ Markdown модели (через HTML) → Word

class _Writer:
    def __init__(self, container, *, base_style: str | None = None, heading_shift: int = 3) -> None:
        self.c = container           # Document или ячейка таблицы
        self.base = base_style       # стиль обычных абзацев (например, «Council Query» для запроса)
        self.shift = heading_shift   # заголовки модели — не выше Заголовка 3 (не попадают в оглавление)
        self.doc = container.part.document if hasattr(container, "part") else None

    def para(self, style: str | None = None):
        st = style or self.base
        return self.c.add_paragraph(style=st) if st else self.c.add_paragraph()

    # --- строчные элементы
    def inline(self, par, el, fmt: dict | None = None) -> None:
        fmt = dict(fmt or {})
        if el.text:
            self._run(par, el.text, fmt)
        for child in el:
            tag = child.tag if isinstance(child.tag, str) else ""
            f = dict(fmt)
            cls = child.get("class") or ""
            if tag in ("strong", "b"):
                f["bold"] = True
            elif tag in ("em", "i"):
                f["italic"] = True
            elif tag == "code":
                f["code"] = True
            elif tag in ("sup", "sub"):
                f[tag] = True
            elif tag == "span" and "math" in cls:
                f["math"] = True
            if tag == "br":
                par.add_run().add_break()
            elif tag == "a" and (child.get("href") or "").startswith(("http://", "https://")):
                _hyperlink(par, child.text_content(), child.get("href"))
            elif tag in ("ul", "ol", "table", "pre", "p", "blockquote", "div"):
                pass  # блочные элементы внутри пункта списка обрабатываются отдельно
            else:
                self.inline(par, child, f)
            if child.tail:
                self._run(par, child.tail.lstrip() if tag == "br" else child.tail, fmt)

    def _run(self, par, text: str, fmt: dict) -> None:
        text = re.sub(r"\s+", " ", text)
        if not text:
            return
        run = par.add_run(text)
        run.bold = fmt.get("bold") or None
        run.italic = fmt.get("italic") or None
        if fmt.get("code"):
            run.style = "Code Char"
            _shade_el(run._r.get_or_add_rPr(), TINT)
        if fmt.get("sup"):
            run.font.superscript = True
        if fmt.get("sub"):
            run.font.subscript = True
        if fmt.get("math"):
            run.font.name = MATH
            run.italic = True

    # --- блочные элементы
    def block(self, el, level: int = 0) -> None:
        tag = el.tag if isinstance(el.tag, str) else ""
        cls = el.get("class") or ""
        if tag == "p":
            self.inline(self.para(), el)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            lvl = min(4, self.shift + (0 if tag in ("h1", "h2") else 1))
            par = self.c.add_paragraph(style=f"Heading {lvl}")
            self.inline(par, el)
        elif tag in ("ul", "ol"):
            self.list(el, level)
        elif tag == "pre":
            par = self.c.add_paragraph(style="Code")
            lines = el.text_content().rstrip("\n").split("\n")
            for i, line in enumerate(lines):
                if i:
                    par.add_run().add_break()
                par.add_run(line)
        elif tag == "blockquote":
            for child in el:
                if getattr(child, "tag", "") == "p":
                    self.inline(self.c.add_paragraph(style="Quote"), child)
                else:
                    self.block(child, level)
        elif tag == "table":
            self.table(el)
        elif tag == "hr":
            par = self.c.add_paragraph()
            _para_border(par._p, "bottom", LINE, 6, 1)
        elif tag == "div" and "math-block" in cls:
            par = self.c.add_paragraph(style="Council Formula")
            self.inline(par, el, {"math": True})
        elif tag in ("div", "section"):  # обёртки (например, блок прокрутки таблицы) — заходим внутрь
            for child in el:
                self.block(child, level)
        elif tag:
            self.inline(self.para(), el)

    def list(self, el, level: int) -> None:
        ordered = el.tag == "ol"
        style = ("List Number" if ordered else "List Bullet") + ("" if level == 0 else f" {min(level + 1, 3)}")
        num_id = _restart_numbering(self.c, style) if ordered else None
        for li in [x for x in el if getattr(x, "tag", "") == "li"]:
            par = self.c.add_paragraph(style=style)
            if num_id is not None:
                _set_num(par, num_id, 0)
            inner_p = [x for x in li if getattr(x, "tag", "") == "p"]
            self.inline(par, inner_p[0] if inner_p else li)
            for child in li:
                tg = getattr(child, "tag", "")
                if tg in ("ul", "ol"):
                    self.list(child, level + 1)
                elif tg in ("pre", "table", "blockquote"):
                    self.block(child, level)
                elif tg == "p" and child is not (inner_p[0] if inner_p else None):
                    self.inline(self.c.add_paragraph(style="List Continue"), child)  # второй абзац того же пункта

    def table(self, el) -> None:
        rows = el.xpath(".//tr")
        if not rows:
            return
        ncols = max(len(r.xpath("./th|./td")) for r in rows)
        has_head = bool(rows[0].xpath("./th"))
        t = _table(self.c, len(rows), ncols, header=has_head)
        lens = [0] * ncols
        for r in rows:
            for j, c in enumerate(r.xpath("./th|./td")):
                lens[j] = max(lens[j], min(40, len(c.text_content().strip())))
        _set_widths(t, [max(4, x) for x in lens])
        size = 7.5 if ncols >= 8 else 8.5 if ncols >= 6 else None  # широкие таблицы — мельче, чтобы поместились
        for i, r in enumerate(rows):
            for j, cell_el in enumerate(r.xpath("./th|./td")):
                cell = t.cell(i, j)
                par = cell.paragraphs[0]
                self.inline(par, cell_el)
                if size:
                    for run in par.runs:
                        run.font.size = Pt(size)


def _restart_numbering(container, style_name: str) -> int | None:
    """Новый номер списка Word, начинающийся с 1 (иначе все нумерованные списки документа продолжают друг друга)."""
    try:
        doc_part = container.part
        numbering = doc_part.numbering_part.element
        style = doc_part.document.styles[style_name]
        num_id = style.element.pPr.numPr.numId.val
    except (AttributeError, KeyError, NotImplementedError):
        return None
    num = next((n for n in numbering.findall(qn("w:num")) if n.get(qn("w:numId")) == str(num_id)), None)
    if num is None:
        return None
    abstract = num.find(qn("w:abstractNumId")).get(qn("w:val"))
    new_id = max(int(n.get(qn("w:numId"))) for n in numbering.findall(qn("w:num"))) + 1
    el = parse_xml(f'<w:num {nsdecls("w")} w:numId="{new_id}"><w:abstractNumId w:val="{abstract}"/>'
                   f'<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>')
    numbering.append(el)
    return new_id


def _set_num(par, num_id: int, ilvl: int) -> None:
    ppr = par._p.get_or_add_pPr()
    numpr = ppr.get_or_add_numPr()
    numpr.get_or_add_ilvl().val = ilvl
    numpr.get_or_add_numId().val = num_id


def _hyperlink(par, text: str, url: str) -> None:
    rel = par.part.relate_to(url, "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                             is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    rs = OxmlElement("w:rStyle")
    rs.set(qn("w:val"), "Hyperlink")
    rpr.append(rs)
    run.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    par._p.append(link)


def _frag(text: str, breaks: bool = False):
    html = md_to_html(text, breaks=breaks).strip()
    return LH.fragment_fromstring(html, create_parent="div") if html else None


def inline_markdown(par, text: str, fmt: dict | None = None) -> None:
    """Короткий Markdown-текст (пункт, ячейка) — в уже созданный абзац."""
    frag = _frag(text)
    if frag is None:
        return
    blocks = list(frag)
    target = blocks[0] if len(blocks) == 1 and blocks[0].tag in ("p", "h1", "h2", "h3", "h4") else None
    if target is not None:
        _Writer(par).inline(par, target, fmt)
    else:
        par.add_run(re.sub(r"\s+", " ", frag.text_content()))


def add_markdown(container, text: str, *, breaks: bool = False, base_style: str | None = None, heading_shift: int = 3) -> None:
    """Markdown модели в документ или ячейку — стилями Word."""
    frag = _frag(text, breaks)
    if frag is None:
        return
    w = _Writer(container, base_style=base_style, heading_shift=heading_shift)
    for el in frag:
        w.block(el)


# ------------------------------------------------------------------ части отчёта

class _Report:
    def __init__(self, v: View, kind: str) -> None:
        self.v = v
        self.kind = kind
        # в «Только решение» вопроса нет нигде — ни в заголовке, ни в колонтитуле, ни в свойствах файла
        self.title = f"{KIND[kind]} · {v.date}" if kind == "decision" else v.display_title
        self.doc = Document()
        self.theme = _setup(self.doc, kind, self.title)
        _headers(self.doc, self.title)
        self._bm = 0

    def h(self, text: str, level: int, anchor: str | None = None):
        par = self.doc.add_paragraph(text, style=f"Heading {level}")
        if anchor:
            self._bm += 1
            _bookmark(par, anchor, self._bm)
        return par

    def bullets(self, items: list[str], style: str = "List Bullet", prefix: str = "", color: str | None = None) -> None:
        for x in items:
            par = self.doc.add_paragraph(style=style)
            if prefix:
                r = par.add_run(prefix)
                r.bold = True
                if color:
                    r.font.color.rgb = RGBColor.from_string(color)
            inline_markdown(par, x)

    def meta(self, text: str):
        return self.doc.add_paragraph(text, style="Council Meta")

    def callout(self, title: str, text: str, kind: str = "warn") -> None:
        color, fill = {"warn": (WARN, WARN_SOFT), "bad": (BAD, BAD_SOFT), "info": (self.theme["accent"], self.theme["accent_soft"])}[kind]
        par = self.doc.add_paragraph(style="Council Callout")
        _shade_el(par._p.get_or_add_pPr(), fill)
        _para_border(par._p, "left", color, 24, 8)
        r = par.add_run(title + ". ")
        r.bold = True
        r.font.color.rgb = RGBColor.from_string(color)
        par.add_run(text)

    # --- шапка первой страницы
    def masthead(self) -> None:
        """Шапка первой страницы: логотип, название и вид отчёта с датой — в таблице без рамок.

        Картинка внутри строки текста садится на линию строки, и Word с LibreOffice выравнивают её по-разному;
        ячейки, выровненные по центру, держат логотип и надпись на одной высоте везде."""
        v, doc, b = self.v, self.doc, brand.current()
        logo = b.logo_png or _mark_png()
        wordmark = b.logo_is_wordmark
        # ширина картинки по её пропорциям: широкий логотип-надпись не обрезается ячейкой
        h_cm = 1.0 if wordmark else 0.7
        w_cm = h_cm
        if logo:
            try:
                from PIL import Image
                iw, ih = Image.open(io.BytesIO(logo)).size
                w_cm = h_cm * iw / max(ih, 1)
                if w_cm > 8.5:
                    h_cm, w_cm = h_cm * 8.5 / w_cm, 8.5
            except Exception:  # noqa: BLE001
                pass
        logo_col = w_cm + 0.35
        t = _table(doc, 1, 3, header=False, widths=[logo_col, 17 - logo_col - 4.6, 4.6])
        _no_borders(t)
        tblPr = t._tbl.tblPr
        bdr = OxmlElement("w:tblBorders")
        e = OxmlElement("w:bottom")
        e.set(qn("w:val"), "single")
        e.set(qn("w:sz"), "12")
        e.set(qn("w:color"), INK)
        bdr.append(e)
        tblPr.append(bdr)
        cells = t.rows[0].cells
        for c in cells:
            va = OxmlElement("w:vAlign")
            va.set(qn("w:val"), "center")
            c._tc.get_or_add_tcPr().append(va)
            c.paragraphs[0].paragraph_format.space_after = Pt(4)
            c.paragraphs[0].paragraph_format.space_before = Pt(0)
            c.paragraphs[0].paragraph_format.line_spacing = 1.0
        if logo:
            try:
                pic = cells[0].paragraphs[0].add_run().add_picture(io.BytesIO(logo), height=Cm(h_cm))
                _alt(pic, b.title)
            except Exception:  # noqa: BLE001 — логотип не должен ломать отчёт
                pass
        if not wordmark:
            cells[1]._tc.get_or_add_tcPr().append(OxmlElement("w:noWrap"))  # название в одну строку
            r = cells[1].paragraphs[0].add_run(b.title)
            r.bold = True
            # длинное название — чуть мельче, чтобы осталось в одну строку (≈0,55 кегля на знак)
            room_cm = 17 - logo_col - 4.6 - 0.3
            fit = room_cm / (max(len(b.title), 1) * 0.55 / 72 * 2.54)
            r.font.size = Pt(max(7.5, min(11.0, fit)))
        cp = cells[2].paragraphs[0]
        cp.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        r = cp.add_run(KIND[self.kind])
        r.bold = True
        r.font.size = Pt(9)
        cp.add_run().add_break()
        _small(cp, v.date, size=8.5)
        doc.add_paragraph().paragraph_format.space_after = Pt(2)
        heading = KIND[self.kind] if self.kind == "decision" else v.display_title
        t2 = doc.add_paragraph(heading, style="Title")
        if len(heading) > 90:  # длинный заголовок — мельче
            for r in t2.runs:
                r.font.size = Pt(17)
        t2.paragraph_format.keep_with_next = True

    def lead(self, refer: bool = True) -> None:
        v = self.v
        if v.followup_title:
            self.meta(f"Уточнение к заседанию «{v.followup_title}»")
        if v.question_is_prompt:
            add_markdown(self.doc, v.question_excerpt(), breaks=True, base_style="Subtitle")
            if refer:
                self.meta("Полный текст запроса — в разделе «Запрос и условия».")
        elif v.question_is_long and v.question_rest:
            add_markdown(self.doc, v.question_rest, breaks=True, base_style="Subtitle")

    def keyfacts(self, full: bool) -> None:
        v = self.v
        t = _table(self.doc, 2 if full else 1, 4, header=False, widths=[5.4, 3.9, 3.9, 3.8])
        _box_borders(t)
        w = v.winner

        def cell(c, label, value, sub=""):
            c.paragraphs[0].style = "Council Label"
            c.paragraphs[0].add_run(label)
            c.add_paragraph(value, style="Council Value")
            if sub:
                c.add_paragraph(sub, style="Council Meta")
        if w:
            who = "ваш вариант" if v.author(w) == "пользователь" else f"предложил {v.author(w)}"
            cell(t.cell(0, 0), "РЕШЕНИЕ", f"◆ {v.title(w)}", f"вариант {w} · {who}")
        else:
            cell(t.cell(0, 0), "РЕШЕНИЕ", "Итог председателя")
        _cell_shade(t.cell(0, 0), self.theme["dec_soft"])
        a = v.agreement
        if a:
            n = AGREE_DOTS[a["level"]]
            cell(t.cell(0, 1), "СОГЛАСИЕ СОВЕТА", AGREE_WORD[a["level"]], "●" * n + "○" * (3 - n) + f"  τ = {a['tau']:.2f}")
        else:
            cell(t.cell(0, 1), "СОГЛАСИЕ СОВЕТА", "—")
        row = next((r for r in v.tally if r.get("label") == w), {}) if w else {}
        if row.get("ballots"):
            beats = ("выигрывает у всех в прямых сравнениях" if row.get("schulze") is not None
                     and row.get("schulze") == len(v.tally) - 1 else f"{len(v.tally)} вариантов в голосовании")
            cell(t.cell(0, 2), "ПЕРВЫХ МЕСТ", f"{row.get('first_places', 0)} из {row['ballots']}", beats)
        else:
            cell(t.cell(0, 2), "ВАРИАНТОВ", str(len(v.proposals)))
        n = len(v.members)
        cell(t.cell(0, 3), "СОВЕТ", f"{n} {_plural(n, 'модель', 'модели', 'моделей')}", v.mode)
        if full:
            cell(t.cell(1, 0), "ДАТА", v.date)
            cell(t.cell(1, 1), "ДЛИТЕЛЬНОСТЬ", v.duration)
            cell(t.cell(1, 2), "СТОИМОСТЬ", f"${v.cost:.2f}")
            cell(t.cell(1, 3), "ВАРИАНТОВ", str(len(v.proposals)))
        self.doc.add_paragraph().paragraph_format.space_after = Pt(2)

    def toc(self) -> None:
        v = self.v
        entries = [("Итог исследования" if v.is_research else "Решение совета", "verdict", 1)]
        if v.is_research:
            entries.append(("Известно, спорно, неизвестно", "kcu", 1))
            if v.subquestions:
                entries.append(("Рамки и подвопросы", "scope", 1))
        if any(v.verdict.get(k) for k in ("why", "risks", "dissent")):
            entries.append(("Обоснование", "why", 1))
        if v.ideas:
            entries.append(("Что взято из других вариантов", "ideas", 1))
        if v.is_research and v.evidence:
            entries.append(("Источники", "sources", 1))
        if v.is_research and v.checked_claims:
            entries.append(("Проверка утверждений", "claims", 1))
        if v.fact_check:
            entries.append(("Проверка фактов", "facts", 1))
        if v.tally or v.ballots:
            entries.append(("Голосование", "voting", 1))
        if v.proposals:
            entries.append(("Варианты", "proposals", 1))
            entries += [(f"{l}. {v.title(l)}", f"proposal_{l}", 2) for l in v.proposals]
        if v.critiques:
            entries.append(("Критика", "critique", 1))
        entries += [("Участники и расходы", "members", 1), ("Запрос и условия", "request", 1)]
        _toc(self.doc, entries)

    def decision(self) -> None:
        v = self.v
        h = self.h("", 1, "verdict")
        r = h.add_run("◆  ")
        r.font.color.rgb = RGBColor.from_string(self.theme["dec"])
        h.add_run("Итог исследования" if v.is_research else "Решение совета")
        if v.tie_line:
            self.callout("Ничья за первое место", v.tie_line)
        if v.deviation_line:
            self.callout("Председатель выбрал не лидера голосования", v.deviation_line, "bad")
        if v.agreement_low:
            self.callout("Низкое согласие совета", v.agreement_line)
        refuted = [c for r in v.fact_check.values() for c in r.get("claims", []) if c.get("status") == "refuted"]
        if refuted:
            self.callout("Проверка фактов нашла ошибки", f"Опровергнуто утверждений: {len(refuted)}. "
                                                          "Подробности — в разделе «Проверка фактов».", "bad")
        add_markdown(self.doc, v.verdict.get("final_answer", ""), heading_shift=2)

    def rationale(self) -> None:
        vd = self.v.verdict
        if not (vd.get("why") or vd.get("risks") or vd.get("dissent")):
            return
        self.h("Обоснование", 1, "why")
        if vd.get("why"):
            self.h("Почему совет так решил", 2)
            add_markdown(self.doc, vd["why"])
        if vd.get("risks"):
            self.h("Риски", 2)
            self.bullets(vd["risks"])
        if vd.get("dissent"):
            self.h("Особые мнения", 2)
            self.bullets(vd["dissent"])

    # ---------------------------------------------------------- режим «Исследование»
    def _erefs(self, par, ids: list[str]) -> None:
        by = self.v.evidence_by_id
        first = True
        for i in ids:
            if i != "DOC" and i not in by:
                continue
            if not first:
                par.add_run(" ")
            first = False
            if i == "DOC":
                _small(par, "DOC", size=8.5)
            else:
                _hyperlink(par, f"[{i}]", by[i]["url"])

    def kcu(self) -> None:
        v = self.v
        if not v.is_research:
            return
        self.h("Что известно, что спорно, чего не знает никто", 1, "kcu")
        self.meta("«Известно» — только подтверждённое найденными источниками или вашими документами; согласие "
                  "моделей и цифры «из памяти» сюда не попадают.")
        self.h("✓ Известно", 2)
        if v.known:
            t = _table(self.doc, len(v.known) + 1, 3, widths=[10.0, 4.0, 3.0])
            for c, text in enumerate(("Утверждение", "Рамки", "Источники")):
                _cell_text(t.cell(0, c), text)
            for n, k in enumerate(v.known, 1):
                inline_markdown(t.cell(n, 0).paragraphs[0], k.get("statement", ""))
                if k.get("volatility") == "changes_fast":
                    _small(t.cell(n, 0).add_paragraph(), "быстро меняется — проверьте дату", size=8)
                _cell_text(t.cell(n, 1), k.get("scope", ""))
                self._erefs(t.cell(n, 2).paragraphs[0], k.get("evidence_ids") or [])
            self.doc.add_paragraph().paragraph_format.space_after = Pt(0)
        else:
            self.meta("Подтверждённых источниками утверждений нет.")
        if v.contested:
            self.h("⇄ Спорно", 2)
            for c in v.contested:
                bits = [f"Позиции: {'; '.join(c.get('positions') or [])}" if c.get("positions") else "",
                        f"Почему расходятся: {c['why_they_differ']}" if c.get("why_they_differ") else "",
                        f"Что решило бы спор: {c['what_would_resolve']}" if c.get("what_would_resolve") else "",
                        "Источники: " + ", ".join(c.get("evidence_ids") or []) if c.get("evidence_ids") else ""]
                self.callout(c.get("issue", ""), "\n".join(b for b in bits if b))
        if v.unknown:
            self.h("? Неизвестно", 2)
            t = _table(self.doc, len(v.unknown) + 1, 3, widths=[8.0, 4.0, 5.0])
            for c, text in enumerate(("Чего не знаем", "Почему", "Как узнать")):
                _cell_text(t.cell(0, c), text)
            for n, u in enumerate(v.unknown, 1):
                inline_markdown(t.cell(n, 0).paragraphs[0], u.get("gap", ""))
                _cell_text(t.cell(n, 1), v.GAP_WORD.get(u.get("reason", ""), u.get("reason", "")))
                inline_markdown(t.cell(n, 2).paragraphs[0], u.get("how_to_check", ""))
            self.doc.add_paragraph().paragraph_format.space_after = Pt(0)
        for key, title in (("what_would_change", "Что изменило бы выводы"), ("next_questions", "Что исследовать дальше")):
            items = v.verdict.get(key) or []
            if items:
                self.h(title, 2)
                self.bullets(items)
        if v.subquestions:
            self.h("Рамки и подвопросы", 1, "scope")
            if v.scope_line:
                self.meta(f"Рамки: {v.scope_line}")
            rows = v.coverage_rows
            t = _table(self.doc, len(rows) + 1, 3, widths=[10.0, 3.5, 3.5])
            for c, text in enumerate(("Подвопрос", "В итоге", "Каркас раздела")):
                _cell_text(t.cell(0, c), text)
            for n, r in enumerate(rows, 1):
                inline_markdown(t.cell(n, 0).paragraphs[0], f"**{r['id']}.** {r['text']}")
                _cell_text(t.cell(n, 1), v.COVER_WORD.get(r["status"], r["status"] or "—"), bold=True,
                           color={"covered": OK, "partial": WARN, "unknown": BAD}.get(r["status"], MUTED))
                _cell_text(t.cell(n, 2), r["backbone"] or "—")
            self.doc.add_paragraph().paragraph_format.space_after = Pt(0)

    def sources(self, full: bool) -> None:
        v = self.v
        if not v.is_research or not v.evidence:
            return
        self.h("Источники", 1, "sources")
        self.meta(f"Нашла поисковая модель ({v.ledger.get('model', '')}) на этапе арбитража доказательств — {v.ledger_line}.")
        t = _table(self.doc, len(v.evidence) + 1, 3, widths=[1.4, 11.6, 4.0])
        for c, text in enumerate(("№", "Источник", "Утверждения")):
            _cell_text(t.cell(0, c), text)
        for n, e in enumerate(v.evidence, 1):
            _cell_text(t.cell(n, 0), e["id"], bold=True)
            cc = t.cell(n, 1)
            inline_markdown(cc.paragraphs[0], e.get("title") or e["url"])
            _hyperlink(cc.add_paragraph(), e["url"], e["url"])
            meta = ", ".join(x for x in (e.get("publisher"), e.get("date")) if x)
            if meta:
                _small(cc.add_paragraph(), meta, size=8)
            if e.get("search_returned") is False:
                _small(cc.add_paragraph(), "нет в результатах поиска — не подтверждение", size=8)
            link = {"dead": "ссылка не открывается — страница не найдена", "blocked": "сайт не пускает автоматическую проверку",
                    "error": "ссылка не открылась при проверке"}.get(e.get("link", ""))
            if link:
                _small(cc.add_paragraph(), link, size=8)
            _cell_text(t.cell(n, 2), ", ".join(e.get("claims") or []))
        self.doc.add_paragraph().paragraph_format.space_after = Pt(0)
        if full and v.checked_claims:
            self.h("Проверка утверждений", 1, "claims")
            claims = v.checked_claims
            t = _table(self.doc, len(claims) + 1, 3, widths=[1.8, 11.2, 4.0])
            for c, text in enumerate(("№", "Утверждение", "Статус")):
                _cell_text(t.cell(0, c), text)
            for n, c in enumerate(claims, 1):
                st = c.get("status", "not_checked")
                _cell_text(t.cell(n, 0), c.get("id", ""), bold=True)
                inline_markdown(t.cell(n, 1).paragraphs[0], c.get("text", ""))
                for note in c.get("notes") or []:
                    _small(t.cell(n, 1).add_paragraph(), note, size=8)
                _cell_text(t.cell(n, 2), f"{v.CLAIM_SIGN.get(st, '')} {v.CLAIM_WORD.get(st, st)}", bold=True,
                           color={"confirmed": OK, "refuted": BAD, "contested_in_sources": WARN}.get(st, MUTED))
                self._erefs(t.cell(n, 2).add_paragraph(), c.get("evidence") or [])
            self.doc.add_paragraph().paragraph_format.space_after = Pt(0)

    def ideas(self) -> None:
        v = self.v
        ideas = v.ideas
        if not ideas:
            return
        self.h("Что взято из других вариантов", 1, "ideas")
        self.meta(f"{IDEAS_INTRO} Итого — {v.ideas_line}.")
        t = _table(self.doc, len(ideas) + 1, 3, widths=[3.0, 7.4, 6.6])
        for c, text in enumerate(("Решение", "Идея", "Как использована или почему отклонена")):
            _cell_text(t.cell(0, c), text)
        for n, i in enumerate(ideas, 1):
            st = i["decision"]
            _cell_text(t.cell(n, 0), f"{v.IDEA_SIGN[st]} {v.IDEA_WORD[st]}", bold=True,
                       color={"included": OK, "rejected": BAD}.get(st, MUTED))
            cc = t.cell(n, 1)
            inline_markdown(cc.paragraphs[0], i["idea"])
            by = ", ".join(i.get("by") or [])
            _small(cc.add_paragraph(), f"вариант {i.get('label', '')}" + (f" · отметил: {by}" if by else ""), size=8.5)
            reason = i.get("reason") or ("председатель не ответил по этой идее" if st == "not_addressed" else "")
            inline_markdown(t.cell(n, 2).paragraphs[0], reason)
        self.doc.add_paragraph().paragraph_format.space_after = Pt(0)

    def facts(self) -> None:
        v = self.v
        if not v.fact_check:
            return
        c = v.fact_counts
        self.h("Проверка фактов", 1, "facts")
        self.meta(f"Модель с поиском в интернете ({v.d.get('fact_checker', '')}) проверила ключевые утверждения лидирующих "
                  "вариантов — " + " · ".join(f"{FACT_WORD[k].lower()}: {c[k]}" for k in ("confirmed", "refuted", "unclear", "user_doc") if c[k]) + ".")
        for label, res in v.fact_check.items():
            self.h(f"{label}. {v.title(label)}", 2)
            if res.get("summary"):
                inline_markdown(self.doc.add_paragraph(), res["summary"])
            claims = res.get("claims", [])
            t = _table(self.doc, len(claims) + 1, 2, widths=[3.4, 13.6])
            _cell_text(t.cell(0, 0), "Статус")
            _cell_text(t.cell(0, 1), "Утверждение и что показали источники")
            for i, cl in enumerate(claims, 1):
                st = cl.get("status", "unclear")
                _cell_text(t.cell(i, 0), f"{FACT_SIGN.get(st, '?')} {FACT_WORD.get(st, st)}", bold=True,
                           color={"refuted": BAD, "confirmed": OK}.get(st, MUTED))
                cc = t.cell(i, 1)
                inline_markdown(cc.paragraphs[0], cl.get("claim", ""))
                if cl.get("note"):
                    _small(cc.add_paragraph(), cl["note"], size=8.5)
                for u in [u for u in map(v.safe_url, cl.get("sources") or []) if u][:3]:
                    _hyperlink(cc.add_paragraph(), u, u)
            self.doc.add_paragraph().paragraph_format.space_after = Pt(0)

    def vote_chart(self) -> None:
        v = self.v
        w = v.winner
        mx = max((float(r.get("borda") or 0) for r in v.tally), default=1) or 1
        t = _table(self.doc, len(v.tally), 4, header=False, widths=[0.7, 8.2, 5.2, 2.9])
        _no_borders(t)
        for i, r in enumerate(v.tally):
            l = r.get("label", "")
            bord = float(r.get("borda") or 0)
            row = t.rows[i].cells
            _cell_text(row[0], str(i + 1), color=MUTED, align=WD_ALIGN_PARAGRAPH.RIGHT)
            p = row[1].paragraphs[0]
            rl = p.add_run(f" {l} ")
            rl.bold = True
            rl.font.color.rgb = RGBColor.from_string("FFFFFF" if l == w else self.theme["accent"])
            _shade_el(rl._r.get_or_add_rPr(), self.theme["dec"] if l == w else self.theme["accent_soft"])
            p.add_run("  " + v.title(l)).bold = True
            _small(row[1].add_paragraph(), v.author(l) + (" · решение" if l == w else ""), size=8)
            _bar(row[2], bord / mx, self.theme["dec"] if l == w else "9AA3B6")
            _cell_text(row[3], f"{r.get('first_places', 0)} из {r.get('ballots', 0)} первых", size=8.5, color=MUTED,
                       align=WD_ALIGN_PARAGRAPH.RIGHT)
            _small(row[3].add_paragraph(), f"балл {bord:.2f}", size=8).font.color.rgb = RGBColor.from_string(MUTED)
            row[3].paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.RIGHT
            if l == w:
                for c in row:
                    _cell_shade(c, self.theme["dec_soft"])

    def voting(self) -> None:
        v = self.v
        if not (v.tally or v.ballots):
            return
        w = v.winner
        self.h("Голосование", 1, "voting")
        if v.tally:
            top = v.tally[0]
            n = len(v.tally)
            txt = f"Модели ранжировали {n} {_plural(n, 'вариант', 'варианта', 'вариантов')}, не зная авторов. "
            if len(v.top_tie) > 1:
                txt += f"Первое место поделили варианты {', '.join(v.top_tie)}. "
            elif top.get("ballots"):
                txt += (f"Вариант {top.get('label')} поставили первым {top.get('first_places', 0)} из {top.get('ballots')}"
                        + ("; он выигрывает у каждого соперника один на один. " if top.get("schulze") == n - 1 else ". "))
            if v.agreement:
                txt += AGREE_SENT[v.agreement["level"]]
            self.doc.add_paragraph(txt)
            self.vote_chart()
        notes = ([f"Согласие совета: {v.agreement_line}."] if v.agreement_line else []) + (
            [v.coverage_line + "."] if v.coverage_line else []) + (
            ["Решающие голоса (лидер меняется без одного бюллетеня): " + "; ".join(v.decisive_notes) + "."] if v.decisive_notes else []) + (
            ["Бюллетени против общего мнения (голоса учтены, это только пометка): " + "; ".join(v.flag_notes) + ". " + v.robust_note]
            if v.flag_notes else []) + ([v.position_line + "."] if v.position_line else []) + v.tie_notes
        if notes:
            self.h("Насколько надёжен результат", 2)
            self.bullets(notes)
        if v.ballots:
            self.h("Бюллетени", 2)
            t = _table(self.doc, len(v.ballots) + 1, 3, widths=[3, 3.6, 10.4])
            for j, hd in enumerate(("Участник", "Порядок", "Почему")):
                _cell_text(t.cell(0, j), hd)
            for i, (voter, b) in enumerate(v.ballots.items(), 1):
                _cell_text(t.cell(i, 0), voter)
                _cell_text(t.cell(i, 1), " › ".join(b.get("ranking", [])), bold=True)
                inline_markdown(t.cell(i, 2).paragraphs[0], b.get("rationale", ""))
        if v.tally:
            self.h("Подробный подсчёт", 2)
            heads = ["#", "Вариант", "Автор"] + (["Шульце"] if v.by_schulze else []) + ["Борда", "Попарно", "Критика", "1-х мест"]
            t = _table(self.doc, len(v.tally) + 1, len(heads),
                       widths=[0.8, 1.6, 3.4] + ([2.0] if v.by_schulze else []) + [1.8, 1.8, 1.8, 1.8])
            for j, hd in enumerate(heads):
                _cell_text(t.cell(0, j), hd, align=WD_ALIGN_PARAGRAPH.RIGHT if j in (0,) or j >= 3 else None)
            for i, r in enumerate(v.tally, 1):
                l = r.get("label", "")
                avg = r.get("avg_score")
                vals = [str(i), l, v.author(l)] + ([v.schulze_str(r, len(v.tally))] if v.by_schulze else []) + [
                    f"{float(r.get('borda') or 0):.2f}", v.pairwise_str(r), "—" if avg is None else f"{avg:.1f}",
                    f"{r.get('first_places', 0)}/{r.get('ballots', 0)}"]
                for j, val in enumerate(vals):
                    _cell_text(t.cell(i, j), val, bold=(l == w), align=WD_ALIGN_PARAGRAPH.RIGHT if j == 0 or j >= 3 else None)
                    if l == w:
                        _cell_shade(t.cell(i, j), self.theme["dec_soft"])
            self.meta(v.order_note + " Борда — средняя позиция в бюллетенях от 0 до 1; попарно — побед минус поражений "
                      "в прямых сравнениях; критика — средняя оценка рецензентов (1–10).")

    def proposals(self) -> None:
        v = self.v
        if not v.proposals:
            return
        w = v.winner
        self.h("Варианты", 1, "proposals")
        for l, p in v.proposals.items():
            hp = self.h("", 2, f"proposal_{l}")
            if l == w:
                r = hp.add_run("◆ ")
                r.font.color.rgb = RGBColor.from_string(self.theme["dec"])
            hp.add_run(f"{l}. {p.get('title', '')}")
            sub = [f"Автор: {v.author(l)}"]
            if l == w:
                sub.insert(0, "Решение совета")
            if v.place(l):
                sub.append(f"место в голосовании: {v.place(l)}")
            if p.get("confidence") is not None and v.author(l) != "пользователь":
                sub.append(f"уверенность автора: {round(float(p['confidence']) * 100)}%")
            if v.revised(l):
                sub.append("доработан после критики")
            self.meta(" · ".join(sub))
            add_markdown(self.doc, p.get("summary", ""))
            if p.get("reasoning"):
                self.h("Обоснование", 3)
                add_markdown(self.doc, p["reasoning"])
            if p.get("pros") or p.get("risks"):
                t = _table(self.doc, 1, 2, header=False, widths=[8.5, 8.5])
                _no_borders(t)
                for c, head, items in ((t.cell(0, 0), "Плюсы", p.get("pros") or []), (t.cell(0, 1), "Риски", p.get("risks") or [])):
                    if not items:
                        continue
                    c.paragraphs[0].style = "Heading 4"
                    c.paragraphs[0].add_run(head)
                    for x in items:
                        inline_markdown(c.add_paragraph(style="List Bullet"), x)
            if p.get("comparison") and v.revised(l):
                self.h("Сравнение с другими вариантами", 3)
                add_markdown(self.doc, p["comparison"])
            if p.get("borrowed") and v.revised(l):
                self.h("Взято у других", 3)
                self.bullets(p["borrowed"])
            if p.get("not_accepted") and v.revised(l):
                self.h("Что не принято из критики и почему", 3)
                self.bullets(p["not_accepted"])
            if p.get("changes"):
                pos = v.position(l)
                par = self.doc.add_paragraph(style="Council Box")
                par.add_run("Что изменено после критики" + (f" ({v.POSITION_NAME.get(pos, pos)})" if pos else "") + ": ").bold = True
                inline_markdown(par, p["changes"])
            ini = v.initial(l)
            if v.revised(l) and ini:
                self.h(f"Первоначальная версия: {ini.get('title', '')}", 3)
                add_markdown(self.doc, ini.get("summary", ""), base_style="Quote", heading_shift=4)
            reviews = v.reviews_for(l)
            if reviews:
                self.h("Оценки других участников", 3)
                t = _table(self.doc, len(reviews) + 1, 3, widths=[2.6, 3.8, 10.6])
                for j, hd in enumerate(("Рецензент", "Оценки", "Сильные и слабые стороны")):
                    _cell_text(t.cell(0, j), hd)
                for i, (reviewer, r) in enumerate(reviews, 1):
                    _cell_text(t.cell(i, 0), reviewer)
                    sc = t.cell(i, 1).paragraphs[0]
                    for k, (name, val) in enumerate((r.get("scores") or {}).items()):
                        if k:
                            sc.add_run().add_break()
                        sc.add_run(f"{name}: ")
                        sc.add_run(f"{float(val):g}").bold = True
                    cc = t.cell(i, 2)
                    first = True
                    for sign, color, items in (("+ ", OK, r.get("strengths", [])), ("− ", BAD, [readable_flaw(x) for x in r.get("weaknesses", [])])):
                        for x in items:
                            par = cc.paragraphs[0] if first else cc.add_paragraph()
                            first = False
                            rs = par.add_run(sign)
                            rs.bold = True
                            rs.font.color.rgb = RGBColor.from_string(color)
                            inline_markdown(par, x)

    def critique(self) -> None:
        v = self.v
        if not v.critiques:
            return
        labels, rows, avgs = v.matrix()
        self.h("Критика", 1, "critique")
        self.meta("Средняя оценка каждого рецензента по всем критериям, от 1 до 10. Оценки даны версиям до доработки "
                  "и в подсчёте голосов не участвуют.")
        t = _table(self.doc, len(rows) + 2, len(labels) + 1, widths=[3.4] + [1.5] * len(labels))
        _cell_text(t.cell(0, 0), "Рецензент")
        for j, l in enumerate(labels, 1):
            _cell_text(t.cell(0, j), l, align=WD_ALIGN_PARAGRAPH.CENTER)
        for i, (reviewer, vals) in enumerate(rows + [("Среднее", avgs)], 1):
            _cell_text(t.cell(i, 0), reviewer, bold=reviewer == "Среднее")
            for j, (l, x) in enumerate(zip(labels, vals), 1):
                c = t.cell(i, j)
                if x is None:
                    _cell_text(c, "свой" if v.author(l) == reviewer else "—", color=MUTED, align=WD_ALIGN_PARAGRAPH.CENTER)
                else:
                    _cell_text(c, f"{x:.1f}", bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
                    if reviewer != "Среднее":
                        _cell_shade(c, _score_fill(x))

    def members(self) -> None:
        v = self.v
        self.h("Участники и расходы", 1, "members")
        t = _table(self.doc, len(v.stats) + 2, 4, widths=[5, 2.6, 5.4, 4])
        for j, hd in enumerate(("Участник", "Обращений", "Токены (вход / выход)", "Стоимость")):
            _cell_text(t.cell(0, j), hd, align=WD_ALIGN_PARAGRAPH.RIGHT if j else None)
        for i, (name, s) in enumerate(v.stats.items(), 1):
            s = s or {}
            note = v.think_note(s)
            for j, val in enumerate((f"{name} ({note})" if note else name, str(s.get("calls", 0)),
                                     f"{int(s.get('prompt_tokens', 0)):,} / {int(s.get('completion_tokens', 0)):,}".replace(",", " "),
                                     f"${float(s.get('cost', 0)):.4f}")):
                _cell_text(t.cell(i, j), val, align=WD_ALIGN_PARAGRAPH.RIGHT if j else None)
        last = len(v.stats) + 1
        _cell_text(t.cell(last, 0), "Итого", bold=True)
        _cell_text(t.cell(last, 3), f"${v.cost:.4f}", bold=True, align=WD_ALIGN_PARAGRAPH.RIGHT)
        if v.cache_line:
            self.meta(v.cache_line + ".")
        fails = v.failures()
        if fails:
            self.h("Сбои", 2)
            t = _table(self.doc, len(fails) + 1, 3, widths=[3, 3, 11])
            for j, hd in enumerate(("Участник", "Этап", "Что случилось")):
                _cell_text(t.cell(0, j), hd)
            for i, (n, st, err) in enumerate(fails, 1):
                for j, val in enumerate((n, st, err)):
                    _cell_text(t.cell(i, j), val)

    def request(self, images: bool) -> None:
        v = self.v
        self.h("Запрос и условия", 1, "request")
        if v.followup_title:
            par = self.doc.add_paragraph()
            par.add_run("Уточнение к заседанию: ").bold = True
            par.add_run(v.followup_title)
        add_markdown(self.doc, v.question, breaks=True, base_style="Council Query", heading_shift=4)
        if v.refined:
            par = self.doc.add_paragraph()
            r = par.add_run("Как председатель уточнил вопрос: ")
            r.font.color.rgb = RGBColor.from_string(MUTED)
            par.add_run(v.refined)
        if v.requirements:
            self.h("Обязательные требования", 2)
            num = _restart_numbering(self.doc, "List Number")
            for x in v.requirements:
                par = self.doc.add_paragraph(x, style="List Number")
                if num:
                    _set_num(par, num, 0)
        if v.attachments:
            self.h("Вложения", 2)
            for m in v.attachments:
                par = self.doc.add_paragraph(v.attachment_label(m), style="List Bullet")
                if m.get("warning"):
                    _small(par, f" — {m['warning']}", WARN, 9)
            if images:
                for m in v.attachments:
                    if m.get("kind") != "image":
                        continue
                    url = v.thumb_data_url(m, 1200)
                    if not url:
                        continue
                    try:
                        pic = self.doc.add_picture(io.BytesIO(base64.b64decode(url.split(",", 1)[1])),
                                                   width=Cm(min(12.0, 12.0 * (m.get("width") or 1200) / 1200)))
                        _alt(pic, str(m.get("name", "")))
                        self.doc.add_paragraph(str(m.get("name", "")), style="Caption")
                    except Exception:  # noqa: BLE001
                        pass
                if v.image_notes:
                    self.meta(f"Описание изображений для моделей без зрения (сделал {v.d.get('image_notes_by') or 'модель'}):")
                    for n, txt in v.image_notes.items():
                        par = self.doc.add_paragraph()
                        par.add_run(f"{n}: ").bold = True
                        inline_markdown(par, txt)
        self.h("Условия заседания", 2)
        kv = [("Режим", v.mode), ("Участники", ", ".join(v.members)), ("Критерии", ", ".join(v.criteria) or "—")]
        if v.chair_note:
            kv.append(("Роли", v.chair_note))
        if v.compact_note:
            kv.append(("Варианты", v.compact_note))
        kv += [("Дата", v.date), ("Длительность", v.duration), ("Стоимость", f"${v.cost:.4f}")]
        t = _table(self.doc, len(kv), 2, header=False, widths=[4, 13])
        _no_borders(t)
        for i, (k, val) in enumerate(kv):
            _cell_text(t.cell(i, 0), k, color=MUTED)
            _cell_text(t.cell(i, 1), val)

    def save(self) -> bytes:
        buf = io.BytesIO()
        self.doc.save(buf)
        return buf.getvalue()


def _bar(cell, share: float, color: str) -> None:
    """Полоса графика: вложенная таблица из двух ячеек, первая залита цветом пропорционально баллу."""
    share = max(0.02, min(1.0, share))
    cell.paragraphs[0].paragraph_format.space_after = Pt(0)
    inner = cell.add_table(rows=1, cols=2)
    _no_borders(inner)
    tw = OxmlElement("w:tblW")
    tw.set(qn("w:type"), "pct")
    tw.set(qn("w:w"), "5000")
    inner._tbl.tblPr.append(tw)
    trpr = inner.rows[0]._tr.get_or_add_trPr()
    h = OxmlElement("w:trHeight")
    h.set(qn("w:val"), "140")
    h.set(qn("w:hRule"), "exact")
    trpr.append(h)
    for c, part, fill in ((inner.cell(0, 0), share, color), (inner.cell(0, 1), 1 - share, "ECEFF5")):
        c.width = Cm(max(0.05, 5.0 * part))
        _cell_shade(c, fill)
        tcw = c._tc.get_or_add_tcPr().find(qn("w:tcW"))
        if tcw is not None:
            tcw.set(qn("w:type"), "pct")
            tcw.set(qn("w:w"), str(max(1, int(5000 * part))))
        r = c.paragraphs[0].add_run(" ")
        r.font.size = Pt(2)
        c.paragraphs[0].paragraph_format.space_after = Pt(0)
    _set_widths(inner, [share, 1 - share], total_cm=4.8)


def _score_fill(v: float) -> str:
    t = max(0.0, min(1.0, (v - 1) / 9))
    mix = lambda c: round(255 - (255 - c) * (0.08 + t * 0.4))  # noqa: E731
    return f"{mix(0x4F):02X}{mix(0x46):02X}{mix(0xE5):02X}"


def _alt(inline_shape, text: str) -> None:
    """Альтернативный текст картинки (для экранных дикторов и проверки доступности Word)."""
    try:
        inline_shape._inline.docPr.set("descr", text or "изображение")
        inline_shape._inline.docPr.set("title", text or "изображение")
    except AttributeError:
        pass


def _mark_png() -> bytes:
    """Знак «Совета нейросетей» (PNG) для шапки, если своего логотипа нет."""
    b = brand.current()
    if b.title != brand.DEFAULT_TITLE:
        return b""
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "web" / "static" / "brand" / "apple-touch-icon.png"
    try:
        return p.read_bytes()
    except OSError:
        return b""


# ------------------------------------------------------------------ три вида отчёта

def render_decision_docx(view: View) -> bytes:
    """Только решение совета: сам ответ, без вопроса."""
    v = view
    rep = _Report(v, "decision")
    rep.masthead()
    add_markdown(rep.doc, v.verdict.get("final_answer", ""), heading_shift=2)
    n = len(v.members)
    rep.meta(f"{v.date} · совет из {n} {_plural(n, 'модели', 'моделей', 'моделей')}: {', '.join(v.members)}")
    return rep.save()


def render_docx(view: View, summary: bool = False, decision: bool = False) -> bytes:
    """Полный отчёт, (summary=True) итог — решение с обоснованием — или (decision=True) только решение."""
    if decision:
        return render_decision_docx(view)
    v = view
    rep = _Report(v, "summary" if summary else "full")
    rep.masthead()
    rep.lead()
    rep.keyfacts(full=not summary)
    if not summary:
        rep.toc()
    rep.decision()
    rep.kcu()
    rep.rationale()
    rep.ideas()
    rep.sources(full=not summary)
    rep.facts()
    if summary:
        rep.request(images=False)
        rep.meta(SUMMARY_NOTE)
        return rep.save()
    rep.voting()
    rep.proposals()
    rep.critique()
    rep.members()
    rep.request(images=True)
    return rep.save()


__all__ = ["render_docx", "render_decision_docx", "add_markdown", "inline_markdown"]
