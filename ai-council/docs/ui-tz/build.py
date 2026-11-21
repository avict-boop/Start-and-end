"""Пересобирает вложение «02-razmetka-i-stili.md» (вёрстка страницы и дизайн-токены) и проверяет лимиты пакета:
python docs/ui-tz/build.py

Скриншоты (screens/s01…s08) снимаются в демо-режиме; 00-tz.md и 01 пишутся вручную.
Лимиты программы: 10 файлов на вопрос; текст — 60 000 символов на файл и 150 000 на все; картинки уменьшаются
до 1600 пикселей по длинной стороне.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
STATIC = ROOT / "council/web/static"


def main() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"<svg[^>]*style=\"display:\s*none\"[\s\S]*?</svg>", "<!-- набор SVG-значков опущен -->", html, count=1)
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    base_end = css.find("/* ------------------------------------------------------------ ", css.find("база") + 10)
    tokens = css[:base_end if base_end > 0 else 6000]
    text = ("# Вёрстка страницы и дизайн-токены\n\n"
            "Для разбора интерфейса: разметка единственной страницы (`council/web/static/index.html`, все экраны —"
            " скрываемые блоки; набор SVG-значков опущен) и начало таблицы стилей: цветовые токены светлой и тёмной"
            " темы, базовая типографика. Остальное оформление — в `style.css` (66 КБ), поведение — в `app.js` и"
            " `settings.js`.\n\n## style.css — токены и база\n\n```css\n" + tokens.strip() + "\n```\n\n"
            "## index.html\n\n```html\n" + html.strip() + "\n```\n")
    (OUT / "02-razmetka-i-stili.md").write_text(text, encoding="utf-8")
    files = sorted(OUT.glob("0[1-9]-*")) + sorted((OUT / "screens").glob("*.png"))
    total = sum(len(f.read_text(encoding="utf-8")) for f in files if f.suffix in (".md", ".txt"))
    for f in files:
        size = len(f.read_text(encoding="utf-8")) if f.suffix in (".md", ".txt") else f.stat().st_size
        print(f"{f.relative_to(OUT)}: {size:,}" + (" символов" if f.suffix != ".png" else " байт")
              + ("  ← БОЛЬШЕ ЛИМИТА" if f.suffix != ".png" and size > 60_000 else ""))
    print(f"файлов: {len(files)} (лимит 10) · текста: {total:,} символов (лимит 150 000)")


if __name__ == "__main__":
    main()
