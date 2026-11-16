"""Режим «Отбор»: таблица «объекты × признаки» с доказательством в каждой ячейке.

Здесь — всё, что не зависит от заседания: схемы ответов моделей, задания (prompts), склейка дублей кандидатов,
оценка полноты списка, разбор чисел и единиц (万/亿, млн, т/год), поиск цитаты в скачанной странице, уровень
источника, сверка значений двух моделей, ранг по весам, устойчивость ранга, «что выяснить» и текст отчёта.
Порядок этапов — council/modes/selection.py.
"""
from __future__ import annotations

import functools
import hashlib
import math
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from .links import plain

from .schemas import to_list, to_text
from .session import CouncilError

CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


# ---------------------------------------------------------------------------------------------------- схемы

Kind = Literal["number", "text", "bool", "list", "date"]
Better = Literal["high", "low", "yes", "no", "newer", "older", ""]
STATUSES = ("document", "official", "secondary", "unverified", "none")
STATUS_RU = {"document": "документ", "official": "сайт компании", "secondary": "вторичный источник",
             "unverified": "не подтверждено", "none": "не найдено"}
STATUS_MARK = {"document": "Д", "official": "С", "secondary": "В", "unverified": "?", "none": "—"}
TRUST = {"document": 1.0, "official": 0.8, "secondary": 0.6, "unverified": 0.4, "none": 0.0}


def _txt(v: Any) -> str:
    return to_text(v).strip()


class Attribute(BaseModel):
    """Признак таблицы: что сравниваем у каждого объекта."""

    id: str = ""
    name: str
    kind: Kind = "text"
    unit: str = ""                    # единица, к которой приводим (например «млн капсул/год»)
    evidence: str = ""                # что считается доказательством
    keywords: list[str] = Field(default_factory=list)  # слова для поиска на страницах, в т. ч. на языке источников
    weight: float = Field(default=1.0, ge=0, le=10)    # вес в ранге (0 — только для сведения)
    better: Better = ""               # что лучше: больше, меньше, «да», «нет», новее, старее
    wanted: list[str] = Field(default_factory=list)    # для списка: какие пункты нужны (формы, сертификаты…)
    screen: bool = False              # признак отсева (короткая проверка всех кандидатов)
    knockout: str = ""                # при каком значении кандидат отсеивается

    @field_validator("name", "unit", "evidence", "knockout", "id", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)

    @field_validator("keywords", "wanted", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return [x for x in to_list(v) if x.strip()]

    @field_validator("kind", mode="before")
    @classmethod
    def _k(cls, v: Any) -> str:
        v = str(v or "text").lower()
        return {"numeric": "number", "int": "number", "float": "number", "boolean": "bool", "yes/no": "bool",
                "array": "list", "set": "list", "string": "text"}.get(v, v if v in Kind.__args__ else "text")

    @field_validator("better", mode="before")
    @classmethod
    def _b(cls, v: Any) -> str:
        v = str(v or "").lower()
        return {"higher": "high", "more": "high", "max": "high", "lower": "low", "less": "low", "min": "low",
                "true": "yes", "false": "no", "later": "newer", "earlier": "older"}.get(v, v if v in Better.__args__ else "")

    @field_validator("weight", mode="before")
    @classmethod
    def _w(cls, v: Any) -> float:
        try:
            return max(0.0, min(10.0, float(v)))
        except (TypeError, ValueError):
            return 1.0

    @property
    def scorable(self) -> bool:
        return self.kind in ("number", "bool", "list", "date") and self.weight > 0


class SelectionFrame(BaseModel):
    """Рамка отбора: кто кандидат, по каким признакам сравниваем, где искать."""

    refined_question: str = ""
    object_kind: str = "объект"
    inclusion: list[str] = Field(default_factory=list)   # кто считается кандидатом
    exclusion: list[str] = Field(default_factory=list)   # кто точно не подходит
    attributes: list[Attribute] = Field(default_factory=list)
    angles: list[str] = Field(default_factory=list)      # направления поиска кандидатов
    # поиски, которых задание требует явно (по каждой категории, по каждому типу производителя, его примеры запросов):
    # идут все и сразу, без «волн» и без предела числа направлений
    required_angles: list[str] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)     # объекты, которые задание даёт как кандидатов на проверку
    # названные в задании, но НЕ кандидаты: конкуренты для сравнения, примеры, исключённые — их не проверяем и не ищем
    benchmarks: list[str] = Field(default_factory=list)
    start: str = ""                                      # что просит задание: find | check | extend (решает председатель)
    shortlist: int = 0                                   # сколько проверять подробно (0 — всех прошедших отсев)
    top_n: int = 0                                       # сколько в «первой группе» (например 12 на переговоры)
    notes: str = ""

    @field_validator("refined_question", "object_kind", "notes", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)

    @field_validator("inclusion", "exclusion", "angles", "required_angles", "objects", "benchmarks", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return [x.strip() for x in to_list(v) if x.strip()]

    @field_validator("start", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        t = _txt(v).lower()
        return t if t in ("find", "check", "extend") else ""

    @field_validator("shortlist", "top_n", mode="before")
    @classmethod
    def _n(cls, v: Any) -> int:
        try:
            return max(0, int(v or 0))
        except (TypeError, ValueError):
            return 0

    @model_validator(mode="after")
    def _screen_rules(self) -> "SelectionFrame":
        """Отсев — только по жёстким условиям: признак без правила исключения или с правилом «нет данных» в отсев не
        идёт (16.11 в отсев попало «Собственное сырьё» — по заданию преимущество, а не условие)."""
        for a in self.attributes:
            k = (a.knockout or "").strip().lower()
            if a.screen and (not k or k in ("-", "—", "none", "n/a", "нет данных", "не найдено")):
                a.screen = False
        return self

    @model_validator(mode="after")
    def _ids(self) -> SelectionFrame:
        seen: set[str] = set()
        out = []
        for a in self.attributes:
            if not a.name or a.name.lower() in seen:
                continue
            seen.add(a.name.lower())
            out.append(a)
        for i, a in enumerate(out, 1):
            a.id = f"A{i}"
        self.attributes = out[:20]
        return self


class Candidate(BaseModel):
    name: str
    local_name: str = ""
    aliases: list[str] = Field(default_factory=list)
    website: str = ""
    url: str = ""          # страница, где видно, что кандидат подходит
    quote: str = ""        # дословная короткая цитата с этой страницы
    location: str = ""
    why: str = ""

    @field_validator("name", "local_name", "website", "url", "quote", "location", "why", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)

    @field_validator("aliases", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return [x for x in to_list(v) if x.strip()]


class Candidates(BaseModel):
    items: list[Candidate] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, list):
            return {"items": data}
        if isinstance(data, dict) and "items" not in data:
            for k in ("candidates", "companies", "objects", "results"):
                if isinstance(data.get(k), list):
                    return {"items": data[k]}
        return data

    @field_validator("items", mode="before")
    @classmethod
    def _items(cls, v: Any) -> list:
        return [x if isinstance(x, dict) else {"name": str(x)} for x in (v or []) if x]


class ListSource(BaseModel):
    """Готовый список компаний: участники выставки, реестр лицензий, члены ассоциации, рейтинг, каталог площадки."""

    url: str
    title: str = ""
    kind: str = ""
    size: str = ""

    @field_validator("url", "title", "kind", "size", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)


class ListSources(BaseModel):
    lists: list[ListSource] = Field(default_factory=list)


class NewAngles(BaseModel):
    angles: list[str] = Field(default_factory=list)

    @field_validator("angles", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return [x for x in to_list(v) if x.strip()]


class ScreenCheck(BaseModel):
    attr: str
    verdict: Literal["pass", "fail", "unclear"] = "unclear"
    value: str = ""
    url: str = ""
    quote: str = ""

    @field_validator("attr", "value", "url", "quote", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)

    @field_validator("verdict", mode="before")
    @classmethod
    def _v(cls, v: Any) -> str:
        v = str(v or "").lower()
        return {"yes": "pass", "ok": "pass", "passed": "pass", "no": "fail", "failed": "fail", "exclude": "fail"}.get(
            v, v if v in ("pass", "fail", "unclear") else "unclear")


class ScreenRow(BaseModel):
    object: str
    website: str = ""
    checks: list[ScreenCheck] = Field(default_factory=list)
    note: str = ""

    @field_validator("object", "website", "note", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)


class ScreenBatch(BaseModel):
    rows: list[ScreenRow] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, list):
            return {"rows": data}
        if isinstance(data, dict) and "rows" not in data:
            for k in ("objects", "items", "results", "candidates"):
                if isinstance(data.get(k), list):
                    return {"rows": data[k]}
        return data


UNIT_NOT = {"bool", "boolean", "text", "string", "str", "list", "date", "number", "yes/no", "да/нет", "n/a", "na",
            "none", "-", "—", "flag", "boolean flag"}


class Cell(BaseModel):
    attr: str                 # A1, A2… (или название признака — программа сопоставит)
    value: str = ""
    unit: str = ""
    as_of: str = ""           # к какой дате относится
    url: str = ""
    quote: str = ""           # дословная цитата со страницы
    note: str = ""

    @field_validator("attr", "value", "unit", "as_of", "url", "quote", "note", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)

    @field_validator("value", "unit", "note")
    @classmethod
    def _clean(cls, v: str) -> str:
        """Без разметки: модель иногда присылает значение заголовками и жирным («### annual **bottles:**»), а список —
        строками «- пункт»: в ячейке это «- a - b - c», поэтому пункты сводим через запятую."""
        v = re.sub(r"(?m)^\s*#+\s*", "", v)
        v = re.sub(r"\*\*|__|`", "", v)
        lines = [ln.strip() for ln in v.splitlines() if ln.strip()]
        if sum(1 for ln in lines if re.match(r"[-*•·]\s+", ln)) >= 2:
            v = ", ".join(re.sub(r"^[-*•·]\s+", "", ln).rstrip(",;") for ln in lines)
        return re.sub(r"\s+", " ", v).strip()

    @field_validator("unit")
    @classmethod
    def _unit(cls, v: str) -> str:
        """Тип поля вместо единицы («bool», «text», «да/нет») — не единица: в таблице было «да bool»."""
        return "" if v.lower().strip(" .") in UNIT_NOT else v


class ScreenConfirmItem(BaseModel):
    n: int = 0
    exclude: bool = False
    why: str = ""

    @field_validator("why", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)


class ScreenConfirm(BaseModel):
    items: list[ScreenConfirmItem] = Field(default_factory=list)


class Source(BaseModel):
    url: str
    title: str = ""
    kind: str = ""
    why: str = ""

    @field_validator("url", "title", "kind", "why", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)


class ObjectSources(BaseModel):
    """Поиск по одному объекту: где о нём написано и что удалось найти сразу."""

    website: str = ""
    sources: list[Source] = Field(default_factory=list)
    cells: list[Cell] = Field(default_factory=list)
    note: str = ""

    @field_validator("website", "note", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)

    @field_validator("sources", mode="before")
    @classmethod
    def _s(cls, v: Any) -> list:
        return [x if isinstance(x, dict) else {"url": str(x)} for x in (v or []) if x]


class ObjectCells(BaseModel):
    """Заполнение ячеек одного объекта по скачанным страницам."""

    cells: list[Cell] = Field(default_factory=list)
    note: str = ""

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, list):
            return {"cells": data}
        return data

    @field_validator("note", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)


class Ruling(BaseModel):
    key: str                  # «A3» — признак спорной ячейки этого объекта
    choice: str = ""          # буква варианта (a, b, c…) или «none»
    reason: str = ""

    @field_validator("key", "choice", "reason", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)


class Rulings(BaseModel):
    items: list[Ruling] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, list):
            return {"items": data}
        if isinstance(data, dict) and "items" not in data:
            for k in ("rulings", "cells", "decisions"):
                if isinstance(data.get(k), list):
                    return {"items": data[k]}
        return data


class Conclusion(BaseModel):
    summary: str
    next_steps: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("summary"):
            for k in ("conclusion", "final_answer", "text", "answer"):
                if data.get(k):
                    return {**data, "summary": data[k]}
        return data

    @field_validator("summary", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return _txt(v)

    @field_validator("next_steps", "risks", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return [x for x in to_list(v) if x.strip()]


class ReviewNeeded(CouncilError):
    """Остановка «проверьте список»: заседание ждёт решения человека (сайт показывает список для правки)."""

    def __init__(self, gate: str, payload: dict[str, Any]) -> None:
        super().__init__(GATE_TEXT.get(gate, "ждёт вашей проверки"))
        self.gate = gate
        self.payload = payload


BY = " · "  # «направление · модель» — кто и как нашёл кандидата

GATE_TEXT = {"frame": "проверьте признаки и веса — заседание ждёт", "candidates": "проверьте найденных кандидатов — "
             "заседание ждёт", "shortlist": "утвердите короткий список — заседание ждёт"}

SEARCH_SYSTEM = """You search the web for a selection study (a table comparing many objects attribute by attribute).
Report only what the search actually returned: names, pages and VERBATIM quotes copied exactly from the page in its
own language (Chinese stays Chinese). Company names stay in their original spelling (Chinese characters and the
official English name) — never translated into another language. Never invent a company, a page or a quote; if unsure — leave it out. Prefer
primary sources: stock-exchange filings and annual reports, government and certification registries, the company's
own site. Answer with the JSON object requested; free text — in {language}."""


# ---------------------------------------------------------------------------------------------------- имена и дубли

LEGAL = re.compile(
    r"\b(co|corp|corporation|company|ltd|limited|llc|inc|plc|gmbh|group|holdings?|international|intl|"
    r"technology|technologies|tech|biotech|biotechnology|bio|biology|biological|pharma|pharmaceuticals?|health|"
    r"healthcare|nutrition|industry|industrial|science|sciences|manufacturing|products?|the|and)\b")
LEGAL_ZH = re.compile(r"(股份有限公司|有限责任公司|有限公司|集团|控股|科技|生物|医药|制药|健康|保健|食品|技术|实业|"
                      r"发展|工程|营养|药业|股份|株式会社|有限会社|合同会社|주식회사|유한회사)")
# формы собственности других стран (после удаления точек: «S.p.A.» → «s p a»)
LEGAL_INTL = re.compile(  # длинные формы раньше коротких: «S.A.S.» не должно стать «S.A.» + «s»
    r"\b(sp\s*z\s*o\s*o|s\s*a\s*r\s*l|s\s*a\s*s|s\s*p\s*a|s\s*r\s*l|s\s*r\s*o|d\s*o\s*o|gmbh\s*co|"
    r"limitada|ltda|sarl|srl|spa|sas|pjsc|jsc|oyj|asa|aps|kft|zrt|pte|pvt|bhd|sdn|tbk|ohg|cia|cie|"
    r"s\s*a|s\s*l|a\s*ş|a\s*s|b\s*v|n\s*v|ag|kg|ug|oy|ab|kk)\b")
LEGAL_RU = re.compile(r"\b(ооо|оао|зао|пао|ао|нао|ип|тов|пат|ат|група|группа|компания|компанія|холдинг|завод|фабрика)\b")
MARKETPLACES = {"alibaba.com", "1688.com", "made-in-china.com", "globalsources.com", "amazon.com", "tradekey.com",
                "indiamart.com", "linkedin.com", "facebook.com", "youtube.com", "wikipedia.org", "baidu.com",
                "zhihu.com", "qcc.com", "tianyancha.com", "aiqicha.baidu.com", "google.com", "bing.com",
                "crunchbase.com", "zoominfo.com", "dnb.com", "europages.com", "kompass.com", "x.com", "twitter.com"}


def norm_name(s: str) -> str:
    """Имя для сравнения: без регистра, знаков, форм собственности и общих слов («биотех», «有限公司»)."""
    t = (s or "").lower().replace("ё", "е")
    t = re.sub(r"[（(].*?[)）]", " ", t)
    t = LEGAL_ZH.sub(" ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = LEGAL.sub(" ", t)
    t = LEGAL_RU.sub(" ", t)
    t = LEGAL_INTL.sub(" ", t)
    return re.sub(r"\s+", "", t)


def domain(url: str) -> str:
    """Сайт ссылки без www и поддоменов (yuwangpharma.com, gd-caxin.com, cninfo.com.cn)."""
    u = (url or "").strip()
    if u and "://" not in u:
        u = "http://" + u
    host = (urlsplit(u).hostname or "").lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("com", "net", "org", "co", "gov", "edu", "ac", "or"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


# общие хостинги: у каждой компании свой поддомен — сравниваем целиком (a.wixsite.com ≠ b.wixsite.com)
SHARED_HOSTS = {"example.com", "wixsite.com", "github.io", "blogspot.com", "wordpress.com", "weebly.com",
                "jimdo.com", "tilda.ws", "site123.me", "webnode.com", "en.alibaba.com", "cn.made-in-china.com"}


def site_of(url: str) -> str:
    """Сайт компании для склейки дублей: домен без www и поддоменов; площадки — пусто; общие хостинги — весь хост."""
    d = domain(url)
    if not d or d in MARKETPLACES:
        return ""
    if d in SHARED_HOSTS:
        u = url if "://" in url else "http://" + url
        return (urlsplit(u).hostname or "").lower().removeprefix("www.")
    return d


def _name_key_ok(n: str) -> bool:
    """Имя годится для склейки: латиница и кириллица — от 3 знаков, иероглифы — от 2 (仙乐 после «健康科技股份有限公司»)."""
    return len(n) >= (2 if CJK.search(n) else 3)


def display_name(cs: list["Candidate"]) -> str:
    """Имя объекта на экране: самое частое латинское (по нему вы ищете) и китайское рядом — «BioGrowing Co., Ltd. /
    均瑶润盈生物科技（上海）有限公司». Раньше бралось самое частое короткое, и выходило только китайское — нужный завод
    в списке было не узнать."""
    def best(xs: list[str]) -> str:
        return max(xs, key=lambda x: (sum(1 for y in xs if y == x), -len(x))) if xs else ""
    lat = best([x.split(" / ")[0].strip() for c in cs for x in [c.name] if x.strip() and not CJK.search(x.split(" / ")[0])])
    loc = best([x.strip() for c in cs for x in [c.local_name, c.name] if x.strip() and CJK.search(x) and " / " not in x])
    if lat and loc and norm_name(loc) not in norm_name(lat):
        return f"{lat} / {loc}"
    return lat or loc or best([c.name for c in cs])


def merge_candidates(found: list[tuple[str, Candidate]], seeds: list[str] | None = None) -> list[dict[str, Any]]:
    """Склейка дублей: одно и то же имя (без форм собственности), имя-псевдоним или один сайт компании — один объект.
    found — (направление поиска, кандидат). seeds — объекты, которые дал человек (направление «ваш список»)."""
    items: list[dict[str, Any]] = []
    for s in seeds or []:
        if s.strip():
            items.append({"angle": "ваш список", "c": Candidate(name=s.strip())})
    for angle, c in found:
        if c.name.strip():
            items.append({"angle": angle, "c": c})
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_key: dict[str, int] = {}
    for i, it in enumerate(items):
        c = it["c"]
        keys = {f"n:{norm_name(x)}" for x in [c.name, c.local_name, *c.aliases] if _name_key_ok(norm_name(x))}
        site = site_of(c.website) or ""
        if site:
            keys.add(f"d:{site}")
        for k in keys:
            if k in by_key:
                union(i, by_key[k])
            else:
                by_key[k] = i
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(find(i), []).append(i)
    out = []
    for n, (root, idx) in enumerate(sorted(groups.items()), 1):
        cs = [items[i]["c"] for i in idx]
        angles = sorted({items[i]["angle"] for i in idx})
        seed = next((c.name for i, c in zip(idx, cs) if items[i]["angle"] == "ваш список"), "")
        names = list(dict.fromkeys(x for c in cs for x in [c.name, c.local_name, *c.aliases] if x))
        main = seed or display_name(cs)
        ev = next((c for c in cs if c.url and c.quote), None)
        out.append({
            "id": f"O{n}", "name": main, "names": names,
            "website": next((c.website for c in cs if site_of(c.website)), ""),
            "location": next((c.location for c in cs if c.location), ""),
            "angles": angles, "seed": bool(seed),
            "url": ev.url if ev else next((c.url for c in cs if c.url), ""),
            "urls": list(dict.fromkeys(c.url for c in cs if c.url.startswith(("http://", "https://"))))[:6],
            "quote": ev.quote if ev else "", "why": next((c.why for c in cs if c.why), ""),
            "merged": len(set(norm_name(c.name) for c in cs)) > 1,
        })
    return out


def completeness(entities: list[dict[str, Any]], n_angles: int) -> dict[str, Any]:
    """Насколько список полон — по «повторным поимкам» (оценка Chao1): если разные направления поиска находят одних и
    тех же, неизвестных осталось мало; если почти каждый найден одним направлением — список далёк от полного."""
    counts = [len([a for a in e["angles"] if a != "ваш список"]) for e in entities]
    counts = [c for c in counts if c > 0]
    s_obs = len(counts)
    if not s_obs or n_angles < 2:
        return {"found": s_obs, "estimate": None, "share": None}
    f1 = sum(1 for c in counts if c == 1)
    f2 = sum(1 for c in counts if c == 2)
    est = s_obs + (f1 * f1 / (2 * f2) if f2 else f1 * (f1 - 1) / 2)
    est = max(est, s_obs)
    return {"found": s_obs, "estimate": round(est), "share": round(s_obs / est, 2) if est else None,
            "singletons": f1, "doubles": f2}


def screen_decide(checks: list[ScreenCheck], attrs: list[Attribute]) -> tuple[str, str]:
    """Отсев: кандидат исключается только при явном «не подходит» хотя бы по одному признаку отсева — с дословной
    цитатой источника (отсутствие сведений — «нет данных», «не найдено» — не повод исключать); все «подходит» —
    проходит; иначе — сомнительный (не выбрасывается). Найдена ли цитата на странице, проверяет программа после."""
    want = {a.id for a in attrs if a.screen}
    got = {c.attr: c for c in checks if c.attr in want}
    fails = [c for c in got.values() if c.verdict == "fail" and len(c.quote.strip()) >= 8
             and not UNKNOWN.search(c.value or "") and not UNKNOWN.search(c.quote)]
    if fails:
        names = {a.id: a.name for a in attrs}
        return "fail", "; ".join(f"{names.get(c.attr, c.attr)}: {c.value or c.quote}"[:200] for c in fails)
    if want and all(got.get(a) and got[a].verdict == "pass" for a in want):
        return "pass", ""
    unclear = [a.name for a in attrs if a.id in want and not (got.get(a.id) and got[a.id].verdict == "pass")]
    return "unclear", "не выяснено: " + ", ".join(unclear)


# ---------------------------------------------------------------------------------------------------- числа и единицы

SCALE = [
    (r"亿", 1e8), (r"万", 1e4), (r"千", 1e3),
    (r"млрд|миллиард\w*|billion|bn\b", 1e9), (r"млн|миллион\w*|million|mln\b|mio\b", 1e6),
    (r"тыс\.?|тысяч\w*|thousand", 1e3), (r"(?<=\d)\s*k\b", 1e3), (r"(?<=\d)\s*m\b", 1e6), (r"(?<=\d)\s*b\b", 1e9),
]
NUM = re.compile(r"[-+]?\d[\d\s  ,.'’]*")


@functools.lru_cache(maxsize=65536)
def parse_number(text: str) -> float | None:
    """Первое число в тексте с учётом множителей: «25亿粒» → 2.5e9, «242 774,46万» → 2.4277446e9,
    «11 000 т/год» → 11000, «1,9 млрд» → 1.9e9. Диапазон «38–40» → середина. Не нашлось — None."""
    t = (text or "").replace(" ", " ").replace(" ", " ")
    m = NUM.search(t)
    if not m:
        return None
    raw = m.group(0).strip()
    rng = re.match(r"\s*[-–—~～]\s*(\d[\d\s,.]*)", t[m.end():])
    # «2,428 млрд» — запятая дробная (по-русски с множителем тысячи не пишут); «242,774.46万» — разряды
    ru_scale = re.match(r"\s*(млрд|млн|тыс|миллиард|миллион|тысяч)", t[m.end():].lower())
    val = _num(raw, decimal_comma=bool(ru_scale))
    if val is None:
        return None
    end = m.end()
    if rng:
        hi = _num(rng.group(1), decimal_comma=bool(ru_scale))
        if hi is not None and hi >= val:
            val = (val + hi) / 2
        end += rng.end()
    tail = t[end:end + 14].lower()
    for pat, mult in SCALE:
        if re.match(r"\s*(" + pat + ")", tail):
            return val * mult
    return val


def _num(raw: str, decimal_comma: bool = False) -> float | None:
    s = re.sub(r"[\s'’]", "", raw).strip(".,")
    if decimal_comma and s.count(",") == 1 and "." not in s:
        s = s.replace(",", ".")
    if not s:
        return None
    if "," in s and "." in s:  # 1,234.5 или 1.234,5 — разделитель дроби — последний
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", "") if len(parts) > 2 or (len(parts[-1]) == 3 and len(parts[0]) <= 3 and parts[0] != "0") \
            else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


UNIT_KIND = [
    ("money", r"юан|yuan|rmb|cny|¥|\$|usd|руб|rub|eur|€|元|долл|dollar"),
    ("mass", r"тонн|\bт\b|\btons?\b|\bt/|吨|\bкг\b|\bkg\b|килограмм"),
    ("volume", r"литр|\bл\b|\blit|升|\bm3\b|м3|куб"),
    ("area", r"м²|м2|\bm2\b|sq\.? ?m|平方米|кв\.? ?м|акр|\bmu\b|亩"),
    ("count", r"капсул|таблет|\bшт|pcs|pieces|units?\b|粒|片|袋|盒|瓶|упак|\bуп\b|bottle|флакон|\bcaps|tablet|softgel|"
              r"саше|sachet|стик|stick|пакет|доз|dose|件"),
]
UNIT_PERIOD = [("day", r"сут|в день|/день|per day|/day|daily|日产|每天|每日|/日"),
               ("month", r"в месяц|/мес|per month|/month|monthly|每月|月产"),
               ("half", r"полугод|half[- ]year|半年"),
               ("year", r"в год|/год|годов|per year|/year|annual|yearly|年产|/年|每年|p\.?a\.?")]


def unit_class(text: str) -> tuple[str | None, str | None]:
    """Вид величины (штуки, тонны, деньги, объём, площадь) и период (сутки, месяц, полугодие, год) по тексту
    значения и единицы. Не распознано — None."""
    t = (text or "").lower()
    kind = next((k for k, pat in UNIT_KIND if re.search(pat, t)), None)
    period = next((k for k, pat in UNIT_PERIOD if re.search(pat, t)), None)
    return kind, period


def comparable(attr: "Attribute", cell: dict[str, Any]) -> bool:
    """Число ячейки можно сравнивать с другими в колонке: вид величины и период — как у признака (если оба
    распознаны). «120 млн юаней/год» в колонке «млн капсул/год» или «1 亿粒/сутки» — несопоставимо."""
    if attr.kind != "number":
        return True
    want = unit_class(f"{attr.unit} {attr.name}")
    got = unit_class(f"{cell.get('value', '')} {cell.get('unit', '')}")
    if want[0] and got[0] and want[0] != got[0]:
        return False
    if want[1] and got[1] and want[1] != got[1]:
        return False
    return True


def number_of(cell: dict[str, Any]) -> float | None:
    return parse_number(f"{cell.get('value', '')} {cell.get('unit', '')}".strip())


YES = re.compile(r"^\s*(да|есть|имеется|yes|true|y\b|有|是|подтвержд|available|certified)", re.I)
NO = re.compile(r"^\s*(нет|отсутств|no\b|false|n\b|无|否|не\s)", re.I)
# «нет данных», «не найдено», «n/a» — это «неизвестно», а не «нет»
UNKNOWN = re.compile(r"нет\s+данных|нет\s+сведений|не\s+найден|не\s+указан|не\s+раскрыт|неизвест|не\s+выяснен|"
                     r"\bn/?a\b|unknown|not\s+(found|disclosed|available|stated)|未知|不详", re.I)


def parse_bool(text: str) -> bool | None:
    if UNKNOWN.search(text or ""):
        return None
    if YES.search(text or ""):
        return True
    if NO.search(text or ""):
        return False
    return None


@functools.lru_cache(maxsize=65536)
def parse_date(text: str) -> float | None:
    """Дата как число (год + доля): 15.03.2026, 2026-03-15, 2026年3月, «март 2026», просто год."""
    t = text or ""
    m = re.search(r"(\d{4})[-./年](\d{1,2})(?:[-./月](\d{1,2}))?", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
        return y + (mo - 1) / 12 + (d - 1) / 365
    m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return y + (mo - 1) / 12 + (d - 1) / 365
    m = re.search(r"\b(19|20)\d{2}\b", t)
    return float(m.group(0)) if m else None


def list_items(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"[,;/、，；\n]|\s+и\s+|\s+and\s+", text or "") if x.strip()]


def values_agree(a: dict[str, Any], b: dict[str, Any], kind: str) -> bool:
    """Совпадают ли два значения ячейки (после приведения): числа — в пределах 2%, да/нет, даты — до месяца,
    списки — общая часть не меньше половины, текст — одинаковый после упрощения или одно содержит другое."""
    va, vb = (a.get("value") or "").strip(), (b.get("value") or "").strip()
    if not va or not vb:
        return False
    if kind == "number":
        x, y = number_of(a), number_of(b)
        if x is None or y is None:
            return plain(va) == plain(vb)
        return abs(x - y) <= 0.02 * max(abs(x), abs(y), 1e-9)
    if kind == "bool":
        return parse_bool(va) is not None and parse_bool(va) == parse_bool(vb)
    if kind == "date":
        x, y = parse_date(va), parse_date(vb)
        return x is not None and y is not None and abs(x - y) < 1 / 12
    if kind == "list":
        sa, sb = {plain(i) for i in list_items(va)}, {plain(i) for i in list_items(vb)}
        return bool(sa and sb) and len(sa & sb) >= 0.5 * min(len(sa), len(sb))
    pa, pb = plain(va), plain(vb)
    return pa == pb or (min(len(pa), len(pb)) >= 4 and (pa in pb or pb in pa))


# ---------------------------------------------------------------------------------------------------- цитаты и источники

def find_quote(text: str, quote: str) -> int:
    """Где цитата в тексте страницы (уже упрощённом — links.plain). Дословно или почти (≥80% кусков по 10 знаков
    подряд, для китайского и опечаток в пробелах). Не нашлась — -1."""
    q = plain(quote)
    if len(q) < 6 or not text:
        return -1
    i = text.find(q)
    if i >= 0:
        return i
    q2 = re.sub(r"\s+", "", q)
    t2 = re.sub(r"\s+", "", text)
    if len(q2) >= 6 and q2 in t2:
        return max(0, text.find(q2[:6]))
    step = 10
    chunks = [q2[k:k + step] for k in range(0, max(1, len(q2) - step + 1), step)] or [q2]
    hits = [t2.find(c) for c in chunks]
    ok = [h for h in hits if h >= 0]
    if len(ok) >= 0.8 * len(chunks) and len(chunks) >= 2 and max(ok) - min(ok) < len(q2) * 3:
        return max(0, text.find(chunks[hits.index(ok[0])][:6]))
    return -1


def numbers_in(text: str) -> list[float]:
    """Все числа текста с множителями (万/亿, млн, billion…)."""
    t = text or ""
    out = []
    # с начала каждой группы цифр: «в 2024, 500 млн» — и 2024, и 500 млн (одно регулярное выражение склеило бы их)
    for m in re.finditer(r"(?<![\d.,])\d", t):
        v = parse_number(t[m.start():m.start() + 60])
        if v is not None:
            out.append(v)
    return out


def quote_supports(attr: Attribute, value: str, unit: str, quote: str) -> bool:
    """Цитата действительно опора значения: не обрывок из пары слов («softgel» есть на любой странице завода), а для
    чисел и дат — то же число (с учётом 万/亿, млн) или тот же год есть в самой цитате."""
    q = re.sub(r"\s+", "", plain(quote))
    if len(q) < (4 if CJK.search(q) else 12):
        return False
    if attr.kind == "number":
        v = parse_number(f"{value} {unit}".strip())
        if v is None:
            return True
        return any(abs(n - v) <= 0.02 * max(abs(n), abs(v), 1e-9) for n in numbers_in(quote))
    if attr.kind == "date":
        v = parse_date(value)
        if v is None:
            return True
        years = {int(y) for y in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", quote)}
        return int(v) in years
    return True


def context_at(text: str, pos: int, width: int = 260) -> str:
    if pos < 0:
        return ""
    a = max(0, pos - width)
    return ("…" if a else "") + text[a:pos + width].strip() + ("…" if pos + width < len(text) else "")


DOC_SITES = ("cninfo.com.cn", "szse.cn", "sse.com.cn", "hkexnews.hk", "hkex.com.hk", "bse.cn", "neeq.com.cn",
             "sec.gov", "nsf.org", "samr.gov.cn", "nmpa.gov.cn", "gov.cn", "fda.gov", "ema.europa.eu", "brcgs.com",
             "fssc.com", "fssc22000.com", "iso.org", "sgs.com", "tuv.com", "intertek.com", "bureauveritas.com",
             "eaeunion.org", "fsa.gov.ru", "gov.ru", "europa.eu", "who.int", "usp.org")


def source_kind(url: str, website: str = "") -> str:
    """Уровень источника по адресу: document — биржевые отчёты, госреестры, сертификационные базы; official — сайт
    самой компании; secondary — остальное (новости, каталоги, площадки)."""
    d = domain(url)
    host = (urlsplit(url if "://" in url else "http://" + url).hostname or "").lower()
    if any(host == s or host.endswith("." + s) for s in DOC_SITES) or d.endswith(".gov") or ".gov." in host:
        return "document"
    if website and d and d == domain(website):
        return "official"
    return "secondary"


def excerpts(text: str, words: list[str], budget: int = 9000, window: int = 700) -> str:
    """Куски страницы, где больше всего слов признаков и названия объекта (целиком страницы моделям не отдаём —
    дорого): окна по window знаков, лучшие по числу совпадений, в порядке страницы, всего не больше budget."""
    if not text:
        return ""
    if len(text) <= budget:
        return text
    ws = [plain(w) for w in words if len(plain(w)) >= 2]
    wins = []
    for k in range(0, len(text), window // 2):
        chunk = text[k:k + window]
        score = sum(chunk.count(w) for w in ws) + (1 if re.search(r"\d", chunk) else 0) * 0.1
        if score > 0.1:
            wins.append((score, k))
    if not wins:
        return text[:budget]
    wins.sort(key=lambda x: -x[0])
    picked: list[int] = []
    used = 0
    for _, k in wins:
        if any(abs(k - p) < window for p in picked):
            continue
        picked.append(k)
        used += window
        if used >= budget:
            break
    return "\n…\n".join(text[k:k + window] for k in sorted(picked))


def drop_repeats(texts: list[tuple[str, str]], chunk: int = 240, share: float = 0.6) -> tuple[list[tuple[str, str]], int]:
    """Убирает из отрывков страниц одного объекта куски, которые уже были на предыдущих страницах (меню, подвал,
    «о компании», баннеры повторяются на каждой странице сайта). Первое появление остаётся — сведения не теряются.
    Возвращает (отрывки, сколько знаков убрано)."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    removed = 0

    def shingles(s: str) -> set[str]:
        w = s.split()
        return {" ".join(w[i:i + 6]) for i in range(max(1, len(w) - 5))} if w else set()

    for url, text in texts:
        parts, keep = re.split(r"(\n…\n)", text), []
        for part in parts:
            if part == "\n…\n":
                keep.append(part)
                continue
            words, buf, pieces = part.split(" "), [], []
            for w in words:  # куски по ~chunk знаков по границам слов
                buf.append(w)
                if sum(len(x) + 1 for x in buf) >= chunk:
                    pieces.append(" ".join(buf))
                    buf = []
            if buf:
                pieces.append(" ".join(buf))
            for p in pieces:
                sh = shingles(p)
                if sh and len(p) >= 60 and len(sh & seen) >= share * len(sh):
                    removed += len(p) + 1
                    continue
                seen |= sh
                keep.append(p)
        joined = re.sub(r"(\n…\n)+", "\n…\n", " ".join(keep).replace(" \n…\n ", "\n…\n")).strip()
        if joined.strip("…\n "):
            out.append((url, joined))
    return out, removed


def sha(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "ignore")).hexdigest()[:16]


def match_attr(key: str, attrs: list[Attribute]) -> Attribute | None:
    k = (key or "").strip()
    for a in attrs:
        if a.id.lower() == k.lower():
            return a
    pk = plain(k)
    for a in attrs:
        if plain(a.name) == pk or (len(pk) >= 4 and (pk in plain(a.name) or plain(a.name) in pk)):
            return a
    m = re.match(r"^a?(\d+)$", k.lower())
    if m and 0 < int(m.group(1)) <= len(attrs):
        return attrs[int(m.group(1)) - 1]
    return None


def resolve_cell(attr: Attribute, options: list[dict[str, Any]]) -> dict[str, Any]:
    """Итог ячейки по вариантам (поиск и две модели): каждый вариант — значение, ссылка, цитата и найдена ли цитата
    на странице (found) с уровнем источника (kind).
      — проверенные варианты (цитата найдена) согласны между собой → подтверждено, уровень — лучшего источника;
      — проверенные расходятся → спор (решает совет);
      — проверенных нет: непроверенные согласны или он один → «не подтверждено»; расходятся → спор;
      — значений нет → «не найдено»."""
    opts = [o for o in options if (o.get("value") or "").strip()]
    if not opts:
        return {"status": "none", "value": "", "options": options}
    if attr.kind == "bool":  # «True», «true», «да — заявлен собственный завод…» → «да» / «нет»
        for o in opts:
            b = parse_bool(o["value"])
            if b is not None:
                o["value"] = "да" if b else "нет"
    verified = [o for o in opts if o.get("found")]
    pool = verified or opts
    rank = {"document": 0, "official": 1, "secondary": 2}
    if attr.kind in ("text", "list"):
        # текст и списки не спорят: разные слова об одном и том же — не противоречие. Текст — лучший источник;
        # список — объединение пунктов (из проверенных вариантов, а если таких нет — из всех)
        best = sorted(pool, key=lambda o: (rank.get(o.get("kind", ""), 3), -len(o.get("quote") or "")))[0]
        value = best["value"]
        if attr.kind == "list":
            items: list[str] = []
            seen: set[str] = set()
            for o in sorted(pool, key=lambda o: rank.get(o.get("kind", ""), 3)):
                for it in list_items(o["value"]):
                    k = plain(it)
                    if k and k not in seen:
                        seen.add(k)
                        items.append(it)
            value = ", ".join(items)[:600]
        return {"status": best.get("kind", "secondary") if verified else "unverified", "value": value,
                "unit": best.get("unit", ""), "as_of": best.get("as_of", ""), "url": best.get("url", ""),
                "quote": best.get("quote", ""), "context": best.get("context", ""),
                "agree": len(pool), "of": len(opts), "options": opts, "merged": attr.kind == "list" and len(pool) > 1}
    groups: list[list[dict[str, Any]]] = []
    for o in pool:
        for g in groups:
            if values_agree(g[0], o, attr.kind):
                g.append(o)
                break
        else:
            groups.append([o])
    if len(groups) > 1:
        # документ (отчёт, реестр, сертификат) против сайта или вторичного источника — документ надёжнее, судьи не
        # нужны. Проверенная цитата обязательна у обоих; равные по уровню источники по-прежнему решает совет
        if verified:
            top = [g for g in groups if any(o.get("kind") == "document" for o in g)]
            if len(top) == 1:
                g = top[0]
                best = next(o for o in g if o.get("kind") == "document")
                return {"status": "document", "value": best["value"], "unit": best.get("unit", ""),
                        "as_of": best.get("as_of", ""), "url": best.get("url", ""), "quote": best.get("quote", ""),
                        "context": best.get("context", ""), "agree": len(g), "of": len(opts), "options": opts,
                        "auto": "выбран документ: он надёжнее сайта и вторичных источников"}
        return {"status": "disputed", "value": "", "options": opts}
    g = groups[0]
    best = sorted(g, key=lambda o: rank.get(o.get("kind", ""), 3))[0]
    status = best.get("kind", "secondary") if verified else "unverified"
    return {"status": status, "value": best["value"], "unit": best.get("unit", ""), "as_of": best.get("as_of", ""),
            "url": best.get("url", ""), "quote": best.get("quote", ""), "context": best.get("context", ""),
            "agree": len(g), "of": len(opts), "options": opts}


# ---------------------------------------------------------------------------------------------------- ранг

def _cell_value(attr: Attribute, cell: dict[str, Any]) -> Any:
    """Разобранное значение ячейки для сравнения: число, дата (год с долей), да/нет, пункты списка; нет — None."""
    if not cell or cell.get("status") in (None, "none", "disputed") or not (cell.get("value") or "").strip():
        return None
    v = cell["value"]
    if attr.kind == "number":  # другая величина или период («млн юаней», «в сутки») в ранг не идёт
        return parse_number(f"{v} {cell.get('unit', '')}") if comparable(attr, cell) else None
    if attr.kind == "date":
        return parse_date(v)
    if attr.kind == "bool":
        return parse_bool(v)
    if attr.kind == "list":
        return [plain(i) for i in list_items(v)] or None
    return None


def _col_stats(attr: Attribute, values: list[Any]) -> tuple[float, float] | int | None:
    """Что нужно знать о столбце для оценки ячейки: (мин, макс) чисел и дат, наибольшая длина списка."""
    vals = [x for x in values if x is not None]
    if not vals:
        return None
    if attr.kind in ("number", "date"):
        return (min(vals), max(vals))
    if attr.kind == "list":
        return max(len(x) for x in vals)
    return None


def _score(attr: Attribute, x: Any, stats: Any) -> float | None:
    if x is None:
        return None
    if attr.kind == "bool":
        return float(x if attr.better != "no" else not x)
    if attr.kind == "list":
        if attr.wanted:
            want = [plain(w) for w in attr.wanted]
            return sum(1 for w in want if any(w in i or i in w for i in x)) / len(want)
        return len(x) / stats if stats else None
    if attr.kind in ("number", "date"):
        if not stats:
            return None
        lo, hi = stats
        if hi == lo:
            return 1.0
        if attr.kind == "number" and lo > 0 and hi / lo > 20:  # разброс на порядки — по логарифму
            s = (math.log(x) - math.log(lo)) / (math.log(hi) - math.log(lo)) if x > 0 else 0.0
        else:
            s = (x - lo) / (hi - lo)
        if attr.better in ("low", "older"):
            s = 1 - s
        return max(0.0, min(1.0, s))
    return None


def cell_score(attr: Attribute, cell: dict[str, Any], column: list[dict[str, Any]]) -> float | None:
    """Оценка значения 0…1 среди всех объектов (без учёта надёжности). Неизвестно или не оценивается — None."""
    return _score(attr, _cell_value(attr, cell), _col_stats(attr, [_cell_value(attr, c) for c in column]))


def score_parts(attrs: list[Attribute], table: dict[str, dict[str, dict[str, Any]]],
                use_trust: bool = True) -> dict[str, dict[str, float]]:
    """Вклад каждой ячейки: оценка значения × надёжность источника (документ 1, сайт 0,8, вторичный 0,6,
    не подтверждено 0,4); неизвестно — 0. Столбец разбирается один раз (ползунки и «что выяснить» пересчитывают
    ранг сотни раз)."""
    out: dict[str, dict[str, float]] = {oid: {} for oid in table}
    for a in attrs:
        if not a.scorable:
            continue
        vals = {oid: _cell_value(a, row.get(a.id) or {}) for oid, row in table.items()}
        stats = _col_stats(a, list(vals.values()))
        for oid, row in table.items():
            s = _score(a, vals[oid], stats)
            trust = TRUST.get((row.get(a.id) or {}).get("status", "none"), 0.4) if use_trust else 1.0
            out[oid][a.id] = round((s or 0.0) * trust, 4)
    return out


def totals(attrs: list[Attribute], parts: dict[str, dict[str, float]], weights: dict[str, float] | None = None
           ) -> dict[str, float]:
    w = {a.id: (weights or {}).get(a.id, a.weight) for a in attrs if a.scorable}
    tw = sum(w.values()) or 1.0
    return {oid: round(sum(w[k] * p.get(k, 0.0) for k in w) / tw * 100, 1) for oid, p in parts.items()}


def ranking(attrs: list[Attribute], table: dict[str, dict[str, dict[str, Any]]], weights: dict[str, float] | None = None,
            use_trust: bool = True) -> list[dict[str, Any]]:
    parts = score_parts(attrs, table, use_trust)
    tot = totals(attrs, parts, weights)
    order = sorted(tot, key=lambda o: (-tot[o], o))
    return [{"object": o, "score": tot[o], "rank": i + 1, "parts": parts[o]} for i, o in enumerate(order)]


def sensitivity(attrs: list[Attribute], table: dict[str, dict[str, dict[str, Any]]], names: dict[str, str],
                top: int = 15, limit: int = 12, use_trust: bool = True) -> list[dict[str, Any]]:
    """Где ранг неустойчив: при каком весе признака соседи по рангу меняются местами (вес меняется, остальные —
    нет). Счёт точный: итог — дробь (Σ wᵢsᵢ)/(Σ wᵢ), равенство двух итогов — линейное уравнение по одному весу."""
    parts = score_parts(attrs, table, use_trust)
    rows = ranking(attrs, table, use_trust=use_trust)[:top]
    w0 = {a.id: a.weight for a in attrs if a.scorable}
    out = []
    for i in range(len(rows) - 1):
        a, b = rows[i]["object"], rows[i + 1]["object"]
        for k in w0:
            sa = sum(w0[j] * parts[a].get(j, 0) for j in w0 if j != k)
            sb = sum(w0[j] * parts[b].get(j, 0) for j in w0 if j != k)
            da, db = parts[a].get(k, 0), parts[b].get(k, 0)
            if da == db:
                continue
            w = (sa - sb) / (db - da)  # sa + w·da = sb + w·db
            if 0 <= w <= 10 and abs(w - w0[k]) >= 1:  # перестановка от изменения веса на десятые — не новость
                out.append({"attr": k, "weight": round(w, 2), "now": w0[k], "a": a, "b": b,
                            "text": f"при весе «{_aname(attrs, k)}» {_fmt_w(w)} (сейчас {_fmt_w(w0[k])}) "
                                    f"{names.get(b, b)} обгонит {names.get(a, a)}"})
    out.sort(key=lambda x: abs(x["weight"] - x["now"]) / max(x["now"], 0.5))
    return out[:limit]


def _aname(attrs: list[Attribute], k: str) -> str:
    return next((a.name for a in attrs if a.id == k), k)


def _fmt_w(w: float) -> str:
    return f"{w:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def value_of_information(attrs: list[Attribute], table: dict[str, dict[str, dict[str, Any]]], names: dict[str, str],
                         top_n: int, limit: int = 15, use_trust: bool = True) -> list[dict[str, Any]]:
    """Что выяснить: неизвестные и неподтверждённые ячейки, от которых зависит, попадёт ли объект в первую группу
    (top_n). Для каждой считается ранг при лучшем (подтверждено документом, лучшее значение) и худшем исходе."""
    base = {r["object"]: r["rank"] for r in ranking(attrs, table, use_trust=use_trust)}
    n = len(base)
    top_n = min(top_n or n, n)
    out = []
    for oid, row in table.items():
        r0 = base[oid]
        if r0 > top_n + 6:
            continue
        for a in attrs:
            if not a.scorable:
                continue
            c = row.get(a.id) or {}
            if c.get("status") not in (None, "none", "unverified", "disputed"):
                continue
            ranks = {}
            for case in ("best", "worst"):
                fake = {o: dict(r) for o, r in table.items()}
                fake[oid] = dict(row)
                fake[oid][a.id] = _extreme(a, table, case)
                ranks[case] = next(x["rank"] for x in ranking(attrs, fake, use_trust=use_trust) if x["object"] == oid)
            enters = r0 > top_n >= ranks["best"]
            leaves = r0 <= top_n < ranks["worst"]
            swing = ranks["worst"] - ranks["best"]
            if not (enters or leaves) and swing < 3:
                continue
            what = (f"может поднять {names.get(oid, oid)} с {r0}-го места до {ranks['best']}-го" if enters else
                    f"может опустить {names.get(oid, oid)} с {r0}-го места на {ranks['worst']}-е" if leaves else
                    f"сдвигает {names.get(oid, oid)} между {ranks['best']}-м и {ranks['worst']}-м местом")
            out.append({"object": oid, "attr": a.id, "status": c.get("status") or "none", "rank": r0,
                        "best": ranks["best"], "worst": ranks["worst"], "crosses": enters or leaves,
                        "text": f"{a.name} — {STATUS_RU.get(c.get('status') or 'none')}; {what}"
                                + (f" (граница первой группы — {top_n})" if enters or leaves else "")})
    out.sort(key=lambda x: (not x["crosses"], -(x["worst"] - x["best"]), x["rank"]))
    return out[:limit]


def _extreme(a: Attribute, table: dict[str, dict[str, dict[str, Any]]], case: str) -> dict[str, Any]:
    if case == "worst":
        return {"status": "none", "value": ""}
    col = [r.get(a.id) or {} for r in table.values()]
    if a.kind == "bool":
        v = "нет" if a.better == "no" else "да"
    elif a.kind == "list":
        v = ", ".join(a.wanted) if a.wanted else max((c.get("value", "") for c in col), key=lambda s: len(list_items(s)),
                                                      default="")
    else:
        f = parse_date if a.kind == "date" else parse_number
        vals = [(f(f"{c.get('value', '')} {c.get('unit', '')}") if a.kind == "number" else f(c.get("value", "")),
                 c) for c in col if c.get("status") not in (None, "none", "disputed")]
        vals = [(x, c) for x, c in vals if x is not None]
        if not vals:
            return {"status": "none", "value": ""}
        x, c = (min if a.better in ("low", "older") else max)(vals, key=lambda t: t[0])
        return {"status": "document", "value": c.get("value", ""), "unit": c.get("unit", "")}
    return {"status": "document", "value": v}


# ---------------------------------------------------------------------------------------------------- задания

def _attrs_block(attrs: list[Attribute]) -> str:
    rows = []
    for a in attrs:
        extra = []
        if a.unit:
            extra.append(f"unit: {a.unit}")
        if a.evidence:
            extra.append(f"evidence: {a.evidence}")
        if a.keywords:
            extra.append("keywords: " + ", ".join(a.keywords[:12]))
        if a.wanted:
            extra.append("wanted: " + ", ".join(a.wanted))
        rows.append(f"- {a.id} «{a.name}» ({a.kind}){' — ' + '; '.join(extra) if extra else ''}")
    return "\n".join(rows)


def frame_prompt(question: str, context: str | None, criteria: list[str] | None, objects_hint: list[str],
                 start: str, max_attrs: int, n_angles: int = 8) -> str:
    hint = ("\n\nObjects named in the task — the program found these names in the text; they may be candidates to evaluate, "
            "competitors or benchmarks to compare with, examples, or explicitly excluded companies (decide from the task):\n"
            + "\n".join(f"- {o}" for o in objects_hint[:300])) if objects_hint else ""
    return f"""Task for a SELECTION study (compare many objects attribute by attribute, every cell backed by a source):

{question}{f"{chr(10)}{chr(10)}Context: {context}" if context else ""}{f"{chr(10)}{chr(10)}User criteria: {'; '.join(criteria)}" if criteria else ""}{hint}

{"Start: decide from the task and put it in \"start\": \"find\" — the task asks to FIND candidates (the usual case even if it names some companies as competitors, examples or exclusions); \"check\" — the task gives a list of candidates and asks only to evaluate them; \"extend\" — evaluate the given candidates AND find more." if start == "auto" else "Start: " + ("find candidates from scratch" if start == "find" else "check the user's list only" if start == "check" else "check the user's list and find missing candidates") + "."}

Build the frame of the study:
- "object_kind": what an object is (e.g. "contract manufacturer");
- "inclusion": who counts as a candidate (3–6 testable rules); "exclusion": who clearly does not;
- "attributes": {max_attrs} attributes at most — what the task asks to compare. For each: "name" (in the user's language),
  "kind" (number | bool | list | date | text), "unit" (target unit for numbers), "evidence" (what source proves it:
  annual report, registry record, certificate, company page…), "keywords" (words to find it on pages, INCLUDING the
  language of the sources — e.g. Chinese terms 产能, 万粒, 保健食品, SC证 — and English), "weight" 0–10 (importance for
  the user's decision; 0 = for information only), "better" (high | low | yes | no | newer | older | "" for text),
  "wanted" (for lists: the items the user needs, e.g. forms), "screen": true for 2–4 quick knock-out attributes
  (is it a manufacturer, makes the needed forms, has a production licence…) with "knockout" (which value excludes,
  in words). NEVER "screen" an attribute the task calls an advantage, a plus or "not a condition" — only hard
  requirements;
- "required_angles": searches the task EXPLICITLY demands — one per category it lists ("a separate search for each
  category"), one per type of object it requires (e.g. universal makers / specialists / makers of their own raw
  materials WITH finished forms), and its example queries grouped by topic. Each is a concrete web-search instruction
  that keeps the task's wording and languages (e.g. Chinese and English terms). Empty if the task demands none;
- "angles": {n_angles} more different directions to find candidates (exhibitor lists, stock exchange filings, registries,
  industry rankings and associations, searches in the local language, B2B platforms, certification databases) —
  each a concrete instruction for a web search, different from the others;
- "objects": ONLY the objects the task gives as CANDIDATES to evaluate (exact spelling), empty if none — never
  competitors to beat, benchmarks, examples or excluded companies;
- "benchmarks": companies named in the task that are NOT candidates (competitors to compare with, examples,
  excluded ones) — they are not checked and are not to be found as candidates;
- "start": "find" | "check" | "extend" (see Start above);
- "shortlist": how many objects the task wants in the comparison table — the WHOLE list to be checked in detail
  (e.g. "a list of 60–80 manufacturers" → 80; a range → its upper bound; else 0). NOT the finalists;
- "top_n": how many the task wants in the first group / finalists / next stage (e.g. "20–25 best for stage 2" → 25,
  "12 for negotiations" → 12; else 0). top_n is never larger than shortlist;
- "refined_question": one sentence.
Return JSON: {{"refined_question": "...", "object_kind": "...", "inclusion": [], "exclusion": [], "attributes":
[{{"name": "...", "kind": "number", "unit": "...", "evidence": "...", "keywords": [], "weight": 5, "better": "high",
"wanted": [], "screen": false, "knockout": ""}}], "required_angles": [], "angles": [], "objects": [], "benchmarks": [], "start": "find",
"shortlist": 0, "top_n": 0}}"""


def discover_prompt(frame: SelectionFrame, angle: str, per_angle: int, exclude: list[str] | None = None) -> str:
    ex = ("\n\nAlready known — do NOT list these, find others:\n" + "\n".join(f"- {x}" for x in exclude[:200])) if exclude else ""
    return f"""Find candidates for this selection study by searching the web.

Study: {frame.refined_question}
A candidate is: {frame.object_kind}. Inclusion rules: {'; '.join(frame.inclusion) or '—'}. Exclude: {'; '.join(frame.exclusion) or '—'}.{f"{chr(10)}Not candidates (competitors, benchmarks or excluded companies named in the task — do not list them): {', '.join(frame.benchmarks)}." if frame.benchmarks else ""}

Search direction (use ONLY this direction): {angle}

Do not drop a company because it is ALSO something else (e.g. it also makes raw materials or ingredients, also
sells, is part of a group): if it may fit the inclusion rules, list it — the screening step checks it. Exclude only
companies that clearly match an exclusion rule.

List up to {per_angle} distinct candidates that this direction finds. For each: "name" (official name, English if
available), "local_name" (name in the local language, e.g. Chinese — exactly as written, never translated), "aliases", "website" (official site), "url"
(the page you saw that shows it fits), "quote" (a short VERBATIM fragment of that page proving it fits — copied
exactly, in the page's language), "location", "why" (one line). Do not invent: only what the search returned.{ex}
Return JSON: {{"items": [{{"name": "...", "local_name": "", "aliases": [], "website": "", "url": "", "quote": "",
"location": "", "why": ""}}]}}"""


def list_sources_prompt(frame: SelectionFrame, n: int) -> str:
    return f"""Find READY-MADE LISTS of companies for this selection study — pages or PDF files that list many candidates
at once, so a program can download them and read every name.

Study: {frame.refined_question}
A candidate is: {frame.object_kind}. Inclusion rules: {'; '.join(frame.inclusion) or '—'}.

Good lists: exhibitor lists of the main trade shows (current or last edition), official registries and licence
databases, member directories of industry associations, published industry rankings, category pages of B2B
platforms, supplier lists. Prefer lists in the local language and the largest, most complete ones; the page must
show company names itself (not just a search form). Up to {n} lists, the most useful first.
Return JSON: {{"lists": [{{"url": "https://...", "title": "what the list is", "kind": "exhibitors | registry |
association | ranking | b2b | other", "size": "about how many companies"}}]}}"""


def list_read_prompt(frame: SelectionFrame, title: str, url: str, text: str, part: int, parts: int) -> str:
    return f"""Below is part {part} of {parts} of a ready-made list of companies, downloaded by the program
(lower-cased): {title or url} — {url}

Study: {frame.refined_question}
A candidate is: {frame.object_kind}. Inclusion rules: {'; '.join(frame.inclusion) or '—'}. Exclude: {'; '.join(frame.exclusion) or '—'}.

Write out EVERY company in this text that may be a candidate (when unsure — include it, a later check will decide;
skip only those clearly excluded or clearly of another kind). For each: "name" (as written; English if given),
"local_name" (name in the local language if given), "website" (if the text shows it), "quote" (the list line or
fragment with this company, copied VERBATIM, up to 30 words), "location" (if shown), "why" (a few words).
Use ONLY this text — do not add companies from memory.

{text}

Return JSON: {{"items": [{{"name": "", "local_name": "", "website": "", "quote": "", "location": "", "why": ""}}]}}"""


def angles_prompt(frame: SelectionFrame, used: list[str], found: int, share: float | None) -> str:
    return f"""The candidate list for this study looks incomplete ({found} found; estimated share of all: {share}).
Study: {frame.refined_question}. Candidate: {frame.object_kind}.
Search directions already used:
{chr(10).join(f"- {a}" for a in used)}
Propose 3–4 NEW search directions that would find candidates the used ones miss (other sources, other languages,
regional clusters, registries, trade shows, supplier lists of known brands). Return JSON: {{"angles": ["..."]}}"""


def screen_prompt(frame: SelectionFrame, attrs: list[Attribute], objs: list[dict[str, Any]]) -> str:
    lines = "\n".join(f"- {o['name']}" + (f" ({', '.join(o['names'][1:4])})" if len(o.get("names") or []) > 1 else "")
                      + (f" — site {o['website']}" if o.get("website") else "") for o in objs)
    return f"""Quick screening for a selection study: check ONLY these knock-out attributes for each object by searching the web.

Study: {frame.refined_question}. Candidate: {frame.object_kind}.
Attributes:
{chr(10).join(f"- {a.id} «{a.name}» — excluded if: {a.knockout or 'clearly does not fit'}" for a in attrs)}

Objects:
{lines}

For each object and attribute: "verdict" pass | fail | unclear (fail ONLY with a source saying so), "value" (short),
"url" and a short VERBATIM "quote" from that page. Unknown → unclear. Return JSON: {{"rows": [{{"object": "<name as
given>", "website": "", "checks": [{{"attr": "A1", "verdict": "pass", "value": "", "url": "", "quote": ""}}]}}]}}"""


def screen_read_prompt(frame: SelectionFrame, attrs: list[Attribute], objs: list[dict[str, Any]],
                       texts: dict[str, list[tuple[str, str]]]) -> str:
    lines = "\n".join(f"- {o['name']}" + (f" ({', '.join(o['names'][1:4])})" if len(o.get("names") or []) > 1 else "")
                      for o in objs)
    pages = "\n\n".join(f"### {o['name']}\n" + "\n".join(f"PAGE {u}\n{t}" for u, t in texts.get(o["id"]) or [])
                         for o in objs)
    return f"""Quick screening for a selection study: check ONLY these knock-out attributes for each object, using ONLY the
page texts below (downloaded by the program; lower-cased).

Study: {frame.refined_question}. Candidate: {frame.object_kind}.
Attributes:
{chr(10).join(f"- {a.id} «{a.name}» — excluded if: {a.knockout or 'clearly does not fit'}" for a in attrs)}

Objects:
{lines}

For each object and attribute: "verdict" pass | fail | unclear. "fail" ONLY when a page clearly proves the exclusion
rule (a company that ALSO makes raw materials is not excluded for that if it makes finished products too); not on the
pages → unclear. "value" (short), "url" (the page) and a short VERBATIM "quote" copied from that page. Return JSON:
{{"rows": [{{"object": "<name as given>", "website": "", "checks": [{{"attr": "A1", "verdict": "pass", "value": "",
"url": "", "quote": ""}}]}}]}}

PAGES
{pages}"""


def sources_prompt(frame: SelectionFrame, obj: dict[str, Any], attrs: list[Attribute], max_sources: int,
                   known: list[str] | None = None) -> str:
    names = ", ".join(obj.get("names") or [obj["name"]])
    known_block = ("\n\nAlready known pages (the program downloads them anyway — do NOT list them again; find OTHER pages):\n"
                   + "\n".join(f"- {u}" for u in known)) if known else ""
    return f"""Detailed check of ONE object for a selection study. Search the web for primary sources about it.

Object: {obj['name']} (also: {names}){f"; official site: {obj['website']}" if obj.get('website') else ''}{f"; location: {obj['location']}" if obj.get('location') else ''}
Study: {frame.refined_question}

Attributes to fill:
{_attrs_block(attrs)}{known_block}

1) "sources": up to {max_sources} best pages for these attributes — prefer primary documents (annual reports and
   exchange filings, registries, certificate databases), then the company's own pages, then others. For each: url,
   title, kind (document | official | secondary), why.
2) "cells": for every attribute you can fill — "attr" (A1…), "value", "unit", "as_of" (date the value refers to),
   "url" and a short VERBATIM "quote" from that page (copied exactly, in the page's language). Not found → omit.
3) "website": the official site.
Only pages about THIS company (same name or its own site) — not a similarly named or unrelated one.
Return JSON: {{"website": "", "sources": [{{"url": "", "title": "", "kind": "official", "why": ""}}], "cells": [{{"attr":
"A1", "value": "", "unit": "", "as_of": "", "url": "", "quote": ""}}]}}"""


CACHE_SPLIT = "\n\u2063\u2063\n"  # граница общего начала запроса: до неё — одинаково у всех объектов (кэш провайдера)


def fill_prompt(frame: SelectionFrame, obj: dict[str, Any], attrs: list[Attribute], pages: list[tuple[str, str]],
                only: list[str] | None = None) -> str:
    body = "\n\n".join(f"### PAGE {i}: {u}\n{t}" for i, (u, t) in enumerate(pages, 1)) or "(no pages could be opened)"
    focus = (f"\n\nFill ONLY these attributes (the others are already confirmed): {', '.join(only)}" if only else "")
    return f"""Fill the table row for ONE object using ONLY the page texts given (downloaded by the program; lower-cased).
Study: {frame.refined_question}

Attributes:
{_attrs_block(attrs)}

Rules: every value must come from a page below; "quote" is copied VERBATIM from that page (10–40 words, same
language, no translation) and "url" is that page. Convert nothing in the quote; put the converted value in "value"
and the unit in "unit" (e.g. 25亿粒 → value "2500", unit "млн капсул/год"). Numbers: "value" is ONE number in the
attribute's unit and period; if the source gives another measure (money instead of pieces, tonnes, per day instead of
per year) — copy it as is and write that unit exactly ("млн юаней/год", "шт/сутки"): the program will not mix it in.
Yes/no attributes: "value" is "да" or "нет". Lists: short items separated by commas. Say what the number is
(capacity vs actual output, which year) in "note". Not on the pages → omit the attribute. Do not guess.
"value" and "note" are written in the language of the attribute names (translate source terms: 软胶囊 → мягкие
капсулы, 口服液 → питьевой раствор); only "quote" stays in the page's language. NEVER translate proper names —
company, group, brand and product names, addresses, licence numbers: copy them exactly as in the source (Chinese
characters, English, Latin), the user searches for them by these names. A page about ANOTHER company (other
name, other address, a different group) is not about this object — take nothing from it.
Return JSON: {{"cells": [{{"attr": "A1", "value": "", "unit": "", "as_of": "", "url": "", "quote": "", "note": ""}}]}}{CACHE_SPLIT}Object: {obj['name']} (also: {', '.join(obj.get('names') or [])}){focus}

{body}"""


def disputes_prompt(frame: SelectionFrame, work: list[tuple[dict[str, Any], list[tuple[Attribute, list[dict[str, Any]]]]]]
                    ) -> str:
    """Споры нескольких объектов одним запросом: ключ ячейки — «O3:A2»."""
    blocks = []
    for obj, disputes in work:
        for a, opts in disputes:
            lines = []
            for j, o in enumerate(opts):
                tag = "quote FOUND on the page" if o.get("found") else "quote NOT found on the page"
                lines.append(f"  ({chr(97 + j)}) {o.get('value')} {o.get('unit', '')} — by {o.get('by')}; {o.get('kind', '')}; "
                             f"{tag}; {o.get('url', '')}\n      quote: «{o.get('quote', '')[:300]}»"
                             + (f"\n      page around it: «{o.get('context', '')[:400]}»" if o.get("context") else "")
                             + (f"\n      note: {o['note']}" if o.get("note") else ""))
            blocks.append(f"{obj['id']}:{a.id} — {obj['name']} — «{a.name}» ({a.kind}{', ' + a.unit if a.unit else ''}):\n"
                          + "\n".join(lines))
    return f"""Disputed cells in a selection study. Two models and a search read the sources and disagree.
Study: {frame.refined_question}

{chr(10).join(blocks)}

For each cell choose the option that the evidence supports best (prefer a quote FOUND on a primary page; mind
capacity vs output, units 万/亿, the period (per day / per year), the year, the legal entity), or "none" if no option is
supported. Return JSON: {{"items": [{{"key": "O3:A1", "choice": "a", "reason": "one line"}}]}}"""


def dispute_prompt(frame: SelectionFrame, obj: dict[str, Any], disputes: list[tuple[Attribute, list[dict[str, Any]]]]) -> str:
    blocks = []
    for a, opts in disputes:
        lines = []
        for j, o in enumerate(opts):
            tag = "quote FOUND on the page" if o.get("found") else "quote NOT found on the page"
            lines.append(f"  ({chr(97 + j)}) {o.get('value')} {o.get('unit', '')} — by {o.get('by')}; {o.get('kind', '')}; "
                         f"{tag}; {o.get('url', '')}\n      quote: «{o.get('quote', '')[:300]}»"
                         + (f"\n      page around it: «{o.get('context', '')[:500]}»" if o.get("context") else "")
                         + (f"\n      note: {o['note']}" if o.get("note") else ""))
        blocks.append(f"{a.id} «{a.name}» ({a.kind}{', ' + a.unit if a.unit else ''}):\n" + "\n".join(lines))
    return f"""Disputed cells of one object in a selection study. Two models and a search read the sources and disagree.
Object: {obj['name']}. Study: {frame.refined_question}

{chr(10).join(blocks)}

For each attribute choose the option that the evidence supports best (prefer a quote FOUND on a primary page; mind
capacity vs output, units 万/亿, the year, the legal entity), or "none" if no option is supported. Return JSON:
{{"items": [{{"key": "A1", "choice": "a", "reason": "one line"}}]}}"""


def conclusion_prompt(frame: SelectionFrame, table_txt: str, voi_txt: str, sens_txt: str, excluded_txt: str,
                      top_n: int, brief: bool, counts_txt: str = "") -> str:
    return f"""Write the conclusion of a selection study. The ranking is computed by the program from verified cells and
the user's weights — do NOT re-rank and do NOT add facts that are not in the table.

Study: {frame.refined_question}
First group: top {top_n}.

Ranking and key cells (status: Д document, С company site, В secondary, ? unverified, — not found):
{table_txt}

What could change the choice:
{voi_txt or '—'}

Where the ranking is sensitive to weights:
{sens_txt or '—'}

Excluded at screening: {excluded_txt or '—'}
{counts_txt}
Company names: exactly as in the table (original Chinese / English spelling), never translated.
Return JSON: {{"summary": "markdown, {'≤150' if brief else '≤350'} words: the decision (who first and why, from the
table), what is solid and what rests on unverified cells, the main gaps", "next_steps": ["concrete step"],
"risks": ["..."]}}"""


# ---------------------------------------------------------------------------------------------------- отчёт

def esc(s: Any) -> str:
    return str(s or "").replace("|", "∣").replace("\n", " ").strip()


COLS_PER_TABLE = 4  # признаков в одной таблице отчёта: шире — на странице A4 текст ломается по буквам


def short_name(name: str, limit: int = 34) -> str:
    """Короткое название для заголовка таблицы: до скобки или первой запятой, не длиннее limit."""
    n = re.split(r"\s*[(（]|\s+—\s+|,\s", name or "", maxsplit=1)[0].strip() or name
    return n if len(n) <= limit else n[:limit - 1].rstrip() + "…"


def cell_text(c: dict[str, Any], short: bool = False, attr: "Attribute | None" = None) -> str:
    if not c or c.get("status") in (None, "none"):
        return "—"
    odd = " ≠" if attr is not None and not comparable(attr, c) else ""
    v = f"{c.get('value', '')}{(' ' + c['unit']) if c.get('unit') and c['unit'] not in c.get('value', '') else ''}"
    if short and len(v) > 48:
        v = v[:45].rstrip() + "…"
    return f"{esc(v)} ({STATUS_MARK.get(c.get('status', ''), '?')}){odd}"


def origin_label(o: dict[str, Any]) -> str:
    """Откуда объект в таблице, если не из поиска: добавлен вами на остановке или назван в задании."""
    if o.get("why") == "добавлено вами":
        return "добавлен вами"
    if o.get("seed"):
        return "из задания"
    return ""


def report_md(sel: dict[str, Any], conclusion: Conclusion | None) -> str:
    """Отчёт режима: вывод председателя, ранг, что выяснить, устойчивость, полная таблица, карточки, исключённые."""
    attrs = [Attribute.model_validate(a) for a in sel["attributes"]]
    names = {o["id"]: o["name"] for o in sel["objects"]}
    table = sel["table"]
    rank = sel["ranking"]
    top_n = sel.get("top_n") or len(rank)
    origin = {o["id"]: t for o in sel["objects"] if (t := origin_label(o))}
    out = []
    if conclusion and conclusion.summary:
        out.append(conclusion.summary.strip())
    if conclusion and conclusion.next_steps:
        out.append("**Следующие шаги:**\n" + "\n".join(f"- {s}" for s in conclusion.next_steps))
    scored = [a for a in attrs if a.scorable]
    out.append(f"### Ранг ({len(rank)} объектов, первая группа — {top_n})\n\n"
               "Ранг считает программа: оценка значения × надёжность источника (документ 1; сайт компании 0,8; "
               "вторичный 0,6; не подтверждено 0,4; не найдено 0), веса — " +
               ", ".join(f"{a.name} {_fmt_w(a.weight)}" for a in scored) + ".\n\n"
               + "\n\n".join(
                   ("" if i == 0 else f"*Ранг, продолжение ({i // COLS_PER_TABLE + 1}):*\n\n")
                   + "| № | Объект | Балл | " + " | ".join(esc(short_name(a.name)) for a in part) + " |\n|---|---|---|"
                   + "---|" * len(part) + "\n"
                   + "\n".join(f"| {r['rank']}{' ✓' if r['rank'] <= top_n else ''} | {esc(short_name(names.get(r['object']) or '', 40))}"
                                f"{' ⁺' if origin.get(r['object']) else ''} | "
                                f"{str(r['score']).replace('.', ',')} | "
                                + " | ".join(cell_text(table[r["object"]].get(a.id) or {}, True, a) for a in part) + " |"
                                for r in rank)
                   for i, part in ((i, scored[i:i + COLS_PER_TABLE]) for i in range(0, max(1, len(scored)), COLS_PER_TABLE)))
               + ("\n\n⁺ — не найден поиском программы: " + "; ".join(
                   f"{esc(short_name(names.get(k) or k, 40))} — {v}" for k, v in origin.items() if k in names) + "."
                  if origin else "")
               + ("\n\n≠ — величина или период несопоставимы с единицей признака (деньги вместо штук, в сутки вместо "
                  "в год): в ранг не идёт." if any(not comparable(a, table[r["object"]].get(a.id) or {})
                                                   for r in rank for a in scored if a.kind == "number") else ""))
    if sel.get("voi"):
        out.append("### Что выяснить, чтобы выбор не поменялся неожиданно\n\n"
                   + "\n".join(f"- {esc(names.get(v['object']))}: {v['text']}" for v in sel["voi"]))
    if sel.get("sensitivity"):
        out.append("### Устойчивость ранга к весам\n\n" + "\n".join(f"- {s['text']}" for s in sel["sensitivity"]))
    info = [a for a in attrs if not a.scorable]
    if info:
        out.append("### Признаки для сведения\n\n" + "\n\n".join(
            "| Объект | " + " | ".join(esc(short_name(a.name)) for a in part) + " |\n|---|" + "---|" * len(part) + "\n"
            + "\n".join(f"| {esc(short_name(names.get(r['object']) or '', 40))} | "
                         + " | ".join(cell_text(table[r["object"]].get(a.id) or {}, True) for a in part) + " |"
                         for r in rank)
            for part in (info[i:i + COLS_PER_TABLE] for i in range(0, len(info), COLS_PER_TABLE))))
    cards = []
    for r in rank:
        oid = r["object"]
        o = next(x for x in sel["objects"] if x["id"] == oid)
        lines = [f"#### {r['rank']}. {esc(o['name'])}" + (f" — {esc(', '.join(o['names'][1:3]))}" if len(o.get('names') or []) > 1 else "")]
        if origin_label(o):
            lines.append(f"*{origin_label(o).capitalize()}: программа его не искала — проверен по вашей просьбе.*"
                         if origin_label(o) == "добавлен вами" else f"*{origin_label(o).capitalize()}.*")
        if o.get("website"):
            lines.append(f"Сайт: {o['website']}")
        for a in attrs:
            c = table[oid].get(a.id) or {}
            if c.get("status") in (None, "none"):
                lines.append(f"- **{esc(a.name)}:** не найдено")
                continue
            safe = str(c.get("url") or "").startswith(("http://", "https://"))  # адреса от моделей — только http(s)
            src = f" — [{domain(c['url']) or 'источник'}](<{c['url']}>)" if safe else ""
            q = f"\n  > «{esc(c['quote'])[:400]}»" if c.get("quote") else ""
            extra = []
            if c.get("as_of"):
                extra.append(c["as_of"])
            if c.get("ruled"):
                extra.append("решено советом")
            elif c.get("auto"):
                extra.append(f"источники расходились, {c['auto']}")
            if c.get("agree") and c.get("of"):
                extra.append(f"совпало {c['agree']} из {c['of']}")
            lines.append(f"- **{esc(a.name)}:** {cell_text(c)} · {STATUS_RU.get(c.get('status', ''), '')}"
                         + (f" ({', '.join(extra)})" if extra else "") + src + q)
        cards.append("\n".join(lines))
    out.append("### Карточки объектов\n\nУ каждого значения — источник и дословная цитата; программа нашла её на "
               "скачанной странице (копия хранится). Д — документ, С — сайт компании, В — вторичный, ? — цитата не "
               "найдена.\n\n" + "\n\n".join(cards))
    if sel.get("unchecked"):
        out.append("### Не удалось проверить\n\nПо этим объектам не нашлось ни одного значения (сайт не открылся, "
                   "поиск ничего не вернул) — в ранг они не вошли:\n\n" + "\n".join(f"- {esc(x['name'])}" for x in sel["unchecked"]))
    if sel.get("excluded"):
        out.append("### Исключены при отсеве\n\n" + "\n".join(f"- {esc(x['name'])}: {esc(x['reason'])}"
                                                             for x in sel["excluded"]))
    if conclusion and conclusion.risks:
        out.append("### Риски\n\n" + "\n".join(f"- {r}" for r in conclusion.risks))
    return "\n\n".join(out)


def table_text(sel: dict[str, Any], limit: int = 40) -> str:
    """Таблица для председателя: ранг, балл и ключевые ячейки (коротко)."""
    attrs = [Attribute.model_validate(a) for a in sel["attributes"]]
    names = {o["id"]: o["name"] for o in sel["objects"]}
    rows = []
    for r in sel["ranking"][:limit]:
        cells = "; ".join(f"{a.name}: {cell_text(sel['table'][r['object']].get(a.id) or {}, True)}" for a in attrs)
        rows.append(f"{r['rank']}. {names.get(r['object'])} — {r['score']} — {cells}")
    return "\n".join(rows)
