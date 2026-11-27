"""Проверка номера свидетельства о государственной регистрации (СГР).

Формат номера СГР ЕАЭС: RU.77.99.11.003.R.001234.05.21
  RU  — страна, выдавшая свидетельство (RU, BY, KZ, AM, KG)
  77  — код органа/субъекта (77 — Москва)
  99  — код вида деятельности
  11  — код вида продукции (БАД — как правило 11)
  003 — код группы продукции
  R/E — литера (E — свидетельства старого образца, R — с 2016 г.)
  001234 — порядковый номер
  05.21  — месяц и год выдачи

Онлайн-проверку по Единому реестру ЕАЭС программа не выполняет автоматически:
у реестра нет стабильного публичного API. Отчёт даёт номер и ссылку для ручной сверки.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

REGISTRY_URL = "https://portal.eaeunion.org"
COUNTRIES = {"RU": "Россия", "BY": "Беларусь", "KZ": "Казахстан", "AM": "Армения", "KG": "Кыргызстан"}

# Латиница и похожая кириллица в литере (на этикетках встречается «Е» кириллицей)
_LETTER = "[A-ZА-ЯЁ]"
SGR_RE = re.compile(
    rf"\b(?P<country>RU|BY|KZ|AM|KG)\s*\.\s*(?P<region>\d{{2}})\s*\.\s*(?P<activity>\d{{2}})\s*\.\s*"
    rf"(?P<kind>\d{{2}})\s*\.\s*(?P<group>\d{{3}})\s*\.\s*(?P<letter>{_LETTER})\s*\.\s*"
    rf"(?P<num>\d{{6}})\s*\.\s*(?P<month>\d{{2}})\s*\.\s*(?P<year>\d{{2}})\b",
    re.IGNORECASE)
# Что-то похожее на номер СГР, но не прошедшее полную проверку формата
LOOSE_RE = re.compile(r"\b(RU|BY|KZ|AM|KG)\s*\.\s*\d{2}\s*\.\s*\d{2}[\d.\sA-ZА-Яa-zа-я]{6,30}", re.IGNORECASE)


@dataclass
class SGRNumber:
    raw: str
    country: str
    region: str
    kind: str
    letter: str
    number: str
    month: int
    year: int

    @property
    def normalized(self) -> str:
        return f"{self.country}.{self.region}.??.{self.kind}.???.{self.letter}.{self.number}.{self.month:02d}.{self.year % 100:02d}"

    def problems(self, today: date | None = None) -> list[str]:
        today = today or date.today()
        out = []
        if not 1 <= self.month <= 12:
            out.append(f"месяц выдачи «{self.month:02d}» некорректен")
        if self.year > today.year or (self.year == today.year and self.month > today.month):
            out.append("дата выдачи в будущем")
        if self.year < 2010:
            out.append("год выдачи раньше 2010 — свидетельства ЕАЭС выдаются с 2010 г.")
        if self.kind != "11":
            out.append(f"код вида продукции «{self.kind}» — для БАД обычно «11»; проверьте, что СГР выдано именно на БАД")
        return out


def find_sgr(text: str) -> list[SGRNumber]:
    found = []
    for m in SGR_RE.finditer(text):
        found.append(SGRNumber(
            raw=re.sub(r"\s+", "", m.group(0)),
            country=m.group("country").upper(),
            region=m.group("region"),
            kind=m.group("kind"),
            letter=m.group("letter").upper(),
            number=m.group("num"),
            month=int(m.group("month")),
            year=2000 + int(m.group("year")),
        ))
    return found


def has_malformed_sgr(text: str) -> str | None:
    """Вернуть фрагмент, похожий на номер СГР, если он не распознан как корректный."""
    if SGR_RE.search(text):
        return None
    m = LOOSE_RE.search(text)
    return m.group(0).strip() if m else None
