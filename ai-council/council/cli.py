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

from .attachments import MAX_FILES, AttachmentError, assign_files, human_size, limit_total, load_attachment, merge
from .client import OpenRouterClient, ProviderError
from .config import load_config as _load_config
from .config import load_dotenv
from .estimate import estimate, parse_pricing, unknown_models, vision_map
from .mock import MockClient
from .report import save
from .reports.brand import set_brand
from .session import Council, CouncilError


def load_config(path):
    """Конфиг + фирменное оформление отчётов."""
    cfg = _load_config(path)
    set_brand(cfg.branding, Path(path).resolve().parent)
    return cfg


console = Console()
PROJECT_DIR = Path(__file__).resolve().parent.parent


class _LiveStatus:
    """Живая строка внизу консоли: этап, сколько ответов готово, что делает каждая модель."""

    PHASE = {"queued": "в очереди", "sent": "запрос отправлен", "thinking": "думает", "writing": "пишет",
             "checking": "проверка", "retry": "повтор", "done": "готово", "failed": "сбой"}

    def __init__(self) -> None:
        import time
        self.time = time
        self.stage = ""
        self.stage_started = time.monotonic()
        self.calls: dict[str, dict] = {}
        self.expected: list[str] = []
        self.status = None

    def on_event(self, event: str, d: dict) -> None:
        now = self.time.monotonic()
        if event == "stage":
            self.stage, self.stage_started, self.calls = d["title"], now, {}
            self.expected = []
        elif event == "call":
            c = self.calls.setdefault(d["member"], {})
            c.update(phase=d["phase"], since=now - float(d.get("elapsed") or 0), chars=d.get("chars", c.get("chars", 0)))
            if d["member"] not in self.expected:
                self.expected.append(d["member"])
            if d["phase"] == "retry":
                console.print(f"  [yellow]↻ {d['member']}: {d.get('detail') or 'повтор'}[/]")
            if d["phase"] in ("done", "failed"):
                c["final"] = float(d.get("elapsed") or 0)
        self.refresh()

    def text(self) -> str:
        now = self.time.monotonic()
        if not self.stage:
            return "Заседание запускается…"
        ready = sum(1 for c in self.calls.values() if c.get("phase") in ("done", "failed"))
        parts = []
        for name in self.expected:
            c = self.calls[name]
            ph = c.get("phase")
            if ph in ("done", "failed"):
                continue
            secs = int(now - c["since"])
            extra = f" ≈{max(1, round((c.get('chars') or 0) / 3.2))} ток." if ph == "writing" else ""
            parts.append(f"{name}: {self.PHASE.get(ph, ph)}{extra} {secs} с")
        stage_secs = int(now - self.stage_started)
        head = f"[bold cyan]{self.stage}[/] · готово {ready} из {len(self.expected) or '…'} · {stage_secs} с"
        return head + ("  —  " + "; ".join(parts) if parts else "")

    def refresh(self) -> None:
        if self.status is not None:
            self.status.update(self.text())


_live = _LiveStatus()


def _on_event(event: str, **d) -> None:
    if event in ("stage", "call"):
        _live.on_event(event, d)
        if event == "call":
            return
    if event == "stage":
        console.rule(f"[bold cyan]{d['title']}")
    elif event == "member_done":
        console.print(f"  [green]✓[/] {d['member']}")
    elif event == "member_failed":
        console.print(f"  [red]✗ {d['member']}[/] ({d['stage']}): {d['error'][:200]}")
    elif event == "warning":
        console.print(f"  [yellow]⚠ {d['member']}[/] ({d['stage']}): {d['text'][:200]}")
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
        for col in ("#", "Вариант", "Автор", "Шульце", "Борда", "Попарно", "Критика", "1-х мест"):
            t.add_column(col)
        for i, r in enumerate(d["rows"], 1):
            t.add_row(
                str(i),
                r.label,
                d["authors"].get(r.label) or "пользователь",
                f"{r.schulze} из {len(d['rows']) - 1}",
                f"{r.borda:.2f}",
                f"{r.pairwise:+.0f}",
                f"{r.avg_score:.1f}" if r.avg_score is not None else "—",
                f"{r.first_places}/{r.ballots}",
            )
        console.print(t)
        a = d.get("agreement")
        if a:
            from .voting import AGREEMENT_HINT, agreement_text
            color = {"high": "green", "medium": "yellow", "low": "red"}[a["level"]]
            console.print(f"Согласие совета: [{color}]{agreement_text(a)}[/] — {AGREEMENT_HINT[a['level']]}")
        if d.get("ballot_flags"):
            from .voting import FLAG_NAME
            console.print("[yellow]⚠ Бюллетени против общего мнения[/] (голоса учтены):")
            for n, f in d["ballot_flags"].items():
                console.print(f"  • {n} — " + "; ".join(FLAG_NAME[k] for k in f["flags"]))
            alt, lead = d.get("leader_without_flagged"), d["rows"][0].label
            console.print(f"  Без них лидер был бы [bold]{alt}[/]." if alt and alt != lead else "  Без них лидер тот же.")
    elif event == "factcheck":
        icon = {"confirmed": "[green]✓[/]", "refuted": "[red]✗[/]", "unclear": "[yellow]?[/]"}
        for label, r in d["results"].items():
            console.print(f"  [bold]{label}[/]: {r.get('summary', '')}")
            for c in r.get("claims", []):
                console.print(f"    {icon.get(c.get('status'), '?')} {c.get('claim')} — [dim]{c.get('note', '')}[/]")


def _read_options(args) -> list[str] | None:
    opts = list(args.option or [])
    if args.options_file:
        text = Path(args.options_file).read_text(encoding="utf-8")
        # варианты разделяются строкой из трёх дефисов или пустой строкой
        sep = "\n---\n" if "\n---\n" in text else "\n\n"
        opts += [o.strip() for o in text.split(sep) if o.strip()]
    return opts or None


def _make_client(cfg, mock: bool, *, need_key: bool = True):
    if mock:
        return MockClient()
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key and need_key:
        console.print(
            "[red]Нет OPENROUTER_API_KEY.[/] Создайте ключ на https://openrouter.ai/keys и положите его в "
            "файл .env (см. .env.example) или в переменную окружения. Для пробного прогона без ключа: --mock"
        )
        sys.exit(2)
    return OpenRouterClient(key, timeout=cfg.timeout_s, max_retries=cfg.max_retries,
                            max_tokens_cap=cfg.max_tokens_cap)


async def _preflight(client, cfg, args, *, mode, rounds, options, criteria, attachments=()) -> int | None:
    """Проверяет модели на OpenRouter, показывает оценку стоимости и спрашивает подтверждение.

    Возвращает код выхода, если запускать заседание не нужно, иначе None.
    """
    pricing: dict = {}
    models: list = []
    if not args.mock:
        try:
            models = await client.list_models()
        except Exception as e:  # noqa: BLE001 — проверка необязательна, само заседание может пройти
            console.print(f"[yellow]Не удалось получить список моделей OpenRouter ({e!r}); проверка пропущена.[/]")
            models = []
        if models:
            pricing = parse_pricing(models)
            missing = unknown_models(cfg, set(pricing))
            if missing:
                console.print("[red bold]На OpenRouter нет моделей из config.yaml:[/]")
                for model, similar in missing.items():
                    hint = f" — возможно: {', '.join(similar)}" if similar else ""
                    console.print(f"  • {model}{hint}")
                console.print("Исправьте id в config.yaml (поиск: python -m council models <название>) "
                              "или исключите участника через --only.")
                return 2

    args.vision = vision_map(cfg, models or None)
    imgs = sum(1 for a in attachments if a.kind == "image")
    from .estimate import learn_lengths, similar_actual
    lengths = learn_lengths(_runs_dir(cfg), compact=cfg.compact_proposals)
    est = estimate(cfg, pricing, mode=mode, rounds=rounds,
                   n_options=len(options) if options else None, has_criteria=bool(criteria),
                   doc_tokens=sum(a.tokens for a in attachments if a.kind == "document"),
                   images=imgs, vision=args.vision, question_chars=len(args.question or "") + (Path(args.context_file).stat().st_size
                                                                       if getattr(args, "context_file", None) else 0),
                   options_chars=sum(len(o) for o in options or []), lengths=lengths)
    console.print(f"[dim]Как посчитано: {est.basis}.[/]")
    sim = similar_actual(lengths, mode, len(cfg.active_members))
    if sim:
        console.print(f"[dim]Похожие заседания ({sim['count']}) стоили ${sim['min']:.2f}–${sim['max']:.2f}, "
                      f"в среднем ${sim['avg']:.2f}.[/]")
    blind = [m.name for m in cfg.active_members if args.vision.get(m.model) is False]
    if imgs and blind:
        console.print(f"[dim]Не видят картинки: {', '.join(blind)} — им передадут описание, "
                      "которое заранее сделает одна из «зрячих» моделей.[/]")
    t = Table(title=f"План заседания: режим {mode}" + (f", раундов {rounds}" if mode == "debate" else ""))
    for col in ("Участник", "Модель", "Вызовов", "Токены ~вход/выход", "Стоимость ~"):
        t.add_column(col)
    for m in est.members:
        cost = f"${m.cost:.4f}" if m.cost is not None else "?"
        t.add_row(m.name, m.model, str(m.calls), f"{m.prompt_tokens}/{m.completion_tokens}", cost)
    console.print(t)
    if pricing:
        console.print(f"Оценка: [bold]${est.low:.3f} – ${est.high:.3f}[/] за {est.calls} вызовов "
                      f"(лимит ${cfg.max_cost_usd:g})")
        if est.high > cfg.max_cost_usd:
            console.print("[yellow]Верхняя оценка выше лимита — заседание может остановиться по бюджету "
                          "(увеличьте --budget или уменьшите --rounds).[/]")
    if args.dry_run:
        return 0
    if not args.mock and not args.yes and sys.stdin.isatty():
        answer = console.input("Начать заседание? [Y/n] ").strip().lower()
        if answer not in ("", "y", "yes", "д", "да"):
            console.print("Отменено.")
            return 0
    return None


async def _ask(args) -> int:
    cfg = load_config(args.config)
    if args.budget:
        cfg.max_cost_usd = args.budget
    if args.chair_proposes:
        cfg.chair_proposes = True
    if args.fact_check:
        cfg.fact_check.enabled = True
    if args.compact:
        cfg.compact_proposals = True
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
    from .diagnostics import setup_logging
    setup_logging(_runs_dir(cfg), "cli", console=False)

    question = args.question
    if question == "-":
        question = sys.stdin.read().strip()
    context = Path(args.context_file).read_text(encoding="utf-8") if args.context_file else None
    criteria = [c for c in args.criteria.split(",")] if args.criteria else None

    names = ", ".join(m.name for m in cfg.active_members)
    console.print(Panel(f"Участники: {names}\nПредседатель: {cfg.chair}"
                        + (f"\nАдвокат дьявола: {cfg.devil_advocate}" if cfg.devil_advocate else "")
                        + f"\nЛимит бюджета: ${cfg.max_cost_usd:g}" + ("\n[yellow]MOCK-режим[/]" if args.mock else ""),
                        title="Совет нейросетей"))
    options = _read_options(args)
    atts = []
    for f in args.file or []:
        try:
            p = Path(f)
            atts.append(load_attachment(p.read_bytes(), p.name))
        except (OSError, AttachmentError) as e:
            console.print(f"[red]Не удалось приложить {f}:[/] {e}")
            return 2
    if len(atts) > MAX_FILES:
        console.print(f"[red]Можно приложить не больше {MAX_FILES} файлов[/]")
        return 2
    history: list = []
    followup_of = ""
    if args.followup:
        from .archive import Archive
        arch = Archive(_runs_dir(cfg))
        stem = args.followup if arch.exists(args.followup) else arch.find_key(args.followup)
        if not stem:
            console.print(f"[red]Заседание «{args.followup}» не найдено в архиве[/] (список: python -m council archive)")
            return 2
        history, parent_atts = arch.followup(stem)
        atts = merge(parent_atts, atts)
        followup_of = stem
        console.print(f"↳ Уточнение к заседанию: {history[-1]['title']}")
    assign_files(atts)
    for w in limit_total(atts):
        console.print(f"[yellow]{w}[/]")
    for a in atts:
        extra = f" [yellow]— {a.warning}[/]" if a.warning else ""
        console.print(f"📎 {a.name} ({a.kind}, {human_size(a.size)}){extra}")
    mode = "evaluate" if options else (args.mode or cfg.mode)
    rounds = args.rounds if args.rounds is not None else cfg.debate_rounds
    client = _make_client(cfg, args.mock, need_key=not args.dry_run)
    try:
        go = await _preflight(client, cfg, args, mode=mode, rounds=rounds, options=options, criteria=criteria,
                              attachments=atts)
        if go is not None:
            return go
        council = Council(cfg, client, on_event=_on_event, seed=args.seed, vision=getattr(args, "vision", None))

        async def ticker() -> None:  # секундомеры в живой строке идут, даже пока модели молчат
            while True:
                await asyncio.sleep(1)
                _live.refresh()

        with console.status(_live.text(), spinner="dots") as status:
            _live.status = status
            tick = asyncio.create_task(ticker())
            try:
                res = await council.run(
                    question,
                    criteria=criteria,
                    options=options,
                    context=context,
                    mode=mode,
                    rounds=rounds,
                    attachments=atts,
                    history=history,
                    followup_of=followup_of,
                )
                res.request = {"question": question, "mode": mode, "rounds": rounds, "criteria": criteria or [],
                               "options": options or [], "context": context or "",
                               "members": [m.name for m in cfg.active_members], "chair": cfg.chair,
                               "devil_advocate": cfg.devil_advocate or "", "budget": cfg.max_cost_usd,
                               "chair_proposes": cfg.chair_proposes, "fact_check": cfg.fact_check.enabled,
                               "compact_proposals": cfg.compact_proposals}
            finally:
                tick.cancel()
                _live.status = None
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
    with console.status("Готовлю отчёты: PDF, Word и Markdown…", spinner="dots"):
        paths = save(res, runs_dir, attachments=atts)
    console.print(f"\nСтоимость: [bold]${res.total_cost:.4f}[/], время: {res.duration_s} с")
    for fmt, title in (("pdf", "PDF"), ("docx", "Word"), ("md", "Markdown"), ("summary.pdf", "Итог (PDF)"),
                       ("summary.docx", "Итог (Word)"), ("decision.pdf", "Только решение (PDF)"),
                       ("decision.docx", "Только решение (Word)"), ("json", "Стенограмма")):
        if fmt in paths:
            console.print(f"{title}: {paths[fmt]}")
    return 0


async def _models(args) -> int:
    from .catalog import date, is_text_model, modalities, sort_newest, upgrades

    client = OpenRouterClient(os.environ.get("OPENROUTER_API_KEY", ""))
    try:
        models = await client.list_models()
    except Exception as e:  # noqa: BLE001 — сеть, прокси, блокировка: понятное сообщение вместо трассировки
        console.print(f"[red]Не удалось получить список моделей OpenRouter:[/] {e.__class__.__name__}: {e}\n"
                      "Проверьте доступ в интернет с этой машины (https://openrouter.ai).")
        return 1
    finally:
        await client.aclose()

    def price(m, k):
        return f"{float((m.get('pricing') or {}).get(k) or 0) * 1e6:.2f}"

    def marks(m):
        mod = modalities(m)
        return " ".join(x for x, ok in (("🖼", "image" in mod), ("🎤", "audio" in mod)) if ok)

    if args.upgrades:
        cfg = load_config(args.config)
        console.print("[bold]Чем можно обновить участников совета[/] (модели новее текущих, по дате выхода на OpenRouter)\n")
        for u in upgrades(cfg, models):
            state = "" if u["enabled"] else " [dim](выключен)[/]"
            cur = u["current"]
            head = (f"[bold]{u['name']}[/]{state}: {u['model']} — "
                    + (f"вышла {date(cur)}" if cur else "[red]такой модели больше нет на OpenRouter[/]"))
            console.print(head)
            rows = [("того же семейства", m) for m in u["same_family"]] + [("новинка разработчика", m) for m in u["newest"]]
            if not rows:
                console.print("   [green]новее ничего нет[/]")
            for kind, m in rows:
                console.print(f"   → {m['id']}  [dim]{date(m)} · {kind} · вход ${price(m, 'prompt')} / выход "
                              f"${price(m, 'completion')} за 1M · контекст {m.get('context_length', '?')}[/] {marks(m)}")
        console.print("\nЗаменить: в config.yaml поменяйте model у участника (на сервере: ai-council config).\n"
                      "Перед заменой стоит прогнать одно заседание и сравнить с прежним составом.")
        return 0

    q = (args.search or "").lower()
    t = Table(title="Модели OpenRouter — сначала новые (цена за 1M токенов, $; 🖼 картинки, 🎤 звук)")
    for col in ("id", "вышла", "контекст", "вход", "выход", ""):
        t.add_column(col)
    shown = 0
    for m in sort_newest(models):
        if q and q not in m["id"].lower() and q not in (m.get("name") or "").lower():
            continue
        if not args.all and not is_text_model(m):
            continue
        t.add_row(m["id"], date(m), str(m.get("context_length", "")), price(m, "prompt"), price(m, "completion"), marks(m))
        shown += 1
        if args.limit and shown >= args.limit:
            break
    console.print(t)
    return 0


def _web(args) -> int:
    try:
        import uvicorn

        from .web.app import create_app
    except ImportError:
        console.print("[red]Для веб-интерфейса установите зависимости:[/] pip install -r requirements.txt")
        return 2
    load_config(args.config)  # сразу показать ошибку конфига, а не при первом запросе
    in_docker = Path("/.dockerenv").exists()  # в контейнере порт наружу ограничивает docker-compose.yml
    if args.host not in ("127.0.0.1", "localhost") and not in_docker and not os.environ.get("WEB_PASSWORD"):
        console.print("[yellow]Внимание: интерфейс без пароля будет доступен всем в сети, "
                      "и любой сможет тратить ваш баланс OpenRouter. Задайте WEB_PASSWORD в .env.[/]")
    console.print(f"Совет нейросетей: [bold]http://{args.host}:{args.port}[/]  (Ctrl+C — остановить)")
    uvicorn.run(create_app(args.config), host=args.host, port=args.port, log_level="warning")
    return 0


def _runs_dir(cfg) -> Path:
    d = Path(cfg.runs_dir)
    return d if d.is_absolute() else PROJECT_DIR / d


def _stats_cmd(args) -> int:
    from .archive import Archive
    from .stats import compute, fmt_duration

    d = compute(Archive(_runs_dir(load_config(args.config))))
    if not d["sessions"]:
        console.print("Пока нет заседаний.")
        return 0
    console.print(f"Заседаний: [bold]{d['sessions']}[/] · потрачено [bold]${d['total_cost']:.2f}[/] · "
                  f"в этом месяце ${d['month_cost']:.2f} · в среднем ${d['avg_cost']:.3f} и "
                  f"{fmt_duration(d['avg_duration'])} на заседание")
    t = Table(title="Модели")
    for col in ("Участник", "Модель", "Заседаний", "Побед", "Доля побед", "Ср. оценка", "Сбоев", "Расходы"):
        t.add_column(col)
    for m in d["models"]:
        t.add_row(m["name"], m["model"], str(m["sessions"]), f"{m['wins']}/{m['proposals']}",
                  f"{round(m['win_rate'] * 100)}%" if m["win_rate"] is not None else "—",
                  f"{m['avg_score']:.1f}" if m["avg_score"] is not None else "—", str(m["failures"]), f"${m['cost']:.4f}")
    console.print(t)
    if d.get("chairs"):
        t = Table(title="Председатели: как часто итог расходится с голосованием")
        for col in ("Председатель", "Заседаний", "Выбрал не лидера", "Доля", "Не ответил", "Ср. согласие"):
            t.add_column(col)
        for c in d["chairs"]:
            t.add_row(c["name"], str(c["sessions"]), f"{c['overrides']}/{c['decisions']}",
                      f"{round(c['override_rate'] * 100)}%" if c["override_rate"] is not None else "—",
                      str(c["fallbacks"]), f"{c['avg_agreement']:.2f}" if c["avg_agreement"] is not None else "—")
        console.print(t)
        console.print("[dim]«Выбрал не лидера» — победителем назван не первый вариант голосования. Немного таких случаев "
                      "(с обоснованием) — нормально; часто — председатель своеволен.[/]")
    return 0


async def _search_test(args) -> int:
    """Один короткий запрос с поиском: работает ли поиск, сколько стоит, возвращает ли ссылки и даты."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        console.print("[red]Нет OPENROUTER_API_KEY[/]")
        return 2
    cfg = load_config(args.config)
    model = args.model or cfg.fact_check.model
    client = OpenRouterClient(key, timeout=cfg.timeout_s, max_retries=1)
    question = ("Find the current official price per 1M input tokens of one popular LLM API and cite the source. "
                'Reply as JSON: {"answer": "...", "sources": [{"url": "...", "date": "..."}]}')
    try:
        comp = await client.complete(model, [{"role": "user", "content": question}], temperature=0.1, max_tokens=800,
                                     web_search=True)
    except ProviderError as e:
        console.print(f"[red]Поиск не сработал:[/] {e}")
        return 1
    finally:
        await client.aclose()
    console.print(f"Модель: [bold]{comp.model or model}[/]  ·  стоимость запроса: [bold]${comp.cost:.4f}[/]  ·  "
                  f"токены {comp.prompt_tokens}/{comp.completion_tokens}")
    console.print(f"Ссылок вернул сам поиск: [bold]{len(comp.citations)}[/]" +
                  ("" if comp.citations else "  [yellow](список ссылок поиск не вернул — в арбитраже ссылки будут "
                                             "приниматься только по ответу модели)[/]"))
    for u in comp.citations[:8]:
        console.print(f"  · {u}")
    console.print("[dim]Ответ модели:[/] " + comp.text[:600])
    return 0


def _archive_cmd(args) -> int:
    from .archive import Archive, available_formats
    from .reports.pdf import PDF_AVAILABLE, PDF_ERROR

    arch = Archive(_runs_dir(load_config(args.config)))
    if args.cmd == "reports":
        if not PDF_AVAILABLE:
            console.print(f"[yellow]PDF недоступен ({PDF_ERROR}); будут созданы {', '.join(available_formats())}[/]")
        built, failed = arch.ensure_all(force=args.force, progress=lambda stem, res: console.print(
            f"  {stem}: " + ", ".join(f if not r.startswith("ошибка") else f"[red]{f} ✗[/]" for f, r in res.items())))
        console.print(f"Готово: обновлено {built} заседаний, с ошибками {failed}. Папка: {arch.dir}")
        return 1 if failed else 0
    items = arch.list(args.search)
    t = Table(title=f"Архив заседаний ({len(items)})")
    for col in ("Ключ", "Дата", "Вопрос", "Решение", "Режим", "$"):
        t.add_column(col, no_wrap=col == "Ключ")
    for s in items:
        t.add_row(s["key"], s["date"], ("↳ " if s.get("followup_of") else "") + s["question"][:70],
                  f"{s['winner']}: {s['winner_title'][:40]}", s["mode_name"], f"{s['cost']:.3f}")
    console.print(t)
    console.print(f"Отчёты лежат рядом со стенограммами: {arch.dir}\n"
                  "Уточняющий вопрос к заседанию: python -m council ask \"вопрос\" --followup <ключ>")
    return 0


def _telegram(args) -> int:
    import logging

    from .telegram.bot import run_bot

    load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    console.print("Telegram-бот запущен (Ctrl+C — остановить)")
    try:
        asyncio.run(run_bot(args.config))
    except KeyboardInterrupt:
        console.print("\n[yellow]Бот остановлен[/]")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_DIR / ".env")
    load_dotenv(".env")
    p = argparse.ArgumentParser(prog="council", description="Совет нейросетей через OpenRouter")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="провести заседание совета")
    a.add_argument("question", help='вопрос (или "-" чтобы прочитать из stdin)')
    a.add_argument("--mode", choices=["quick", "debate", "evaluate", "research", "creative"], help="режим (по умолчанию из конфига)")
    a.add_argument("--rounds", type=int, help="число раундов критики/доработки в режиме debate")
    a.add_argument("--criteria", help='свои критерии через запятую, напр. "цена,скорость,риск"')
    a.add_argument("--option", action="append", help="готовый вариант для оценки (можно несколько раз)")
    a.add_argument("--options-file", help="файл с вариантами, разделёнными строкой ---")
    a.add_argument("--context-file", help="файл с дополнительным контекстом")
    a.add_argument("--followup", metavar="ID", help="уточняющий вопрос к заседанию из архива (id из `archive`)")
    a.add_argument("--file", action="append", metavar="ПУТЬ",
                   help="приложить файл: картинку, PDF, Word, Excel, текст (можно несколько раз)")
    a.add_argument("--only", help="участвуют только эти участники, через запятую")
    a.add_argument("--budget", type=float, help="лимит стоимости заседания в $")
    a.add_argument("--compact", action="store_true",
                   help="компактные варианты: участники пишут суть сжато, полный формат — только в итоге (дешевле)")
    a.add_argument("--fact-check", action="store_true",
                   help="проверить факты в лидирующих вариантах моделью с поиском (по умолчанию — Perplexity)")
    a.add_argument("--chair-proposes", action="store_true",
                   help="председатель тоже предлагает свой вариант (по умолчанию — только ставит задачу, оценивает и пишет итог)")
    a.add_argument("--config", default=str(PROJECT_DIR / "config.yaml"))
    a.add_argument("--seed", type=int, help="seed для перемешивания меток")
    a.add_argument("--mock", action="store_true", help="прогон без ключа на фейковых ответах")
    a.add_argument("--dry-run", action="store_true", help="только проверить модели и оценить стоимость")
    a.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждение перед запуском")

    m = sub.add_parser("models", help="модели OpenRouter: сначала новые; --upgrades — чем обновить совет")
    m.add_argument("search", nargs="?", help="фильтр, напр. claude, gpt, gemini, kimi, deepseek")
    m.add_argument("--upgrades", action="store_true", help="для каждого участника — более новые модели")
    m.add_argument("--limit", type=int, default=40, help="сколько строк показать (0 — все)")
    m.add_argument("--all", action="store_true", help="включая модели не для текста (картинки, эмбеддинги)")
    m.add_argument("--config", default=str(PROJECT_DIR / "config.yaml"))

    w = sub.add_parser("web", help="запустить веб-интерфейс")
    w.add_argument("--host", default="127.0.0.1", help="адрес (0.0.0.0 — доступ из локальной сети)")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--config", default=str(PROJECT_DIR / "config.yaml"))

    stp = sub.add_parser("stats", help="статистика: расходы, длительность, успехи моделей")
    stp.add_argument("--config", default=str(PROJECT_DIR / "config.yaml"))

    ar = sub.add_parser("archive", help="архив заседаний (список, поиск)")
    ar.add_argument("search", nargs="?", default="", help="текст для поиска по вопросам и решениям")
    ar.add_argument("--config", default=str(PROJECT_DIR / "config.yaml"))

    rp = sub.add_parser("reports", help="создать недостающие отчёты PDF/DOCX/MD для всех заседаний архива")
    rp.add_argument("--force", action="store_true", help="пересобрать все отчёты заново")
    rp.add_argument("--config", default=str(PROJECT_DIR / "config.yaml"))

    st = sub.add_parser("search-test", help="проверить поиск в интернете: один дешёвый запрос, цена и найденные ссылки")
    st.add_argument("--model", help="модель (по умолчанию — модель проверки фактов из config.yaml)")
    st.add_argument("--config", default=str(PROJECT_DIR / "config.yaml"))

    t = sub.add_parser("telegram", help="запустить Telegram-бота")
    t.add_argument("--config", default=str(PROJECT_DIR / "config.yaml"))

    args = p.parse_args(argv)
    if args.cmd == "telegram":
        return _telegram(args)
    if args.cmd in ("archive", "reports"):
        return _archive_cmd(args)
    if args.cmd == "stats":
        return _stats_cmd(args)
    if args.cmd == "web":
        return _web(args)
    if args.cmd == "search-test":
        return asyncio.run(_search_test(args))
    try:
        return asyncio.run(_ask(args) if args.cmd == "ask" else _models(args))
    except KeyboardInterrupt:
        console.print("\n[yellow]Прервано[/]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
