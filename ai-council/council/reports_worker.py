"""Сборка отчётов в отдельном процессе: `python -m council.reports_worker <папка архива> <id> <формат>...`.

Сайт и бот собирают отчёты так, чтобы тяжёлая вёрстка PDF не занимала их собственный процесс: пока идёт
сборка, сайт отвечает, а память, нужная вёрстке, освобождается сразу после выхода процесса.
Печатает JSON {формат: путь или «ошибка: …»}.
"""
from __future__ import annotations

import json
import logging
import sys

from .archive import Archive


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("использование: python -m council.reports_worker <папка> <id> <формат>...", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.ERROR)  # предупреждения вёрстки не засоряют журнал сайта
    folder, stem, formats = argv[0], argv[1], argv[2:]
    print(json.dumps(Archive(folder).build(stem, formats), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
