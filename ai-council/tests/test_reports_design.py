"""Новое оформление отчётов: порядок разделов, PDF с тегами и закладками, Word со стилями, Markdown, формулы и ссылки."""
import io
import re

import pytest
from docx import Document
from docx.oxml.ns import qn

from council.archive import Archive, all_reports
from council.reports import samples
from council.reports.docx import render_docx
from council.reports.html import render_html
from council.reports.md import demote, render_md
from council.reports.pdf import PDF_AVAILABLE, render_pdf
from council.reports.view import View, latex_to_html, md_to_html


def test_decision_comes_before_details_and_request_comes_last():
    v = View(samples.full_debate(), "x")
    html = render_html(v)
    order = [html.index(f'id="{a}"') for a in ("verdict", "why", "facts", "voting", "proposals", "critique", "members", "request")]
    assert order == sorted(order)
    md = render_md(v)
    assert md.index("## Решение совета") < md.index("## Голосование") < md.index("## Запрос и условия")
    assert "Какую CRM выбрать" in v.display_title


def test_long_prompt_title_comes_from_framing():
    d = samples.quick_short()
    d["question"] = "Ты — эксперт по CRM.\n" + "\n".join(f"Пункт {i}: подробное условие задачи." for i in range(12))
    d["refined_question"] = "Какую CRM выбрать отделу продаж из 12 человек?"
    v = View(d, "x")
    assert v.question_is_prompt and v.display_title == "Какую CRM выбрать отделу продаж из 12 человек?"
    html = render_html(v, summary=True)
    assert "Полный текст запроса" in html and html.index('id="verdict"') < html.index('id="request"')
    assert "Ты — эксперт по CRM." in html  # начало запроса и полный текст в конце — данные не теряются


def test_short_question_is_not_repeated_under_title():
    v = View(samples.full_debate(), "x")
    html = render_html(v)
    lead = html.split('<div class="lead">', 1)[1].split("</div></div>", 1)[0]
    assert "Какую CRM выбрать" not in lead and "Сейчас заявки ведём" in lead


def test_latex_and_bare_links():
    assert latex_to_html(r"x^2 + \frac{a}{b} \le \alpha") == "x<sup>2</sup> + a⁄b ≤ α"
    h = md_to_html("Цена $215 и $300, формула $x_i^2$, ссылка https://ex.com/a_b?x=1. Код `$a_b$`")
    assert "$215 и $300" in h and '<span class="math">x<sub>i</sub><sup>2</sup></span>' in h
    assert '<a href="https://ex.com/a_b?x=1">' in h and "<code>$a_b$</code>" in h


def test_markdown_model_headings_are_demoted():
    assert demote("# Итог\n## План\n```\n# не заголовок\n```", 3) == "### Итог\n#### План\n```\n# не заголовок\n```"
    md = render_md(View(samples.full_debate(), "x"))
    assert "\n## Почему amoCRM" not in md and "\n### Почему amoCRM" in md


def test_word_is_a_real_document():
    doc = Document(io.BytesIO(render_docx(View(samples.full_debate(), "x"))))
    styles = {p.style.name for p in doc.paragraphs}
    assert {"Title", "Heading 1", "Heading 2", "List Bullet", "List Number"} <= styles
    assert doc.sections[0].page_width.cm == pytest.approx(21, abs=0.01)  # A4
    assert doc.core_properties.title and doc.core_properties.language == "ru-RU"
    body = doc.element.body.xml
    assert 'TOC \\o "1-2"' in body and "<w:tblHeader/>" in body  # оглавление и повтор шапки таблиц
    # каждый нумерованный список начинается с 1: у разных списков разные номера списка Word
    num_ids = {p._p.pPr.numPr.numId.val for p in doc.paragraphs
               if p.style.name == "List Number" and p._p.pPr is not None and p._p.pPr.numPr is not None}
    assert len(num_ids) >= 2
    assert any(t.style.name == "Council Table" for t in doc.tables)
    pics = doc.element.body.findall(".//" + qn("wp:docPr"))
    assert pics and all(p.get("descr") for p in pics)  # у картинок есть альтернативный текст


@pytest.mark.skipif(not PDF_AVAILABLE, reason="нет WeasyPrint")
def test_pdf_is_tagged_with_section_bookmarks():
    from pypdf import PdfReader
    pdf = PdfReader(io.BytesIO(render_pdf(View(samples.full_debate(), "x"))))
    root = pdf.trailer["/Root"]
    assert "/StructTreeRoot" in root and root.get("/Lang") in ("ru", "ru-RU")
    titles = [o.title for o in pdf.outline if not isinstance(o, list)]
    assert titles[:3] == ["Решение совета", "Обоснование", "Проверка фактов"]
    assert "Почему amoCRM" not in str(pdf.outline)  # заголовки из ответа модели — не в закладках
    assert pdf.metadata.title.startswith("Какую CRM выбрать")


def test_all_samples_build_in_every_format(tmp_path):
    arc = Archive(tmp_path)
    for name in ("03-nichya-i-sboi", "04-ocenka-variantov-en-tr", "06-utochnenie"):
        res = arc.build(name, all_reports(), data=samples.SAMPLES[name]())
        assert not [k for k, x in res.items() if str(x).startswith("ошибка")], res
    text = (tmp_path / "03-nichya-i-sboi.md").read_text()
    assert "Ничья за первое место" in text and "Председатель выбрал не лидера" in render_html(View(samples.tie_deviation(), "x"))
    assert re.search(r"Уточнение к заседанию «Как назвать кофейню", (tmp_path / "06-utochnenie.md").read_text())
