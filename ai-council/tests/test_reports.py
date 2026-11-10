import io

import pytest
from docx import Document

from council.reports.docx import render_docx
from council.reports.html import render_html
from council.reports.md import render_md
from council.reports.pdf import PDF_AVAILABLE, render_pdf
from council.reports.view import View, md_to_html

WIDE = ("| К | " + " | ".join(f"Колонка{i}" for i in range(8)) + " |\n|" + "---|" * 9 + "\n"
        "| строка | " + " | ".join("очень длинный текст ячейки, который не помещается" for _ in range(8)) + " |")


def session(answer: str) -> dict:
    p = {"title": "Вариант", "summary": answer, "reasoning": "", "pros": ["+1 плюс"], "risks": [], "confidence": 0.5}
    return {"question": "Вопрос?", "mode": "quick", "criteria": ["x"], "authors": {"A": "a", "B": "b"},
            "initial_proposals": {"A": p, "B": p}, "proposals": {"A": p, "B": p}, "critiques": {},
            "ballots": {"a": {"ranking": ["B"], "rationale": "ok"}},
            "tally": [{"label": "A", "borda": 1, "avg_score": None, "first_places": 1, "ballots": 1},
                      {"label": "B", "borda": 0, "avg_score": None, "first_places": 0, "ballots": 1}],
            "verdict": {"winner": "A", "final_answer": answer, "why": "так", "dissent": [], "risks": []},
            "stats": {"a": {"calls": 1, "cost": 0.01}}, "total_cost_usd": 0.01, "duration_s": 3}


def test_tables_get_width_class_and_wrapper():
    html = md_to_html(WIDE + "\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert '<div class="tbl"><table class="mdt xwide" data-cols="9">' in html
    assert 'class="mdt normal" data-cols="2"' in html


def test_docx_keeps_model_tables():
    """Регрессия: таблицы в обёртке <div class="tbl"> пропадали из Word."""
    doc = Document(io.BytesIO(render_docx(View(session(WIDE)))))
    widths = sorted(len(t.columns) for t in doc.tables)
    assert widths.count(9) >= 3  # решение + два варианта
    wide = next(t for t in doc.tables if len(t.columns) == 9)
    assert wide.cell(0, 1).text == "Колонка0"
    assert wide.cell(1, 1).paragraphs[0].runs[0].font.size.pt == 7.5


def test_html_and_md_reports():
    v = View(session(WIDE + "\n\n- пункт\n  - вложенный"))
    html = render_html(v)
    assert "mdt xwide" in html and "markScroll" in html and "<ul>" in html
    md = render_md(v)
    assert "| Колонка0 |" in md.replace("| К | Колонка0 |", "| Колонка0 |")


@pytest.mark.skipif(not PDF_AVAILABLE, reason="нет WeasyPrint/Pango")
def test_pdf_wide_table_fits_page():
    import re
    pdf = render_pdf(View(session(WIDE)))
    assert pdf[:5] == b"%PDF-"
    # все блоки текста внутри ширины A4 (595pt) за вычетом полей: проверяем через макет WeasyPrint
    import weasyprint
    doc = weasyprint.HTML(string=render_html(View(session(WIDE)))).render()
    page_width = doc.pages[0].width
    def boxes(box):
        yield box
        for child in getattr(box, "children", []) or []:
            yield from boxes(child)
    over = [b for p in doc.pages for b in boxes(p._page_box)
            if getattr(b, "element_tag", "") in ("table", "td", "th") and b.position_x + b.margin_width() > page_width + 0.5]
    assert not over, f"{len(over)} элементов таблицы выходят за страницу"
