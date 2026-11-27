"""Форматирование отчёта: текст, Markdown, JSON."""
from __future__ import annotations

import json

from .models import Level, Report

ORDER = [Level.CRITICAL, Level.WARNING, Level.INFO, Level.OK]
DISCLAIMER = ("Результат носит справочный характер и не заменяет экспертизу и государственную "
              "регистрацию (СГР). Справочники требуют сверки с актуальными редакциями нормативных актов.")


def _sorted(report: Report):
    return sorted(report.findings, key=lambda f: ORDER.index(f.level))


def to_text(report: Report) -> str:
    p = report.product
    dose = (f"{p.units_per_day:g} × {p.unit_name}, количества на 1 {p.unit_name}"
            if p.amounts_per_unit else "количества указаны на суточную дозу")
    lines = [
        "=" * 72,
        f"Анализ БАД: {p.name}",
        f"Суточная доза: {dose}",
        f"Компонентов распознано: {len(p.ingredients)}",
        "=" * 72,
        f"ИТОГ: {report.verdict}",
        f"  нарушений: {report.count(Level.CRITICAL)}, предупреждений: {report.count(Level.WARNING)}",
        "",
    ]
    if report.nutrients:
        lines.append("Нормируемые вещества (в сутки):")
        lines.append(f"  {'Вещество':<28}{'Доза':>12}  {'% АУП':>7}  {'% ВДУ':>7}")
        for n in report.nutrients:
            lines.append(f"  {n.name[:27]:<28}{n.daily_amount:>9.4g} {n.unit:<3}"
                         f"{n.pct_adequate:>7.0f}%  {n.pct_upper:>6.0f}%")
        lines.append("")
    for f in _sorted(report):
        lines.append(f"[{f.level.label}] {f.message}")
        if f.basis:
            lines.append(f"    основание: {f.basis}")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def to_markdown(report: Report) -> str:
    p = report.product
    out = [f"# Анализ БАД: {p.name}", "", f"**Итог:** {report.verdict}", ""]
    if report.nutrients:
        out += ["| Вещество | Доза в сутки | % АУП | % ВДУ |", "|---|---:|---:|---:|"]
        for n in report.nutrients:
            out.append(f"| {n.name} | {n.daily_amount:.4g} {n.unit} | {n.pct_adequate:.0f}% | {n.pct_upper:.0f}% |")
        out.append("")
    out.append("## Результаты проверки\n")
    for f in _sorted(report):
        basis = f" _({f.basis})_" if f.basis else ""
        out.append(f"- **{f.level.label}** — {f.message}{basis}")
    out += ["", f"> {DISCLAIMER}"]
    return "\n".join(out)


def to_json(report: Report) -> str:
    return json.dumps({
        "product": report.product.name,
        "verdict": report.verdict,
        "nutrients": [n.to_dict() for n in report.nutrients],
        "findings": [f.to_dict() for f in _sorted(report)],
        "disclaimer": DISCLAIMER,
    }, ensure_ascii=False, indent=2)


FORMATTERS = {"text": to_text, "md": to_markdown, "json": to_json}
