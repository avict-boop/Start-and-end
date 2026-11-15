"""Выгрузка «Отбора» в Excel: ранг, полная таблица со статусами, источники и цитаты по ячейкам, исключённые."""
from __future__ import annotations

import io
from typing import Any

from .. import selection as S

FILL = {"document": "C6EFCE", "official": "DDEBF7", "secondary": "FFF2CC", "unverified": "FCE4D6", "none": "EDEDED"}


def selection_xlsx(d: dict[str, Any], weights: dict[str, float] | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    from ..modes.selection import rerank

    sel = d["selection"]
    attrs = [S.Attribute.model_validate(a) for a in sel["attributes"]]
    if weights:
        attrs = [a.model_copy(update={"weight": weights.get(a.id, a.weight)}) for a in attrs]
    rr = rerank(sel, weights)
    names = rr["names"]
    objs = {o["id"]: o for o in sel["objects"]}
    table = sel["table"]
    top_n = sel.get("top_n") or len(rr["ranking"])
    bold = Font(bold=True)
    wrap = Alignment(wrap_text=True, vertical="top")

    wb = Workbook()
    ws = wb.active
    ws.title = "Ранг"
    scored = [a for a in attrs if a.scorable]
    ws.append(["№", "Объект", "Сайт", "Балл", "Первая группа"] + [f"{a.name} (вес {a.weight:g})" for a in scored])
    for r in rr["ranking"]:
        o = objs.get(r["object"], {})
        ws.append([r["rank"], names.get(r["object"]), o.get("website", ""), r["score"], "да" if r["rank"] <= top_n else ""]
                  + [round(r["parts"].get(a.id, 0) * 100, 1) for a in scored])
    _style_head(ws, bold)

    ws2 = wb.create_sheet("Таблица")
    ws2.append(["№", "Объект"] + [a.name for a in attrs])
    for r in rr["ranking"]:
        row = [r["rank"], names.get(r["object"])]
        for a in attrs:
            c = table[r["object"]].get(a.id) or {}
            v = "" if c.get("status") in (None, "none") else f"{c.get('value', '')} {c.get('unit', '')}".strip()
            row.append(v)
        ws2.append(row)
        for j, a in enumerate(attrs):
            st = (table[r["object"]].get(a.id) or {}).get("status", "none")
            ws2.cell(row=ws2.max_row, column=3 + j).fill = PatternFill("solid", fgColor=FILL.get(st, "FFFFFF"))
    ws2.append([])
    ws2.append(["Цвет:", "зелёный — документ; голубой — сайт компании; жёлтый — вторичный источник; "
                         "оранжевый — цитата не найдена; серый — не найдено"])
    _style_head(ws2, bold)

    ws3 = wb.create_sheet("Источники и цитаты")
    ws3.append(["Объект", "Признак", "Значение", "Единица", "На дату", "Статус", "Ссылка", "Цитата", "Решено советом",
                "Совпало"])
    for r in rr["ranking"]:
        for a in attrs:
            c = table[r["object"]].get(a.id) or {}
            if c.get("status") in (None, "none") and not c.get("options"):
                continue
            ws3.append([names.get(r["object"]), a.name, c.get("value", ""), c.get("unit", ""), c.get("as_of", ""),
                        S.STATUS_RU.get(c.get("status", "none"), ""), c.get("url", ""), c.get("quote", ""),
                        "да" if c.get("ruled") else "", f"{c.get('agree', '')} из {c.get('of', '')}" if c.get("of") else ""])
            ws3.cell(row=ws3.max_row, column=8).alignment = wrap
    _style_head(ws3, bold)

    if sel.get("voi") or rr.get("voi"):
        ws4 = wb.create_sheet("Что выяснить")
        ws4.append(["Объект", "Что", "Место сейчас", "Лучший исход", "Худший исход"])
        for v in rr.get("voi") or []:
            ws4.append([names.get(v["object"]), v["text"], v["rank"], v["best"], v["worst"]])
        _style_head(ws4, bold)
    if sel.get("excluded"):
        ws5 = wb.create_sheet("Исключены")
        ws5.append(["Объект", "Причина"])
        for x in sel["excluded"]:
            ws5.append([x["name"], x["reason"]])
        _style_head(ws5, bold)
    if sel.get("sources"):
        ws6 = wb.create_sheet("Страницы")
        ws6.append(["Адрес", "Уровень", "Скачана", "Открылась", "Ячеек", "Копия"])
        for x in sel["sources"]:
            ws6.append([x["url"], S.STATUS_RU.get(x.get("kind", ""), x.get("kind", "")), x.get("fetched", ""),
                        x.get("status", ""), x.get("cells", 0), x.get("copy", "")])
        _style_head(ws6, bold)

    for sh in wb.worksheets:
        for i, col in enumerate(sh.columns, 1):
            width = max((len(str(c.value or "")) for c in col), default=8)
            sh.column_dimensions[get_column_letter(i)].width = max(8, min(60, width + 2))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _style_head(ws: Any, bold: Any) -> None:
    for c in ws[1]:
        c.font = bold
    ws.freeze_panes = "C2"
