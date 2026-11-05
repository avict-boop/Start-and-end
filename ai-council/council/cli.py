"""Командная строка: `python -m council ask "вопрос"` и `python -m council models`."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .client import OpenRouterClient
from .config import load_config, load_dotenv
from .mock import MockClient
from .report import save
from .session import Council, CouncilError

console = Console()
PROJECT_DIR = Path(__file__).resolve().parent.parent


def _on_event(event: str, **d) -> None:
    if event == "stage":
        console.rule(f"[bold cyan]{d['title']}")
    elif event == "member_done":
        console.print(f"  [green]✓[/] {d['member']}")
    elif event == "member_failed":
        console.print(f"  [red]✗ {d['member']}[/] ({d['stage']}): {d['error'][:200]}")
    elif event == "framing":
        console.print(f"[bold]Вопрос:[/] {d['refined_question']}")
        console.print(f"[bold]Критерии:[/] {', '.join(d['criteria'])}")
    elif event == "proposals":
        for label, p in d["proposals"].items():
            console.print(f"  [bold]{label}[/]: {p.title}")
    elif event == "revisions":
        for label, p in d["revised"].items():
            console.print(f"  [bold]{label}[/] → {p.title}")
    elif event == "tally":
        t = Table(title="Итоги голосования")
        for col in ("#", "Вариант", "Автор", "Борда", "Критика", "1-х мест"):
            t.add_column(col)
        for i, r in enumerate(d["rows"], 1):
            t.add_row(
                str(i),
                r.label,
                d["authors"].get(r.label) or "пользователь",
                f"{r.borda:.2f}",
                f"{r.avg_score:.1f}" if r.avg_score is not None else "—",
                f"{r.first_places}/{r.ballots}",
            )
        console.print(t)


def _read_options(args) -> list[str] | None:
    opts = list(args.option or [])
    if args.options_file:
        text = Path(args.options_file).read_text(encoding="utf-8")
        # варианты разделяются строкой из трёх дефисов или пустой строкой
        sep = "\n---\n" if "\n---\n" in text else "\n\n"
        opts += [o.strip() for o in text.split(sep) if o.strip()]
    return opts or None


def _make_client(cfg, mock: bool):
    if mock:
        return MockClient()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        console.print(
            "[red]Нет OPENROUTER_API_KEY.[/] Создайте ключ на https://openrouter.ai/keys и положите его в "
            "файл .env (см. .env.example) или в переменную окружения. Для пробного прогона без ключа: --mock"
        )
        sys.exit(2)
    return OpenRouterClient(key, timeout=cfg.timeout_s, max_retries=cfg.max_retries)


async def _ask(args) -> int:
    cfg = load_config(args.config)
    if args.budget:
        cfg.max_cost_usd = args.budget
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        unknown = wanted - {m.name for m in cfg.members}
        if unknown:
            console.print(f"[red]Неизвестные участники:[/] {', '.join(sorted(unknown))}")
            return 2
        for m in cfg.members:
            m.enabled = m.name in wanted
        if cfg.chair not in wanted:
            cfg.chair = next(n for n in (m.name for m in cfg.members) if n in wanted)
        if cfg.devil_advocate not in wanted:
            cfg.devil_advocate = None
    cfg = type(cfg).model_validate(cfg.model_dump())  # перепроверка после изменений

    question = args.question
    if question == "-":
        question = sys.stdin.read().strip()
    context = Path(args.context_file).read_text(encoding="utf-8") if args.context_file else None
    criteria = [c for c in args.criteria.split(",")] if args.criteria else None

    names = ", ".join(m.name for m in cfg.active_members)
    console.print(Panel(f"Участники: {names}\nПредседатель: {cfg.chair}"
                        + (f"\nАдвокат дьявола: {cfg.devil_advocate}" if cfg.devil_advocate else "")
                        + f"\nЛимит бюджета: ${cfg.max_cost_usd:.2f}" + ("\n[yellow]MOCK-режим[/]" if args.mock else ""),
                        title="Совет нейросетей"))
    client = _make_client(cfg, args.mock)
    council = Council(cfg, client, on_event=_on_event, seed=args.seed)
    try:
        res = await council.run(
            question,
            criteria=criteria,
            options=_read_options(args),
            context=context,
            mode=args.mode,
            rounds=args.rounds,
        )
    except CouncilError as e:
        console.print(f"[red bold]Заседание прервано:[/] {e}")
        return 1
    finally:
        await client.aclose()

    v = res.verdict
    win = res.proposals[v.winner]
    console.print(Panel(Markdown(v.final_answer), title=f"Решение: {v.winner} — {win.title} "
                        f"({res.authors.get(v.winner) or 'пользователь'})", border_style="green"))
    console.print(Markdown(f"**Почему:** {v.why}"))
    if v.dissent:
        console.print(Markdown("**Особые мнения:**\n" + "\n".join(f"- {x}" for x in v.dissent)))
    if v.risks:
        console.print(Markdown("**Риски:**\n" + "\n".join(f"- {x}" for x in v.risks)))
    runs_dir = Path(cfg.runs_dir)
    if not runs_dir.is_absolute():
        runs_dir = PROJECT_DIR / runs_dir
    json_path, md_path = save(res, runs_dir)
    console.print(f"\nСтоимость: [bold]${res.total_cost:.4f}[/], время: {res.duration_s} с")
    console.print(f"Отчёт: {md_path}\nСтенограмма: {json_path}")
    return 0


async def _models(args) -> int:
    client = OpenRouterClient(os.environ.get("OPENROUTER_API_KEY", ""))
    try:
        models = await client.list_models()
    finally:
        await client.aclose()
    q = (args.search or "").lower()
    t = Table(title="Модели OpenRouter (цена за 1M токенов, $)")
    for col in ("id", "контекст", "вход", "выход"):
        t.add_column(col)
    for m in sorted(models, key=lambda m: m["id"]):
        if q and q not in m["id"].lower() and q not in (m.get("name") or "").lower():
            continue
        pr = m.get("pricing") or {}
        price = lambda k: f"{float(pr.get(k) or 0) * 1e6:.2f}"  # noqa: E731
        t.add_row(m["id"], str(m.get("context_length", "")), price("prompt"), price("completion"))
    console.print(t)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_DIR / ".env")
    load_dotenv(".env")
    p = argparse.ArgumentParser(prog="council", description="Совет нейросетей через OpenRouter")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="провести заседание совета")
    a.add_argument("question", help='вопрос (или "-" чтобы прочитать из stdin)')
    a.add_argument("--mode", choices=["quick", "debate", "evaluate"], help="режим (по умолчанию из конфига)")
    a.add_argument("--rounds", type=int, help="число раундов критики/доработки в режиме debate")
    a.add_argument("--criteria", help='свои критерии через запятую, напр. "цена,скорость,риск"')
    a.add_argument("--option", action="append", help="готовый вариант для оценки (можно несколько раз)")
    a.add_argument("--options-file", help="файл с вариантами, разделёнными строкой ---")
    a.add_argument("--context-file", help="файл с дополнительным контекстом")
    a.add_argument("--only", help="участвуют только эти участники, через запятую")
    a.add_argument("--budget", type=float, help="лимит стоимости заседания в $")
    a.add_argument("--config", default=str(PROJECT_DIR / "config.yaml"))
    a.add_argument("--seed", type=int, help="seed для перемешивания меток")
    a.add_argument("--mock", action="store_true", help="прогон без ключа на фейковых ответах")

    m = sub.add_parser("models", help="список моделей OpenRouter (чтобы найти точные id)")
    m.add_argument("search", nargs="?", help="фильтр, напр. kimi или deepseek")

    args = p.parse_args(argv)
    try:
        return asyncio.run(_ask(args) if args.cmd == "ask" else _models(args))
    except KeyboardInterrupt:
        console.print("\n[yellow]Прервано[/]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
