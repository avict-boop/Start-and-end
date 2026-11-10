"""Отчёт в Markdown: для чтения в редакторах, GitHub, Obsidian и т. п."""
from __future__ import annotations

from .view import View


def _cell(text: object) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def render_md(view: View, summary: bool = False) -> str:
    """Полный отчёт или (summary=True) только итоговое решение."""
    v = view
    vd = v.verdict
    w = v.winner
    from . import brand
    b = brand.current()
    out = ([f"*{b.title}*", ""] if b.title != brand.DEFAULT_TITLE else []) + [f"# {v.title_text}", ""]
    if v.followup_title:
        out += [f"↳ **Уточнение к заседанию:** {v.followup_title}", ""]
    if v.question_is_long:
        # запрос целиком, с его переносами строк (два пробела в конце строки — перенос в Markdown)
        out += ["**Запрос:**", ""] + [f"> {line}  " if line.strip() else ">" for line in v.question.splitlines()] + [""]
    if v.attachments:
        out += [f"**Вложения ({len(v.attachments)}):**", ""]
        for m in v.attachments:
            link = f"{v.stem}.files/{m.get('file')}" if v.stem else ""
            label = v.attachment_label(m)
            name = str(m.get("name", ""))
            text = f"[{name}](<{link}>){label[len(name):]}" if link else label
            out.append(f"- {text}" + (f" — ⚠️ {m['warning']}" if m.get("warning") else ""))
        out.append("")
        if v.image_notes and not summary:
            out += [f"**Описание изображений** (сделал {v.d.get('image_notes_by') or 'модель'}):", ""]
            out += [f"- **{n}:** {t}" for n, t in v.image_notes.items()] + [""]
    if v.refined:
        out += [f"**Уточнённая постановка:** {v.refined}", ""]
    if summary:
        out += [f"*{v.date} · {v.mode} · участники: {', '.join(v.members)}*", "", "## Решение совета", ""]
        if w:
            out += [f"*Лучший вариант: {v.title(w)} (предложил {v.author(w)})*", ""]
        out += [vd.get("final_answer", ""), ""]
    else:
        out += [
            "| | |", "|---|---|",
            f"| Дата | {v.date} |", f"| Режим | {v.mode} |",
            f"| Участники | {', '.join(v.members)} |",
            f"| Критерии | {_cell(', '.join(v.criteria)) or '—'} |",
            f"| Стоимость | ${v.cost:.4f} |", f"| Длительность | {v.duration} |", "",
            f"## Решение совета: вариант {w} — {v.title(w)} ({v.author(w)})" if w else "## Решение совета", "",
            vd.get("final_answer", ""), "",
        ]
    if vd.get("why"):
        out += ["### Почему", "", vd["why"], ""]
    if vd.get("dissent"):
        out += ["### Особые мнения", "", *[f"- {x}" for x in vd["dissent"]], ""]
    if vd.get("risks"):
        out += ["### Риски", "", *[f"- {x}" for x in vd["risks"]], ""]
    if summary:
        out += ["---", "*Это краткая версия: только итоговое решение. Полный отчёт с вариантами всех моделей, "
                "критикой и голосованием скачивается отдельно.*"]
        return "\n".join(out).rstrip() + "\n"

    out += ["## Голосование", "", "| # | Вариант | Автор | Борда (0–1) | Оценка критики | 1-х мест |",
            "|---|---|---|---|---|---|"]
    for i, r in enumerate(v.tally, 1):
        l = r.get("label", "")
        avg = r.get("avg_score")
        name = f"**{l}: {_cell(v.title(l))}**" if l == w else f"{l}: {_cell(v.title(l))}"
        out.append(f"| {i} | {name} | {v.author(l)} | {float(r.get('borda') or 0):.2f} | "
                   f"{'—' if avg is None else f'{avg:.1f}'} | {r.get('first_places', 0)}/{r.get('ballots', 0)} |")
    out.append("")
    if v.ballots:
        out += ["### Бюллетени", ""]
        out += [f"- **{voter}**: {' › '.join(b.get('ranking', []))} — {b.get('rationale', '')}"
                for voter, b in v.ballots.items()]
        out.append("")

    out += ["## Варианты", ""]
    for l, p in v.proposals.items():
        out += [f"### Вариант {l}: {p.get('title', '')}" + (" 🏆" if l == w else ""), ""]
        sub = [f"Автор: **{v.author(l)}**"]
        if v.place(l):
            sub.append(f"место: {v.place(l)}")
        if v.revised(l):
            sub.append("доработан после критики")
        out += ["*" + " · ".join(sub) + "*", "", p.get("summary", ""), ""]
        if p.get("reasoning"):
            out += ["**Обоснование.** " + p["reasoning"], ""]
        if p.get("pros"):
            out += ["**Плюсы:**", "", *[f"- {x}" for x in p["pros"]], ""]
        if p.get("risks"):
            out += ["**Риски:**", "", *[f"- {x}" for x in p["risks"]], ""]
        if p.get("changes"):
            out += [f"> **Что изменено после критики:** {p['changes']}", ""]
        ini = v.initial(l)
        if v.revised(l) and ini:
            out += [f"<details><summary>Первоначальная версия: {ini.get('title', '')}</summary>", "",
                    ini.get("summary", ""), "", "</details>", ""]
        reviews = v.reviews_for(l)
        if reviews:
            out += ["**Оценки других участников:**", ""]
            for reviewer, r in reviews:
                scores = ", ".join(f"{k}: {float(s):g}" for k, s in (r.get("scores") or {}).items())
                out.append(f"- **{reviewer}** ({scores})")
                out += [f"  - ➕ {x}" for x in r.get("strengths", [])]
                out += [f"  - ➖ {x}" for x in r.get("weaknesses", [])]
            out.append("")

    if v.critiques:
        labels, rows, avgs = v.matrix()
        out += ["## Критика: средние оценки (1–10)", "", "| Рецензент | " + " | ".join(labels) + " |",
                "|---|" + "---|" * len(labels)]
        for reviewer, vals in rows:
            out.append(f"| {reviewer} | " + " | ".join(
                ("свой" if v.author(l) == reviewer else "—") if x is None else f"{x:.1f}"
                for l, x in zip(labels, vals)) + " |")
        out.append("| **Среднее** | " + " | ".join("" if x is None else f"**{x:.1f}**" for x in avgs) + " |")
        out.append("")

    out += ["## Участники и расходы", "", "| Участник | Вызовов | Токены (вход / выход) | Стоимость |",
            "|---|---|---|---|"]
    for name, s in v.stats.items():
        s = s or {}
        out.append(f"| {name} | {s.get('calls', 0)} | {s.get('prompt_tokens', 0)} / {s.get('completion_tokens', 0)} "
                   f"| ${float(s.get('cost', 0)):.4f} |")
    out.append(f"| **Итого** | | | **${v.cost:.4f}** |")
    fails = v.failures()
    if fails:
        out += ["", "### Сбои", "", *[f"- **{n}** ({st}): {err}" for n, st, err in fails]]
    return "\n".join(out).rstrip() + "\n"
