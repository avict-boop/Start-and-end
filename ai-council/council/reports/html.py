"""Отчёт в HTML: основа для PDF и полноэкранного просмотра в браузере."""
from __future__ import annotations

from html import escape

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
h1 { font-size: 19pt; line-height: 1.25; margin: 4px 0 8px; }
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
@media (max-width: 640px) { .page { padding: 16px; } .toc { columns: 1; } h1 { font-size: 16pt; }
  table { display: block; overflow-x: auto; } }
"""


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


def render_html(view: View, *, toolbar: str = "") -> str:
    v = view
    vd = v.verdict
    w = v.winner
    parts: list[str] = []
    add = parts.append

    add('<div class="page">')
    add('<div class="brand">Совет нейросетей · отчёт о заседании</div>')
    add(f"<h1>{_e(v.question)}</h1>")
    if v.refined:
        add(f'<div class="refined"><b>Уточнённая постановка:</b> {_e(v.refined)}</div>')
    add('<div class="meta">')
    rows = [("Дата", v.date), ("Режим", v.mode),
            ("Участники", ", ".join(v.members)),
            ("Критерии", "".join(f'<span class="chip">{_e(c)}</span>' for c in v.criteria) or "—"),
            ("Стоимость", f"${v.cost:.4f}"), ("Длительность", v.duration)]
    for k, val in rows:
        val_html = val if k == "Критерии" else _e(val)
        add(f"<div><span>{k}</span><span>{val_html}</span></div>")
    add("</div>")

    toc = [("verdict", "Решение совета"), ("voting", "Голосование")]
    toc += [(f"proposal-{l}", f"Вариант {l}: {v.title(l)}") for l in v.proposals]
    if v.critiques:
        toc.append(("critique", "Критика"))
    toc.append(("members", "Участники и расходы"))
    add('<div class="toc">' + "".join(f'<div><a href="#{a}">{_e(t)}</a></div>' for a, t in toc) + "</div>")

    # Решение
    add('<div class="verdict" id="verdict">')
    if w:
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

    body = "\n".join(parts)
    return (f'<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{_e(v.question[:80])} — Совет нейросетей</title><style>{CSS}</style></head>"
            f"<body>{toolbar}{body}</body></html>")
