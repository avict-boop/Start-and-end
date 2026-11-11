"""Пересобирает вложения пакета аудита из документации и кода: python docs/audit-tz/build.py

01 и 02 — из docs/logic.md (разделы 1–7 и 8–16 с картой кода) и config.yaml; 03 — дословные запросы к моделям
из council/prompts.py, council/research.py и council/advisor.py. 00-tz.md и 04 пишутся вручную.
Лимит программы на вложения: 60 000 символов на файл, 150 000 на все — скрипт это проверяет.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
PER_FILE, TOTAL = 60_000, 150_000

RESEARCH_PROMPTS = ["fill_lenses", "research_head", "framing", "_claims_rule", "CLAIM_JSON", "proposal", "critique",
                    "ARBITRATION_SYSTEM", "arbitration", "digest", "revision", "vote", "verdict", "check_length",
                    "check_verdict"]
ADVISOR_PROMPTS = ["MODE_ROLES", "dossier_prompt", "advise_prompt"]


def sections(md: str) -> list[tuple[str, str]]:
    parts = re.split(r"(?m)^(## .*)$", md)
    return [(parts[i], parts[i + 1]) for i in range(1, len(parts), 2)]


def pick(src: str, names: list[str]) -> str:
    """Исходный текст функций и констант с этими именами — в том порядке, как в файле."""
    tree = ast.parse(src)
    out = []
    for node in tree.body:
        name = getattr(node, "name", None)
        if name is None and isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        if name in names:
            out.append(ast.get_source_segment(src, node))
    return "\n\n\n".join(out)


def main() -> None:
    logic = (ROOT / "docs/logic.md").read_text(encoding="utf-8")
    secs = sections(logic)
    first = [s for s in secs if re.match(r"## [1-7]\.|## 4а\.", s[0])]
    # голосовой ввод — не по теме аудита; «известные ограничения» устарели — актуальные в 04
    rest = [s for s in secs if s not in first and not s[0].startswith(("## Содержание", "## 10.", "## 16."))]
    head = "Выдержка из docs/logic.md — как устроен совет. Часть 1: принципы, архитектура, роли, этапы, режимы, подсчёт голосов, надёжность.\n\n"
    (OUT / "01-logika-chast-1.md").write_text("# Логика совета, часть 1\n\n" + head + "".join(h + b for h, b in first),
                                              encoding="utf-8")
    cfg = (ROOT / "config.yaml").read_text(encoding="utf-8")
    (OUT / "02-logika-chast-2-i-nastroyki.md").write_text(
        "# Логика совета, часть 2, и все настройки\n\nВыдержка из docs/logic.md: вложения, уточнения, оценка стоимости, "
        "подбор состава, хранение, интерфейсы, статистика, развёртывание, известные ограничения, карта кода. "
        "В конце — config.yaml целиком (все параметры с комментариями).\n\n"
        + "".join(h + b for h, b in rest) + "\n## Приложение. config.yaml\n\n```yaml\n" + cfg + "```\n", encoding="utf-8")
    prompts = (ROOT / "council/prompts.py").read_text(encoding="utf-8")
    research = pick((ROOT / "council/research.py").read_text(encoding="utf-8"), RESEARCH_PROMPTS)
    advisor = pick((ROOT / "council/advisor.py").read_text(encoding="utf-8"), ADVISOR_PROMPTS)
    (OUT / "03-zaprosy-k-modelyam.txt").write_text(
        "Дословные запросы к моделям (код Python с шаблонами). Модели отвечают JSON по схемам.\n\n"
        "==================== council/prompts.py — обсуждение, быстрый ответ, выбор из вариантов (целиком)\n\n"
        + prompts + "\n\n==================== council/research.py — режим «Исследование» (только запросы и проверки ответа)\n\n"
        + research + "\n\n==================== council/advisor.py — «Подобрать состав» (только запросы)\n\n" + advisor + "\n",
        encoding="utf-8")
    total = 0
    for f in sorted(OUT.glob("0[1-9]-*")):
        n = len(f.read_text(encoding="utf-8"))
        total += n
        print(f"{f.name}: {n:,} символов" + ("  ← БОЛЬШЕ ЛИМИТА НА ФАЙЛ" if n > PER_FILE else ""))
    print(f"всего: {total:,}" + ("  ← БОЛЬШЕ ОБЩЕГО ЛИМИТА" if total > TOTAL else " — в лимите"))


if __name__ == "__main__":
    main()
