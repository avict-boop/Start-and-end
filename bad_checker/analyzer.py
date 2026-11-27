"""Проверка состава и маркировки БАД на соответствие требованиям РФ/ЕАЭС."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .models import Finding, Ingredient, Level, NutrientResult, Product, Report
from .parser import normalize

DATA_DIR = Path(__file__).parent / "data"

MASS_FACTORS = {"г": 1_000_000.0, "мг": 1_000.0, "мкг": 1.0}  # в микрограммах

MINERALS = {"calcium", "phosphorus", "magnesium", "iron", "zinc", "iodine", "copper",
            "manganese", "selenium", "chromium", "molybdenum"}
COMPOUND_MARKERS = ("цитрат", "оксид", "карбонат", "сульфат", "глюконат", "фумарат",
                    "бисглицинат", "пиколинат", "лактат", "хлорид", "йодид", "йодат",
                    "селенит", "молибдат", "хелат", "аспарагинат", "малат")
ELEMENTAL_MARKERS = ("в пересчете на", "элементарн", "в т.ч.", "в том числе")

# Обязательные элементы маркировки БАД (ТР ТС 022/2011 ст. 4.4, ТР ТС 021/2011 ст. 7 и др.)
LABEL_REQUIREMENTS = [
    ("label_bad_status", [r"биологически активн\w* добавк\w* к пище", r"\bбад\b"],
     "Не указано, что продукт является биологически активной добавкой к пище",
     "ТР ТС 022/2011, ст. 4.4; ТР ТС 021/2011"),
    ("label_not_drug", [r"не является лекарственн\w* (средством|препаратом)"],
     "Нет надписи «Не является лекарственным средством»",
     "ТР ТС 022/2011, ст. 4.4"),
    ("label_usage", [r"рекомендаци\w* по применению", r"способ применения", r"принимать по"],
     "Нет рекомендаций по применению (дозировка, способ применения)",
     "ТР ТС 022/2011, ст. 4.4"),
    ("label_contra", [r"противопоказани"],
     "Не указаны противопоказания",
     "ТР ТС 022/2011, ст. 4.4"),
    ("label_storage", [r"услови\w* хранения", r"хранить"],
     "Не указаны условия хранения",
     "ТР ТС 022/2011, ст. 4.1"),
    ("label_shelf", [r"срок годности", r"годен до"],
     "Не указан срок годности",
     "ТР ТС 022/2011, ст. 4.1"),
    ("label_manufacturer", [r"изготовител", r"производител"],
     "Не указан изготовитель",
     "ТР ТС 022/2011, ст. 4.1"),
    ("label_sgr", [r"свидетельств\w* о (государственной )?регистрации", r"\bсгр\b", r"ru\.\d{2}\.\d{2}"],
     "Не указаны сведения о государственной регистрации (СГР)",
     "ТР ТС 021/2011, ст. 24; ТР ТС 022/2011, ст. 4.4"),
]

# БАД не может позиционироваться как лекарство (ТР ТС 022/2011 ст. 4.4; 38-ФЗ «О рекламе» ст. 25)
MEDICAL_CLAIMS = [
    r"\bлеч(ит|ение|ения|ебн\w*)\b", r"\bизлеч\w*", r"\bвылеч\w*",
    r"\bисцел\w*", r"\bот (всех )?болезн\w*", r"\bтерапи\w*",
    r"\bзаменяет (лекарств\w*|препарат\w*)", r"\bгарантирует\w*",
    r"\b100\s*% (результат|эффект)\w*",
]


@lru_cache(maxsize=None)
def load_db(name: str) -> dict:
    return json.loads((DATA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _stem(word: str) -> str:
    return re.sub(r"[аяыиоейьу]+$", "", word) if len(word) > 4 else word


def _ru_pattern(name: str) -> re.Pattern:
    """Шаблон для русского названия с учётом окончаний (эфедра → эфедры)."""
    words = normalize(name).split()
    parts = []
    for w in words:
        st = _stem(w)
        if st == w and len(w) <= 4:
            parts.append(re.escape(w) + r"(?![а-яa-z0-9])")
        else:
            parts.append(re.escape(st) + r"[а-яa-z]*")
    return re.compile(r"(?<![а-яa-z0-9])" + r"[\s-]+".join(parts))


def _latin_pattern(name: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(normalize(name)) + r"(?![a-z0-9])")


@lru_cache(maxsize=None)
def _prohibited_patterns():
    out = []
    for item in load_db("prohibited")["items"]:
        pats = [_ru_pattern(n) for n in item["names"]] + [_latin_pattern(n) for n in item["latin"]]
        out.append((item, pats))
    return out


@lru_cache(maxsize=None)
def _nutrient_aliases():
    """(alias, nutrient) от длинных к коротким — чтобы «пантотенат кальция» не стал кальцием."""
    pairs = []
    for n in load_db("nutrients")["items"]:
        for a in n["aliases"]:
            pairs.append((normalize(a), n))
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def find_nutrient(name: str) -> dict | None:
    text = normalize(name)
    for alias, nutrient in _nutrient_aliases():
        if re.search(r"(?<![а-яa-z])" + re.escape(alias) + r"(?![a-z0-9])", text):
            return nutrient
    return None


def convert(amount: float, unit: str, nutrient: dict) -> float | None:
    """Перевести количество в единицы, в которых заданы нормы нутриента."""
    target = nutrient["unit"]
    if unit == "МЕ":
        k = nutrient.get("iu_to_unit")
        return amount * k if k else None
    if unit in MASS_FACTORS and target in MASS_FACTORS:
        return amount * MASS_FACTORS[unit] / MASS_FACTORS[target]
    return None


def _fmt(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".") if x < 10 else f"{x:.1f}".rstrip("0").rstrip(".")


class Analyzer:
    def analyze(self, product: Product) -> Report:
        report = Report(product=product)
        if not product.ingredients:
            report.findings.append(Finding(Level.CRITICAL, "no_ingredients",
                                           "Состав не распознан или пуст"))
            return report
        if product.units_per_day <= 0:
            report.findings.append(Finding(Level.CRITICAL, "bad_dose",
                                           "Суточная доза должна быть больше нуля"))
            return report

        for ing in product.ingredients:
            self._check_prohibited(ing, report)
            self._check_additives(ing, report)
            self._check_nutrient(ing, product, report)

        self._check_full_text(product, report)
        if product.label_text:
            self._check_label(product.label_text, report)
        else:
            report.findings.append(Finding(Level.INFO, "label_missing",
                                           "Текст этикетки не передан — проверка маркировки не выполнялась"))
        return report

    # --- состав -------------------------------------------------------------
    def _check_prohibited(self, ing: Ingredient, report: Report) -> None:
        text = normalize(ing.raw or ing.name)
        for item, pats in _prohibited_patterns():
            if any(p.search(text) for p in pats):
                sev = Level(item.get("severity", "critical"))
                verb = "запрещён для использования в БАД" if sev == Level.CRITICAL else "ограничен — требует проверки"
                report.findings.append(Finding(
                    sev, f"prohibited:{item['id']}",
                    f"Компонент «{ing.name}» ({item['category']}) {verb}. {item['note']}",
                    ingredient=ing.name, basis=item["basis"]))

    def _check_additives(self, ing: Ingredient, report: Report) -> None:
        text = normalize(ing.raw or ing.name)
        for item in load_db("additives")["items"]:
            code = item["code"].lower()
            num = code[1:]
            if re.search(rf"(?<![a-zа-я0-9])[eе][\s-]?{num}(?![0-9])", text) or re.search(r"(?<![а-яa-z])" + re.escape(normalize(item["name"])) + r"(?![а-яa-z])", text):
                sev = Level(item.get("severity", "critical"))
                report.findings.append(Finding(
                    sev, f"additive:{item['code']}",
                    f"Пищевая добавка {item['code']} ({item['name']}): {item['note']}",
                    ingredient=ing.name, basis="ТР ТС 029/2012, прил. 2"))

    def _check_nutrient(self, ing: Ingredient, product: Product, report: Report) -> None:
        nutrient = find_nutrient(ing.name)
        if nutrient is None:
            if ing.amount is not None and not any(f.ingredient == ing.name for f in report.findings):
                report.findings.append(Finding(
                    Level.INFO, "unknown_ingredient",
                    f"«{ing.name}» нет в справочнике нормируемых веществ — проверьте допустимость по СГР/экспертизе",
                    ingredient=ing.name))
            return
        if ing.amount is None or ing.unit is None:
            report.findings.append(Finding(
                Level.WARNING, "no_amount",
                f"Для «{ing.name}» ({nutrient['name']}) не указано количество — невозможно сверить с ВДУ",
                ingredient=ing.name))
            return
        value = convert(ing.amount, ing.unit, nutrient)
        if value is None:
            report.findings.append(Finding(
                Level.WARNING, "unit_mismatch",
                f"Не удалось перевести {_fmt(ing.amount)} {ing.unit} в {nutrient['unit']} для «{ing.name}»",
                ingredient=ing.name))
            return

        daily = value * product.units_per_day if product.amounts_per_unit else value
        res = NutrientResult(nutrient["id"], nutrient["name"], ing.name, daily,
                             nutrient["unit"], nutrient["adequate"], nutrient["upper"])
        report.nutrients.append(res)
        basis = load_db("nutrients")["_meta"]["short_source"]
        amount_str = f"{_fmt(daily)} {nutrient['unit']}/сут"

        name_n = normalize(ing.name)
        if (nutrient["id"] in MINERALS and any(m in name_n for m in COMPOUND_MARKERS)
                and not any(m in normalize(ing.raw) for m in ELEMENTAL_MARKERS)):
            report.findings.append(Finding(
                Level.WARNING, "compound_mass",
                f"«{ing.name}»: похоже, указана масса соединения, а не элементарного вещества "
                f"({nutrient['name']}). Нормы установлены для элемента — уточните пересчёт",
                ingredient=ing.name))

        if daily > nutrient["upper"]:
            report.findings.append(Finding(
                Level.CRITICAL, f"over_upper:{nutrient['id']}",
                f"{nutrient['name']}: {amount_str} превышает верхний допустимый уровень "
                f"{_fmt(nutrient['upper'])} {nutrient['unit']} ({res.pct_upper:.0f}% ВДУ)",
                ingredient=ing.name, basis=basis))
        elif res.pct_adequate < 15:
            report.findings.append(Finding(
                Level.INFO, f"low:{nutrient['id']}",
                f"{nutrient['name']}: {amount_str} — {res.pct_adequate:.0f}% адекватного уровня (<15%); "
                f"заявлять продукт «источником» этого вещества нельзя",
                ingredient=ing.name, basis="ТР ТС 022/2011, ст. 4.4, п. 13; Приложение 2"))
        else:
            report.findings.append(Finding(
                Level.OK, f"within:{nutrient['id']}",
                f"{nutrient['name']}: {amount_str} — {res.pct_adequate:.0f}% АУП, "
                f"не превышает ВДУ {_fmt(nutrient['upper'])} {nutrient['unit']}",
                ingredient=ing.name, basis=basis))

    def _check_full_text(self, product: Product, report: Report) -> None:
        """Ищет запрещённые компоненты в тексте этикетки, если их нет в списке состава."""
        if not product.label_text:
            return
        already = {f.code for f in report.findings}
        text = normalize(product.label_text)
        for item, pats in _prohibited_patterns():
            code = f"prohibited:{item['id']}"
            if code not in already and any(p.search(text) for p in pats):
                report.findings.append(Finding(
                    Level(item.get("severity", "critical")), code,
                    f"В тексте этикетки упомянут «{'/'.join(item['names'][:2])}» — {item['note']}",
                    basis=item["basis"]))

    # --- маркировка ---------------------------------------------------------
    def _check_label(self, label: str, report: Report) -> None:
        text = normalize(label)
        for code, patterns, message, basis in LABEL_REQUIREMENTS:
            if not any(re.search(p, text) for p in patterns):
                report.findings.append(Finding(Level.WARNING, code, message, basis=basis))
        for p in MEDICAL_CLAIMS:
            m = re.search(p, text)
            if m:
                report.findings.append(Finding(
                    Level.CRITICAL, "medical_claim",
                    f"Недопустимое заявление лечебных свойств: «{m.group(0)}». "
                    f"БАД не может позиционироваться как лекарственное средство",
                    basis="ТР ТС 022/2011, ст. 4.4; ФЗ № 38-ФЗ «О рекламе», ст. 25"))


def analyze(product: Product) -> Report:
    return Analyzer().analyze(product)
