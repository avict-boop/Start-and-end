"""Распознавание ответов, где модель «положила» результат в файл, которого не существует.

Через API модели не создают файлов, но некоторые (особенно привыкшие к «песочнице» с кодом) пишут
«отчёт сохранён в файл, [скачать](sandbox:/mnt/data/report.docx)» — а самого содержимого в ответе нет.
Такой ответ нельзя анализировать: совет получил бы ссылку вместо исследования.

Ищем только сильные признаки, чтобы не путать с упоминанием файлов пользователя («в приложенном вами файле»)
и с обычными ссылками на сайты.
"""
from __future__ import annotations

import re

FILE_EXT = r"(?:docx?|pdf|xlsx?|xlsm|csv|tsv|pptx?|md|txt|json|zip|html?|png|jpe?g|ipynb|py)"

PATTERNS = [
    # песочница исполнения кода и локальные пути
    re.compile(r"sandbox:/\S*", re.I),
    re.compile(r"/mnt/data/\S*", re.I),
    re.compile(r"\b(?:file|attachment)://\S+", re.I),
    # ссылка в Markdown на файл, а не на сайт: [скачать](report.docx), [отчёт](./out/data.csv)
    re.compile(r"\[[^\]\n]{1,80}\]\((?!https?://)[^)\s]+\." + FILE_EXT + r"\)", re.I),
    # «я сохранил / прилагаю / подготовил файл …»
    re.compile(r"\b(?:прилагаю|приложил[аи]?|прикрепил[аи]?|прикрепляю|сохранил[аи]?|выгрузил[аи]?|"
               r"сгенерировал[аи]?|сформировал[аи]?\s+и\s+сохранил[аи]?)\b[^.\n]{0,50}?"
               r"(?:\b(?:файл|документ|архив|презентаци)\w*|\.\b" + FILE_EXT + r"\b)", re.I),
    re.compile(r"\b(?:вы\s+можете\s+)?скача(?:ть|йте)\s+(?:готов\w+\s+|полн\w+\s+)?(?:файл|документ|отч[её]т|таблиц)\w*", re.I),
    re.compile(r"\bI(?:'ve|\s+have)?\s+(?:attached|saved|generated|exported|uploaded|created)\b[^.\n]{0,50}?"
               r"\b(?:file|document|spreadsheet|report|workbook|csv|pdf|docx|xlsx)\b", re.I),
    re.compile(r"\bdownload\s+(?:the|this|your)\s+(?:full\s+)?(?:file|report|document|spreadsheet|workbook)\b", re.I),
]


def find_file_refs(text: str, limit: int = 3) -> list[str]:
    """Фрагменты текста, где модель ссылается на созданный ею файл (пусто — всё в порядке)."""
    found: list[str] = []
    for rx in PATTERNS:
        for m in rx.finditer(text or ""):
            frag = m.group(0).strip()[:120]
            if frag and frag not in found:
                found.append(frag)
            if len(found) >= limit:
                return found
    return found


def refs_in(obj: object, limit: int = 3) -> list[str]:
    """То же для всех текстовых полей разобранного ответа (строки, списки, словари)."""
    parts: list[str] = []

    def walk(x: object) -> None:
        if isinstance(x, str):
            parts.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    walk(obj)
    return find_file_refs("\n".join(parts), limit)


FIX_MESSAGE = ("your reply refers to a file, attachment or download that does not exist ({refs}). "
               "You have no tools, code execution or file system here, and nothing you 'save' reaches the user. "
               "Put the FULL content — all text, tables, data and code — directly into the JSON fields, "
               "and do not mention files or downloads.")
