"""Отчёт в Markdown: для чтения в редакторах, GitHub, Obsidian и т. п."""
from __future__ import annotations

from .view import View


def _cell(text: object) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def render_decision_md(view: View) -> str:
    """Только решение совета: вопрос и итоговый ответ."""
    from . import brand
    v = view
    b = brand.current()
    out = ([f"*{b.title}*", ""] if b.title != brand.DEFAULT_TITLE else []) + [f"# {v.title_text}", ""]
    if v.question_is_long:
        out += ["**Запрос:**", ""] + [f"> {line}  " if line.strip() else ">" for line in v.question.splitlines()] + [""]
    out += [v.verdict.get("final_answer", ""), "", "---", f"*{v.date} · участники: {', '.join(v.members)}*"]
    return "\n".join(out).rstrip() + "\n"


def render_md(view: View, summary: bool = False, decision: bool = False) -> str:
    """Полный отчёт, (summary=True) итоговое решение с обоснованием или (decision=True) только решение."""
    if decision:
        return render_decision_md(view)
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
        out += [f"*{v.date} · {v.mode} · участники: {', '.join(v.members)}*", ""]
        if v.agreement_line:
            out += [f"*Согласие совета: {v.agreement_line}*", ""]
        out += ["## Решение совета", ""]
        if w:
            out += [f"*Лучший вариант: {v.title(w)} (предложил {v.author(w)})*", ""]
        out += [vd.get("final_answer", ""), ""]
    else:
        out += [
            "| | |", "|---|---|",
            f"| Дата | {v.date} |", f"| Режим | {v.mode} |",
            f"| Участники | {', '.join(v.members)} |",
            f"| Критерии | {_cell(', '.join(v.criteria)) or '—'} |",
            f"| Стоимость | ${v.cost:.4f} |", f"| Длительность | {v.duration} |",
            *([f"| Согласие совета | {_cell(v.agreement_line)} |"] if v.agreement_line else []),
            *([f"| Роли | {_cell(v.chair_note)} |"] if v.chair_note else []), "",
            f"## Решение совета: вариант {w} — {v.title(w)} ({v.author(w)})" if w else "## Решение совета", "",
            vd.get("final_answer", ""), "",
        ]
    if v.agreement_low:
        out.insert(out.index(vd.get("final_answer", "")), f"> ⚠️ **Низкое согласие совета:** {v.agreement_line}\n")
    if v.deviation_line:
        out.insert(out.index(vd.get("final_answer", "")), f"> ⚖️ **{v.deviation_line}**\n")
    if vd.get("why"):
        out += ["### Почему", "", vd["why"], ""]
    if vd.get("dissent"):
        out += ["### Особые мнения", "", *[f"- {x}" for x in vd["dissent"]], ""]
    if vd.get("risks"):
        out += ["### Риски", "", *[f"- {x}" for x in vd["risks"]], ""]
    if v.fact_check:
        out += ["## Проверка фактов", "", f"*{v.d.get('fact_checker', '')}, поиск в интернете — {v.fact_line}*", ""]
        for label, r in v.fact_check.items():
            out += [f"### Вариант {label}: {v.title(label)}", ""]
            if r.get("summary"):
                out += [r["summary"], ""]
            for c in r.get("claims", []):
                st = c.get("status", "unclear")
                links = [u for u in map(v.safe_url, c.get("sources") or []) if u][:3]
                out.append(f"- {v.FACT_ICON.get(st, '❔')} **{v.FACT_NAME.get(st, st)}:** {c.get('claim', '')}"
                           + (f" — {c['note']}" if c.get("note") else "")
                           + ("".join(f" [[{i + 1}]](<{u}>)" for i, u in enumerate(links))))
            out.append("")
    if summary:
        out += ["---", "*Это краткая версия: только итоговое решение. Полный отчёт с вариантами всех моделей, "
                "критикой и голосованием скачивается отдельно.*"]
        return "\n".join(out).rstrip() + "\n"

    out += ["## Голосование", "", "| # | Вариант | Автор | Шульце | Борда (0–1) | Попарно | Оценка критики | 1-х мест |",
            "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(v.tally, 1):
        l = r.get("label", "")
        avg = r.get("avg_score")
        name = f"**{l}: {_cell(v.title(l))}**" if l == w else f"{l}: {_cell(v.title(l))}"
        out.append(f"| {i} | {name} | {v.author(l)} | {v.schulze_str(r, len(v.tally))} | {float(r.get('borda') or 0):.2f} | {v.pairwise_str(r)} | "
                   f"{'—' if avg is None else f'{avg:.1f}'} | {r.get('first_places', 0)}/{r.get('ballots', 0)} |")
    out += ["", f"*{v.order_note}*"]
    out += [f"- {n}" for n in v.tie_notes]
    if v.position_line:
        out += [f"*{v.position_line}.*"]
    if v.flag_notes:
        out += ["", "> ⚠️ **Бюллетени против общего мнения** (голоса учтены):"] + [f"> - {n}" for n in v.flag_notes]
        out += [f"> {v.robust_note}"]
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
        if p.get("comparison") and v.revised(l):
            out += ["**Сравнение с другими вариантами.** " + p["comparison"], ""]
        if p.get("borrowed") and v.revised(l):
            out += ["**Взято у других:**", "", *[f"- {x}" for x in p["borrowed"]], ""]
        if p.get("not_accepted") and v.revised(l):
            out += ["**Что не принято из критики и почему:**", "", *[f"- {x}" for x in p["not_accepted"]], ""]
        if p.get("changes"):
            pos = v.position(l)
            out += [f"> **Что изменено после критики{f' ({v.POSITION_NAME.get(pos, pos)})' if pos else ''}:** {p['changes']}", ""]
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
