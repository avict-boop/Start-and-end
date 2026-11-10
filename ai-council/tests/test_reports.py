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


def test_summary_reports_contain_only_the_decision():
    v = View(session(WIDE + "\n\nИтоговый ответ совета."))
    html = render_html(v, summary=True)
    assert "Итоговый ответ совета." in html and "Решение совета" in html
    for absent in ('id="voting"', "Бюллетени", 'id="proposal-A"', "Участники и расходы", 'class="toc"'):
        assert absent not in html
    md = render_md(v, summary=True)
    assert "Итоговый ответ совета." in md and "## Голосование" not in md and "## Варианты" not in md
    doc = Document(io.BytesIO(render_docx(v, summary=True)))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Итоговый ответ совета." in text and "Голосование" not in text and "Варианты" not in text
    full = "\n".join(p.text for p in Document(io.BytesIO(render_docx(v))).paragraphs)
    assert "Голосование" in full and len(text) < len(full)


def test_decision_reports_contain_only_the_answer():
    v = View(session("Итоговый ответ совета."))
    html = render_html(v, decision=True)
    assert "Итоговый ответ совета." in html and "решение совета" in html
    for absent in ("Почему", "Особые мнения", "Риски", 'id="voting"', "Бюллетени", 'id="proposal-A"', "Стоимость"):
        assert absent not in html
    md = render_md(v, decision=True)
    assert "Итоговый ответ совета." in md and "Почему" not in md and "Голосование" not in md
    text = "\n".join(p.text for p in Document(io.BytesIO(render_docx(v, decision=True))).paragraphs)
    assert "Итоговый ответ совета." in text and "Почему" not in text and "Голосование" not in text


def test_archive_backfills_summary(tmp_path):
    import json
    from council.archive import Archive, all_reports
    (tmp_path / "20250101-000000-old.json").write_text(json.dumps(session("Ответ")), encoding="utf-8")
    a = Archive(tmp_path)
    assert a.ensure_all() == (1, 0)
    for kind in all_reports():
        assert (tmp_path / f"20250101-000000-old.{kind}").is_file()
    assert a.delete("20250101-000000-old") and not list(tmp_path.glob("20250101-000000-old.*"))


def test_reports_show_agreement_and_pairwise():
    d = session("Ответ совета")
    d["ballots"] = {"x": {"ranking": ["A", "B"], "rationale": ""}, "y": {"ranking": ["B", "A"], "rationale": ""}}
    d["tally"] = [{"label": "A", "borda": 0.5, "avg_score": None, "first_places": 1, "ballots": 2, "pairwise": 0,
                   "tie_break": "label"},
                  {"label": "B", "borda": 0.5, "avg_score": None, "first_places": 1, "ballots": 2, "pairwise": 0,
                   "tie_break": ""}]
    v = View(d)
    assert v.agreement["level"] == "low"  # противоположные бюллетени — считается и для старых заседаний
    html = render_html(v)
    assert "Согласие совета" in html and "Низкое согласие совета" in html and "Попарно" in html
    assert "A выше B: при равном балле Борда решило жребий (метка)" in html
    md = render_md(v)
    assert "| Попарно |" in md and "Низкое согласие" in md
    text = "\n".join(p.text for p in Document(io.BytesIO(render_docx(v))).paragraphs)
    assert "Низкое согласие совета" in text


def test_reports_show_fact_check_and_flags():
    d = session("Ответ совета")
    d["fact_checker"] = "perplexity/sonar-pro"
    d["fact_check"] = {"A": {"summary": "Почти всё верно", "claims": [
        {"claim": "Население — 1 млн", "status": "confirmed", "note": "Росстат", "sources": ["https://rosstat.example/x"]},
        {"claim": "Налог 5%", "status": "refuted", "note": "На деле 6%", "sources": ["javascript:alert(1)"]}]}}
    d["ballot_flags"] = {"kimi": {"tau": -1.0, "flags": ["against", "leader_last"], "consensus_top": "A"}}
    d["leader_without_flagged"] = "B"
    v = View(d)
    assert "✅ 1 подтверждено" in v.fact_line and "❌ 1 опровергнуто" in v.fact_line
    html = render_html(v)
    assert 'id="facts"' in html and "Налог 5%" in html and "https://rosstat.example/x" in html
    assert "javascript:" not in html  # небезопасные ссылки отбрасываются
    assert "Бюллетени против общего мнения" in html and "лидером был бы вариант B" in html
    assert "Проверка фактов" in render_html(v, summary=True)  # и в кратком итоге
    assert "Проверка фактов" not in render_html(v, decision=True)
    md = render_md(v)
    assert "## Проверка фактов" in md and "❌ **опровергнуто:** Налог 5%" in md and "kimi —" in md
    text = "\n".join(p.text for p in Document(io.BytesIO(render_docx(v))).paragraphs)
    assert "Проверка фактов" in text and "опровергнуто" in text and "Бюллетени против общего мнения" in text


def test_stats_chair_overrides(tmp_path):
    import json
    from council.archive import Archive
    from council.stats import compute
    base = session("Ответ")
    base["request"] = {"chair": "claude"}
    base["tally"] = [{"label": "A", "borda": 1, "first_places": 1, "ballots": 1},
                     {"label": "B", "borda": 0, "first_places": 0, "ballots": 1}]
    for i, winner in enumerate(["A", "B", "A"]):
        d = json.loads(json.dumps(base))
        d["verdict"]["winner"] = winner
        (tmp_path / f"2025010{i}-000000-s{i}.json").write_text(json.dumps(d), encoding="utf-8")
    d = json.loads(json.dumps(base))
    d["verdict"]["why"] = "Председатель не ответил; победитель определён по голосованию."
    (tmp_path / "20250109-000000-fb.json").write_text(json.dumps(d), encoding="utf-8")
    ch = compute(Archive(tmp_path))["chairs"]
    assert ch[0]["name"] == "claude" and ch[0]["sessions"] == 4 and ch[0]["decisions"] == 3
    assert ch[0]["overrides"] == 1 and abs(ch[0]["override_rate"] - 1 / 3) < 1e-9 and ch[0]["fallbacks"] == 1
