"""Отчёт в HTML: основа для PDF (WeasyPrint) и просмотра в браузере.

Порядок — «сначала вывод»: решение и его обоснование на первых страницах, подробности голосования и варианты —
для тех, кто хочет проверить, полный текст запроса и условия — в конце. Три вида: полный отчёт, итог
(решение с обоснованием) и только решение.
"""
from __future__ import annotations

import base64
from functools import lru_cache
from html import escape
from pathlib import Path

from . import brand
from .view import View, md_to_html

TURQUOISE = "#00a596"   # ромб «решения» из логотипа (темнее фирменного #00C7B5 — читается при печати)
INK = "#18233d"

CSS = """
@page {
  size: A4; margin: 22mm 18mm 20mm 18mm;
  @top-left { content: "BRANDTEXT"; font: 600 7.5pt var(--font); color: #7a8396; vertical-align: bottom; padding-bottom: 4mm; }
  @top-right { content: "DOCTITLE"; font: 7.5pt var(--font); color: #7a8396; vertical-align: bottom; padding-bottom: 4mm;
               width: 62%; text-align: right; }
  @bottom-left { content: "FOOTER"; font: 7.5pt var(--font); color: #7a8396; vertical-align: top; padding-top: 4mm; }
  @bottom-right { content: "стр. " counter(page) " из " counter(pages); font: 7.5pt var(--font); color: #7a8396;
                  vertical-align: top; padding-top: 4mm; }
}
@page :first { @top-left { content: none; } @top-right { content: none; } }
:root { --font: "Inter", "DejaVu Sans", "Segoe UI", Roboto, Arial, sans-serif;
  --mono: "JetBrains Mono", "DejaVu Sans Mono", Consolas, monospace;
  --ink: #18233d; --muted: #586379; --soft: #7a8396; --line: #dfe3ec; --tint: #f5f6fa; --tint2: #eceff5;
  --accent: #4f46e5; --accent-soft: #eef0ff; --dec: TURQ; --dec-soft: #e3f6f3;
  --ok: #13773f; --warn: #8a5200; --warn-soft: #fcf1da; --bad: #b8283a; --bad-soft: #fce8ea; }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { font-family: var(--font); color: var(--ink); font-size: 9.8pt; line-height: 1.5; margin: 0; background: #fff;
  font-variant-numeric: tabular-nums; hyphens: manual; }
.md p, .md li, .md td, .lead p { hyphens: auto; }  /* переносы по слогам — только в длинном тексте */
.page { max-width: 820px; margin: 0 auto; }
a { color: var(--accent); text-decoration: none; overflow-wrap: anywhere; }
p { margin: 0 0 6pt; orphans: 3; widows: 3; }
ul, ol { margin: 0 0 6pt; padding-left: 16pt; }
li { margin: 0 0 2.5pt; }
.muted { color: var(--muted); }
.small { font-size: 8.6pt; }

/* ---------- шапка первой страницы */
.masthead { display: flex; justify-content: space-between; align-items: center; gap: 12pt; padding-bottom: 9pt;
  border-bottom: 1.5pt solid var(--ink); margin-bottom: 16pt; }
.brand-box { display: flex; align-items: center; gap: 7pt; min-width: 0; }
.brand-box { flex: none; }
.doc-kind { flex: 1 1 auto; min-width: 0; }
.brand-box img { height: 22pt; width: auto; max-width: 150pt; flex: none; }
.brand-box img.wordmark { height: 26pt; max-width: 200pt; }
.brand-name { font-weight: 700; font-size: 10pt; letter-spacing: -.01em; white-space: nowrap; }  /* название — в одну строку, целиком */
.doc-kind { text-align: right; font-size: 8.4pt; color: var(--muted); line-height: 1.35; }
.doc-kind b { display: block; color: var(--ink); font-size: 9pt; }
h1 { font-size: 19pt; line-height: 1.22; font-weight: 700; letter-spacing: -.018em; margin: 0 0 8pt;
  overflow-wrap: break-word; bookmark-level: none; }
h1.long { font-size: 15.5pt; line-height: 1.25; }
.lead { font-size: 10.4pt; color: #2c3852; margin: 0 0 12pt; }
.lead p { margin: 0 0 4pt; }
.lead .more { font-size: 8.6pt; color: var(--muted); }

/* ---------- «Коротко»: ключевые факты */
.keyfacts { width: 100%; border-collapse: separate; border-spacing: 0; margin: 4pt 0 14pt; border: 1pt solid var(--line);
  border-radius: 6pt; break-inside: avoid; }
.keyfacts td { width: 22%; vertical-align: top; padding: 8pt 10pt; border-left: 1pt solid var(--line); }
.keyfacts td:first-child { border-left: 0; width: 34%; }
.keyfacts .k { font-size: 7.6pt; color: var(--muted); font-weight: 600; letter-spacing: .02em; margin-bottom: 2pt; }
.keyfacts .v { font-size: 10pt; font-weight: 650; line-height: 1.3; }
.keyfacts .s { font-size: 8.2pt; color: var(--muted); margin-top: 2pt; }
.keyfacts tr + tr td { border-top: 1pt solid var(--line); }
.keyfacts .dec-cell { background: var(--dec-soft); border-radius: 5pt 0 0 5pt; }
.dots { letter-spacing: 1pt; color: var(--ink); }
.dots .off { color: #c4cad6; }

/* ---------- оглавление */
.toc { margin: 0 0 6pt; break-inside: avoid; }
.toc-title { font-size: 7.6pt; color: var(--muted); font-weight: 600; letter-spacing: .02em; margin-bottom: 3pt; }
.toc ol { list-style: none; padding: 0; margin: 0; columns: 2; column-gap: 18pt; font-size: 8.8pt; }
.toc li { margin: 0 0 1.5pt; break-inside: avoid; }
.toc li.sub { padding-left: 10pt; color: var(--muted); }
.toc a { color: var(--ink); }
.toc a::after { content: leader(".") " " target-counter(attr(href), page); color: var(--soft); }

/* ---------- разделы */
h2 { font-size: 13pt; line-height: 1.3; font-weight: 700; letter-spacing: -.01em; margin: 20pt 0 8pt; padding-top: 6pt;
  border-top: 1pt solid var(--line); bookmark-level: 1; break-after: avoid; }
h2.first { border-top: 0; padding-top: 0; margin-top: 10pt; }
h3 { font-size: 10.6pt; font-weight: 700; margin: 13pt 0 5pt; bookmark-level: 2; break-after: avoid; }
h4 { font-size: 9.6pt; font-weight: 700; margin: 9pt 0 3pt; bookmark-level: none; break-after: avoid; color: #2c3852; }
#ideas > p, #facts > p, #kcu > p, #scope > p, #sources > p, #claims > p { break-after: avoid; }
#kcu h3, #kcu .callout, #scope tr, #sources tr, #claims tr { break-inside: avoid; }
#ideas tr { break-inside: avoid; }
.decision h2 .mark, .dec-mark { display: inline-block; width: 7.5pt; height: 7.5pt; background: var(--dec); transform: rotate(45deg);
  border-radius: 1.2pt; margin: 0 8pt 1.5pt 2pt; vertical-align: middle; }
.decision .answer { border-left: 2.2pt solid var(--dec); padding: 2pt 0 2pt 12pt; margin: 6pt 0 4pt; }

/* ---------- плашки-предупреждения: цвет + слово-метка (читается и в ч/б) */
.callout { border: 1pt solid var(--line); border-left: 3pt solid var(--warn); background: var(--warn-soft); border-radius: 0 5pt 5pt 0;
  padding: 6pt 10pt; margin: 6pt 0; font-size: 9pt; break-inside: avoid; }
.callout b.t { display: block; font-size: 8.4pt; margin-bottom: 1pt; }
.callout.bad { border-left-color: var(--bad); background: var(--bad-soft); }
.callout.info { border-left-color: var(--accent); background: var(--accent-soft); }

/* ---------- метки вариантов и теги */
.lbl { display: inline-block; min-width: 14pt; padding: 0 3pt; text-align: center; border: 1pt solid var(--accent); color: var(--accent);
  border-radius: 3.5pt; font-weight: 700; font-size: 8.6pt; line-height: 13pt; }
.lbl.win { background: var(--dec); border-color: var(--dec); color: #fff; }
.tag { display: inline-block; font-size: 7.6pt; font-weight: 600; border-radius: 8pt; padding: 0 6pt; margin-left: 4pt;
  border: .8pt solid var(--line); color: var(--muted); vertical-align: 1.5pt; line-height: 12pt; }
.tag.dec { border-color: var(--dec); color: var(--dec); }
.chip { display: inline-block; border: .8pt solid var(--line); border-radius: 8pt; padding: 0 7pt; margin: 0 3pt 3pt 0; font-size: 8.6pt; }

/* ---------- таблицы отчёта */
table { border-collapse: collapse; width: 100%; margin: 6pt 0 8pt; font-size: 8.8pt; }
thead { display: table-header-group; }
th, td { text-align: left; vertical-align: top; padding: 4pt 6pt; border-bottom: .7pt solid var(--line); }
th { font-weight: 650; color: var(--muted); font-size: 8pt; border-bottom: 1.1pt solid #b9c0cf; }
tr { break-inside: avoid; }
td.num, th.num { text-align: right; white-space: nowrap; }
tr.win td { background: var(--dec-soft); }
tfoot td, tr.total td { font-weight: 700; border-top: 1.1pt solid #b9c0cf; }

/* ---------- голосование: график */
.vchart { margin: 6pt 0 10pt; }
.vchart td { border: 0; padding: 3pt 4pt; vertical-align: middle; }
.vchart .place { width: 16pt; color: var(--muted); font-size: 8.4pt; text-align: right; }
.vchart .who { width: 48%; }
.vchart .who .t { font-weight: 600; }
.vchart .who .a { color: var(--muted); font-size: 8.2pt; }
.vchart .bar { height: 7pt; background: var(--tint2); border-radius: 3.5pt; overflow: hidden; }
.vchart .bar i { display: block; height: 7pt; background: #9aa3b6; border-radius: 3.5pt; }
.vchart tr.win .bar i { background: var(--dec); }
.vchart .val { width: 70pt; text-align: right; font-size: 8.2pt; color: var(--muted); white-space: nowrap; }
.notes { list-style: none; padding: 0; margin: 4pt 0 8pt; font-size: 8.8pt; color: #2c3852; }
.notes li { padding-left: 10pt; text-indent: -10pt; margin-bottom: 3pt; }
.notes li::before { content: "–  "; color: var(--soft); }
.method { font-size: 8.2pt; color: var(--muted); }

/* ---------- варианты */
.proposal { margin: 10pt 0 14pt; }
.proposal > h3 { font-size: 11.2pt; margin: 14pt 0 3pt; padding-top: 8pt; border-top: .7pt dashed var(--line); }
.proposal.win > h3 { border-top: 1.4pt solid var(--dec); }
.proposal .sub { font-size: 8.4pt; color: var(--muted); margin-bottom: 6pt; }
.cols { width: 100%; margin: 4pt 0; }
.cols td { width: 50%; border: 0; padding: 0 8pt 0 0; vertical-align: top; }
.box { background: var(--tint); border-radius: 5pt; padding: 6pt 9pt; margin: 6pt 0; font-size: 9pt; break-inside: avoid; }
.initial { border-left: 2pt solid var(--line); padding: 2pt 0 2pt 10pt; margin: 8pt 0; color: #3a4560; }
.reviews td { font-size: 8.4pt; }
.reviews .plus::before { content: "+ "; color: var(--ok); font-weight: 700; }
.reviews .minus::before { content: "− "; color: var(--bad); font-weight: 700; }
.reviews ul { margin: 0; padding-left: 0; list-style: none; }
.matrix td.cell, .matrix th.cell { text-align: center; }
.matrix td.cell { font-weight: 650; }

/* ---------- запрос и вложения */
.query { background: var(--tint); border-radius: 5pt; padding: 8pt 11pt; margin: 4pt 0 8pt; font-size: 9.2pt; }
.query p { margin: 0 0 4pt; }
.imgs { margin: 6pt 0; }
.imgs figure { display: inline-block; margin: 0 6pt 6pt 0; border: .7pt solid var(--line); border-radius: 5pt; padding: 4pt; max-width: 48%;
  vertical-align: top; break-inside: avoid; }
.imgs img { max-width: 100%; max-height: 200pt; display: block; }
.imgs figcaption { font-size: 7.8pt; color: var(--muted); margin-top: 2pt; }
.kv td { border: 0; padding: 2pt 8pt 2pt 0; }
.kv td:first-child { width: 120pt; color: var(--muted); }

/* ---------- содержимое ответов моделей (Markdown) */
.md { overflow-wrap: break-word; }
.md h1, .md h2, .md h3, .md h4, .md h5, .md h6 { bookmark-level: none; border: 0; padding: 0; letter-spacing: 0; color: var(--ink); }
.md h1, .md h2 { font-size: 11pt; margin: 11pt 0 4pt; }
.md h3 { font-size: 10pt; margin: 9pt 0 3pt; }
.md h4, .md h5, .md h6 { font-size: 9.6pt; margin: 8pt 0 2pt; }
.md blockquote { margin: 6pt 0; padding: 2pt 0 2pt 10pt; border-left: 2.2pt solid var(--accent-soft); color: #2c3852; }
.md code { font-family: var(--mono); font-size: 8.4pt; background: var(--tint); border-radius: 2.5pt; padding: 0 2.5pt; overflow-wrap: anywhere; }
.md pre { font-family: var(--mono); background: var(--tint); border-left: 2pt solid #c8ceda; border-radius: 0 4pt 4pt 0; padding: 6pt 9pt;
  font-size: 8.1pt; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; margin: 6pt 0; }
.md pre code { background: none; padding: 0; font-size: inherit; }
.md hr { border: 0; border-top: .7pt solid var(--line); margin: 10pt 0; }
.md img { max-width: 100%; }
.md table { font-size: 8.6pt; }
.md tbody tr:nth-child(even) td { background: #fafbfd; }
table.mdt th, table.mdt td { hyphens: auto; overflow-wrap: break-word; }
table.mdt.wide { font-size: 7.8pt; }
table.mdt.wide th, table.mdt.wide td { padding: 3pt 4pt; }
table.mdt.xwide { font-size: 6.9pt; }
table.mdt.xwide th, table.mdt.xwide td { padding: 2pt 3pt; }
.math { font-family: "DejaVu Serif", "Times New Roman", serif; font-style: italic; white-space: nowrap; }
.math .mt { font-style: normal; }
.math-block { font-family: "DejaVu Serif", "Times New Roman", serif; font-style: italic; text-align: center; margin: 7pt 0; font-size: 10.4pt; }
sup, sub { font-size: 70%; line-height: 0; }

/* ---------- печать и PDF */
@media print {
  table.mdt td { overflow-wrap: anywhere; }
  table.mdt th { overflow-wrap: normal; word-break: normal; }
  .noprint { display: none !important; }
  a[href^="http"] { color: var(--accent); }
}

/* ---------- экран (браузер, телефон) */
@media screen {
  html { background: #eef0f5; }
  body { font-size: 15px; background: #eef0f5; }
  .page { background: #fff; padding: 40px 48px 56px; margin: 24px auto 40px; border-radius: 14px; box-shadow: 0 2px 18px rgb(24 35 61 / .08); }
  h1 { font-size: 28px; } h2 { font-size: 20px; } h3 { font-size: 16px; } h4 { font-size: 14.5px; }
  .md h1, .md h2 { font-size: 17px; } .md h3 { font-size: 15.5px; }
  .tbl { overflow-x: auto; max-width: 100%; margin: 8px 0; -webkit-overflow-scrolling: touch; }
  .tbl > table { margin: 0; width: auto; min-width: 100%; }
  table.mdt th, table.mdt td { min-width: 7em; max-width: 26em; }
  table.mdt.wide, table.mdt.xwide { font-size: 13px; }
  table { font-size: 13.5px; }
  .keyfacts .v { font-size: 15px; } .keyfacts .k, .keyfacts .s { font-size: 12.5px; }
  .tbl.more-right { box-shadow: inset -16px 0 14px -14px rgba(0, 0, 0, .35); }
  .tbl.more-left { box-shadow: inset 16px 0 14px -14px rgba(0, 0, 0, .35); }
  .tbl.more-left.more-right { box-shadow: inset -16px 0 14px -14px rgba(0,0,0,.35), inset 16px 0 14px -14px rgba(0,0,0,.35); }
  .toc a::after { content: none; }
}
@media screen and (max-width: 640px) {
  .page { padding: 20px 16px 32px; margin: 0; border-radius: 0; box-shadow: none; }
  h1 { font-size: 23px; }
  .masthead { flex-wrap: wrap; }
  .keyfacts, .keyfacts tbody, .keyfacts tr, .keyfacts td { display: block; width: 100% !important; }
  .keyfacts td { border-left: 0; border-top: 1px solid var(--line); }
  .keyfacts tr:first-child td:first-child { border-top: 0; }
  .toc ol { columns: 1; }
  .cols, .cols tbody, .cols tr, .cols td { display: block; width: 100% !important; }
  .vchart .who { width: auto; }
  .vchart td:nth-child(3) { width: 26%; min-width: 64px; }
  .vchart .val { width: auto; font-size: 12px; }
  /* таблицы отчёта шире экрана — прокручиваются внутри себя, страница не уезжает вбок */
  section table:not(.vchart):not(.cols):not(.kv):not(.mdt), .page > table:not(.keyfacts):not(.vchart):not(.cols) {
    display: block; overflow-x: auto; -webkit-overflow-scrolling: touch; }
}
.toolbar { position: sticky; top: 0; background: rgba(255,255,255,.96); border-bottom: 1px solid var(--line);
  padding: 8px 20px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; z-index: 2; font-size: 14px; }
.toolbar a, .toolbar button { font: inherit; font-size: 13.5px; border: 1px solid var(--line); border-radius: 9px; padding: 6px 12px;
  background: #fff; color: var(--ink); text-decoration: none; cursor: pointer; }
.toolbar .sp { flex: 1; }
[id] { scroll-margin-top: 64px; }
"""

# В браузере: тень у края широкой таблицы, если её можно прокрутить. В PDF не используется.
SCROLL_HINT = """<script>
function markScroll(el) {
  const max = el.scrollWidth - el.clientWidth;
  el.classList.toggle("more-right", max > 1 && el.scrollLeft < max - 1);
  el.classList.toggle("more-left", max > 1 && el.scrollLeft > 1);
}
function markAll() { document.querySelectorAll(".tbl").forEach(markScroll); }
document.addEventListener("scroll", (e) => { if (e.target.classList && e.target.classList.contains("tbl")) markScroll(e.target); }, true);
window.addEventListener("resize", markAll); markAll();
</script>"""

KIND = {"full": "Отчёт о заседании", "summary": "Итог заседания", "decision": "Решение совета"}
DECISION_TITLE = "Решение совета"
AGREE_WORD = {"high": "Высокое", "medium": "Среднее", "low": "Низкое"}
AGREE_SENT = {"high": "Мнения моделей в основном совпадают.", "medium": "Мнения моделей частично расходятся.",
              "low": "Мнения моделей разошлись — смотрите особые мнения."}
AGREE_DOTS = {"high": 3, "medium": 2, "low": 1}
FACT_WORD = {"confirmed": "Подтверждено", "refuted": "Опровергнуто", "unclear": "Не проверено", "user_doc": "Из документов"}
FACT_SIGN = {"confirmed": "✓", "refuted": "✕", "unclear": "?", "user_doc": "▤"}
SUMMARY_NOTE = ("Это краткая версия: решение и его обоснование. Полный отчёт с вариантами всех моделей, критикой "
                "и голосованием скачивается отдельно.")


def _e(x: object) -> str:
    return escape(str(x if x is not None else ""))


def _inline(text: str) -> str:
    """Короткий текст (пункт списка, ячейка): Markdown без обёртки в <p>."""
    h = md_to_html(text).strip()
    if h.startswith("<p>") and h.endswith("</p>") and h.count("<p>") == 1:
        h = h[3:-4]
    return h


def _list(items: list[str], cls: str = "") -> str:
    if not items:
        return ""
    return f'<ul class="{cls}">' + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>"


def _plural(n: int, one: str, few: str, many: str) -> str:
    m10, m100 = n % 10, n % 100
    return one if m10 == 1 and m100 != 11 else few if 2 <= m10 <= 4 and not 12 <= m100 <= 14 else many


@lru_cache(maxsize=1)
def _mark_url() -> str:
    """Знак «Совета нейросетей» для шапки, если своего логотипа нет."""
    p = Path(__file__).resolve().parent.parent / "web" / "static" / "brand" / "icon.svg"
    try:
        return "data:image/svg+xml;base64," + base64.b64encode(p.read_bytes()).decode("ascii")
    except OSError:
        return ""


def _accent_soft(hex_color: str) -> str:
    """Очень светлый оттенок цвета (для фона плашек) — смешение с белым на 92%."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    mix = lambda c: round(c + (255 - c) * .92)  # noqa: E731
    return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"


def _theme() -> dict[str, str]:
    """Цвета отчёта: фирменный цвет из настроек — акцент; без своего цвета ромб решения бирюзовый, как в логотипе."""
    b = brand.current()
    custom = b.color.lower() != brand.DEFAULT_COLOR.lower()
    return {"accent": b.color, "accent_soft": _accent_soft(b.color),
            "dec": b.color if custom else TURQUOISE, "dec_soft": _accent_soft(b.color) if custom else "#e3f6f3"}


# ------------------------------------------------------------------ части отчёта

def _masthead(v: View, kind: str) -> str:
    b = brand.current()
    # длинное название — мельче, но всегда целиком и в одну строку (≈0,55 кегля на знак, место — около 110 мм)
    size = max(7.0, min(10.0, 110 / (max(len(b.title), 1) * 0.55 * 0.3528)))
    style = f' style="font-size:{size:.1f}pt"' if size < 10 else ""
    name = f'<span class="brand-name"{style}>{_e(b.title)}</span>'
    if b.logo_png and b.logo_is_wordmark:
        logo, name = f'<img class="wordmark" src="{b.logo_data_url}" alt="{_e(b.title)}">', ""
    elif b.logo_png:
        logo = f'<img src="{b.logo_data_url}" alt="Логотип">'
    elif b.title == brand.DEFAULT_TITLE and _mark_url():
        logo = f'<img src="{_mark_url()}" alt="Знак «Совета нейросетей»">'
    else:
        logo = ""
    return (f'<header class="masthead"><div class="brand-box">{logo}{name}</div>'
            f'<div class="doc-kind"><b>{KIND[kind]}</b>{_e(v.date)}</div></header>'
            f'')


def _h1(v: View) -> str:
    """Заголовок отчёта; длинный (больше ~90 знаков) — чуть мельче, чтобы не занимать полстраницы."""
    t = v.display_title
    return f'<h1 class="long">{_e(t)}</h1>' if len(t) > 90 else f"<h1>{_e(t)}</h1>"


def _lead(v: View, *, refer: bool = True) -> str:
    """Под заголовком: сам вопрос (если он короткий) или его начало со ссылкой на полный текст."""
    parts = []
    if v.followup_title:
        parts.append(f'<p class="more">Уточнение к заседанию «{_e(v.followup_title)}»</p>')
    if v.question_is_prompt:
        parts.append(f'<div class="md">{md_to_html(v.question_excerpt(), breaks=True)}</div>')
        if refer:
            parts.append('<p class="more">Полный текст запроса — в разделе <a href="#request">«Запрос и условия»</a>.</p>')
    elif v.question_is_long:
        rest = v.question_rest
        if rest:
            parts.append(f'<div class="md">{md_to_html(rest, breaks=True)}</div>')
    return f'<div class="lead">{"".join(parts)}</div>' if parts else ""


def _first_places(v: View, label: str) -> tuple[int, int, int | None]:
    row = next((r for r in v.tally if r.get("label") == label), {})
    return int(row.get("first_places") or 0), int(row.get("ballots") or 0), row.get("schulze")


def _keyfacts(v: View, *, full: bool) -> str:
    w = v.winner
    if w:
        who = "ваш вариант" if v.author(w) == "пользователь" else f"предложил {_e(v.author(w))}"
        dec = (f'<div class="k">Решение</div><div class="v"><span class="dec-mark"></span>{_e(v.title(w))}</div>'
               f'<div class="s">вариант {_e(w)} · {who}</div>')
    else:
        dec = '<div class="k">Решение</div><div class="v">Итог председателя</div>'
    a = v.agreement
    if a:
        n = AGREE_DOTS[a["level"]]
        dots = '<span class="dots">' + "●" * n + f'<span class="off">{"●" * (3 - n)}</span></span>'
        agree = (f'<div class="k">Согласие совета</div><div class="v">{AGREE_WORD[a["level"]]}</div>'
                 f'<div class="s">{dots} &nbsp;τ = {a["tau"]:.2f}</div>')
    else:
        agree = '<div class="k">Согласие совета</div><div class="v">—</div>'
    fp, nb, sch = _first_places(v, w) if w else (0, 0, None)
    if nb:
        beats = ("выигрывает у всех в прямых сравнениях" if sch is not None and sch == len(v.tally) - 1
                 else f"{len(v.tally)} {_plural(len(v.tally), 'вариант', 'варианта', 'вариантов')} в голосовании")
        votes = f'<div class="k">Первых мест</div><div class="v">{fp} из {nb}</div><div class="s">{beats}</div>'
    else:
        votes = f'<div class="k">Вариантов</div><div class="v">{len(v.proposals)}</div>'
    n = len(v.members)
    council = (f'<div class="k">Совет</div><div class="v">{n} {_plural(n, "модель", "модели", "моделей")}</div>'
               f'<div class="s">{_e(v.mode)}</div>')
    rows = [f'<tr><td class="dec-cell">{dec}</td><td>{agree}</td><td>{votes}</td><td>{council}</td></tr>']
    if full:
        rows.append(f'<tr><td><div class="k">Дата</div><div class="v">{_e(v.date)}</div></td>'
                    f'<td><div class="k">Длительность</div><div class="v">{_e(v.duration)}</div></td>'
                    f'<td><div class="k">Стоимость</div><div class="v">${v.cost:.2f}</div></td>'
                    f'<td><div class="k">Вариантов</div><div class="v">{len(v.proposals)}</div></td></tr>')
    return f'<table class="keyfacts" role="presentation"><tbody>{"".join(rows)}</tbody></table>'


def _callouts(v: View) -> str:
    out = []
    if v.tie_line:
        out.append(("", "Ничья за первое место", v.tie_line))
    if v.deviation_line:
        out.append(("bad", "Председатель выбрал не лидера голосования", v.deviation_line))
    if v.agreement_low:
        out.append(("", "Низкое согласие совета", v.agreement_line))
    refuted = [c for r in v.fact_check.values() for c in r.get("claims", []) if c.get("status") == "refuted"]
    if refuted:
        out.append(("bad", "Проверка фактов нашла ошибки",
                    f"Опровергнуто утверждений: {len(refuted)}. Подробности — в разделе «Проверка фактов»."))
    return "".join(f'<div class="callout {k}"><b class="t">{_e(t)}</b>{_e(x)}</div>' for k, t, x in out)


def _decision(v: View, *, first: bool = True) -> str:
    vd = v.verdict
    return (f'<section class="decision" id="verdict"><h2 class="{"first" if first else ""}"><span class="mark"></span>{"Итог исследования" if v.is_research else "Решение совета"}</h2>'
            f'{_callouts(v)}<div class="answer md">{md_to_html(vd.get("final_answer", ""))}</div></section>')


def _rationale(v: View) -> str:
    vd = v.verdict
    if not (vd.get("why") or vd.get("risks") or vd.get("dissent")):
        return ""
    parts = ['<section id="why"><h2>Обоснование</h2>']
    if vd.get("why"):
        parts.append(f'<h3>Почему совет так решил</h3><div class="md">{md_to_html(vd["why"])}</div>')
    if vd.get("risks"):
        parts.append("<h3>Риски</h3>" + _list(vd["risks"]))
    if vd.get("dissent"):
        parts.append("<h3>Особые мнения</h3>" + _list(vd["dissent"]))
    return "".join(parts) + "</section>"


IDEAS_INTRO = ("Совет не только выбирает лучший вариант, но и собирает ценное из остальных. Критики отметили, что стоит "
               "сохранить из вариантов, не ставших основой решения, а председатель решил по каждой идее: включить её "
               "в ответ или отклонить с причиной.")


def _ideas(v: View) -> str:
    ideas = v.ideas
    if not ideas:
        return ""
    rows = []
    for i in ideas:
        st = i["decision"]
        color = {"included": "var(--ok)", "rejected": "var(--bad)"}.get(st, "var(--muted)")
        by = ", ".join(i.get("by") or [])
        reason = i.get("reason") or ("председатель не ответил по этой идее" if st == "not_addressed" else "")
        rows.append(f'<tr><td style="color:{color}"><b>{v.IDEA_SIGN[st]} {v.IDEA_WORD[st]}</b></td>'
                    f'<td>{_inline(i["idea"])}<div class="muted small">вариант <span class="lbl">{_e(i.get("label", ""))}</span>'
                    f'{" · отметил: " + _e(by) if by else ""}</div></td><td>{_inline(reason)}</td></tr>')
    return (f'<section id="ideas"><h2>Что взято из других вариантов</h2><p class="muted small">{IDEAS_INTRO} '
            f'Итого — {_e(v.ideas_line)}.</p>'
            '<table><thead><tr><th style="width:17%">Решение</th><th style="width:43%">Идея</th>'
            '<th>Как использована или почему отклонена</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></section>")


# ------------------------------------------------------------------ режим «Исследование»

CLAIM_COLOR = {"confirmed": "var(--ok)", "refuted": "var(--bad)", "contested_in_sources": "var(--warn)"}


def _eref(v: View, ids: list[str]) -> str:
    """[E1] [E3] — ссылки на источники в разделе «Источники»; DOC — данные пользователя."""
    out = []
    for i in ids:
        if i == "DOC":
            out.append('<span class="lbl">DOC</span>')
        elif i in v.evidence_by_id:
            out.append(f'<a class="eref" href="#src-{_e(i)}">[{_e(i)}]</a>')
    return " ".join(out)


def _kcu(v: View) -> str:
    """Известно / Спорно / Неизвестно — главное в исследовании."""
    if not v.is_research:
        return ""
    parts = ['<section id="kcu"><h2>Что известно, что спорно, чего не знает никто</h2>',
             '<p class="muted small">«Известно» — только то, что подтвердили найденные источники или ваши документы; '
             'согласие моделей и цифры «из памяти» сюда не попадают.</p>']
    known = v.known
    parts.append('<h3>✓ Известно</h3>' + ('<table><thead><tr><th>Утверждение</th><th style="width:22%">Рамки</th>'
                 '<th style="width:16%">Источники</th></tr></thead><tbody>' + "".join(
                     f'<tr><td>{_inline(k.get("statement", ""))}'
                     + ('<div class="muted small">быстро меняется — проверьте дату</div>' if k.get("volatility") == "changes_fast" else "")
                     + f'</td><td class="small">{_inline(k.get("scope", ""))}</td><td>{_eref(v, k.get("evidence_ids") or [])}</td></tr>'
                     for k in known) + "</tbody></table>" if known else '<p class="muted">Подтверждённых источниками утверждений нет.</p>'))
    cont = v.contested
    if cont:
        parts.append("<h3>⇄ Спорно</h3>")
        for c in cont:
            pos = _list(c.get("positions") or [])
            meta = [f"<b>Почему расходятся:</b> {_inline(c['why_they_differ'])}" if c.get("why_they_differ") else "",
                    f"<b>Что решило бы спор:</b> {_inline(c['what_would_resolve'])}" if c.get("what_would_resolve") else "",
                    _eref(v, c.get("evidence_ids") or [])]
            parts.append(f'<div class="callout"><b class="t">{_inline(c.get("issue", ""))}</b>{pos}'
                         + "".join(f'<p class="small">{m}</p>' for m in meta if m) + "</div>")
    unk = v.unknown
    if unk:
        reason = v.GAP_WORD
        parts.append('<h3>? Неизвестно</h3><table><thead><tr><th>Чего не знаем</th><th style="width:22%">Почему</th>'
                     '<th style="width:30%">Как узнать</th></tr></thead><tbody>' + "".join(
                         f'<tr><td>{_inline(u.get("gap", ""))}</td><td class="small">{_e(reason.get(u.get("reason", ""), u.get("reason", "")))}</td>'
                         f'<td class="small">{_inline(u.get("how_to_check", ""))}</td></tr>' for u in unk) + "</tbody></table>")
    wwc = v.verdict.get("what_would_change") or []
    if wwc:
        parts.append("<h3>Что изменило бы выводы</h3>" + _list(wwc))
    nq = v.verdict.get("next_questions") or []
    if nq:
        parts.append("<h3>Что исследовать дальше</h3>" + _list(nq))
    return "".join(parts) + "</section>"


def _scope(v: View) -> str:
    """Рамки исследования и покрытие подвопросов (с каркасом раздела по голосованию)."""
    if not v.is_research or not v.subquestions:
        return ""
    rows = []
    for r in v.coverage_rows:
        st = r["status"]
        color = {"covered": "var(--ok)", "partial": "var(--warn)", "unknown": "var(--bad)"}.get(st, "var(--muted)")
        votes = ", ".join(f"{l}: {n}" for l, n in sorted(r["counts"].items(), key=lambda x: -x[1]))
        rows.append(f'<tr><td><span class="lbl">{_e(r["id"])}</span> {_inline(r["text"])}</td>'
                    f'<td style="color:{color}"><b>{_e(v.COVER_WORD.get(st, st or "—"))}</b>'
                    f'{"<div class=\"muted small\">" + _inline(r["note"]) + "</div>" if r["note"] else ""}</td>'
                    f'<td>{"<span class=\"lbl\">" + _e(r["backbone"]) + "</span>" if r["backbone"] else "—"}'
                    f'{"<div class=\"muted small\">голоса: " + _e(votes) + "</div>" if votes else ""}</td></tr>')
    scope = f'<p><b>Рамки:</b> {_e(v.scope_line)}</p>' if v.scope_line else ""
    return (f'<section id="scope"><h2>Рамки и подвопросы</h2>{scope}'
            '<table><thead><tr><th>Подвопрос</th><th style="width:20%">В итоге</th><th style="width:22%">Каркас раздела</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table><p class="muted small">Каркас раздела — вариант, '
            'который участники сочли лучшим по этому подвопросу; это выбор основы текста, а не проверка фактов.</p></section>')


def _sources(v: View) -> str:
    ev = v.evidence
    if not v.is_research or not ev:
        return ""
    rows = []
    for e in ev:
        meta = ", ".join(_e(x) for x in (e.get("publisher"), e.get("date"),
                                         {"primary": "первичный", "secondary": "пересказ"}.get(e.get("source_type", ""), "")) if x)
        seen = {"page": "страница", "snippet": "фрагмент", "search_answer": "ответ поиска"}.get(e.get("material_seen", ""), "")
        flag = ('<div class="small" style="color:var(--bad)">нет в результатах поиска — не подтверждение</div>'
                if e.get("search_returned") is False else "")
        url = v.safe_url(e.get("url", ""))
        rows.append(f'<tr id="src-{_e(e["id"])}"><td><b>{_e(e["id"])}</b></td><td>{_e(e.get("title") or url)}'
                    f'<div class="small"><a href="{_e(url)}">{_e(url)}</a></div>'
                    f'{"<div class=\"muted small\">" + meta + "</div>" if meta else ""}{flag}</td>'
                    f'<td class="small">{_e(", ".join(e.get("claims") or []))}{"<div class=\"muted\">" + seen + "</div>" if seen else ""}</td></tr>')
    return (f'<section id="sources"><h2>Источники</h2><p class="muted small">Нашла поисковая модель '
            f'({_e(v.ledger.get("model", ""))}) на этапе арбитража доказательств — {_e(v.ledger_line)}.</p>'
            '<table><thead><tr><th style="width:7%">№</th><th>Источник</th><th style="width:22%">Утверждения</th>'
            '</tr></thead><tbody>' + "".join(rows) + "</tbody></table></section>")


def _claims_check(v: View) -> str:
    claims = v.checked_claims
    if not v.is_research or not claims:
        return ""
    rows = []
    for c in claims:
        st = c.get("status", "not_checked")
        rows.append(f'<tr><td><span class="lbl">{_e(c.get("id", ""))}</span></td><td>{_inline(c.get("text", ""))}'
                    f'<div class="muted small">{_e(v.BASIS_WORD.get(c.get("basis", ""), ""))}'
                    f'{" · критики спорили: " + str(c["disputed"]) if c.get("disputed") else ""}</div>'
                    + "".join(f'<div class="small">{_inline(n)}</div>' for n in c.get("notes") or [])
                    + f'</td><td style="color:{CLAIM_COLOR.get(st, "var(--muted)")}"><b>{v.CLAIM_SIGN.get(st, "")} '
                    f'{_e(v.CLAIM_WORD.get(st, st))}</b><div>{_eref(v, c.get("evidence") or [])}</div></td></tr>')
    return ('<section id="claims"><h2>Проверка утверждений</h2><p class="muted small">Утверждения всех вариантов, '
            'которые совет счёл важными или спорными, проверены поиском двумя заданиями: найти подтверждения и найти '
            'опровержения.</p><table><thead><tr><th style="width:9%">№</th><th>Утверждение</th>'
            '<th style="width:22%">Статус</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table></section>")


def _facts(v: View) -> str:
    if not v.fact_check:
        return ""
    c = v.fact_counts
    summary = " · ".join(f"{FACT_WORD[k].lower()}: {c[k]}" for k in ("confirmed", "refuted", "unclear", "user_doc") if c[k])
    parts = [f'<section id="facts"><h2>Проверка фактов</h2><p class="muted small">Модель с поиском в интернете '
             f'({_e(v.d.get("fact_checker", ""))}) проверила ключевые утверждения лидирующих вариантов — {summary}.</p>']
    for label, r in v.fact_check.items():
        parts.append(f'<h3><span class="lbl">{_e(label)}</span> {_e(v.title(label))}</h3>')
        if r.get("summary"):
            parts.append(f'<p class="muted">{_inline(r["summary"])}</p>')
        parts.append('<table><thead><tr><th style="width:21%">Статус</th><th>Утверждение и что показали источники</th></tr></thead><tbody>')
        for cl in r.get("claims", []):
            st = cl.get("status", "unclear")
            links = [u for u in map(v.safe_url, cl.get("sources") or []) if u][:3]
            src = "<br>".join(f'<a href="{_e(u)}">{_e(u)}</a>' for u in links)
            color = {"refuted": "var(--bad)", "confirmed": "var(--ok)"}.get(st, "var(--muted)")
            parts.append(f'<tr><td><b style="color:{color}">{FACT_SIGN.get(st, "?")} {FACT_WORD.get(st, st)}</b></td>'
                         f'<td>{_inline(cl.get("claim", ""))}'
                         + (f'<div class="small muted">{_inline(cl["note"])}</div>' if cl.get("note") else "")
                         + (f'<div class="small">{src}</div>' if src else "") + "</td></tr>")
        parts.append("</tbody></table>")
    return "".join(parts) + "</section>"


def _vote_lead(v: View) -> str:
    t = v.tally
    if not t:
        return ""
    n = len(t)
    top = t[0]
    txt = f"Модели ранжировали {n} {_plural(n, 'вариант', 'варианта', 'вариантов')}, не зная авторов. "
    if len(v.top_tie) > 1:
        txt += f"Первое место поделили варианты {', '.join(v.top_tie)}. "
    elif top.get("ballots"):
        txt += f"Вариант {top.get('label')} поставили первым {top.get('first_places', 0)} из {top.get('ballots')}"
        txt += ("; он выигрывает у каждого соперника один на один. " if top.get("schulze") == n - 1 else ". ")
    if v.agreement:
        txt += AGREE_SENT[v.agreement["level"]]
    return f"<p>{_e(txt)}</p>"


def _vote_chart(v: View) -> str:
    if not v.tally:
        return ""
    w = v.winner
    mx = max((float(r.get("borda") or 0) for r in v.tally), default=1) or 1
    rows = []
    for i, r in enumerate(v.tally, 1):
        l = r.get("label", "")
        bord = float(r.get("borda") or 0)
        rows.append(f'<tr class="{"win" if l == w else ""}"><td class="place">{i}</td>'
                    f'<td class="who"><span class="lbl{" win" if l == w else ""}">{_e(l)}</span> <span class="t">{_e(v.title(l))}</span>'
                    f'<div class="a">{_e(v.author(l))}{" · решение" if l == w else ""}</div></td>'
                    f'<td><div class="bar"><i style="width:{max(2.0, bord / mx * 100):.0f}%"></i></div></td>'
                    f'<td class="val">{r.get("first_places", 0)} из {r.get("ballots", 0)} первых<br>балл {bord:.2f}</td></tr>')
    return f'<table class="vchart" aria-label="Итоги голосования"><tbody>{"".join(rows)}</tbody></table>'


def _reliability(v: View) -> str:
    notes = []
    if v.agreement_line:
        notes.append(f"Согласие совета: {v.agreement_line}.")
    if v.coverage_line:
        notes.append(v.coverage_line + ".")
    if v.decisive_notes:
        notes.append("Решающие голоса (лидер меняется без одного бюллетеня): " + "; ".join(v.decisive_notes) + ".")
    if v.flag_notes:
        notes.append("Бюллетени против общего мнения (голоса учтены, это только пометка): " + "; ".join(v.flag_notes)
                     + ". " + v.robust_note)
    if v.position_line:
        notes.append(v.position_line + ".")
    notes += v.tie_notes
    return ('<h3>Насколько надёжен результат</h3><ul class="notes">' + "".join(f"<li>{_e(n)}</li>" for n in notes) + "</ul>"
            if notes else "")


def _voting(v: View) -> str:
    w = v.winner
    parts = ['<section id="voting"><h2>Голосование</h2>', _vote_lead(v), _vote_chart(v), _reliability(v)]
    if v.ballots:
        parts.append('<h3>Бюллетени</h3><table><thead><tr><th style="width:18%">Участник</th><th style="width:22%">Порядок</th>'
                     "<th>Почему</th></tr></thead><tbody>")
        for voter, b in v.ballots.items():
            parts.append(f"<tr><td>{_e(voter)}</td><td><b>{_e(' › '.join(b.get('ranking', [])))}</b></td>"
                         f"<td>{_inline(b.get('rationale', ''))}</td></tr>")
        parts.append("</tbody></table>")
    if v.tally:
        parts.append('<h3>Подробный подсчёт</h3><table><thead><tr><th class="num">#</th><th>Вариант</th><th>Автор</th>'
                     + ('<th class="num">Шульце</th>' if v.by_schulze else "")
                     + '<th class="num">Борда</th><th class="num">Попарно</th><th class="num">Критика</th>'
                       '<th class="num">1-х мест</th></tr></thead><tbody>')
        for i, r in enumerate(v.tally, 1):
            l = r.get("label", "")
            avg = r.get("avg_score")
            parts.append(f'<tr class="{"win" if l == w else ""}"><td class="num">{i}</td><td><span class="lbl">{_e(l)}</span></td>'
                         f"<td>{_e(v.author(l))}</td>"
                         + (f'<td class="num">{v.schulze_str(r, len(v.tally))}</td>' if v.by_schulze else "")
                         + f'<td class="num">{float(r.get("borda") or 0):.2f}</td><td class="num">{v.pairwise_str(r)}</td>'
                           f'<td class="num">{"—" if avg is None else f"{avg:.1f}"}</td>'
                           f'<td class="num">{r.get("first_places", 0)}/{r.get("ballots", 0)}</td></tr>')
        parts.append(f'</tbody></table><p class="method">{_e(v.order_note)} Борда — средняя позиция в бюллетенях от 0 до 1; '
                     "попарно — побед минус поражений в прямых сравнениях; критика — средняя оценка рецензентов (1–10).</p>")
    return "".join(parts) + "</section>"


def _proposal(v: View, l: str, p: dict) -> str:
    w = v.winner
    tags = ('<span class="tag dec">решение</span>' if l == w else "") + (
        '<span class="tag">доработан после критики</span>' if v.revised(l) else "")
    sub = [f"Автор: <b>{_e(v.author(l))}</b>"]
    if v.place(l):
        sub.append(f"место в голосовании: {v.place(l)}")
    if p.get("confidence") is not None and v.author(l) != "пользователь":
        sub.append(f"уверенность автора: {round(float(p['confidence']) * 100)}%")
    parts = [f'<article class="proposal{" win" if l == w else ""}" id="proposal-{_e(l)}">'
             f'<h3><span class="lbl{" win" if l == w else ""}">{_e(l)}</span> {_e(p.get("title", ""))}{tags}</h3>'
             f'<div class="sub">{" · ".join(sub)}</div><div class="md">{md_to_html(p.get("summary", ""))}</div>']
    if p.get("reasoning"):
        parts.append(f'<h4>Обоснование</h4><div class="md">{md_to_html(p["reasoning"])}</div>')
    if p.get("pros") or p.get("risks"):
        parts.append('<table class="cols" role="presentation"><tr>'
                     f'<td>{"<h4>Плюсы</h4>" + _list(p["pros"]) if p.get("pros") else ""}</td>'
                     f'<td>{"<h4>Риски</h4>" + _list(p["risks"]) if p.get("risks") else ""}</td></tr></table>')
    if p.get("comparison") and v.revised(l):
        parts.append(f'<h4>Сравнение с другими вариантами</h4><div class="md">{md_to_html(p["comparison"])}</div>')
    if p.get("borrowed") and v.revised(l):
        parts.append("<h4>Взято у других</h4>" + _list(p["borrowed"]))
    if p.get("not_accepted") and v.revised(l):
        parts.append("<h4>Что не принято из критики и почему</h4>" + _list(p["not_accepted"]))
    if p.get("changes"):
        pos = v.position(l)
        parts.append('<div class="box"><b>Что изменено после критики'
                     + (f" ({_e(v.POSITION_NAME.get(pos, pos))})" if pos else "") + f":</b> {_inline(p['changes'])}</div>")
    ini = v.initial(l)
    if v.revised(l) and ini:
        parts.append(f'<div class="initial"><h4>Первоначальная версия: {_e(ini.get("title", ""))}</h4>'
                     f'<div class="md">{md_to_html(ini.get("summary", ""))}</div></div>')
    reviews = v.reviews_for(l)
    if reviews:
        parts.append('<h4>Оценки других участников</h4><table class="reviews"><thead><tr><th style="width:16%">Рецензент</th>'
                     '<th style="width:24%">Оценки</th><th>Сильные и слабые стороны</th></tr></thead><tbody>')
        for reviewer, r in reviews:
            scores = "<br>".join(f"{_e(k)}: <b>{float(s):g}</b>" for k, s in (r.get("scores") or {}).items())
            pm = "".join(f'<li class="plus">{_inline(x)}</li>' for x in r.get("strengths", [])) + "".join(
                f'<li class="minus">{_inline(x)}</li>' for x in r.get("weaknesses", []))
            parts.append(f"<tr><td>{_e(reviewer)}</td><td>{scores}</td><td><ul>{pm}</ul></td></tr>")
        parts.append("</tbody></table>")
    return "".join(parts) + "</article>"


def _score_style(x: float | None) -> str:
    """Тепловая шкала оценок: светлый — низко, насыщенный — высоко (число в ячейке остаётся — читается и в ч/б)."""
    if x is None:
        return ""
    t = max(0.0, min(1.0, (x - 1) / 9))
    return f' style="background: rgba(79, 70, 229, {0.04 + t * 0.30:.2f})"'


def _critique(v: View) -> str:
    if not v.critiques:
        return ""
    labels, mrows, avgs = v.matrix()
    head = "".join(f'<th class="cell">{_e(l)}</th>' for l in labels)
    body = []
    for reviewer, vals in mrows:
        cells = "".join(f'<td class="cell muted">{"свой" if v.author(l) == reviewer else "—"}</td>' if x is None
                        else f'<td class="cell"{_score_style(x)}>{x:.1f}</td>' for l, x in zip(labels, vals))
        body.append(f"<tr><td>{_e(reviewer)}</td>{cells}</tr>")
    foot = "".join("<td></td>" if x is None else f'<td class="cell">{x:.1f}</td>' for x in avgs)
    return (f'<section id="critique"><h2>Критика</h2><p class="muted small">Средняя оценка каждого рецензента по всем '
            f'критериям, от 1 до 10. Оценки даны версиям до доработки и в подсчёте голосов не участвуют.</p>'
            f'<table class="matrix"><thead><tr><th>Рецензент</th>{head}</tr></thead><tbody>{"".join(body)}'
            f'<tr class="total"><td>Среднее</td>{foot}</tr></tbody></table></section>')


def _members(v: View) -> str:
    rows = []
    for name, s in v.stats.items():
        s = s or {}
        rt = int(s.get("reasoning_tokens") or 0)
        think = (f'<div class="muted small">рассуждения: {rt:,}</div>' if rt else "") + (
            '<div class="muted small">рассуждения выключены</div>' if s.get("reasoning_off") else "")
        rows.append(f'<tr><td>{_e(name)}{think}</td><td class="num">{s.get("calls", 0)}</td>'
                    f'<td class="num">{int(s.get("prompt_tokens", 0)):,} / {int(s.get("completion_tokens", 0)):,}</td>'
                    f'<td class="num">${float(s.get("cost", 0)):.4f}</td></tr>'.replace(",", " "))
    parts = ['<section id="members"><h2>Участники и расходы</h2><table><thead><tr><th>Участник</th><th class="num">Обращений</th>'
             '<th class="num">Токены (вход / выход)</th><th class="num">Стоимость</th></tr></thead><tbody>', "".join(rows),
             f'<tr class="total"><td colspan="3">Итого</td><td class="num">${v.cost:.4f}</td></tr></tbody></table>']
    if v.cache_line:
        parts.append(f'<p class="muted small">{_e(v.cache_line)}.</p>')
    fails = v.failures()
    if fails:
        parts.append('<h3>Сбои</h3><table><thead><tr><th style="width:18%">Участник</th><th style="width:18%">Этап</th>'
                     "<th>Что случилось</th></tr></thead><tbody>"
                     + "".join(f"<tr><td>{_e(n)}</td><td>{_e(st)}</td><td>{_e(err)}</td></tr>" for n, st, err in fails)
                     + "</tbody></table>")
    return "".join(parts) + "</section>"


def _request(v: View, *, images: bool) -> str:
    parts = ['<section id="request"><h2>Запрос и условия</h2>']
    if v.followup_title:
        parts.append(f'<p><b>Уточнение к заседанию:</b> {_e(v.followup_title)}</p>')
    parts.append(f'<div class="query md">{md_to_html(v.question, breaks=True)}</div>')
    if v.refined:
        parts.append(f'<p><span class="muted">Как председатель уточнил вопрос:</span> {_e(v.refined)}</p>')
    if v.requirements:
        parts.append('<h3>Обязательные требования</h3><ol>' + "".join(f"<li>{_e(r)}</li>" for r in v.requirements) + "</ol>")
    if v.attachments:
        items = "".join(f"<li>{_e(v.attachment_label(m))}" + (f' <span style="color:var(--warn)">— {_e(m["warning"])}</span>'
                                                               if m.get("warning") else "") + "</li>" for m in v.attachments)
        parts.append(f"<h3>Вложения</h3><ul>{items}</ul>")
        if images:
            figs = []
            for m in v.attachments:
                url = v.thumb_data_url(m) if m.get("kind") == "image" else ""
                if url:
                    figs.append(f'<figure><img src="{url}" alt="{_e(m.get("name"))}"><figcaption>{_e(m.get("name"))}</figcaption></figure>')
            if figs:
                parts.append('<div class="imgs">' + "".join(figs) + "</div>")
            if v.image_notes:
                by = v.d.get("image_notes_by") or "модель"
                parts.append(f'<p class="muted small">Описание изображений для моделей без зрения (сделал {_e(by)}):</p>'
                             + "".join(f'<p class="small"><b>{_e(n)}:</b> {_inline(t)}</p>' for n, t in v.image_notes.items()))
    kv = [("Режим", v.mode), ("Участники", ", ".join(v.members)), ("Критерии", ", ".join(v.criteria) or "—")]
    if v.chair_note:
        kv.append(("Роли", v.chair_note))
    if v.compact_note:
        kv.append(("Варианты", v.compact_note))
    kv += [("Дата", v.date), ("Длительность", v.duration), ("Стоимость", f"${v.cost:.4f}")]
    parts.append('<h3>Условия заседания</h3><table class="kv" role="presentation"><tbody>'
                 + "".join(f"<tr><td>{k}</td><td>{_e(x)}</td></tr>" for k, x in kv) + "</tbody></table>")
    return "".join(parts) + "</section>"


def _toc(v: View) -> str:
    items = [("verdict", "Итог исследования" if v.is_research else "Решение совета", "")]
    if any(v.verdict.get(k) for k in ("why", "risks", "dissent")):
        items.append(("why", "Обоснование", ""))
    if v.is_research:
        items.insert(1, ("kcu", "Известно, спорно, неизвестно", ""))
        if v.subquestions:
            items.insert(2, ("scope", "Рамки и подвопросы", ""))
    if v.ideas:
        items.append(("ideas", "Что взято из других вариантов", ""))
    if v.is_research and v.evidence:
        items.append(("sources", "Источники", ""))
    if v.is_research and v.checked_claims:
        items.append(("claims", "Проверка утверждений", ""))
    if v.fact_check:
        items.append(("facts", "Проверка фактов", ""))
    if v.tally or v.ballots:
        items.append(("voting", "Голосование", ""))
    if v.proposals:
        items.append((f"proposal-{next(iter(v.proposals))}", "Варианты", ""))
        items += [(f"proposal-{l}", f"{l}. {v.title(l)}", "sub") for l in v.proposals]
    if v.critiques:
        items.append(("critique", "Критика", ""))
    items += [("members", "Участники и расходы", ""), ("request", "Запрос и условия", "")]
    return ('<nav class="toc" aria-label="Содержание"><div class="toc-title">Содержание</div><ol>'
            + "".join(f'<li class="{c}"><a href="#{a}">{_e(t if len(t) < 70 else t[:68] + "…")}</a></li>' for a, t, c in items)
            + "</ol></nav>")


# ------------------------------------------------------------------ три вида отчёта

def render_decision_html(view: View, *, toolbar: str = "") -> str:
    """Только решение совета: сам ответ, без вопроса, обоснования, вариантов и голосования."""
    v = view
    title = DECISION_TITLE
    parts = ['<main class="page">', _masthead(v, "decision"), f"<h1>{title}</h1>",
             f'<section class="decision" id="verdict"><div class="answer md">{md_to_html(v.verdict.get("final_answer", ""))}'
             "</div></section>"]
    n = len(v.members)
    parts.append(f'<p class="muted small" style="margin-top:14pt">{_e(v.date)} · совет из {n} '
                 f'{_plural(n, "модели", "моделей", "моделей")}: {_e(", ".join(v.members))}</p></main>')
    return _document(v, "".join(parts), toolbar, title=f"{title} · {v.date}")


def render_html(view: View, *, toolbar: str = "", summary: bool = False, decision: bool = False) -> str:
    """Полный отчёт, (summary=True) итог — решение с обоснованием — или (decision=True) только решение."""
    if decision:
        return render_decision_html(view, toolbar=toolbar)
    v = view
    kind = "summary" if summary else "full"
    parts = ['<main class="page">', _masthead(v, kind), _h1(v), _lead(v),
             _keyfacts(v, full=not summary)]
    if not summary:
        parts.append(_toc(v))
    parts += [_decision(v), _kcu(v), _scope(v), _rationale(v), _ideas(v), _sources(v)]
    if not summary:
        parts.append(_claims_check(v))
    parts.append(_facts(v))
    if summary:
        parts += [_request(v, images=False), f'<p class="muted small" style="margin-top:12pt">{SUMMARY_NOTE}</p></main>']
        return _document(v, "".join(parts), toolbar)
    parts.append(_voting(v))
    if v.proposals:
        parts.append('<section id="proposals"><h2>Варианты</h2>'
                     + "".join(_proposal(v, l, p) for l, p in v.proposals.items()) + "</section>")
    parts += [_critique(v), _members(v), _request(v, images=True), "</main>"]
    return _document(v, "".join(parts), toolbar)


def _css(title: str = "") -> str:
    """CSS с фирменными цветами и текстом колонтитула."""
    b = brand.current()
    t = _theme()
    footer = (b.footer_text or b.title).replace("\\", "").replace('"', "'")
    return (CSS.replace("TURQ", t["dec"]).replace("--dec-soft: #e3f6f3", f"--dec-soft: {t['dec_soft']}")
            .replace("--accent: #4f46e5", f"--accent: {t['accent']}").replace("--accent-soft: #eef0ff", f"--accent-soft: {t['accent_soft']}")
            .replace('"FOOTER"', f'"{footer}"').replace('"BRANDTEXT"', f'"{_q(b.title)}"')
            .replace('"DOCTITLE"', f'"{_q(_short(title, 70))}"'))


def _q(text: str) -> str:
    """Текст для CSS content: без кавычек и обратной косой черты."""
    return str(text).replace("\\", "").replace('"', "'").replace("\n", " ")


def _short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def _document(v: View, body: str, toolbar: str, title: str | None = None) -> str:
    """title — заголовок документа вместо вопроса (в «Только решение» вопроса нет нигде)."""
    b = brand.current()
    t = title or v.display_title
    desc = title or v.title_text
    return (f'<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="author" content="{_e(b.title)}"><meta name="description" content="{_e(desc)}">'
            f'<meta name="generator" content="Совет нейросетей">'
            f"<title>{_e(t)} — {_e(b.title)}</title><style>{_css(t)}</style></head>"
            f"<body>{toolbar}{body}{SCROLL_HINT}</body></html>")
