"""Модели данных анализатора состава БАД."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class Level(str, Enum):
    CRITICAL = "critical"   # нарушение — продукт не соответствует
    WARNING = "warning"     # требует проверки экспертом
    INFO = "info"           # справочная информация
    OK = "ok"               # проверка пройдена

    @property
    def label(self) -> str:
        return {
            "critical": "НАРУШЕНИЕ",
            "warning": "ВНИМАНИЕ",
            "info": "ИНФО",
            "ok": "OK",
        }[self.value]


@dataclass
class Ingredient:
    name: str
    amount: Optional[float] = None
    unit: Optional[str] = None
    raw: str = ""


@dataclass
class Product:
    name: str = "Без названия"
    ingredients: list[Ingredient] = field(default_factory=list)
    # Сколько единиц (таблеток/капсул/порций) принимается в сутки.
    units_per_day: float = 1.0
    # True — количества указаны на 1 единицу; False — уже на суточную дозу.
    amounts_per_unit: bool = True
    unit_name: str = "порция"
    label_text: str = ""


@dataclass
class Finding:
    level: Level
    code: str
    message: str
    ingredient: Optional[str] = None
    basis: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["level"] = self.level.value
        return d


@dataclass
class NutrientResult:
    nutrient_id: str
    name: str
    ingredient: str
    daily_amount: float
    unit: str
    adequate: float
    upper: float

    @property
    def pct_adequate(self) -> float:
        return 100.0 * self.daily_amount / self.adequate if self.adequate else 0.0

    @property
    def pct_upper(self) -> float:
        return 100.0 * self.daily_amount / self.upper if self.upper else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pct_adequate"] = round(self.pct_adequate, 1)
        d["pct_upper"] = round(self.pct_upper, 1)
        return d


@dataclass
class Report:
    product: Product
    findings: list[Finding] = field(default_factory=list)
    nutrients: list[NutrientResult] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        levels = {f.level for f in self.findings}
        if Level.CRITICAL in levels:
            return "НЕ СООТВЕТСТВУЕТ"
        if Level.WARNING in levels:
            return "ТРЕБУЕТ ПРОВЕРКИ"
        return "СООТВЕТСТВУЕТ (по проверенным критериям)"

    def count(self, level: Level) -> int:
        return sum(1 for f in self.findings if f.level == level)
