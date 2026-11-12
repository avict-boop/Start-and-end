"""Отчёт в Markdown: для чтения в редакторах, GitHub, Obsidian и т. п.

Порядок — как в PDF и Word: коротко о решении, решение, обоснование, затем подробности. Заголовки внутри
ответов моделей опускаются ниже заголовков отчёта, чтобы структура документа не путалась.
"""
from __future__ import annotations

import re

from .view import View, readable_flaw

AGREE = {"high": "высокое", "medium": "среднее", "low": "низкое"}
KIND = {"full": "Отчёт о заседании", "summary": "Итог заседания", "decision": "Решение совета"}


def _cell(text: object) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def demote(text: str, top: int = 3) -> str:
    """Заголовки Markdown из ответа модели: самый крупный становится уровнем top (### по умолчанию)."""
    lines = str(text or "").split("\n")
    in_code = False
    levels = []
    for line in lines:
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif not in_code:
            m = re.match(r"^(#{1,6})\s", line)
            if m:
                levels.append(len(m.group(1)))
    if not levels:
        return str(text or "")
    shift = max(0, top - min(levels))
    out, in_code = [], False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif not in_code and shift:
            m = re.match(r"^(#{1,6})(\s.*)$", line)
            if m:
                line = "#" * min(6, len(m.group(1)) + shift) + m.group(2)
        out.append(line)
    return "\n".join(out)


def _quote(text: str) -> list[str]:
    return [f"> {line}  " if line.strip() else ">" for line in str(text).splitlines()]


def _head(v: View, kind: str) -> list[str]:
    from . import brand
    b = brand.current()
    out = [f"# {v.display_title}", "", f"*{KIND[kind]} · {v.date} · {b.title}*", ""]
    if v.followup_title:
        out += [f"↳ Уточнение к заседанию «{v.followup_title}»", ""]
    if v.question_is_prompt:
        out += _quote(v.question_excerpt()) + ["", "*Полный текст запроса — в разделе «Запрос и условия».*", ""]
    elif v.question_is_long and v.question_rest:
        out += _quote(v.question_rest) + [""]
    return out


def _keyfacts(v: View) -> list[str]:
    w = v.winner
    a = v.agreement
    row = next((r for r in v.tally if r.get("label") == w), {}) if w else {}
    dec = f"**{_cell(v.title(w))}** (вариант {w}, {_cell(v.author(w))})" if w else "итог председателя"
    if w and v.unacceptable:
        dec = f"⛔ ни один вариант не подходит; лидер сравнения — вариант {w} (не рекомендован)"
    agree = f"{AGREE[a['level']]} (τ = {a['tau']:.2f})" if a else "—"
    first = f"{row.get('first_places', 0)} из {row.get('ballots')}" if row.get("ballots") else "—"
    return ["| Решение | Согласие совета | Первых мест | Совет |", "|---|---|---|---|",
            f"| {dec} | {agree} | {first} | {len(v.members)} моделей, {_cell(v.mode)} |", ""]


def _card(v: View) -> list[str]:
    c = v.decision_card
    if not c:
        return []
    out = ["> **Карточка решения**", ">"]
    if c.get("summary_decision"):
        out += [f"> {c['summary_decision']}", ">"]
    for t, k in (("Известно", "top_known"), ("Главные пробелы", "key_unknowns"), ("Следующие шаги", "next_steps"),
                 ("⚠ Не подтверждено", "unverified")):
        if c.get(k):
            out += [f"> **{t}:**", *[f"> - {x}" for x in c[k]], ">"]
    return out[:-1] + [""]


def _callouts(v: View) -> list[str]:
    out = []
    if v.unacceptable:
        out += [f"> ⛔ **Ни один вариант не подходит:** {'; '.join(v.unacceptable)}", ""]
    if v.tie_line:
        out += [f"> **Ничья за первое место.** {v.tie_line}", ""]
    if v.deviation_line:
        out += [f"> **⚖️ {v.deviation_line}**", ""]
    if v.agreement_low:
        out += [f"> ⚠️ **Низкое согласие совета:** {v.agreement_line}", ""]
    return out


def _rationale(v: View) -> list[str]:
    vd = v.verdict
    if not (vd.get("why") or vd.get("risks") or vd.get("dissent")):
        return []
    out = ["## Обоснование", ""]
    if vd.get("why"):
        out += ["### Почему совет так решил", "", demote(vd["why"], 4), ""]
    if vd.get("risks"):
        out += ["### Риски", "", *[f"- {x}" for x in vd["risks"]], ""]
    if vd.get("dissent"):
        out += ["### Особые мнения", "", *[f"- {x}" for x in vd["dissent"]], ""]
    return out


def _ideas(v: View) -> list[str]:
    ideas = v.ideas
    if not ideas:
        return []
    out = ["## Что взято из других вариантов", "",
           f"*Критики отметили, что стоит сохранить из вариантов, не ставших основой решения; председатель решил по "
           f"каждой идее. Итого — {v.ideas_line}.*", "",
           "| Решение | Идея | Вариант | Как использована или почему отклонена |", "|---|---|---|---|"]
    for i in ideas:
        st = i["decision"]
        reason = i.get("reason") or ("председатель не ответил по этой идее" if st == "not_addressed" else "")
        out.append(f"| {v.IDEA_SIGN[st]} {v.IDEA_WORD[st]} | {_cell(i['idea'])} | {i.get('label', '')} | {_cell(reason)} |")
    return out + [""]


def _erefs(v: View, ids: list[str]) -> str:
    by = v.evidence_by_id
    return " ".join("DOC" if i == "DOC" else f"[{i}](<{by[i]['url']}>)" for i in ids if i == "DOC" or i in by)


def _kcu(v: View) -> list[str]:
    if not v.is_research:
        return []
    out = ["## Что известно, что спорно, чего не знает никто", "",
           "*«Известно» — только подтверждённое найденными источниками или вашими документами; согласие моделей и "
           "цифры «из памяти» сюда не попадают.*", "", "### ✓ Известно", ""]
    if v.known:
        out += ["| Утверждение | Рамки | Источники |", "|---|---|---|"]
        out += [f"| {_cell(k.get('statement', ''))}{' *(быстро меняется)*' if k.get('volatility') == 'changes_fast' else ''}"
                f"{(' <br>*' + _cell(v.known_basis(k)) + '*') if v.known_basis(k) else ''} "
                f"| {_cell(k.get('scope', ''))} | {_erefs(v, k.get('evidence_ids') or [])} |" for k in v.known]
    else:
        out.append("*Подтверждённых источниками утверждений нет.*")
    out.append("")
    if v.contested:
        out += ["### ⇄ Спорно", ""]
        for c in v.contested:
            out.append(f"- **{c.get('issue', '')}**")
            out += [f"  - {p}" for p in c.get("positions") or []]
            if c.get("why_they_differ"):
                out.append(f"  - *почему расходятся:* {c['why_they_differ']}")
            if c.get("what_would_resolve"):
                out.append(f"  - *что решило бы спор:* {c['what_would_resolve']}")
            refs = _erefs(v, c.get("evidence_ids") or [])
            if refs:
                out.append(f"  - источники: {refs}")
        out.append("")
    if v.unknown:
        out += ["### ? Неизвестно", "", "| Чего не знаем | Почему | Как узнать |", "|---|---|---|"]
        out += [f"| {_cell(u.get('gap', ''))} | {_cell(v.GAP_WORD.get(u.get('reason', ''), u.get('reason', '')))} | {_cell(u.get('how_to_check', ''))} |"
                for u in v.unknown]
        out.append("")
    for key, title in (("what_would_change", "Что изменило бы выводы"), ("next_questions", "Что исследовать дальше")):
        items = v.verdict.get(key) or []
        if items:
            out += [f"### {title}", "", *[f"- {x}" for x in items], ""]
    if v.subquestions:
        out += ["## Рамки и подвопросы", ""]
        if v.scope_line:
            out += [f"**Рамки:** {v.scope_line}", ""]
        out += ["| Подвопрос | В итоге | Каркас раздела |", "|---|---|---|"]
        for r in v.coverage_rows:
            out.append(f"| {r['id']}. {_cell(r['text'])} | {v.COVER_WORD.get(r['status'], r['status'] or '—')} | "
                       f"{r['backbone'] or '—'} |")
        out.append("")
    return out


def _sources(v: View, full: bool) -> list[str]:
    if not v.is_research or not v.evidence:
        return []
    out = ["## Источники", "", f"*{v.ledger.get('model', '')}, арбитраж доказательств — {v.ledger_line}*", ""]
    for e in v.evidence:
        meta = ", ".join(x for x in (e.get("publisher"), e.get("date")) if x)
        flag = " — ⚠ нет в результатах поиска" if e.get("search_returned") is False else ""
        flag += {"dead": " — ⚠ ссылка не открывается (страница не найдена)", "blocked": " — сайт не пускает автоматическую проверку",
                 "error": " — ссылка не открылась при проверке", "refused": " — внутренний адрес — не открывался"}.get(e.get("link", ""), "")
        out.append(f"- **{e['id']}** [{e.get('title') or e['url']}](<{e['url']}>)" + (f" — {meta}" if meta else "")
                   + f" · утверждения: {', '.join(e.get('claims') or [])}{flag}")
    out.append("")
    if full and v.checked_claims:
        out += ["## Проверка утверждений", "", "| № | Утверждение | Статус | Источники |", "|---|---|---|---|"]
        for c in v.checked_claims:
            st = c.get("status", "not_checked")
            out.append(f"| {c.get('id', '')} | {_cell(c.get('text', ''))} | {v.CLAIM_SIGN.get(st, '')} "
                       f"{v.CLAIM_WORD.get(st, st)} | {_erefs(v, c.get('evidence') or [])} |")
        out.append("")
    return out


def _facts(v: View) -> list[str]:
    if not v.fact_check:
        return []
    out = ["## Проверка фактов", "", f"*{v.d.get('fact_checker', '')}, поиск в интернете — {v.fact_line}*", ""]
    for label, r in v.fact_check.items():
        out += [f"### {label}. {v.title(label)}", ""]
        if r.get("summary"):
            out += [r["summary"], ""]
        for c in r.get("claims", []):
            st = c.get("status", "unclear")
            links = [u for u in map(v.safe_url, c.get("sources") or []) if u][:3]
            out.append(f"- {v.FACT_ICON.get(st, '❔')} **{v.FACT_NAME.get(st, st)}:** {c.get('claim', '')}"
                       + (f" — {c['note']}" if c.get("note") else "")
                       + ("".join(f" [[{i + 1}]](<{u}>)" for i, u in enumerate(links))))
        out.append("")
    return out


def _request(v: View, summary: bool) -> list[str]:
    out = ["## Запрос и условия", ""]
    if v.followup_title:
        out += [f"**Уточнение к заседанию:** {v.followup_title}", ""]
    out += _quote(v.question) + [""]
    if v.refined:
        out += [f"*Как председатель уточнил вопрос:* {v.refined}", ""]
    if v.requirements:
        out += ["### Обязательные требования", "", *[f"{i}. {r}" for i, r in enumerate(v.requirements, 1)], ""]
    if v.attachments:
        out += ["### Вложения", ""]
        for m in v.attachments:
            link = f"{v.stem}.files/{m.get('file')}" if v.stem else ""
            label = v.attachment_label(m)
            name = str(m.get("name", ""))
            text = f"[{name}](<{link}>){label[len(name):]}" if link else label
            out.append(f"- {text}" + (f" — ⚠️ {m['warning']}" if m.get("warning") else ""))
        out.append("")
        if v.image_notes and not summary:
            out += [f"*Описание изображений для моделей без зрения (сделал {v.d.get('image_notes_by') or 'модель'}):*", ""]
            out += [f"- **{n}:** {t}" for n, t in v.image_notes.items()] + [""]
    rows = [("Режим", v.mode), ("Участники", ", ".join(v.members)), ("Критерии", ", ".join(v.criteria) or "—")]
    if v.chair_note:
        rows.append(("Роли", v.chair_note))
    if v.compact_note:
        rows.append(("Варианты", v.compact_note))
    rows += [("Дата", v.date), ("Длительность", v.duration), ("Стоимость", f"${v.cost:.4f}")]
    out += ["### Условия заседания", "", "| | |", "|---|---|", *[f"| {k} | {_cell(x)} |" for k, x in rows], ""]
    return out


def render_decision_md(view: View) -> str:
    """Только решение совета: сам ответ, без вопроса."""
    from . import brand
    v = view
    out = [f"# {KIND['decision']}", "", f"*{v.date} · {brand.current().title}*", "", *_callouts(v), *_card(v),
           demote(v.verdict.get("final_answer", ""), 2), "",
           "---", f"*{v.date} · участники: {', '.join(v.members)}*"]
    return "\n".join(out).rstrip() + "\n"


def render_md(view: View, summary: bool = False, decision: bool = False) -> str:
    """Полный отчёт, (summary=True) итог — решение с обоснованием — или (decision=True) только решение."""
    if decision:
        return render_decision_md(view)
    v = view
    w = v.winner
    out = _head(v, "summary" if summary else "full") + _keyfacts(v)
    out += [f"## {'Итог исследования' if v.is_research else 'Решение совета'}", "", *_callouts(v), *_card(v),
            demote(v.verdict.get("final_answer", ""), 3), ""]
    out += _kcu(v) + _rationale(v) + _ideas(v) + _sources(v, full=not summary) + _facts(v)
    if summary:
        out += _request(v, True)
        out += ["---", "*Это краткая версия: решение и его обоснование. Полный отчёт с вариантами всех моделей, "
                "критикой и голосованием скачивается отдельно.*"]
        return "\n".join(out).rstrip() + "\n"

    out += ["## Голосование", ""]
    if v.tally:
        out += ["| Место | Вариант | Автор | Первых мест | Балл |", "|---|---|---|---|---|"]
        for i, r in enumerate(v.tally, 1):
            l = r.get("label", "")
            name = f"**{l}. {_cell(v.title(l))}**" if l == w else f"{l}. {_cell(v.title(l))}"
            out.append(f"| {i} | {name} | {_cell(v.author(l))} | {r.get('first_places', 0)} из {r.get('ballots', 0)} | "
                       f"{float(r.get('borda') or 0):.2f} |")
        out.append("")
    notes = ([f"Согласие совета: {v.agreement_line}."] if v.agreement_line else []) + (
        [v.coverage_line + "."] if v.coverage_line else []) + (
        ["Решающие голоса (лидер меняется без одного бюллетеня): " + "; ".join(v.decisive_notes) + "."] if v.decisive_notes else []) + (
        [v.position_line + "."] if v.position_line else []) + v.tie_notes
    if notes:
        out += ["### Насколько надёжен результат", "", *[f"- {n}" for n in notes], ""]
    if v.flag_notes:
        out += ["> ⚠️ **Бюллетени против общего мнения** (голоса учтены):"] + [f"> - {n}" for n in v.flag_notes]
        out += [f"> {v.robust_note}", ""]
    if v.ballots:
        out += ["### Бюллетени", "", "| Участник | Порядок | Почему |", "|---|---|---|"]
        out += [f"| {_cell(voter)} | **{' › '.join(b.get('ranking', []))}** | {_cell(b.get('rationale', ''))} |"
                for voter, b in v.ballots.items()]
        out.append("")
    if v.tally:
        out += ["### Подробный подсчёт", "", "| # | Вариант | Автор | " + ("Шульце | " if v.by_schulze else "")
                + "Борда | Попарно | Критика | 1-х мест |", "|---|---|---|" + ("---|" if v.by_schulze else "") + "---|---|---|---|"]
        for i, r in enumerate(v.tally, 1):
            l = r.get("label", "")
            avg = r.get("avg_score")
            out.append(f"| {i} | {l} | {_cell(v.author(l))} | " + (f"{v.schulze_str(r, len(v.tally))} | " if v.by_schulze else "")
                       + f"{float(r.get('borda') or 0):.2f} | {v.pairwise_str(r)} | {'—' if avg is None else f'{avg:.1f}'} | "
                       f"{r.get('first_places', 0)}/{r.get('ballots', 0)} |")
        out += ["", f"*{v.order_note}*", ""]

    out += ["## Варианты", ""]
    for l, p in v.proposals.items():
        out += [f"### {l}. {p.get('title', '')}" + (" — решение совета" if l == w else ""), ""]
        sub = [f"Автор: **{v.author(l)}**"]
        if v.place(l):
            sub.append(f"место: {v.place(l)}")
        if v.revised(l):
            sub.append("доработан после критики")
        out += ["*" + " · ".join(sub) + "*", "", demote(p.get("summary", ""), 4), ""]
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
                    demote(ini.get("summary", ""), 4), "", "</details>", ""]
        reviews = v.reviews_for(l)
        if reviews:
            out += ["**Оценки других участников:**", ""]
            for reviewer, r in reviews:
                scores = ", ".join(f"{k}: {float(s):g}" for k, s in (r.get("scores") or {}).items())
                out.append(f"- **{reviewer}** ({scores})")
                out += [f"  - ➕ {x}" for x in r.get("strengths", [])]
                out += [f"  - ➖ {readable_flaw(x)}" for x in r.get("weaknesses", [])]
            out.append("")

    if v.critiques:
        labels, rows, avgs = v.matrix()
        out += ["## Критика", "", "*Средняя оценка каждого рецензента, от 1 до 10.*", "",
                "| Рецензент | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
        for reviewer, vals in rows:
            out.append(f"| {reviewer} | " + " | ".join(
                ("свой" if v.author(l) == reviewer else "—") if x is None else f"{x:.1f}"
                for l, x in zip(labels, vals)) + " |")
        out.append("| **Среднее** | " + " | ".join("" if x is None else f"**{x:.1f}**" for x in avgs) + " |")
        out.append("")

    out += ["## Участники и расходы", "", "| Участник | Обращений | Токены (вход / выход) | Стоимость |", "|---|---|---|---|"]
    for name, s in v.stats.items():
        s = s or {}
        note = v.think_note(s)
        out.append(f"| {name}{f' ({note})' if note else ''} | {s.get('calls', 0)} | {s.get('prompt_tokens', 0)} / {s.get('completion_tokens', 0)} "
                   f"| ${float(s.get('cost', 0)):.4f} |")
    out.append(f"| **Итого** | | | **${v.cost:.4f}** |")
    fails = v.failures()
    if fails:
        out += ["", "### Сбои", "", "| Участник | Этап | Что случилось |", "|---|---|---|"]
        out += [f"| {n} | {st} | {_cell(err)} |" for n, st, err in fails]
    if v.cache_line:
        out += ["", f"*{v.cache_line}.*"]
    sc = v.stage_costs
    if sc["stages"]:
        out += ["", "### Расходы по этапам", "", "| Этап | Обращений (из них повторы и продолжения) | Вход из кэша | Стоимость | Доля |",
                "|---|---|---|---|---|"]
        out += [f"| {r['stage_name']} | {r['requests']} ({r['extra']}) | {r['cache_share']:.0%} | ${r['cost']:.4f} | {r['share']:.0%} |"
                for r in sc["stages"]]
        if sc["extra_cost"]:
            out += ["", f"*На повторы и продолжения ушло ${sc['extra_cost']:.4f}.*"]
    out += [""] + _request(v, False)
    return "\n".join(out).rstrip() + "\n"
