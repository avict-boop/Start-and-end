import base64
from datetime import date
from pathlib import Path

import pytest

from bad_checker import analyze
from bad_checker.parser import extract_composition, product_from_text
from bad_checker.sgr import find_sgr, has_malformed_sgr

EXAMPLES = Path(__file__).parent.parent / "examples"


def codes(report):
    return {f.code for f in report.findings}


# --- СГР -------------------------------------------------------------------
def test_sgr_parse_valid():
    [n] = find_sgr("СГР № RU.77.99.11.003.R.001234.05.24 от ...")
    assert (n.country, n.kind, n.number, n.month, n.year) == ("RU", "11", "001234", 5, 2024)
    assert n.problems(date(2026, 1, 1)) == []


def test_sgr_cyrillic_letter_and_spaces():
    assert find_sgr("RU. 77. 99. 11. 003. Е. 012345. 11. 15")


@pytest.mark.parametrize("raw,problem", [
    ("RU.77.99.11.003.R.001234.13.24", "месяц"),
    ("RU.77.99.11.003.R.001234.05.30", "будущем"),
    ("RU.77.99.32.003.R.001234.05.24", "вида продукции"),
])
def test_sgr_problems(raw, problem):
    [n] = find_sgr(raw)
    assert any(problem in p for p in n.problems(date(2026, 11, 1)))


def test_sgr_malformed_detected():
    assert has_malformed_sgr("СГР RU.77.99.11.003.001234") is not None
    assert has_malformed_sgr("нет номера") is None


def test_sgr_in_report():
    r = analyze(product_from_text("Цинк 10 мг", label_text="СГР RU.77.99.11.003.R.001234.13.24"))
    assert "sgr_suspicious" in codes(r)


# --- разбор этикетки --------------------------------------------------------
def test_extract_composition_stops_at_label_sections():
    text = "БАД «X»\nСостав:\nЦинк 10 мг\nСелен 50 мкг\nРекомендации по применению: 1 таблетка\nСрок годности 2 года"
    block = extract_composition(text)
    assert "Цинк" in block and "Селен" in block and "Рекомендации" not in block
    assert [i.name for i in product_from_text(text).ingredients] == ["Цинк", "Селен"]


@pytest.mark.parametrize("claim", ["Помогает в лечении усталости", "Лечит простуду", "Излечивает от всех болезней"])
def test_medical_claims(claim):
    assert "medical_claim" in codes(analyze(product_from_text("Цинк 10 мг", label_text=claim)))


def test_no_medical_claim_false_positive():
    r = analyze(product_from_text("Цинк 10 мг", label_text="Не является лекарственным средством. Беречь от детей."))
    assert "medical_claim" not in codes(r)


# --- OCR ---------------------------------------------------------------------
def _ocr_available():
    try:
        from bad_checker.ocr import _deps
        _deps()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _ocr_available(), reason="Tesseract с русским языком не установлен")
def test_ocr_label_photo():
    from bad_checker import load_product
    r = analyze(load_product(EXAMPLES / "label_photo.png"))
    assert {"prohibited:ephedra", "over_upper:vit_c", "sgr_found"} <= codes(r)


# --- веб-API -----------------------------------------------------------------
def test_web_analyze_text():
    from bad_checker.web.server import handle_analyze
    res = handle_analyze({"name": "T", "composition_text": "Цинк 10 мг", "units_per_day": "3"})
    assert res["verdict"] == "НЕ СООТВЕТСТВУЕТ"
    assert res["nutrients"][0]["daily_amount"] == 30
    assert res["markdown"].startswith("# Анализ БАД: T")


@pytest.mark.skipif(not _ocr_available(), reason="Tesseract с русским языком не установлен")
def test_web_analyze_image():
    from bad_checker.web.server import handle_analyze
    img = base64.b64encode((EXAMPLES / "label_photo.png").read_bytes()).decode()
    res = handle_analyze({"image": "data:image/png;base64," + img})
    assert "эфедр" in res["ocr_text"].lower()
    assert res["counts"]["critical"] >= 2
