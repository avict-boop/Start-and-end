"""Отчёт в HTML: основа для PDF и полноэкранного просмотра в браузере."""
from __future__ import annotations

from html import escape

from . import brand
from .view import View, md_to_html

CSS = """
@page { size: A4; margin: 18mm 16mm 18mm 16mm;
  @bottom-left { content: "Совет нейросетей"; font-size: 8pt; color: #888; }
  @bottom-right { content: counter(page) " / " counter(pages); font-size: 8pt; color: #888; } }
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { font-family: "DejaVu Sans", "Segoe UI", Roboto, Arial, sans-serif; color: #1d1d27; font-size: 10.5pt;
  line-height: 1.5; margin: 0; background: #fff; }
.page { max-width: 920px; margin: 0 auto; padding: 28px 24px 60px; }
@media print { .page { padding: 0; max-width: none; } .noprint { display: none !important; } }
h1 { font-size: 15pt; line-height: 1.3; margin: 4px 0 8px; overflow-wrap: anywhere; }
.query { border-left: 3px solid #c9c9e6; background: #f7f7fb; border-radius: 0 7px 7px 0; padding: 6px 12px; margin: 6px 0 10px; }
.query-label { font-size: 8.5pt; text-transform: uppercase; letter-spacing: .06em; color: #6b6b80; font-weight: bold; margin-bottom: 2px; }
.query .md p { margin: 3px 0; }
.atts { margin: 4px 0 10px; font-size: 9.5pt; }
.atts ul { margin: 2px 0; }
.atts .warn { color: #a05a00; }
.imgs { display: flex; flex-wrap: wrap; gap: 8px; margin: 6px 0; }
.imgs figure { margin: 0; border: 1px solid #e0e0ea; border-radius: 6px; padding: 4px; max-width: 48%; page-break-inside: avoid; }
.imgs img { max-width: 100%; max-height: 260px; display: block; }
.imgs figcaption { font-size: 8.5pt; color: #6b6b80; margin-top: 2px; }
h2 { font-size: 14pt; margin: 26px 0 10px; padding-bottom: 4px; border-bottom: 2px solid #e4e4ee; }
h3 { font-size: 11.5pt; margin: 18px 0 6px; }
h4 { font-size: 10.5pt; margin: 12px 0 4px; }
p { margin: 5px 0; }
a { color: #4545c8; }
.brand { font-size: 8.5pt; text-transform: uppercase; letter-spacing: .08em; color: #6b6b80; font-weight: bold; }
.refined { color: #555; margin-bottom: 10px; }
.meta { display: table; width: 100%; border-collapse: collapse; margin: 10px 0 4px; font-size: 9.5pt; }
.meta > div { display: table-row; }
.meta > div > span { display: table-cell; padding: 3px 8px 3px 0; vertical-align: top; }
.meta > div > span:first-child { color: #6b6b80; width: 130px; white-space: nowrap; }
.chip { display: inline-block; border: 1px solid #d8d8e6; border-radius: 10px; padding: 0 7px; margin: 0 3px 3px 0;
  font-size: 9pt; background: #f5f5fa; }
.verdict { border: 2px solid #e8b931; border-radius: 10px; padding: 12px 16px; margin: 18px 0; background: #fffcf2;
  page-break-inside: auto; }
.verdict > h2 { border: 0; margin-top: 2px; }
.label { display: inline-block; min-width: 1.7em; text-align: center; border-radius: 5px; background: #ececfd;
  color: #4545c8; font-weight: bold; padding: 0 5px; }
.muted { color: #6b6b80; }
.small { font-size: 9pt; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; font-size: 9.5pt; page-break-inside: auto; }
th, td { border: 1px solid #dcdce8; padding: 4px 7px; text-align: left; vertical-align: top; }
th { background: #f2f2f8; font-weight: bold; }
tr { page-break-inside: avoid; }
td.num { text-align: right; white-space: nowrap; }
tr.win td { background: #fff6d8; }
.bar { display: inline-block; height: 7px; background: #5b5bd6; border-radius: 3px; vertical-align: middle; margin-right: 6px; }
.proposal { border: 1px solid #e0e0ea; border-radius: 9px; padding: 10px 14px; margin: 14px 0; }
.proposal.win { border: 2px solid #e8b931; }
.proposal h2 { border: 0; margin: 2px 0 2px; }
.tag { display: inline-block; font-size: 8.5pt; border-radius: 9px; padding: 0 7px; margin-left: 4px;
  background: #e3f5ec; color: #1f7f55; font-weight: normal; }
.tag.gold { background: #fff0c2; color: #8a6400; }
.box { background: #f6f6fa; border-radius: 7px; padding: 8px 12px; margin: 8px 0; }
.initial { border-left: 3px solid #c9c9da; padding: 4px 0 4px 12px; margin: 10px 0; color: #444; }
.md pre { background: #f3f3f8; padding: 8px 10px; border-radius: 6px; white-space: pre-wrap; font-size: 9pt; }
.md code { background: #f0f0f6; padding: 0 3px; border-radius: 3px; font-family: "DejaVu Sans Mono", monospace; font-size: 9pt; }
.md pre code { background: none; padding: 0; }
.md blockquote { margin: 6px 0; padding-left: 10px; border-left: 3px solid #d0d0e0; color: #555; }
.md ul, .md ol, ul, ol { margin: 4px 0; padding-left: 22px; }
.md h1, .md h2, .md h3 { border: 0; font-size: 11.5pt; margin: 12px 0 4px; }
.cell { text-align: center; font-weight: bold; }
.review { margin: 6px 0 10px; page-break-inside: avoid; }
.plus { color: #1f7f55; } .minus { color: #b4233a; }
.toolbar { position: sticky; top: 0; background: rgba(255,255,255,.95); border-bottom: 1px solid #e4e4ee;
  padding: 8px 24px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; z-index: 2; }
.toolbar a, .toolbar button { font: inherit; font-size: 9.5pt; border: 1px solid #d8d8e6; border-radius: 7px; padding: 5px 10px;
  background: #fff; color: #1d1d27; text-decoration: none; cursor: pointer; }
.toolbar .sp { flex: 1; }
[id] { scroll-margin-top: 64px; }
.toc { columns: 2; font-size: 9.5pt; margin: 6px 0; }
.toc a { text-decoration: none; }
.toc-title { font-size: 8.5pt; text-transform: uppercase; letter-spacing: .06em; color: #6b6b80; font-weight: bold; margin-top: 8px; }
.logo { max-height: 36px; max-width: 180px; vertical-align: middle; margin-right: 8px; }
.brand-row { display: flex; align-items: center; gap: 8px; }
@media print {
  /* оглавление с номерами страниц (в PDF) */
  .toc { columns: 1; }
  .toc a { display: block; }
  .toc a::after { content: leader(".") " " target-counter(attr(href), page); color: #6b6b80; }
}
/* Широкие таблицы из ответов моделей.
   PDF (print): таблица всегда в ширину страницы — мельче шрифт, переносы по слогам и внутри длинных слов.
   Экран: таблица прокручивается внутри своего блока, а не раздвигает страницу. */
.md, .md p, .md li { overflow-wrap: anywhere; }
.md pre { overflow-wrap: anywhere; }
table.mdt th, table.mdt td { hyphens: auto; }
table.mdt.wide { font-size: 8pt; }
table.mdt.wide th, table.mdt.wide td { padding: 3px 4px; }
table.mdt.xwide { font-size: 7pt; }
table.mdt.xwide th, table.mdt.xwide td { padding: 2px 3px; }
@media print {
  table.mdt { width: 100%; table-layout: auto; }
  table.mdt td { overflow-wrap: anywhere; }
  /* заголовки колонок — только по словам и слогам: колонка получает ширину хотя бы своего названия */
  table.mdt th { overflow-wrap: normal; word-break: normal; }
}
@media screen {
  .tbl { overflow-x: auto; max-width: 100%; margin: 8px 0; -webkit-overflow-scrolling: touch; }
  .tbl > table { margin: 0; width: auto; min-width: 100%; }
  table.mdt th, table.mdt td { min-width: 7em; max-width: 26em; overflow-wrap: break-word; }
  table.mdt.wide, table.mdt.xwide { font-size: 9pt; }
  .tbl.more-right { box-shadow: inset -16px 0 14px -14px rgba(0, 0, 0, .45); }
  .tbl.more-left { box-shadow: inset 16px 0 14px -14px rgba(0, 0, 0, .45); }
  .tbl.more-left.more-right { box-shadow: inset -16px 0 14px -14px rgba(0,0,0,.45), inset 16px 0 14px -14px rgba(0,0,0,.45); }
}
@media (max-width: 640px) { .page { padding: 16px; } .toc { columns: 1; } h1 { font-size: 16pt; }
  .page > table, .verdict table:not(.mdt) { display: block; overflow-x: auto; } }
"""


# В браузере: тень у края широкой таблицы, если её можно прокрутить. При печати и в PDF не используется.
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


def _e(x: object) -> str:
    return escape(str(x if x is not None else ""))


def _score_style(v: float | None) -> str:
    if v is None:
        return ""
    t = max(0.0, min(1.0, (v - 1) / 9))
    return f' style="background: hsl({round(t * 130)}, 60%, 85%)"'


def _list(items: list[str], cls: str = "") -> str:
    if not items:
        return ""
    return f'<ul class="{cls}">' + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>"


def _inline(text: str) -> str:
    """Короткий текст (пункт списка): Markdown без обёртки в <p>."""
    h = md_to_html(text).strip()
    if h.startswith("<p>") and h.endswith("</p>") and h.count("<p>") == 1:
        h = h[3:-4]
    return h


SUMMARY_NOTE = ("Это краткая версия: только итоговое решение. Полный отчёт с вариантами всех моделей, "
                "критикой и голосованием скачивается отдельно.")


def render_html(view: View, *, toolbar: str = "", summary: bool = False) -> str:
    """Полный отчёт или (summary=True) только итоговое решение."""
    v = view
    vd = v.verdict
    w = v.winner
    parts: list[str] = []
    add = parts.append

    add('<div class="page">')
    b = brand.current()
    logo = f'<img class="logo" src="{b.logo_data_url}" alt="">' if b.logo_png else ""
    add(f'<div class="brand-row">{logo}<div class="brand">{_e(b.title)} · '
        f'{"итоговое решение" if summary else "отчёт о заседании"}</div></div>')
    add(f"<h1>{_e(v.title_text)}</h1>")
    if v.followup_title:
        add(f'<div class="refined">↳ <b>Уточнение к заседанию:</b> {_e(v.followup_title)}</div>')
    if v.question_is_long:
        add(f'<div class="query"><div class="query-label">Запрос</div><div class="md">{md_to_html(v.question, breaks=True)}</div></div>')
    if v.attachments:
        items = "".join(
            f"<li>{_e(v.attachment_label(m))}"
            + (f' <span class="warn">— {_e(m["warning"])}</span>' if m.get("warning") else "") + "</li>"
            for m in v.attachments)
        add(f'<div class="atts"><div class="query-label">Вложения ({len(v.attachments)})</div><ul>{items}</ul></div>')
        imgs = [m for m in v.attachments if m.get("kind") == "image"] if not summary else []
        figs = []
        for m in imgs:
            url = v.thumb_data_url(m)
            if url:
                figs.append(f'<figure><img src="{url}" alt="{_e(m.get("name"))}"><figcaption>{_e(m.get("name"))}</figcaption></figure>')
        if figs:
            add('<div class="imgs">' + "".join(figs) + "</div>")
        notes = v.image_notes
        if notes and not summary:
            by = v.d.get("image_notes_by") or "модель"
            add(f'<details open><summary class="small muted">Описание изображений для моделей без зрения (сделал {_e(by)})</summary>'
                + "".join(f'<div class="small"><b>{_e(n)}:</b> {_inline(t)}</div>' for n, t in notes.items())
                + "</details>")
    if v.refined:
        add(f'<div class="refined"><b>Уточнённая постановка:</b> {_e(v.refined)}</div>')
    add('<div class="meta">')
    rows = [("Дата", v.date), ("Режим", v.mode),
            ("Участники", ", ".join(v.members)),
            ("Критерии", "".join(f'<span class="chip">{_e(c)}</span>' for c in v.criteria) or "—"),
            ("Стоимость", f"${v.cost:.4f}"), ("Длительность", v.duration)]
    if summary:
        rows = rows[:3]
    for k, val in rows:
        val_html = val if k == "Критерии" else _e(val)
        add(f"<div><span>{k}</span><span>{val_html}</span></div>")
    add("</div>")

    toc = [("verdict", "Решение совета"), ("voting", "Голосование")]
    toc += [(f"proposal-{l}", f"Вариант {l}: {v.title(l)}") for l in v.proposals]
    if v.critiques:
        toc.append(("critique", "Критика"))
    toc.append(("members", "Участники и расходы"))
    if not summary:
        add('<div class="toc-title">Содержание</div><div class="toc">'
            + "".join(f'<div><a href="#{a}">{_e(t)}</a></div>' for a, t in toc) + "</div>")

    # Решение
    add('<div class="verdict" id="verdict">')
    if w and summary:
        add(f'<h2>Решение совета</h2><div class="muted small" style="margin:-4px 0 8px">Лучший вариант: '
            f'<b>{_e(v.title(w))}</b> (предложил {_e(v.author(w))})</div>')
    elif w:
        add(f'<h2>Решение совета: <span class="label">{_e(w)}</span> {_e(v.title(w))} '
            f'<span class="muted small">({_e(v.author(w))})</span></h2>')
    add(f'<div class="md">{md_to_html(vd.get("final_answer", ""))}</div>')
    if vd.get("why"):
        add(f'<h3>Почему</h3><div class="md">{md_to_html(vd["why"])}</div>')
    if vd.get("dissent"):
        add("<h3>Особые мнения</h3>" + _list(vd["dissent"]))
    if vd.get("risks"):
        add("<h3>Риски</h3>" + _list(vd["risks"]))
    add("</div>")
    if summary:
        add(f'<p class="muted small">{SUMMARY_NOTE}</p></div>')
        return _document(v, "\n".join(parts), toolbar)

    # Голосование
    add('<h2 id="voting">Голосование</h2>')
    add('<table><thead><tr><th>#</th><th>Вариант</th><th>Автор</th><th>Борда (0–1)</th>'
        '<th>Оценка критики</th><th>1-х мест</th></tr></thead><tbody>')
    for i, r in enumerate(v.tally, 1):
        l = r.get("label", "")
        avg = r.get("avg_score")
        borda = float(r.get("borda") or 0)
        add(f'<tr class="{"win" if l == w else ""}"><td class="num">{i}</td>'
            f'<td><a href="#proposal-{_e(l)}"><span class="label">{_e(l)}</span></a> {_e(v.title(l))}</td>'
            f"<td>{_e(v.author(l))}</td>"
            f'<td class="num"><span class="bar" style="width:{borda * 60:.0f}px"></span>{borda:.2f}</td>'
            f'<td class="num">{"—" if avg is None else f"{avg:.1f}"}</td>'
            f'<td class="num">{r.get("first_places", 0)}/{r.get("ballots", 0)}</td></tr>')
    add("</tbody></table>")
    if v.ballots:
        add("<h3>Бюллетени</h3><table><thead><tr><th>Участник</th><th>Порядок (лучший → худший)</th>"
            "<th>Почему</th></tr></thead><tbody>")
        for voter, b in v.ballots.items():
            add(f"<tr><td>{_e(voter)}</td><td><b>{_e(' › '.join(b.get('ranking', [])))}</b></td>"
                f"<td>{_inline(b.get('rationale', ''))}</td></tr>")
        add("</tbody></table>")

    # Варианты
    add("<h2>Варианты</h2>")
    for l, p in v.proposals.items():
        place = v.place(l)
        tags = ""
        if l == w:
            tags += '<span class="tag gold">победитель</span>'
        if v.revised(l):
            tags += '<span class="tag">доработан после критики</span>'
        add(f'<div class="proposal{" win" if l == w else ""}" id="proposal-{_e(l)}">')
        add(f'<h2><span class="label">{_e(l)}</span> {_e(p.get("title", ""))}{tags}</h2>')
        sub = [f"Автор: <b>{_e(v.author(l))}</b>"]
        if place:
            sub.append(f"место в голосовании: {place}")
        if p.get("confidence") is not None and v.author(l) != "пользователь":
            sub.append(f"уверенность автора: {round(float(p['confidence']) * 100)}%")
        add(f'<div class="muted small">{" · ".join(sub)}</div>')
        add(f'<div class="md">{md_to_html(p.get("summary", ""))}</div>')
        if p.get("reasoning"):
            add(f'<h4>Обоснование</h4><div class="md">{md_to_html(p["reasoning"])}</div>')
        if p.get("pros"):
            add("<h4>Плюсы</h4>" + _list(p["pros"]))
        if p.get("risks"):
            add("<h4>Риски</h4>" + _list(p["risks"]))
        if p.get("changes"):
            add(f'<div class="box"><b>Что изменено после критики:</b> {_inline(p["changes"])}</div>')
        ini = v.initial(l)
        if v.revised(l) and ini:
            add(f'<div class="initial"><h4>Первоначальная версия: {_e(ini.get("title", ""))}</h4>'
                f'<div class="md">{md_to_html(ini.get("summary", ""))}</div></div>')
        reviews = v.reviews_for(l)
        if reviews:
            add("<h4>Оценки других участников</h4>")
            for reviewer, r in reviews:
                scores = ", ".join(f"{_e(k)}: {float(s):g}" for k, s in (r.get("scores") or {}).items())
                add(f'<div class="review"><b>{_e(reviewer)}</b> <span class="muted small">({scores})</span>')
                add(_list([f"\\+ {x}" for x in r.get("strengths", [])], "plus")
                    + _list([f"− {x}" for x in r.get("weaknesses", [])], "minus") + "</div>")
        add("</div>")

    # Матрица критики
    if v.critiques:
        labels, mrows, avgs = v.matrix()
        add('<h2 id="critique">Критика: средние оценки (1–10)</h2>')
        add("<table><thead><tr><th>Рецензент</th>" + "".join(f'<th class="cell">{_e(l)}</th>' for l in labels)
            + "</tr></thead><tbody>")
        for reviewer, vals in mrows:
            cells = "".join(
                f'<td class="cell muted">{"свой" if v.author(l) == reviewer else "—"}</td>' if x is None
                else f'<td class="cell"{_score_style(x)}>{x:.1f}</td>' for l, x in zip(labels, vals))
            add(f"<tr><td>{_e(reviewer)}</td>{cells}</tr>")
        add("<tr><th>Среднее</th>" + "".join(
            "<td></td>" if x is None else f'<td class="cell"{_score_style(x)}>{x:.1f}</td>' for x in avgs) + "</tr>")
        add("</tbody></table>")

    # Участники
    add('<h2 id="members">Участники и расходы</h2>')
    add("<table><thead><tr><th>Участник</th><th>Вызовов</th><th>Токены (вход / выход)</th><th>Стоимость</th>"
        "</tr></thead><tbody>")
    for name, s in v.stats.items():
        s = s or {}
        add(f'<tr><td>{_e(name)}</td><td class="num">{s.get("calls", 0)}</td>'
            f'<td class="num">{s.get("prompt_tokens", 0)} / {s.get("completion_tokens", 0)}</td>'
            f'<td class="num">${float(s.get("cost", 0)):.4f}</td></tr>')
    add(f'<tr><th colspan="3">Итого</th><th class="num">${v.cost:.4f}</th></tr></tbody></table>')
    fails = v.failures()
    if fails:
        add("<h3>Сбои</h3><ul>" + "".join(
            f"<li><b>{_e(n)}</b> ({_e(st)}): {_e(err)}</li>" for n, st, err in fails) + "</ul>")
    add("</div>")

    return _document(v, "\n".join(parts), toolbar)


def _css() -> str:
    """CSS с фирменным цветом и текстом колонтитула."""
    b = brand.current()
    footer = b.footer_text.replace("\\", "").replace('"', "'")
    return (CSS.replace("#4545c8", b.color).replace("#5b5bd6", b.color)
            .replace('content: "Совет нейросетей"', f'content: "{footer}"'))


def _document(v: View, body: str, toolbar: str) -> str:
    return (f'<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{_e(v.title_text)} — {_e(brand.current().title)}</title><style>{_css()}</style></head>"
            f"<body>{toolbar}{body}{SCROLL_HINT}</body></html>")
