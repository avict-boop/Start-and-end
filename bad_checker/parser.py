"""Разбор состава БАД из текста или JSON."""
from __future__ import annotations

import json
import re
from pathlib import Path

from .models import Ingredient, Product

# Единицы → каноническое обозначение
UNIT_ALIASES = {
    "мг": "мг", "mg": "мг",
    "мкг": "мкг", "mcg": "мкг", "µg": "мкг", "μg": "мкг", "ug": "мкг",
    "г": "г", "гр": "г", "g": "г",
    "ме": "МЕ", "iu": "МЕ", "me": "МЕ",
    "кое": "КОЕ", "cfu": "КОЕ",
}

_UNIT_RE = "|".join(sorted((re.escape(u) for u in UNIT_ALIASES), key=len, reverse=True))
AMOUNT_RE = re.compile(
    rf"(?P<num>\d+(?:[.,]\d+)?(?:\s*[x×]\s*10\^?\d+)?)\s*(?P<unit>{_UNIT_RE})\.?(?![а-яa-z])",
    re.IGNORECASE,
)


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def _to_float(num: str) -> float:
    num = num.replace(",", ".").replace(" ", "")
    m = re.match(r"([\d.]+)[x×]10\^?(\d+)", num)
    if m:
        return float(m.group(1)) * 10 ** int(m.group(2))
    return float(num)


def parse_ingredient(raw: str) -> Ingredient | None:
    raw = raw.strip(" \t.;,")
    if not raw:
        return None
    m = AMOUNT_RE.search(raw)
    if not m:
        return Ingredient(name=raw, raw=raw)
    name = raw[: m.start()].strip(" -:–—(\t")
    if not name:  # количество в начале строки: «500 мг магния цитрата»
        name = raw[m.end():].strip(" -:–—)\t")
    unit = UNIT_ALIASES[m.group("unit").lower()]
    return Ingredient(name=name, amount=_to_float(m.group("num")), unit=unit, raw=raw)


def parse_composition_text(text: str) -> list[Ingredient]:
    """Разбивает текст состава на компоненты.

    Каждый компонент — отдельная строка или элемент, разделённый «;».
    Запятые внутри строки не разделяют (они встречаются в «1,5 мг»).
    """
    parts: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(состав|ingredients)\s*:\s*", "", line, flags=re.IGNORECASE)
        parts.extend(line.split(";"))
    result = []
    for p in parts:
        ing = parse_ingredient(p)
        if ing:
            result.append(ing)
    return result


def product_from_dict(data: dict) -> Product:
    ingredients: list[Ingredient] = []
    for item in data.get("ingredients", []):
        if isinstance(item, str):
            ing = parse_ingredient(item)
            if ing:
                ingredients.append(ing)
        else:
            unit = item.get("unit")
            ingredients.append(Ingredient(
                name=item["name"],
                amount=float(item["amount"]) if item.get("amount") is not None else None,
                unit=UNIT_ALIASES.get(unit.lower(), unit) if unit else None,
                raw=item.get("raw", item["name"]),
            ))
    if data.get("composition_text"):
        ingredients.extend(parse_composition_text(data["composition_text"]))

    dose = data.get("daily_dose", {})
    return Product(
        name=data.get("name", "Без названия"),
        ingredients=ingredients,
        units_per_day=float(dose.get("units_per_day", 1)),
        amounts_per_unit=bool(dose.get("amounts_per_unit", True)),
        unit_name=dose.get("unit_name", "порция"),
        label_text=data.get("label_text", ""),
    )


def load_product(path: str | Path, units_per_day: float | None = None) -> Product:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        product = product_from_dict(json.loads(text))
    else:
        product = Product(name=path.stem, ingredients=parse_composition_text(text), label_text=text)
    if units_per_day is not None:
        product.units_per_day = units_per_day
    return product
