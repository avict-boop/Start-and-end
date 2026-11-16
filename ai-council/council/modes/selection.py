"""Режим «Отбор»: рамка → поиск кандидатов → отсев → подробное заполнение → разбор спорного → ранг и вывод.

Таблица «объекты × признаки» вместо сочинений: модели заполняют ячейки (значение, единица, дата, ссылка, дословная
цитата), программа сама скачивает страницы, ищет цитату и пересчитывает единицы. Совпало у двух источников и цитата
найдена — ячейка подтверждена без обсуждения; совет решает только спорное. Ранг, устойчивость ранга и «что выяснить»
считает программа по весам признаков; председатель пишет только короткий вывод.

Остановки «проверьте список» (на сайте): после рамки, после поиска кандидатов, после отсева — заседание встаёт и ждёт
решения человека; при продолжении всё оплаченное берётся из журнала, решение — из запроса (review).
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import selection as S
from ..client import OutOfCredits, ProviderError
from ..config import MemberConfig
from ..schemas import Verdict
from ..session import FACT_CHECKER, BudgetExceeded, InvalidResponse, MemberStats, SessionResult

if TYPE_CHECKING:
    from ..session import Council

log = logging.getLogger("council.selection")
META = {"selection": True}


def _soft(e: BaseException) -> bool:
    """Сбой одного запроса (не деньги и не бюджет) — этап идёт дальше без него."""
    return isinstance(e, (ProviderError, InvalidResponse)) and not isinstance(e, OutOfCredits)


NATIVE_SEARCH = ("openai/", "google/", "anthropic/", "x-ai/")  # у этих разработчиков свой поисковик в OpenRouter


def has_native_search(model: str) -> bool:
    return model.startswith(NATIVE_SEARCH) or model.startswith("perplexity/")


def discover_plan(angles: list[str], hunters: list[MemberConfig], k: int, shift: int = 0
                  ) -> list[tuple[str, MemberConfig]]:
    """Кто ищет по какому направлению: модель поиска (первая в hunters) — по всем; участники — по k на направление,
    по очереди, чтобы каждому досталось поровну и у каждого направления были разные поисковики. k=0 — все по всем."""
    if not k or len(hunters) <= k + 1:
        return [(a, m) for a in angles for m in hunters]
    head, rest = hunters[0], hunters[1:]
    plan = []
    for i, a in enumerate(angles):
        plan.append((a, head))
        plan += [(a, rest[(shift * k + i * k + j) % len(rest)]) for j in range(k)]
    return plan


def model_title(model: str) -> str:
    """«perplexity/sonar-pro» → «Sonar Pro»."""
    tail = (model or "").split("/")[-1].split(":")[0]
    return " ".join(w if any(ch.isdigit() for ch in w) and len(w) > 3 else w.capitalize()
                    for w in tail.replace("_", "-").split("-") if w) or model


def search_label(c: Council) -> str:
    return f"поиск · {model_title(c.cfg.selection.search_model or c.cfg.fact_check.model)}"


LIST_CHUNK = 30_000  # знаков списка в одном запросе чтения


def list_reader(c: Council, hunters: list[MemberConfig]) -> MemberConfig:
    """Кто читает готовые списки: из настроек; иначе самый дешёвый разведчик (не Perplexity — ему нужен поиск, а
    здесь текст уже скачан); иначе самый дешёвый участник."""
    sc = c.cfg.selection
    if sc.list_reader:
        return MemberConfig(name="чтение списков", model=sc.list_reader, max_tokens=12000, temperature=0.1,
                            reasoning_effort="low")
    price = getattr(c, "pricing", None) or {}

    def cost(m: MemberConfig) -> float:
        p = price.get(m.model)
        return (p.prompt * 4 + p.completion) if p else 1e9

    pool = [m for m in hunters if m.name.startswith("поиск ") and not m.model.startswith("perplexity/")]
    pool = sorted(pool, key=cost) or pick_fillers(c)
    m = pool[0]
    return m.model_copy(update={"max_tokens": max(m.max_tokens or 0, 12000), "reasoning_effort": "low"})


async def harvest_lists(c: Council, frame: S.SelectionFrame, hunters: list[MemberConfig]
                        ) -> tuple[list[tuple[str, S.Candidate]], int]:
    """Готовые списки компаний: модель поиска находит ссылки, программа скачивает, дешёвая модель выписывает всех
    подходящих. Возвращает (находки «список · читатель», сколько списков прочитано)."""
    sc = c.cfg.selection
    if not sc.lists:
        return [], 0
    try:
        ls = await c._ask(searcher(c), "lists", S.list_sources_prompt(frame, sc.lists), S.ListSources,
                          meta=META, web_search=True)
    except (ProviderError, InvalidResponse) as e:
        if not _soft(e):
            raise
        return [], 0
    items = [x for x in ls.lists if x.url.startswith(("http://", "https://"))][:sc.lists]
    if not items:
        return [], 0
    pages = await fetch_pages(c, [x.url for x in items])
    reader = list_reader(c, hunters)
    jobs, meta_of = [], []
    for x in items:
        text = (pages.get(x.url) or {}).get("text") or ""
        if len(text) < 300:
            continue
        text = text[:sc.list_chars]
        parts = [text[i:i + LIST_CHUNK] for i in range(0, len(text), LIST_CHUNK)]
        label = f"список: {(x.title or S.site_of(x.url) or x.url)[:80]}"
        for k, part in enumerate(parts, 1):
            jobs.append((label, S.list_read_prompt(frame, x.title, x.url, part, k, len(parts))))
            meta_of.append((label, x.url))
    if not jobs:
        c._emit("lists_read", lists=len(items), opened=0, found=0)
        return [], 0
    prog = {"done": 0, "total": len(jobs), "found": 0, "round": 0}
    c._emit("progress", stage="discover", unit="частей списков", **prog)

    async def read(label: str, prompt: str) -> Any:
        try:
            return await c._ask(reader, "list_read", prompt, S.Candidates, meta={**META, "angle": label})
        finally:
            prog["done"] += 1
            c._emit("progress", stage="discover", unit="частей списков", **prog)

    got = await asyncio.gather(*(read(lb, p) for lb, p in jobs), return_exceptions=True)
    out: list[tuple[str, S.Candidate]] = []
    per: dict[str, int] = {}
    for (label, url), r in zip(meta_of, got):
        if isinstance(r, (BudgetExceeded, OutOfCredits)):
            raise r
        if isinstance(r, BaseException):
            if not _soft(r):
                raise r
            continue
        for cand in r.items:
            if not cand.url:
                cand.url = url
            out.append((f"{label}{S.BY}{reader.name}", cand))
            per[label] = per.get(label, 0) + 1
    for label, n in per.items():
        c._emit("candidates_found", angle=label, by=reader.name, count=n)
    c._emit("lists_read", lists=len(items), opened=len({u for _, u in meta_of}), found=len(S.merge_candidates(out)))
    log.info("%sготовые списки: найдено %d, открыто %d, кандидатов %d", c._tag, len(items),
             len({u for _, u in meta_of}), len(S.merge_candidates(out)))
    return out, len(per)


def discover_hunters(c: Council) -> list[MemberConfig]:
    """Кто ищет кандидатов. scouts — модель поиска, вторая модель Perplexity и младшие модели Google, OpenAI,
    Anthropic, xAI со своими поисковиками (каждый — по всем направлениям); all — модель поиска и участники совета;
    search — только модель поиска."""
    sc = c.cfg.selection
    base = [searcher(c)]
    if sc.discover_by == "search":
        return base
    if sc.discover_by == "scouts":
        from ..scouts import PROVIDERS, model_title, perplexity_pair, pick_scouts, scout_name
        catalog = getattr(c, "catalog", None)
        auto = pick_scouts(catalog, tuple(p for p in PROVIDERS if p not in sc.scouts_skip))
        ids = list(dict.fromkeys(perplexity_pair(base[0].model, catalog) + (list(sc.scouts) or list(auto.values()))))
        ids = [i for i in ids if i != base[0].model]
        eff = None if sc.search_reasoning == "default" else sc.search_reasoning
        scouts, labels = [], {}
        for mid in ids:
            name = scout_name(mid)
            k = 2
            while any(s.name == name for s in scouts):
                name = f"{scout_name(mid)} {k}"
                k += 1
            scouts.append(MemberConfig(name=name, model=mid, max_tokens=max(c.cfg.fact_check.max_tokens, 8000),
                                       temperature=0.2, reasoning_effort=None if mid.startswith("perplexity/") else eff))
            c.stats.setdefault(name, MemberStats())
            labels[name] = f"{name} · {model_title(mid)}"
        if len(scouts) >= 2:
            c._emit("labels", labels=labels)
            log.info("%sпоиск кандидатов: разведчики %s", c._tag, {s.name: s.model for s in scouts})
            return base + scouts
        log.warning("%sразведчиков не подобрать (нет каталога моделей) — ищут участники совета", c._tag)
    return base + [quick(c, m) for m in c.cfg.active_members]


def quick(c: Council, m: MemberConfig) -> MemberConfig:
    """Участник для поиска кандидатов: короткие рассуждения (выписать названия из найденного — не рассуждение)."""
    eff = c.cfg.selection.search_reasoning
    return m if eff == "default" else m.model_copy(update={"reasoning_effort": eff})


def searcher(c: Council) -> MemberConfig:
    sc = c.cfg.selection
    fc = c.cfg.fact_check
    c.stats.setdefault(FACT_CHECKER, MemberStats())
    return MemberConfig(name=FACT_CHECKER, model=sc.search_model or fc.model,
                        max_tokens=max(fc.max_tokens, 8000), temperature=0.1, reasoning_effort="none")


def pick_fillers(c: Council) -> list[MemberConfig]:
    """Две модели, независимо читающие страницы: из настроек или две самые дешёвые (кроме председателя)."""
    sc = c.cfg.selection
    members = c.cfg.active_members
    named = [m for n in sc.fillers for m in members if m.name == n]
    if len(named) >= 2:
        return named[:2]
    pool = [m for m in members if m.name != c.cfg.chair and m not in named] or [m for m in members if m not in named]

    def price(m: MemberConfig) -> float:
        p = c.pricing.get(m.model)
        return (p.prompt * 4 + p.completion) if p else 1e9

    pool.sort(key=lambda m: (price(m), members.index(m)))
    out = (named + pool)[:2]
    if len(out) < 2 and members:
        out = (out + [m for m in members if m not in out])[:2]
    return out


def pick_judges(c: Council, fillers: list[MemberConfig]) -> list[MemberConfig]:
    """Кто решает спорное: председатель и другие участники, по возможности не те, кто заполнял."""
    sc = c.cfg.selection
    members = c.cfg.active_members
    chair = c.cfg.member(c.cfg.chair)
    others = [m for m in members if m.name != chair.name and m not in fillers]
    rest = [m for m in members if m.name != chair.name and m in fillers]
    return ([chair] + others + rest)[:max(1, sc.judges)]


def gate(c: Council, name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Остановка «проверьте»: решение уже есть (продолжение после правки) — вернуть его; остановки выключены — None;
    иначе заседание встаёт (сайт показывает список)."""
    if not c.review_enabled or name not in c.cfg.selection.review:
        return None
    dec = (c.review_decisions or {}).get(name)
    if dec is not None:
        c._emit("review_applied", gate=name)
        return dec
    c._emit("review", gate=name, **payload)
    raise S.ReviewNeeded(name, payload)


# ------------------------------------------------------------------------------------------------ страницы

def _snap_path(c: Council, url: str) -> Path | None:
    d = getattr(c, "snapshot_dir", None)
    return Path(d) / f"{S.sha(url)}.json.gz" if d and c.cfg.selection.snapshots else None


async def fetch_pages(c: Council, urls: list[str], browser: bool = True) -> dict[str, dict[str, Any]]:
    """Текст страниц (HTML и PDF): из памяти, из копии на диске (продолжение — те же тексты, ответы моделей из
    журнала совпадут) или скачать заново; скачанное сохраняется копией с датой."""
    out: dict[str, dict[str, Any]] = {}
    todo = []
    for u in dict.fromkeys(u for u in urls if u and u.startswith(("http://", "https://"))):
        if u in c.page_cache:
            out[u] = {"text": c.page_cache[u], "fetched": c.page_meta.get(u, {}).get("fetched", ""),
                      "status": c.page_meta.get(u, {}).get("status", "ok")}
            continue
        p = _snap_path(c, u)
        if p and p.is_file():
            try:
                d = await asyncio.to_thread(lambda: json.loads(gzip.decompress(p.read_bytes())))
                c.page_cache[u] = d.get("text", "")
                c.page_meta[u] = {"fetched": d.get("fetched", ""), "status": d.get("status", "ok"), "sha": d.get("sha", "")}
                out[u] = {"text": c.page_cache[u], "fetched": d.get("fetched", ""), "status": d.get("status", "ok")}
                continue
            except (OSError, ValueError):
                pass
        todo.append(u)
    if todo and c.link_checker:
        try:
            got = await (c.link_checker(todo, texts=True) if browser else c.link_checker(todo, texts=True, browser=False))
        except TypeError:
            try:  # проверка ссылок без параметра browser (тесты, старые подмены)
                got = await c.link_checker(todo, texts=True) if not browser else {}
            except TypeError:
                got = {}
        except Exception as e:  # noqa: BLE001 — страницы не скачались: ячейки останутся «не подтверждено»
            log.warning("%sстраницы не скачались: %s", c._tag, e)
            got = {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for u in todo:
            r = (got or {}).get(u) or {}
            text = S.plain(r.get("text") or "")  # как у проверки ссылок: без регистра, кавычек и разных тире
            status = r.get("status") or "error"
            c.page_cache[u] = text
            c.page_meta[u] = {"fetched": now, "status": status, "sha": S.sha(text) if text else "",
                              "pdf": bool(r.get("pdf"))}
            out[u] = {"text": text, "fetched": now, "status": status}
            p = _snap_path(c, u)
            if p and text:
                try:
                    await asyncio.to_thread(_save_snapshot, p, {"url": u, "fetched": now, "status": status,
                                                                "sha": S.sha(text), "text": text})
                except OSError as e:
                    log.warning("%sкопия страницы не сохранилась: %s", c._tag, e)
    for u in todo:
        out.setdefault(u, {"text": "", "fetched": "", "status": "error"})
    return out


def _save_snapshot(p: Path, d: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(gzip.compress(json.dumps(d, ensure_ascii=False).encode("utf-8")))


def trim_snapshots(folder: Path | str, limit_mb: int) -> int:
    """Папка копий страниц не больше limit_mb: старые копии удаляются первыми. Возвращает число удалённых."""
    d = Path(folder)
    if not d.is_dir():
        return 0
    files = sorted(d.glob("*.json.gz"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    removed = 0
    while files and total > limit_mb * 1024 * 1024:
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)
        removed += 1
    return removed


# ------------------------------------------------------------------------------------------------ этапы

async def run(c: Council, question: str, *, criteria: list[str] | None, context: str | None, followup_of: str,
              started: str, t0: float) -> SessionResult:
    sc = c.cfg.selection
    chair = c.cfg.member(c.cfg.chair)
    docs = "\n".join(a.text for a in c.attachments if getattr(a, "kind", "") == "document")
    from ..splitter import heuristic_objects
    hint = list(c.fixed_objects) or heuristic_objects("\n".join([question, context or "", docs]))

    # 1. Рамка
    c._emit("labels", labels={FACT_CHECKER: search_label(c)})  # на экране — «поиск · Sonar Pro», а не «факт-чек»
    for st in ("discover", "screen", "sources", "lists"):  # свой предел поиска: найденное идёт модели на вход
        c.stage_search_limits[st] = (sc.search_uses, sc.search_results)
    c._emit("stage", stage="framing", title="Рамка отбора: кто кандидат, признаки и веса, где искать")
    start0 = sc.start
    try:
        # этапы «Отбора» — свои имена в журнале и в истории длин ответов (не путать с «framing» и «verdict» других
        # режимов: короткий вывод отбора иначе занижал бы резерв длинного итога исследования)
        # что просит задание — найти кандидатов, проверить данный список или и то и другое — решает председатель:
        # названия в тексте бывают конкурентами, примерами и исключёнными, а не списком на проверку
        frame = await c._ask(chair, "frame",
                             S.frame_prompt(question, context, criteria, hint, start0, sc.max_attributes, sc.angles),
                             S.SelectionFrame, meta=META)
    except (ProviderError, InvalidResponse) as e:
        if not _soft(e):
            raise
        raise S.CouncilError(f"рамка отбора не получилась: {e}") from e
    if not frame.attributes:
        raise S.CouncilError("рамка отбора без признаков — нечего сравнивать")
    bench = {S.norm_name(b) for b in frame.benchmarks if S.norm_name(b)}
    named = list(c.fixed_objects) or (hint if start0 in ("check", "extend") and not frame.objects else [])
    seeds = [o for o in dict.fromkeys([*named, *frame.objects]) if S.norm_name(o) not in bench]
    if start0 != "auto":
        start = start0
    elif c.fixed_objects:  # объекты части серии — всегда проверка данного списка
        start = "check"
    else:
        start = frame.start or ("check" if len(seeds) >= 3 else "find")
    if start == "check" and not seeds:
        start = "find"  # проверять нечего — ищем
    log.info("%sотбор: председатель — «%s», кандидатов в задании %d, не кандидаты (конкуренты, исключённые): %s",
             c._tag, frame.start or "—", len(seeds), ", ".join(frame.benchmarks) or "—")
    frame = apply_frame_edits(frame, gate(c, "frame", {"frame": frame.model_dump()}))
    attrs = frame.attributes
    if not attrs:
        raise S.CouncilError("в рамке не осталось признаков — нечего сравнивать")
    top_n = sc.top_n or frame.top_n or 12  # первая группа: из настроек, из задания, иначе 12
    c._emit("selection_frame", frame=frame.model_dump(), start=start, seeds=len(seeds))
    log.info("%sотбор: старт %s, признаков %d, объектов в задании %d", c._tag, start, len(attrs), len(seeds))

    # 2. Поиск кандидатов
    found_meta: dict[str, Any] = {}
    if start in ("find", "extend"):
        entities, found_meta = await discover(c, frame, seeds if start == "extend" else [])
    else:
        entities = S.merge_candidates([], seeds)
    if not entities:
        raise S.CouncilError("кандидатов не нашлось — уточните задание или дайте свой список")
    # предел списка: по очереди из каждого направления (а не «найденных чаще всего» — те, кого нашёл один поиск,
    # специалисты и сырьевики, иначе уходили первыми); сколько не вошло — видно на остановке
    # предел не меньше двойного размера таблицы из задания: часть кандидатов отсеется, а таблица должна набраться
    cap_c = max(sc.max_candidates, 2 * max(sc.shortlist, frame.shortlist, frame.top_n)) if sc.max_candidates else 0
    cut = max(0, len(entities) - cap_c) if cap_c else 0  # 0 — без предела
    if cut:
        entities = diverse_order(entities)[:cap_c]
        log.info("%sотбор: кандидатов %d, предел %d — %d не вошли", c._tag, cap_c + cut, cap_c, cut)
        c._emit("candidates_cut", shown=len(entities), cut=cut, limit=cap_c)
    if start in ("find", "extend"):
        dec = gate(c, "candidates", {"items": entities, "completeness": found_meta.get("completeness"), "cut": cut})
        entities = apply_list_edits(entities, dec)

    # 3. Отсев
    excluded: list[dict[str, Any]] = []
    screened: dict[str, dict[str, Any]] = {}
    screen_attrs = [a for a in attrs if a.screen]
    if sc.screen and screen_attrs and start != "check":
        screened = await screen(c, frame, screen_attrs, entities)
        for e in entities:
            r = screened.get(e["id"]) or {"result": "unclear", "reason": "отсев не дал ответа"}
            e["screen"] = r
        # ваш список отсев не выбрасывает: вы сами назвали эти объекты — они идут на подробную проверку с пометкой
        excluded = [{"id": e["id"], "name": e["name"], "reason": e["screen"]["reason"]}
                    for e in entities if e["screen"]["result"] == "fail" and not e.get("seed")]
        passed = [e for e in entities if e["screen"]["result"] == "pass"]
        unclear = [e for e in entities if e["screen"]["result"] == "unclear"]
        seeds_failed = [e for e in entities if e["screen"]["result"] == "fail" and e.get("seed")]
        keep = passed + unclear + seeds_failed
    else:
        keep = list(entities)
    # сколько проверять подробно: из настроек; иначе — сколько просит задание (широкий список «60–80» → 80: это
    # число сильнее предела max_deep — его назвали вы сами), но не меньше первой группы; иначе — max_deep
    task_n = max(frame.shortlist, frame.top_n) if frame.shortlist else 0
    limit = sc.shortlist or task_n or sc.max_deep
    cap = min(200, max(sc.max_deep, sc.shortlist or task_n))
    keep.sort(key=lambda e: (not e.get("seed"), (e.get("screen") or {}).get("result") != "pass", -len(e["angles"])))
    keep = diverse_order(keep)
    shortlist = keep[:min(limit, cap)]
    if start != "check":
        dec = gate(c, "shortlist", {"items": entities, "shortlist": [e["id"] for e in shortlist],
                                    "excluded": excluded})
        if dec is not None:
            ids = [i for i in dec.get("keep") or [] if any(e["id"] == i for e in entities)]
            # добавленное вами по имени, если программа его уже нашла (под другим названием) — это тот же кандидат
            # с его страницами, а не новый пустой объект
            add = []
            for name in dec.get("add") or []:
                hit = _match_entity_strict(str(name), entities)
                if hit is not None:
                    hit["added"] = True
                    if hit["id"] not in ids:
                        ids.append(hit["id"])
                else:
                    add.append(name)
            shortlist = [e for i in ids for e in entities if e["id"] == i] + apply_list_edits([], {"add": add},
                                                                                                start_n=_max_id(entities))
            entities += [e for e in shortlist if e not in entities]
    keep_ids = {e["id"] for e in shortlist}
    excluded = [x for x in excluded if x["id"] not in keep_ids]  # возвращённые вами на остановке — не «исключены»
    c._emit("shortlist", count=len(shortlist), excluded=len(excluded), total=len(entities),
            objects=[{"id": e["id"], "name": e["name"]} for e in shortlist])

    # 4. Подробное заполнение
    # страница, общая для трёх и более объектов, — список или каталог (выставка, реестр), а не страница завода:
    # её не считаем «уже найденной страницей объекта»
    freq: dict[str, int] = {}
    for e in shortlist:
        for u in {e.get("url") or "", *(e.get("urls") or [])}:
            if u:
                freq[u] = freq.get(u, 0) + 1
    c.list_urls = {u for u, k in freq.items() if k >= 3}
    # сайт → объект: страницу с сайта другого объекта таблицы не берём в источники этого (ZOOMSHEAL получил адрес
    # и «идентификацию» со страниц Shandong Yuwang)
    c.object_sites = {}
    for e in shortlist:
        st = S.site_of(e.get("website") or "")
        if st:
            c.object_sites.setdefault(st, e["id"])
    fillers = pick_fillers(c)
    c._emit("stage", stage="fill", title=f"Заполнение таблицы: {len(shortlist)} {_obj_word(len(shortlist))} × "
                                          f"{len(attrs)} признаков; читают {', '.join(m.name for m in fillers)}")
    rows: dict[str, dict[str, Any]] = {}
    sem = asyncio.Semaphore(sc.parallel)

    fprog = {"done": 0, "total": len(shortlist)}
    c._emit("progress", stage="fill", unit="объектов", **fprog)

    async def one(e: dict[str, Any]) -> None:
        async with sem:
            try:
                rows[e["id"]] = await fill_object(c, frame, attrs, e, fillers)
            finally:
                fprog["done"] += 1
                c._emit("progress", stage="fill", unit="объектов", **fprog)

    results = await asyncio.gather(*(one(e) for e in shortlist), return_exceptions=True)
    for r in results:
        if isinstance(r, (BudgetExceeded, OutOfCredits)):
            raise r
        if isinstance(r, BaseException) and not _soft(r):
            raise r

    # 5. Разбор спорного
    disputed = [(oid, a) for oid, row in rows.items() for a in attrs
                if (row["cells"].get(a.id) or {}).get("status") == "disputed"]
    if disputed and sc.skip_neutral_disputes:
        quiet = neutral_disputes(attrs, rows, top_n, sc.use_trust)
        if quiet:
            c._emit("disputes_neutral", cells=len(quiet), total=len(disputed))
            log.info("%sспорных ячеек, не меняющих первую группу и лидера: %d из %d — без судей", c._tag,
                     len(quiet), len(disputed))
    if any((row["cells"].get(a.id) or {}).get("status") == "disputed" and not row["cells"][a.id].get("neutral")
           for row in rows.values() for a in attrs):
        await resolve_disputes(c, frame, attrs, shortlist, rows, fillers)

    # 6. Решение: ранг, устойчивость, что выяснить — расчётом; вывод председателя
    # объекты, о которых не нашлось ни одного значения, — не в ранг (иначе стоят последними с нулём, как «худшие»)
    unchecked = [{"id": e["id"], "name": e["name"]} for e in shortlist if e["id"] in rows
                 and not any(cell.get("status") not in (None, "none") for cell in rows[e["id"]]["cells"].values())]
    for x in unchecked:
        rows.pop(x["id"], None)
    objects = [e for e in shortlist if e["id"] in rows]
    table = {oid: rows[oid]["cells"] for oid in rows}
    names = {e["id"]: e["name"] for e in objects}
    rank = S.ranking(attrs, table, use_trust=sc.use_trust)
    top_n = min(top_n or len(rank), len(rank))
    sens = S.sensitivity(attrs, table, names, use_trust=sc.use_trust)
    voi = S.value_of_information(attrs, table, names, top_n, use_trust=sc.use_trust)
    sel = {"frame": frame.model_dump(), "start": start, "attributes": [a.model_dump() for a in attrs],
           "objects": objects, "table": table, "ranking": rank, "top_n": top_n, "sensitivity": sens, "voi": voi,
           "excluded": excluded, "unchecked": unchecked, "candidates": len(entities), "discovery": found_meta,
           "fillers": [m.name for m in fillers], "sources": sources_used(c, table, rows),
           "stats": cell_stats(table), "settings": sc.model_dump()}
    c._emit("ranked", ranking=rank[:30], names=names, top_n=top_n, stats=sel["stats"])
    c._emit("stage", stage="verdict", title="Вывод председателя по таблице")
    conclusion = None
    try:
        conclusion = await c._ask(
            chair, "conclusion",
            S.conclusion_prompt(frame, S.table_text(sel), "\n".join(f"- {names.get(v['object'])}: {v['text']}" for v in voi),
                                "\n".join(f"- {s['text']}" for s in sens),
                                "; ".join(f"{x['name']} ({x['reason'][:80]})" for x in excluded[:30]), top_n,
                                c.cfg.answer_size == "brief", _counts_text(len(entities), len(excluded), len(shortlist),
                                                                           len(objects))),
            S.Conclusion, meta=META, max_tokens=8000)  # этап в журнале — «conclusion» (см. рамку)
    except (ProviderError, InvalidResponse) as e:
        if not _soft(e):
            raise
        c.stats.setdefault(chair.name, MemberStats()).failures.append(f"verdict: {e}")
        c._emit("member_failed", stage="verdict", member=chair.name, error=str(e))
    winner = names.get(rank[0]["object"], "") if rank else ""
    verdict = Verdict(winner=winner, final_answer=S.report_md(sel, conclusion),
                      why=(conclusion.summary if conclusion else "Ранг посчитан по таблице; вывод председателя не получен."),
                      risks=conclusion.risks if conclusion else [])
    sel["conclusion"] = conclusion.model_dump() if conclusion else None
    c._emit("verdict", verdict=verdict, ideas=[])
    if c.snapshot_dir and sc.snapshots:  # папка копий страниц — не больше предела; старые копии удаляются
        try:
            await asyncio.to_thread(trim_snapshots, c.snapshot_dir, sc.snapshot_mb)
        except OSError as e:
            log.warning("%sпапка копий страниц не очищена: %s", c._tag, e)
    return SessionResult(
        question=question, refined_question=frame.refined_question or question, criteria=[a.name for a in attrs],
        mode="selection", authors={}, initial_proposals={}, proposals={}, critiques={}, ballots={}, tally=[],
        verdict=verdict, stats=c.stats, transcript=c.transcript, started_at=started,
        duration_s=round(time.monotonic() - t0, 1), attachments=[a.meta() for a in c.attachments],
        image_notes=dict(c.image_notes), image_notes_by=c.image_notes_by, followup_of=followup_of,
        thread=c.history, chair_proposed=False, requirements=c.requirements, selection=sel)


def _word(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    return one if n % 10 == 1 and n != 11 else few if 2 <= n % 10 <= 4 and not 12 <= n <= 14 else many


def _obj_word(n: int) -> str:
    n = abs(n) % 100
    return "объект" if n % 10 == 1 and n != 11 else "объекта" if 2 <= n % 10 <= 4 and not 12 <= n <= 14 else "объектов"


def apply_frame_edits(frame: S.SelectionFrame, dec: dict[str, Any] | None) -> S.SelectionFrame:
    """Правка рамки человеком: веса, признаки отсева, удалённые и новые признаки."""
    if not dec:
        return frame
    attrs = []
    drop = set(dec.get("drop") or [])
    weights = dec.get("weights") or {}
    screen_ids = dec.get("screen")
    for a in frame.attributes:
        if a.id in drop:
            continue
        upd: dict[str, Any] = {}
        if a.id in weights:
            upd["weight"] = max(0.0, min(10.0, float(weights[a.id])))
        if screen_ids is not None:
            upd["screen"] = a.id in screen_ids
        attrs.append(a.model_copy(update=upd))
    for x in dec.get("add") or []:
        if isinstance(x, dict) and str(x.get("name", "")).strip():
            attrs.append(S.Attribute.model_validate(x))
        elif isinstance(x, str) and x.strip():
            attrs.append(S.Attribute(name=x.strip()))
    data = frame.model_dump()
    data["attributes"] = [a.model_dump() for a in attrs]
    return S.SelectionFrame.model_validate(data)


def _max_id(entities: list[dict[str, Any]]) -> int:
    return max((int(e["id"][1:]) for e in entities if str(e.get("id", "")).startswith("O") and e["id"][1:].isdigit()),
               default=0)


def apply_list_edits(entities: list[dict[str, Any]], dec: dict[str, Any] | None, start_n: int | None = None
                     ) -> list[dict[str, Any]]:
    """Правка списка человеком: убрать (drop), добавить своих (add)."""
    if not dec:
        return entities
    drop = set(dec.get("drop") or [])
    out = [e for e in entities if e["id"] not in drop]
    n = start_n if start_n is not None else _max_id(entities)  # новые номера — после самого большого (не после длины)
    known = {S.norm_name(x) for e in out for x in e.get("names") or [e["name"]]}
    for name in dec.get("add") or []:
        name = str(name).strip()
        if not name or S.norm_name(name) in known:
            continue
        n += 1
        known.add(S.norm_name(name))
        out.append({"id": f"O{n}", "name": name, "names": [name], "website": "", "location": "", "angles": ["ваш список"],
                    "seed": True, "added": True, "url": "", "quote": "", "why": "добавлено вами", "merged": False})
    return out


async def discover(c: Council, frame: S.SelectionFrame, seeds: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Поиск кандидатов: каждое направление ищет отдельно; дубли склеиваются; полнота — по повторным находкам.
    Мало повторов (список далёк от полного) — ещё круг с новыми направлениями от председателя."""
    sc = c.cfg.selection
    chair = c.cfg.member(c.cfg.chair)
    required = list(dict.fromkeys(frame.required_angles))[:20]
    others = [a for a in frame.angles[:sc.angles] if a not in required]
    angles = required + others or ["general web search for the candidates"]
    # кто ищет: модель поиска и (по умолчанию) все участники — каждый по всем направлениям, своим поисковиком
    hunters = discover_hunters(c)
    scouting = any(m.name.startswith("поиск ") for m in hunters)
    c.stage_search_engine["discover"] = "native" if scouting else sc.discover_engine
    if scouting or sc.discover_engine == "native":
        pool = list(sc.discover_engines) or ["exa"]
        others = [m for m in hunters if m.name != FACT_CHECKER and not has_native_search(m.model)]
        for i, m in enumerate(others):
            c.stage_search_engine[f"discover:{m.name}"] = pool[i % len(pool)]
        engines = {m.name: c.stage_search_engine.get(f"discover:{m.name}", "свой") for m in hunters
                   if m.name != FACT_CHECKER}
        log.info("%sпоиск кандидатов: поисковики %s", c._tag, engines)
    found: list[tuple[str, S.Candidate]] = []
    used: list[str] = []
    rounds_meta = []
    # волны: обязательные по заданию и половина остальных направлений рамки, затем остальные — если список ещё не
    # полон; затем новые от председателя. Обязательные (по каждой категории, каждому типу) идут всегда
    if required:
        half = len(required) + max(0, -(-len(others) // 2))
    else:
        half = max(4, -(-len(angles) // 2))
    waves = [angles[:half], angles[half:]] if sc.discover_waves and len(angles) > half else [angles]
    waves = [w for w in waves if w]
    frame_angles = list(angles)
    wave_i = 0
    pairs = first_pairs = 0  # независимых поисков (направление · модель): по ним считается полнота
    strikes: dict[str, int] = {}  # сбоев подряд у модели на этом этапе
    gates: dict[str, asyncio.Semaphore] = {}
    # готовые списки компаний — до поиска по направлениям: найденное в них идёт в общий список как независимые источники
    listed, n_lists = await harvest_lists(c, frame, hunters)
    if listed:
        found.extend(listed)
        frame_angles.extend(dict.fromkeys(x[0].split(S.BY)[0] for x in listed))
        pairs += n_lists
        first_pairs += n_lists
    for rnd in range(sc.rounds + len(waves) - 1):
        if rnd:
            comp = S.completeness(S.merge_candidates(found), pairs)
            # остальные направления рамки пропускаем, только если список почти полон (≥ 90%): глубина важнее
            enough = max(sc.saturation, 0.9) if wave_i < len(waves) else sc.saturation
            if comp["share"] is None or comp["share"] >= enough:
                if wave_i < len(waves):
                    log.info("%sпоиск кандидатов: полнота %s после %d направлений — остальные %d не нужны",
                             c._tag, comp["share"], len(used), sum(len(w) for w in waves[wave_i:]))
                break
        if wave_i < len(waves):
            angles = waves[wave_i]
            wave_i += 1
        elif rnd:
            try:
                na = await c._ask(chair, "angles", S.angles_prompt(frame, used, comp["found"], comp["share"]),
                                  S.NewAngles, meta=META)
                angles = [a for a in na.angles if a not in used][:4]
            except (ProviderError, InvalidResponse) as e:
                if not _soft(e):
                    raise
                break
            if not angles:
                break
        from_frame = all(a in frame_angles for a in angles)
        # новые направления председателя ищут «кроме уже найденных»; волны рамки — независимо (для оценки полноты)
        known = [e["name"] for e in S.merge_candidates(found)] if rnd and not from_frame else []
        per = len(hunters) if scouting or not sc.discover_models else min(len(hunters), sc.discover_models + 1)
        c._emit("stage", stage="discover", title=f"Поиск кандидатов{'' if not rnd else ' — вторая волна' if from_frame else ' — ещё круг'}: "
                                                 f"{len(angles)} {_word(len(angles), 'направление', 'направления', 'направлений')}, "
                                                 f"по каждому — {per} {_word(per, 'модель', 'модели', 'моделей')} "
                                                 f"(всего ищут: {', '.join(m.name for m in hunters)})",
                members=[m.name for m in hunters])
        plan = discover_plan(angles, hunters, 0 if scouting else sc.discover_models, shift=len(used))
        left = {m.name: sum(1 for _, x in plan if x.name == m.name) for m in hunters}
        left = {k: v for k, v in left.items() if v}
        by = {k: {"done": 0, "total": v, "found": 0, "failed": 0} for k, v in left.items()}
        prog = {"done": 0, "total": len(plan), "round": rnd + 1}
        taken = {(a, m.name) for a, m in plan}
        before = len(S.merge_candidates(found)) if rnd else 0

        def progress() -> None:
            c._emit("progress", stage="discover", unit="поисков", **prog, by=by,
                    found=len(S.merge_candidates(found)), new=len(S.merge_candidates(found)) - before if rnd else None)

        progress()

        async def hunt(a: str, m: MemberConfig) -> Any:
            gate = gates.setdefault(m.name, asyncio.Semaphore(2))  # у модели не больше двух поисков сразу: успеем
            m_use = m                                              # заметить, что она сбоит, до остальных запросов
            try:
                async with gate:
                    if strikes.get(m.name, 0) >= 2:  # два сбоя подряд — дальше не платим за неё
                        sub = next((h for h in hunters if h.name != m.name and strikes.get(h.name, 0) < 2
                                    and (a, h.name) not in taken), None)
                        if sub is None:
                            raise ProviderError(f"{m.name}: пропущено после двух сбоев подряд")
                        log.info("%s%s сбоит — направление «%s» ищет %s", c._tag, m.name, a[:40], sub.name)
                        taken.add((a, sub.name))
                        m_use = sub
                    r = await c._ask(m_use, "discover", S.discover_prompt(frame, a, sc.per_angle, known), S.Candidates,
                                     meta={**META, "angle": a}, web_search=True)
                    strikes[m_use.name] = 0
                    if m_use is not m:
                        found.extend((f"{a}{S.BY}{m_use.name}", x) for x in r.items)
                        if m_use.name in by:
                            by[m_use.name]["found"] += len(r.items)
                        c._emit("candidates_found", angle=a, by=m_use.name, count=len(r.items))
                        return S.Candidates()
                # источник находки — пара «направление · модель»: полнота считается по независимым находкам
                found.extend((f"{a}{S.BY}{m.name}", x) for x in r.items)
                by[m.name]["found"] += len(r.items)
                c._emit("candidates_found", angle=a, by=m.name, count=len(r.items))
                return r
            except BaseException as err:
                by[m.name]["failed"] += 1
                if isinstance(err, (ProviderError, InvalidResponse)) and m_use is m:
                    strikes[m.name] = strikes.get(m.name, 0) + 1
                elif isinstance(err, (ProviderError, InvalidResponse)):
                    strikes[m_use.name] = strikes.get(m_use.name, 0) + 1
                raise
            finally:
                left[m.name] -= 1
                by[m.name]["done"] += 1
                prog["done"] += 1
                progress()
                if not left[m.name]:
                    c._emit("member_done", stage="discover", member=m.name)

        got = await asyncio.gather(*(hunt(a, m) for a, m in plan), return_exceptions=True)
        for (a, m), r in zip(plan, got):
            if isinstance(r, (BudgetExceeded, OutOfCredits)):
                raise r
            if isinstance(r, BaseException):
                if not _soft(r):
                    raise r
                c.stats.setdefault(m.name, MemberStats()).failures.append(f"discover: {a}: {r}")
                c._emit("member_failed", stage="discover", member=f"{m.name} ({a[:40]})", error=str(r))
        used += angles
        pairs += len(plan)
        if all(a in frame_angles for a in angles):
            first_pairs += len(plan)
        rounds_meta.append({"angles": angles, "found": len(S.merge_candidates(found)), "searches": len(plan)})
    first_angles = set(frame_angles) or set(used)  # направления рамки и готовые списки (без новых от председателя)
    first = [x for x in found if x[0].split(S.BY)[0] in first_angles]
    comp = S.completeness(S.merge_candidates(first), first_pairs)
    entities = S.merge_candidates(found, seeds)
    bench = {S.norm_name(b) for b in frame.benchmarks if S.norm_name(b)}
    if bench:  # конкуренты и исключённые, названные в задании, — не кандидаты, даже если поиск их принёс
        entities = [e for e in entities if not any(S.norm_name(n) in bench for n in e.get("names") or [e["name"]])]
    for e in entities:  # какие модели нашли кандидата
        # кто нашёл: модели поиска по направлениям; найденное в готовом списке — «список: …» (а не читавшая модель)
        e["models"] = sorted({a.split(S.BY)[0] if a.startswith("список: ") else a.split(S.BY)[-1]
                              for a in e["angles"] if S.BY in a})
    entities.sort(key=lambda e: (not e["seed"], -len(e["angles"]), e["name"].lower()))
    for i, e in enumerate(entities, 1):
        e["id"] = f"O{i}"
    c._emit("candidates", count=len(entities), completeness=comp, merged=sum(1 for e in entities if e["merged"]))
    log.info("%sотбор: найдено %d кандидатов (оценка полноты %s)", c._tag, len(entities), comp.get("share"))
    return entities, {"completeness": comp, "rounds": rounds_meta, "angles": used}


async def screen(c: Council, frame: S.SelectionFrame, attrs: list[S.Attribute], entities: list[dict[str, Any]]
                 ) -> dict[str, dict[str, Any]]:
    """Отсев: короткая проверка признаков отсева у всех кандидатов пачками — по скачанным страницам (pages) или
    поиском (search)."""
    if c.cfg.selection.screen_by == "pages":
        return await screen_pages(c, frame, attrs, entities)
    sc = c.cfg.selection
    c._emit("stage", stage="screen", title=f"Отсев: {len(entities)} кандидатов по {len(attrs)} признакам "
                                          f"({', '.join(a.name for a in attrs)})")
    batches = [entities[i:i + sc.screen_batch] for i in range(0, len(entities), sc.screen_batch)]
    prog = {"done": 0, "total": len(batches), "checked": 0, "objects": len(entities)}
    c._emit("progress", stage="screen", unit="пачек", **prog)

    async def check(b: list[dict[str, Any]]) -> Any:
        try:
            return await c._ask(searcher(c), "screen", S.screen_prompt(frame, attrs, b), S.ScreenBatch,
                                meta={**META, "objects": [e["id"] for e in b]}, web_search=True)
        finally:
            prog["done"] += 1
            prog["checked"] += len(b)
            c._emit("progress", stage="screen", unit="пачек", **prog)

    got = await asyncio.gather(*(check(b) for b in batches), return_exceptions=True)
    out: dict[str, dict[str, Any]] = {}
    for b, r in zip(batches, got):
        if isinstance(r, (BudgetExceeded, OutOfCredits)):
            raise r
        if isinstance(r, BaseException):
            if not _soft(r):
                raise r
            c.stats[FACT_CHECKER].failures.append(f"screen: {r}")
            continue
        for row in r.rows:
            e = _match_entity(row.object, b)
            if not e:
                continue
            checks = [ch.model_copy(update={"attr": (S.match_attr(ch.attr, attrs) or S.Attribute(name=ch.attr)).id
                                            or ch.attr}) for ch in row.checks]
            result, reason = S.screen_decide(checks, attrs)
            out[e["id"]] = {"result": result, "reason": reason, "name": e["name"],
                            "checks": [x.model_dump() for x in checks]}
            # адреса из отсева — уже найденные страницы объекта: при заполнении их не ищут заново
            e["urls"] = list(dict.fromkeys([*(e.get("urls") or []),
                                            *(x.url for x in checks if x.url.startswith(("http://", "https://")))]))[:10]
            if row.website and not e.get("website"):
                e["website"] = row.website
    await verify_screen_fails(c, out, frame, attrs)
    counts = {k: sum(1 for v in out.values() if v["result"] == k) for k in ("pass", "fail", "unclear")}
    c._emit("screened", **counts, total=len(entities))
    return out


def diverse_order(keep: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Порядок короткого списка: внутри каждой ступени (ваши → прошедшие отсев → сомнительные) — по очереди из каждого
    направления поиска. Раньше брали «найденных чаще всего», и универсальные заводы, которых находит любой поиск,
    вытесняли специалистов категории (пробиотики, пребиотики…), найденных одним направлением."""
    def tier(e: dict[str, Any]) -> tuple[bool, bool]:
        return (not e.get("seed"), (e.get("screen") or {}).get("result") != "pass")
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(keep):
        j = i
        while j < len(keep) and tier(keep[j]) == tier(keep[i]):
            j += 1
        group = keep[i:j]
        by_angle: dict[str, list[dict[str, Any]]] = {}
        for e in group:  # каждый кандидат — в очереди своего самого «узкого» направления (где найдено меньше всего)
            angs = [a.split(S.BY)[0] for a in e.get("angles") or []] or [""]
            by_angle.setdefault(min(angs, key=lambda a: sum(1 for x in group if a in
                                                              [y.split(S.BY)[0] for y in x.get("angles") or []])), []).append(e)
        queues = list(by_angle.values())
        while any(queues):
            for q in queues:
                if q:
                    out.append(q.pop(0))
        i = j
    return out


def _counts_text(found: int, excluded: int, checked: int, ranked: int) -> str:
    """Для вывода председателя: сколько кандидатов найдено и сколько из них проверено подробно. Без этого он видит
    только таблицу и пишет «найдено 25 вместо 60–80», хотя найдено 151, а в таблицу взяли 25."""
    rest = max(0, found - excluded - checked)
    return (f"Counts: candidates found {found}; excluded at screening {excluded}; checked in detail (the table) {checked}"
            f"{f', of them ranked {ranked}' if ranked != checked else ''}; found but NOT checked in detail {rest} (a "
            "size limit of the table, not a lack of candidates). Do not confuse the number found with the table size.\n")


async def screen_pages(c: Council, frame: S.SelectionFrame, attrs: list[S.Attribute], entities: list[dict[str, Any]]
                       ) -> dict[str, dict[str, Any]]:
    """Отсев по страницам: программа скачивает страницу, где поиск нашёл кандидата (нет её — главную сайта), дешёвая
    модель по тексту решает «подходит / не подходит / не ясно» с цитатой. Без поиска — в разы дешевле, и ответ не
    пустой (поиск по пачке из пяти компаний почти всегда возвращал «не выяснено»). Не открылась страница —
    «не выяснено», кандидат остаётся.

    Кандидаты идут частями по SCREEN_CHUNK: часть скачивается (без браузера — он медленный, для отсева хватает
    обычной загрузки) и сразу читается, пока качается следующая; ход виден по кандидатам. Раньше все страницы
    (556 кандидатов — до 1500 страниц) качались разом до начала чтения: минуты без движения на экране, а общий
    предел времени загрузки обрывал большую часть."""
    sc = c.cfg.selection
    reader = quick(c, pick_fillers(c)[0])
    c._emit("stage", stage="screen", title=f"Отсев: {len(entities)} кандидатов по {len(attrs)} признакам "
                                          f"({', '.join(a.name for a in attrs)}) — читаем их страницы",
            members=[reader.name])
    shared = getattr(c, "list_urls", set())
    plan: dict[str, list[str]] = {}
    for e in entities:
        us = [u for u in [e.get("url") or "", *(e.get("urls") or [])] if u.startswith(("http://", "https://"))
              and u not in shared]
        home = _home(e["website"]) if S.site_of(e.get("website") or "") else ""
        # страница, где нашли кандидата, и главная его сайта (если это другая страница) — не больше двух
        plan[e["id"]] = list(dict.fromkeys(([us[0]] if us else []) + ([home] if home else [])))[:2]
    words = [w for a in attrs for w in [a.name, *a.keywords]]
    out: dict[str, dict[str, Any]] = {}
    prog = {"done": 0, "total": len(entities), "checked": 0, "objects": len(entities)}
    c._emit("progress", stage="screen", unit="кандидатов", **prog)
    stats = {"readable": 0}
    chunks = [entities[k:k + SCREEN_CHUNK] for k in range(0, len(entities), SCREEN_CHUNK)]
    gate_chunks = asyncio.Semaphore(SCREEN_CHUNKS_AT_ONCE)

    async def check(b: list[dict[str, Any]], texts: dict[str, list[tuple[str, str]]]) -> None:
        try:
            r = await c._ask(reader, "screen", S.screen_read_prompt(frame, attrs, b, texts), S.ScreenBatch,
                             meta={**META, "objects": [e["id"] for e in b]}, max_tokens=4000)
        except (BudgetExceeded, OutOfCredits):
            raise
        except (ProviderError, InvalidResponse) as err:
            if not _soft(err):
                raise
            c.stats.setdefault(reader.name, MemberStats()).failures.append(f"screen: {err}")
            return
        finally:
            prog["done"] += len(b)
            prog["checked"] += len(b)
            c._emit("progress", stage="screen", unit="кандидатов", **prog)
        for row in r.rows:
            e = _match_entity(row.object, b)
            if not e:
                continue
            checks = [ch.model_copy(update={"attr": (S.match_attr(ch.attr, attrs) or S.Attribute(name=ch.attr)).id
                                            or ch.attr}) for ch in row.checks]
            result, reason = S.screen_decide(checks, attrs)
            out[e["id"]] = {"result": result, "reason": reason, "name": e["name"],
                            "checks": [x.model_dump() for x in checks]}
            if row.website and not e.get("website"):
                e["website"] = row.website

    async def chunk(ch: list[dict[str, Any]]) -> None:
        async with gate_chunks:
            pages = await fetch_pages(c, [u for e in ch for u in plan[e["id"]]], browser=False)
        texts: dict[str, list[tuple[str, str]]] = {}
        for e in ch:
            budget = sc.screen_chars
            got = []
            for u in plan[e["id"]]:
                t = (pages.get(u) or {}).get("text") or ""
                if t and budget > 300:
                    ex = S.excerpts(t, words + list(e.get("names") or [e["name"]]), min(budget, sc.screen_chars // 2 + 500))
                    budget -= len(ex)
                    got.append((u, ex))
            texts[e["id"]] = got
        readable = [e for e in ch if texts[e["id"]]]
        stats["readable"] += len(readable)
        for e in ch:
            if not texts[e["id"]]:
                out[e["id"]] = {"result": "unclear", "reason": "не выяснено: страницы кандидата не открылись",
                                "name": e["name"], "checks": []}
        if len(readable) < len(ch):
            prog["done"] += len(ch) - len(readable)
            prog["checked"] += len(ch) - len(readable)
            c._emit("progress", stage="screen", unit="кандидатов", **prog)
        batches = [readable[k:k + sc.screen_batch] for k in range(0, len(readable), sc.screen_batch)]
        got = await asyncio.gather(*(check(b, texts) for b in batches), return_exceptions=True)
        for r in got:
            if isinstance(r, BaseException):
                raise r

    done = await asyncio.gather(*(chunk(ch) for ch in chunks), return_exceptions=True)
    for r in done:
        if isinstance(r, BaseException):
            raise r
    await verify_screen_fails(c, out, frame, attrs)
    counts = {k: sum(1 for v in out.values() if v["result"] == k) for k in ("pass", "fail", "unclear")}
    c._emit("screened", **counts, total=len(entities))
    log.info("%sотбор: отсев по страницам — %s (страницы открылись у %d из %d)", c._tag, counts, stats["readable"],
             len(entities))
    return out


SCREEN_CHUNK = 25          # кандидатов в одной части отсева (скачать страницы → прочитать)
SCREEN_CHUNKS_AT_ONCE = 3  # частей, которые качаются одновременно (по 5 страниц каждая — 15 загрузок сразу)


async def verify_screen_fails(c: Council, out: dict[str, dict[str, Any]], frame: S.SelectionFrame | None = None,
                              attrs: list[S.Attribute] | None = None) -> int:
    """«Не подходит» на отсеве засчитывается, только если программа нашла цитату этого вердикта на скачанной
    странице. Не нашла (страница не открылась, цитата неточная или не оттуда) — кандидат «сомнительный» и остаётся:
    выбросить нужный завод хуже, чем лишний раз его проверить. Возвращает, сколько исключений отменено."""
    fails = {oid: r for oid, r in out.items() if r["result"] == "fail"}
    if not fails:
        return 0
    urls = list(dict.fromkeys(ch.get("url") or "" for r in fails.values() for ch in r["checks"]
                              if ch.get("verdict") == "fail" and (ch.get("url") or "").startswith(("http://", "https://"))))
    pages = await fetch_pages(c, urls) if urls and c.link_checker else {}
    undone = 0
    proven_all: dict[str, list[dict[str, Any]]] = {}
    for oid, r in fails.items():
        proven = [ch for ch in r["checks"] if ch.get("verdict") == "fail" and ch.get("quote")
                  and S.find_quote((pages.get(ch.get("url") or "") or {}).get("text") or "", ch["quote"]) >= 0]
        if proven:
            proven_all[oid] = proven
            continue
        undone += 1
        r.update(result="unclear", reason=f"не выяснено: «не подходит» без цитаты, найденной на странице ({r['reason'][:150]})")
    # цитата настоящая — но следует ли из неё исключение? (Henan, Zhengzhou → «не материковый Китай» — нет)
    ok = await confirm_screen_fails(c, out, proven_all, frame, attrs) if proven_all else set()
    for oid in proven_all:
        r = out[oid]
        if oid in ok:
            r["verified"] = True
            continue
        undone += 1
        r.update(result="unclear", reason=f"не выяснено: вторая модель не согласилась, что цитата исключает ({r['reason'][:150]})")
    if undone:
        log.info("%sотсев: %d из %d исключений отменено — цитата не найдена на странице", c._tag, undone, len(fails))
        c._emit("screen_undone", count=undone, total=len(fails))
    return undone


async def confirm_screen_fails(c: Council, out: dict[str, dict[str, Any]], proven: dict[str, list[dict[str, Any]]],
                               frame: S.SelectionFrame | None, attrs_l: list[S.Attribute] | None) -> set[str]:
    """Одна короткая проверка всех исключений другой моделью (самой дешёвой читающей): правило исключения + цитата →
    исключать или нет. Возвращает id, кого исключить. Сбой проверки — никого не исключаем (выбросить нужный завод
    хуже, чем лишний раз его проверить)."""
    attrs = {a.id: a for a in attrs_l or []}
    items: list[tuple[str, dict[str, Any]]] = [(oid, ch) for oid, chs in proven.items() for ch in chs]
    lines = []
    for i, (oid, ch) in enumerate(items, 1):
        a = attrs.get(ch.get("attr") or "")
        lines.append(f"{i}. {out[oid].get('name') or oid} — «{a.name if a else ch.get('attr')}», excluded if: "
                     f"{(a.knockout if a else '') or 'clearly does not fit'}; found value: {ch.get('value') or '—'}; "
                     f"quote: «{(ch.get('quote') or '')[:300]}»")
    prompt = (f"Screening check for a selection study{f': {frame.refined_question}' if frame else ''}.\n"
              "For each item decide whether the QUOTE really proves that the exclusion rule applies. Exclude only if it "
              "clearly does (e.g. a factory address in Henan does NOT prove «not mainland China» — it proves the opposite; "
              "silence or vagueness proves nothing).\n\n" + "\n".join(lines)
              + '\n\nReturn JSON: {"items": [{"n": 1, "exclude": true, "why": "short"}]}')
    try:
        who = pick_fillers(c)[0]
        res = await c._ask(quick(c, who), "screen_confirm", prompt, S.ScreenConfirm, meta=META, max_tokens=3000)
    except (ProviderError, InvalidResponse) as err:
        if not _soft(err):
            raise
        c._emit("member_failed", stage="screen", member="проверка отсева", error=str(err))
        return set()
    yes = {x.n for x in res.items if x.exclude}
    keep_ex: set[str] = set()
    for i, (oid, ch) in enumerate(items, 1):
        if i in yes:
            keep_ex.add(oid)
    return keep_ex


def _match_entity_strict(name: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Найденный кандидат с этим именем: совпадение имени (без форм собственности) с любым его названием или сайт."""
    n = S.norm_name(name)
    if not S._name_key_ok(n):
        return None
    site = S.site_of(name) if "." in name else ""
    for e in items:
        if any(S.norm_name(x) == n for x in e.get("names") or [e["name"]]) or (site and S.site_of(e.get("website") or "") == site):
            return e
    return None


def _match_entity(name: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    n = S.norm_name(name)
    for e in items:
        if any(S.norm_name(x) == n for x in e.get("names") or [e["name"]]):
            return e
    for e in items:
        if any(n and (n in S.norm_name(x) or S.norm_name(x) in n) for x in e.get("names") or [e["name"]] if S.norm_name(x)):
            return e
    return items[0] if len(items) == 1 else None


async def fill_object(c: Council, frame: S.SelectionFrame, attrs: list[S.Attribute], e: dict[str, Any],
                      fillers: list[MemberConfig]) -> dict[str, Any]:
    """Один объект: поиск источников → программа скачивает страницы → две модели заполняют ячейки по тексту →
    программа ищет каждую цитату на странице и сводит варианты."""
    sc = c.cfg.selection
    meta = {**META, "object": e["id"]}
    c._emit("object_filling", object=e["id"], name=e["name"])
    src = S.ObjectSources()
    # страницы, найденные при поиске кандидатов и отсеве, уже известны: поиск источников ищет только недостающие
    shared = getattr(c, "list_urls", set())
    known = list(dict.fromkeys(u for u in [e.get("url") or "", *(e.get("urls") or [])]
                               if u.startswith(("http://", "https://")) and u not in shared))
    need = max(0, sc.pages_per_object - len(known))
    if need:
        try:
            src = await c._ask(searcher(c), "sources", S.sources_prompt(frame, e, attrs, need, known),
                               S.ObjectSources, meta=meta, web_search=True)
        except (ProviderError, InvalidResponse) as err:
            if not _soft(err):
                raise
            c.stats[FACT_CHECKER].failures.append(f"sources {e['name']}: {err}")
            c._emit("member_failed", stage="sources", member=f"{FACT_CHECKER} ({e['name'][:40]})", error=str(err))
    if src.website and not S.site_of(e.get("website", "")):
        e["website"] = src.website
    urls = list(dict.fromkeys(known + [s.url for s in src.sources if s.url] + [x.url for x in src.cells if x.url]
                              + ([_home(e["website"])] if S.site_of(e.get("website", "")) else [])))
    sites = getattr(c, "object_sites", {}) or {}
    urls = [u for u in urls if sites.get(S.site_of(u), e["id"]) == e["id"]][:sc.pages_per_object + 2]
    pages = await fetch_pages(c, urls)
    words = [w for a in attrs for w in [a.name, *a.keywords]] + list(e.get("names") or [e["name"]])
    budget = sc.object_chars
    texts: list[tuple[str, str]] = []
    for u in urls:
        t = pages.get(u, {}).get("text") or ""
        if not t or budget <= 0:
            continue
        ex = S.excerpts(t, words, min(sc.page_chars, budget))
        budget -= len(ex)
        texts.append((u, ex))
    texts, cut = S.drop_repeats(texts)  # меню, подвалы, «о компании» — один раз, а не на каждой странице
    if cut:
        c.saved_chars = getattr(c, "saved_chars", 0) + cut
    if not texts:  # страницы не открылись — модели читают хотя бы цитаты, которые вернул поиск
        texts = [(x.url or "search", f"(search snippet) {x.quote}") for x in src.cells if x.quote]
    reads: dict[str, list[S.Cell]] = {"поиск": list(src.cells)}
    got_all: list[tuple[str, Any]] = []
    first, rest = fillers[0], fillers[1:]
    if sc.second_read == "always" or not rest:
        jobs = {m.name: c._ask(m, "fill", S.fill_prompt(frame, e, attrs, texts), S.ObjectCells, meta=meta) for m in fillers}
        got_all = list(zip(jobs, await asyncio.gather(*jobs.values(), return_exceptions=True)))
    else:
        # первое прочтение; второе — только по признакам, которые влияют на ранг и ещё не подтверждены дословной
        # цитатой из документа или с сайта компании (или не найдены). Всё подтверждено — второе не нужно
        r1 = (await asyncio.gather(c._ask(first, "fill", S.fill_prompt(frame, e, attrs, texts), S.ObjectCells, meta=meta),
                                   return_exceptions=True))[0]
        if isinstance(r1, (BudgetExceeded, OutOfCredits)):  # бюджет или баланс — второе прочтение не начинаем
            raise r1
        got_all.append((first.name, r1))
        firm: set[str] = set()
        if not isinstance(r1, BaseException):
            for a in attrs:
                x = next((x for x in r1.cells if (S.match_attr(x.attr, attrs) or S.Attribute(name="?")).id == a.id
                          and x.value.strip()), None)
                v = verify_option(c, x, first.name, e, pages, a) if x is not None else {}
                if v.get("found") and v.get("kind") in ("document", "official"):
                    firm.add(a.id)
        # первое прочтение не удалось — второе читает всё (иначе признаки «для сведения» остались бы без значения)
        todo = attrs if isinstance(r1, BaseException) else [a for a in attrs if a.scorable and a.id not in firm]
        if todo:
            m2 = rest[0]
            only = None if len(todo) == len(attrs) else [a.id for a in todo]
            r2 = (await asyncio.gather(c._ask(m2, "fill", S.fill_prompt(frame, e, attrs, texts, only=only),
                                              S.ObjectCells, meta=meta), return_exceptions=True))[0]
            got_all.append((m2.name, r2))
        else:
            c.skipped_reads = getattr(c, "skipped_reads", 0) + 1
    for name, r in got_all:
        if isinstance(r, (BudgetExceeded, OutOfCredits)):
            raise r
        if isinstance(r, BaseException):
            if not _soft(r):
                raise r
            c.stats.setdefault(name, MemberStats()).failures.append(f"fill {e['name']}: {r}")
            c._emit("member_failed", stage="fill", member=name, error=f"{e['name']}: {r}")
            continue
        reads[name] = list(r.cells)
    cells: dict[str, dict[str, Any]] = {}
    for a in attrs:
        options = []
        for who, cl in reads.items():
            for x in cl:
                if (S.match_attr(x.attr, attrs) or S.Attribute(name="?")).id != a.id or not x.value.strip():
                    continue
                options.append(verify_option(c, x, who, e, pages, a))
                break
        cells[a.id] = S.resolve_cell(a, options)
    st = {k: sum(1 for v in cells.values() if v["status"] == k) for k in (*S.STATUSES, "disputed")}
    c._emit("object_filled", object=e["id"], name=e["name"], pages=sum(1 for u in urls if pages.get(u, {}).get("text")),
            urls=len(urls), cells=live_cells(cells), **{f"n_{k}": v for k, v in st.items()})
    return {"cells": cells, "urls": urls, "website": e.get("website", "")}


def _home(site: str) -> str:
    s = site.strip()
    return s if s.startswith(("http://", "https://")) else "https://" + s


def verify_option(c: Council, x: S.Cell, who: str, e: dict[str, Any], pages: dict[str, dict[str, Any]],
                  attr: S.Attribute | None = None) -> dict[str, Any]:
    """Вариант ячейки: ищем его цитату на указанной странице, а если ссылки нет или там не нашлось — на других
    скачанных страницах объекта (модель могла перепутать адрес). Цитата засчитывается, только если она опора значения:
    не обрывок в пару слов, а у чисел и дат — то же число или год в самой цитате."""
    o = {"value": x.value, "unit": x.unit, "as_of": x.as_of, "url": x.url, "quote": x.quote, "note": x.note, "by": who,
         "found": False, "kind": ""}
    if x.quote and attr is not None and not S.quote_supports(attr, x.value, x.unit, x.quote):
        o["weak"] = True  # цитата не подтверждает само значение — ищем её, но не засчитываем
    if x.quote and not o.get("weak"):
        order = ([x.url] if x.url in pages else []) + [u for u in pages if u != x.url]
        for u in order:
            t = pages.get(u, {}).get("text") or ""
            pos = S.find_quote(t, x.quote)
            if pos >= 0:
                o.update(found=True, url=u, kind=S.source_kind(u, e.get("website", "")), context=S.context_at(t, pos),
                         fetched=pages[u].get("fetched", ""))
                break
    if not o["found"] and x.url:
        o["kind"] = S.source_kind(x.url, e.get("website", ""))
    return o


async def resolve_disputes(c: Council, frame: S.SelectionFrame, attrs: list[S.Attribute], objs: list[dict[str, Any]],
                           rows: dict[str, dict[str, Any]], fillers: list[MemberConfig]) -> None:
    """Совет решает только спорные ячейки: каждый судья выбирает вариант по цитатам; большинство, при равенстве —
    председатель. Выбранный вариант с найденной цитатой — подтверждён, без неё — «не подтверждено»."""
    judges = pick_judges(c, fillers)
    sc = c.cfg.selection
    by_id = {e["id"]: e for e in objs}
    work = []
    for oid, row in rows.items():
        ds = [(a, row["cells"][a.id]["options"]) for a in attrs if row["cells"].get(a.id, {}).get("status") == "disputed"
              and not row["cells"][a.id].get("neutral")]
        if ds:
            work.append((oid, ds))
    n = sum(len(ds) for _, ds in work)
    c._emit("stage", stage="dispute", title=f"Разбор спорного: {n} ячеек у {len(work)} {_obj_word(len(work))}; "
                                            f"решают {', '.join(m.name for m in judges)}")
    chair = c.cfg.chair

    async def batch(part: list[tuple[str, list[tuple[S.Attribute, list[dict[str, Any]]]]]]) -> None:
        """Споры нескольких объектов — одним запросом к каждому судье (раньше — по запросу на объект)."""
        votes: dict[str, dict[str, dict[str, tuple[str, str]]]] = {}
        # сначала столько судей, сколько нужно для большинства; согласны — остальные не нужны (их голос ничего не
        # изменит); остальных спрашиваем только о ячейках, где первые разошлись
        need = len(judges) // 2 + 1 if sc.judges_on_demand and len(judges) > 2 else len(judges)
        await ask(judges[:need], part, votes)
        if need < len(judges):
            open_part = []
            for oid, ds in part:
                left = []
                for a, opts in ds:
                    ch = [x[0] for x in (votes.get(oid, {}).get(a.id) or {}).values()]
                    if len(ch) < need or len(set(ch)) > 1:
                        left.append((a, opts))
                if left:
                    open_part.append((oid, left))
            if open_part:
                await ask(judges[need:], open_part, votes)
        for oid, ds in part:
            await one(oid, ds, votes.get(oid) or {})

    async def ask(who: list[MemberConfig], part: list[tuple[str, list[tuple[S.Attribute, list[dict[str, Any]]]]]],
                  votes: dict[str, dict[str, dict[str, tuple[str, str]]]]) -> None:
        prompt = S.disputes_prompt(frame, [(by_id[oid], ds) for oid, ds in part])
        jobs = {m.name: c._ask(m, "dispute", prompt, S.Rulings, meta={**META, "objects": [oid for oid, _ in part]})
                for m in who}
        got = await asyncio.gather(*jobs.values(), return_exceptions=True)
        for name, r in zip(jobs, got):
            if isinstance(r, (BudgetExceeded, OutOfCredits)):
                raise r
            if isinstance(r, BaseException):
                if not _soft(r):
                    raise r
                c.stats.setdefault(name, MemberStats()).failures.append(f"dispute: {r}")
                continue
            for it in r.items:
                key = it.key.strip()
                oid, _, ak = key.partition(":") if ":" in key else (part[0][0] if len(part) == 1 else "", "", key)
                ds = next((d for o, d in part if o == oid.strip()), None)
                if ds is None:
                    continue
                a = S.match_attr(ak.strip(), [x for x, _ in ds])
                if a:
                    votes.setdefault(oid.strip(), {}).setdefault(a.id, {})[name] = (_choice(it.choice), it.reason)
        for oid, ds in part:
            await one(oid, ds, votes.get(oid) or {})

    async def one(oid: str, ds: list[tuple[S.Attribute, list[dict[str, Any]]]],
                  votes: dict[str, dict[str, tuple[str, str]]]) -> None:
        for a, opts in ds:
            v = votes.get(a.id) or {}
            tally: dict[str, int] = {}
            for ch, _ in v.values():
                tally[ch] = tally.get(ch, 0) + 1
            best = max(tally.values(), default=0)
            top = [k for k, t in tally.items() if t == best]
            choice = (v.get(chair, ("",))[0] if len(top) > 1 and v.get(chair, ("",))[0] in top else
                      (top[0] if top else "none"))
            idx = ord(choice) - 97 if len(choice) == 1 else -1
            cell: dict[str, Any]
            if 0 <= idx < len(opts):
                o = opts[idx]
                cell = {"status": (o.get("kind") or "secondary") if o.get("found") else "unverified",
                        "value": o["value"], "unit": o.get("unit", ""), "as_of": o.get("as_of", ""),
                        "url": o.get("url", ""), "quote": o.get("quote", ""), "context": o.get("context", ""),
                        "options": opts}
            else:  # ни один вариант не подтверждён доказательствами — значение неизвестно
                cell = {"status": "none", "value": "", "options": opts}
            cell.update(ruled=True, votes={k: {"choice": x[0], "reason": x[1]} for k, x in v.items()})
            rows[oid]["cells"][a.id] = cell
        c._emit("object_ruled", object=oid, cells=live_cells({a.id: rows[oid]["cells"][a.id] for a, _ in ds}))

    size = 6  # объектов в одном запросе к судье
    res = await asyncio.gather(*(batch(work[i:i + size]) for i in range(0, len(work), size)), return_exceptions=True)
    for r in res:
        if isinstance(r, BaseException):
            raise r
    c._emit("disputes_done", cells=n)


def neutral_disputes(attrs: list[S.Attribute], rows: dict[str, dict[str, Any]], top_n: int,
                     use_trust: bool = True) -> list[tuple[str, str]]:
    """Спорные ячейки, от которых выбор не зависит: при любом из спорных значений (и без значения) первая группа —
    те же объекты, а лидер тот же (если первая группа — все объекты, то и весь порядок тот же). Такие ячейки совет не
    судит: в таблице — оба значения с пометкой «спорно, на выбор не влияет». Отмечает их и возвращает (объект, признак)."""
    # объекты без единого значения в ранг не идут (их уберут перед рангом) — и здесь не считаются
    table = {oid: row["cells"] for oid, row in rows.items()
             if any(cell.get("status") not in (None, "none") for cell in row["cells"].values())}
    n = len(table)
    k = min(top_n or n, n)

    def outcome(tb: dict[str, dict[str, dict[str, Any]]]) -> tuple[Any, ...]:
        order = [r["object"] for r in S.ranking(attrs, tb, use_trust=use_trust)]
        return tuple(order) if k >= n else (frozenset(order[:k]), order[0] if order else None)

    base = outcome(table)
    out = []
    by_id = {a.id: a for a in attrs}
    for oid, row in rows.items():
        if oid not in table:
            continue
        for aid, cell in list(row["cells"].items()):
            a = by_id.get(aid)
            if a is None or cell.get("status") != "disputed" or not a.scorable:
                continue
            opts = [o for o in cell.get("options") or [] if (o.get("value") or "").strip()]
            same = True
            for o in opts:
                alt = {**table[oid], aid: {"status": (o.get("kind") or "secondary") if o.get("found") else "unverified",
                                           "value": o["value"], "unit": o.get("unit", "")}}
                if outcome({**table, oid: alt}) != base:
                    same = False
                    break
            if same and opts:
                vals = list(dict.fromkeys(f"{o['value']}{(' ' + o['unit']) if o.get('unit') and o['unit'] not in o['value'] else ''}"
                                          for o in opts))
                cell.update(neutral=True, value=" / ".join(vals)[:200],
                            note="спорно, на выбор не влияет: при любом значении первая группа и лидер те же")
                out.append((oid, aid))
    return out


def _choice(raw: str) -> str:
    """Выбор судьи: буква варианта («a», «(b)», «Option C», «вариант c») или «none»."""
    t = (raw or "").strip().lower()
    if not t or re.match(r"^\W*(none|no option|нет|ни один|neither)", t):
        return "none"
    m = re.match(r"^\W*(?:option|вариант)?\s*\(?([a-z])\)?(?![a-z])", t)
    return m.group(1) if m else "none"


def live_cells(cells: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Ячейки для живой таблицы на сайте: статус, значение, единица, цитата (коротко)."""
    return {aid: {"status": c.get("status", "none"), "value": str(c.get("value") or "")[:120],
                  "unit": str(c.get("unit") or "")[:40], "quote": str(c.get("quote") or "")[:200],
                  "ruled": bool(c.get("ruled"))} for aid, c in cells.items()}


def sources_used(c: Council, table: dict[str, dict[str, Any]], rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Источники таблицы: адрес, когда скачан, открылся ли, уровень, номер копии (sha) и сколько ячеек на нём."""
    count: dict[str, int] = {}
    for row in table.values():
        for cell in row.values():
            if cell.get("url") and cell.get("status") in ("document", "official", "secondary"):
                count[cell["url"]] = count.get(cell["url"], 0) + 1
    urls = list(dict.fromkeys(u for r in rows.values() for u in r.get("urls") or []))
    out = []
    for u in urls:
        m = c.page_meta.get(u, {})
        out.append({"url": u, "fetched": m.get("fetched", ""), "status": m.get("status", ""), "sha": m.get("sha", ""),
                    "kind": S.source_kind(u), "cells": count.get(u, 0), "copy": S.sha(u) if m.get("sha") else ""})
    return out


def cell_stats(table: dict[str, dict[str, Any]]) -> dict[str, int]:
    out = {k: 0 for k in (*S.STATUSES, "disputed")}
    ruled = 0
    for row in table.values():
        for cell in row.values():
            out[cell.get("status", "none")] = out.get(cell.get("status", "none"), 0) + 1
            ruled += bool(cell.get("ruled"))
    out["ruled"] = ruled
    out["total"] = sum(v for k, v in out.items() if k != "ruled")
    return out


def rerank(sel: dict[str, Any], weights: dict[str, float] | None = None, use_trust: bool | None = None) -> dict[str, Any]:
    """Пересчёт готового отбора с другими весами (ползунки в отчёте) — без моделей и без денег."""
    attrs = [S.Attribute.model_validate(a) for a in sel["attributes"]]
    if weights:
        attrs = [a.model_copy(update={"weight": max(0.0, min(10.0, float(weights.get(a.id, a.weight))))}) for a in attrs]
    trust = sel.get("settings", {}).get("use_trust", True) if use_trust is None else use_trust
    names = {o["id"]: o["name"] for o in sel["objects"]}
    top_n = sel.get("top_n") or len(sel["objects"])
    rank = S.ranking(attrs, sel["table"], use_trust=trust)
    return {"ranking": rank, "sensitivity": S.sensitivity(attrs, sel["table"], names, use_trust=trust),
            "voi": S.value_of_information(attrs, sel["table"], names, top_n, use_trust=trust), "names": names,
            "origin": {o["id"]: t for o in sel["objects"] if (t := S.origin_label(o))},
            "weights": {a.id: a.weight for a in attrs}}

