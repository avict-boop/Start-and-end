"""Сохранение результатов заседания: полная стенограмма (JSON) и отчёт (Markdown)."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .session import SessionResult


def render_markdown(res: SessionResult) -> str:
    v = res.verdict
    author = lambda l: res.authors.get(l) or "пользователь"  # noqa: E731
    win = res.proposals[v.winner]
    out = [
        "# Решение совета",
        "",
        f"**Вопрос:** {res.question}",
        "",
    ]
    if res.refined_question != res.question:
        out += [f"**Уточнённая постановка:** {res.refined_question}", ""]
    out += [
        f"**Критерии:** {', '.join(res.criteria)}  ",
        f"**Режим:** {res.mode} · **Время:** {res.duration_s} с · **Стоимость:** ${res.total_cost:.4f}",
        "",
        f"## Победитель: вариант {v.winner} — {win.title} ({author(v.winner)})",
        "",
        v.final_answer,
        "",
        "### Почему",
        v.why,
        "",
    ]
    if v.dissent:
        out += ["### Особые мнения", *[f"- {d}" for d in v.dissent], ""]
    if v.risks:
        out += ["### Риски", *[f"- {r}" for r in v.risks], ""]

    out += [
        "## Голосование",
        "",
        "| # | Вариант | Автор | Борда (0–1) | Оценка критики | 1-х мест |",
        "|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(res.tally, 1):
        avg = f"{r.avg_score:.1f}" if r.avg_score is not None else "—"
        out.append(
            f"| {i} | {r.label}: {res.proposals[r.label].title} | {author(r.label)} "
            f"| {r.borda:.2f} | {avg} | {r.first_places}/{r.ballots} |"
        )
    out.append("")
    if res.ballots:
        out += ["### Бюллетени", ""]
        for name, b in res.ballots.items():
            out.append(f"- **{name}**: {' > '.join(b.ranking)} — {b.rationale}")
        out.append("")

    out += ["## Все варианты (финальные версии)", ""]
    for label, p in res.proposals.items():
        out += [f"### {label}: {p.title} ({author(label)})", "", p.summary, ""]
        if p.reasoning:
            out += [f"*Обоснование:* {p.reasoning}", ""]
        if p.pros:
            out += ["*Плюсы:* " + "; ".join(p.pros), ""]
        if p.risks:
            out += ["*Риски:* " + "; ".join(p.risks), ""]
        changes = getattr(p, "changes", "")
        if changes:
            out += [f"*Что изменено после критики:* {changes}", ""]

    if res.critiques:
        out += ["## Критика (последний раунд)", ""]
        for reviewer, cs in res.critiques.items():
            out.append(f"### {reviewer} оценивает")
            for rv in cs.reviews:
                scores = ", ".join(f"{k}: {v:g}" for k, v in rv.scores.items())
                out.append(f"- **{rv.label}** ({scores})")
                out += [f"  - + {s}" for s in rv.strengths]
                out += [f"  - − {w}" for w in rv.weaknesses]
            out.append("")

    out += ["## Участники", "", "| Участник | Вызовов | Токены (вход/выход) | Стоимость | Сбои |", "|---|---|---|---|---|"]
    for name, s in res.stats.items():
        fails = "; ".join(s.failures) if s.failures else "—"
        out.append(f"| {name} | {s.calls} | {s.prompt_tokens}/{s.completion_tokens} | ${s.cost:.4f} | {fails} |")
    return "\n".join(out) + "\n"


def save(res: SessionResult, runs_dir: str | Path) -> tuple[Path, Path]:
    d = Path(runs_dir)
    d.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w]+", "-", res.question.lower(), flags=re.U).strip("-")[:40] or "session"
    stem = f"{datetime.now():%Y%m%d-%H%M%S}-{slug}"
    json_path = d / f"{stem}.json"
    md_path = d / f"{stem}.md"
    json_path.write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(res), encoding="utf-8")
    return json_path, md_path
