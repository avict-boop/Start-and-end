from pathlib import Path

import pytest

from bad_checker import analyze, parse_composition_text, product_from_dict
from bad_checker.analyzer import find_nutrient
from bad_checker.models import Level
from bad_checker.parser import parse_ingredient

EXAMPLES = Path(__file__).parent.parent / "examples"


def codes(report):
    return {f.code for f in report.findings}


def make(ingredients, per_day=1, label=""):
    return product_from_dict({"name": "t", "ingredients": ingredients,
                              "daily_dose": {"units_per_day": per_day}, "label_text": label})


@pytest.mark.parametrize("raw,name,amount,unit", [
    ("Витамин C – 60 мг", "Витамин C", 60, "мг"),
    ("Витамин B2 1,5 mg", "Витамин B2", 1.5, "мг"),
    ("Витамин D3: 1000 МЕ", "Витамин D3", 1000, "МЕ"),
    ("Селен (селенометионин) 50 мкг", "Селен (селенометионин)", 50, "мкг"),
    ("Микрокристаллическая целлюлоза", "Микрокристаллическая целлюлоза", None, None),
])
def test_parse_ingredient(raw, name, amount, unit):
    ing = parse_ingredient(raw)
    assert (ing.name, ing.amount, ing.unit) == (name, amount, unit)


def test_parse_text_splits_lines_and_semicolons():
    ings = parse_composition_text("Состав: Цинк 10 мг; Медь 1 мг\nЖелезо 14 мг")
    assert [i.name for i in ings] == ["Цинк", "Медь", "Железо"]


@pytest.mark.parametrize("name,expected", [
    ("Пантотенат кальция", "pantothenic"),
    ("Аскорбат кальция", "vit_c"),
    ("Кальция карбонат", "calcium"),
    ("Витамин D3 (холекальциферол)", "vit_d"),
    ("Пиридоксина гидрохлорид", "vit_b6"),
    ("Экстракт ромашки", None),
])
def test_find_nutrient(name, expected):
    n = find_nutrient(name)
    assert (n["id"] if n else None) == expected


def test_vitamin_d_iu_conversion_and_upper_limit():
    r = analyze(make([{"name": "Витамин D3", "amount": 5000, "unit": "МЕ"}]))
    assert r.nutrients[0].daily_amount == pytest.approx(125)
    assert "over_upper:vit_d" in codes(r)


def test_daily_multiplier():
    ok = analyze(make([{"name": "Цинк", "amount": 10, "unit": "мг"}], per_day=2))
    bad = analyze(make([{"name": "Цинк", "amount": 10, "unit": "мг"}], per_day=3))
    assert "within:zinc" in codes(ok)
    assert "over_upper:zinc" in codes(bad)


def test_mg_to_mcg_conversion():
    r = analyze(make([{"name": "Селен", "amount": 0.2, "unit": "мг"}]))
    assert r.nutrients[0].daily_amount == pytest.approx(200)
    assert r.verdict == "НЕ СООТВЕТСТВУЕТ"


@pytest.mark.parametrize("ingredient,code", [
    ("Экстракт травы эфедры", "prohibited:ephedra"),
    ("Ephedra sinica extract", "prohibited:ephedra"),
    ("Кора йохимбе", "prohibited:yohimbe"),
    ("Сибутрамин", "prohibited:sibutramine"),
    ("Краситель Е123", "additive:E123"),
])
def test_prohibited(ingredient, code):
    r = analyze(make([ingredient]))
    assert code in codes(r)
    assert r.verdict == "НЕ СООТВЕТСТВУЕТ"


@pytest.mark.parametrize("ingredient", [
    "Пиридоксина гидрохлорид 2 мг",   # не «хлор» E925
    "Экстракт ромашки 100 мг",
    "Магния цитрат 100 мг",
    "Борщевик",                        # не «борец»
])
def test_no_false_prohibited(ingredient):
    r = analyze(make([ingredient]))
    assert not any(c.startswith(("prohibited", "additive")) for c in codes(r))


def test_compound_mass_warning():
    r = analyze(make(["Магния цитрат 300 мг"]))
    assert "compound_mass" in codes(r)
    r2 = analyze(make(["Магния цитрат 300 мг (в пересчете на магний)"]))
    assert "compound_mass" not in codes(r2)


def test_label_checks_and_medical_claims():
    r = analyze(make(["Витамин C 80 мг"], label="Лечит простуду"))
    c = codes(r)
    assert {"label_not_drug", "label_contra", "medical_claim"} <= c


def test_examples():
    from bad_checker import load_product
    good = analyze(load_product(EXAMPLES / "multivitamin_ok.json"))
    assert good.count(Level.CRITICAL) == 0 and good.count(Level.WARNING) == 0
    bad = analyze(load_product(EXAMPLES / "fatburner_bad.txt", units_per_day=2))
    assert {"prohibited:ephedra", "prohibited:yohimbe", "over_upper:caffeine"} <= codes(bad)


def test_empty_composition():
    assert analyze(make([])).verdict == "НЕ СООТВЕТСТВУЕТ"
