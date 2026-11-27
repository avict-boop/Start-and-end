"""Командная строка: python -m bad_checker <файл> [опции]."""
from __future__ import annotations

import argparse
import sys

from .analyzer import analyze
from .models import Level
from .parser import load_product
from .report import FORMATTERS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="bad_checker",
        description="Анализ состава БАД на соответствие требованиям законодательства РФ/ЕАЭС")
    ap.add_argument("file", help="JSON-описание продукта или текстовый файл с составом/этикеткой")
    ap.add_argument("-n", "--per-day", type=float, default=None,
                    help="сколько единиц (таблеток/капсул) принимается в сутки")
    ap.add_argument("-f", "--format", choices=FORMATTERS, default="text", help="формат отчёта")
    ap.add_argument("-o", "--output", help="сохранить отчёт в файл")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    report = analyze(load_product(args.file, units_per_day=args.per_day))
    text = FORMATTERS[args.format](report)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"Отчёт сохранён: {args.output} — {report.verdict}")
    else:
        print(text)
    # Код возврата: 0 — ок, 1 — есть предупреждения, 2 — есть нарушения
    if report.count(Level.CRITICAL):
        return 2
    return 1 if report.count(Level.WARNING) else 0


if __name__ == "__main__":
    sys.exit(main())
