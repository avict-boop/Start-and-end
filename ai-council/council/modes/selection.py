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


async def fetch_pages(c: Council, urls: list[str]) -> dict[str, dict[str, Any]]:
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
            got = await c.link_checker(todo, texts=True)
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
    c._emit("stage", stage="framing", title="Рамка отбора: кто кандидат, признаки и веса, где искать")
    start0 = sc.start
    try:
        # этапы «Отбора» — свои имена в журнале и в истории длин ответов (не путать с «framing» и «verdict» других
        # режимов: короткий вывод отбора иначе занижал бы резерв длинного итога исследования)
        frame = await c._ask(chair, "frame",
                             S.frame_prompt(question, context, criteria, hint,
                                            start0 if start0 != "auto" else ("check" if len(hint) >= 3 else "find"),
                                            sc.max_attributes),
                             S.SelectionFrame, meta=META)
    except (ProviderError, InvalidResponse) as e:
        if not _soft(e):
            raise
        raise S.CouncilError(f"рамка отбора не получилась: {e}") from e
    if not frame.attributes:
        raise S.CouncilError("рамка отбора без признаков — нечего сравнивать")
    seeds = list(dict.fromkeys([*hint, *frame.objects]))
    start = start0 if start0 != "auto" else ("check" if len(seeds) >= 3 else "find")
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
    entities = entities[:sc.max_candidates]
    if start in ("find", "extend"):
        dec = gate(c, "candidates", {"items": entities, "completeness": found_meta.get("completeness")})
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
    limit = sc.shortlist or frame.shortlist or sc.max_deep
    keep.sort(key=lambda e: (not e.get("seed"), (e.get("screen") or {}).get("result") != "pass", -len(e["angles"])))
    shortlist = keep[:min(limit, sc.max_deep)]
    if start != "check":
        dec = gate(c, "shortlist", {"items": entities, "shortlist": [e["id"] for e in shortlist],
                                    "excluded": excluded})
        if dec is not None:
            ids = [i for i in dec.get("keep") or [] if any(e["id"] == i for e in entities)]
            shortlist = [e for i in ids for e in entities if e["id"] == i] + apply_list_edits([], {"add": dec.get("add")},
                                                                                                start_n=_max_id(entities))
            entities += [e for e in shortlist if e not in entities]
    keep_ids = {e["id"] for e in shortlist}
    excluded = [x for x in excluded if x["id"] not in keep_ids]  # возвращённые вами на остановке — не «исключены»
    c._emit("shortlist", count=len(shortlist), excluded=len(excluded), total=len(entities))

    # 4. Подробное заполнение
    fillers = pick_fillers(c)
    c._emit("stage", stage="fill", title=f"Заполнение таблицы: {len(shortlist)} {_obj_word(len(shortlist))} × "
                                          f"{len(attrs)} признаков; читают {', '.join(m.name for m in fillers)}")
    rows: dict[str, dict[str, Any]] = {}
    sem = asyncio.Semaphore(sc.parallel)

    async def one(e: dict[str, Any]) -> None:
        async with sem:
            rows[e["id"]] = await fill_object(c, frame, attrs, e, fillers)

    results = await asyncio.gather(*(one(e) for e in shortlist), return_exceptions=True)
    for r in results:
        if isinstance(r, (BudgetExceeded, OutOfCredits)):
            raise r
        if isinstance(r, BaseException) and not _soft(r):
            raise r

    # 5. Разбор спорного
    disputed = [(oid, a) for oid, row in rows.items() for a in attrs
                if (row["cells"].get(a.id) or {}).get("status") == "disputed"]
    if disputed:
        await resolve_disputes(c, frame, attrs, shortlist, rows, fillers)

    # 6. Решение: ранг, устойчивость, что выяснить — расчётом; вывод председателя
    objects = [e for e in shortlist if e["id"] in rows]
    table = {oid: rows[oid]["cells"] for oid in rows}
    names = {e["id"]: e["name"] for e in objects}
    rank = S.ranking(attrs, table, use_trust=sc.use_trust)
    top_n = min(top_n or len(rank), len(rank))
    sens = S.sensitivity(attrs, table, names, use_trust=sc.use_trust)
    voi = S.value_of_information(attrs, table, names, top_n, use_trust=sc.use_trust)
    sel = {"frame": frame.model_dump(), "start": start, "attributes": [a.model_dump() for a in attrs],
           "objects": objects, "table": table, "ranking": rank, "top_n": top_n, "sensitivity": sens, "voi": voi,
           "excluded": excluded, "candidates": len(entities), "discovery": found_meta,
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
                                c.cfg.answer_size == "brief"),
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
                    "seed": True, "url": "", "quote": "", "why": "добавлено вами", "merged": False})
    return out


async def discover(c: Council, frame: S.SelectionFrame, seeds: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Поиск кандидатов: каждое направление ищет отдельно; дубли склеиваются; полнота — по повторным находкам.
    Мало повторов (список далёк от полного) — ещё круг с новыми направлениями от председателя."""
    sc = c.cfg.selection
    chair = c.cfg.member(c.cfg.chair)
    angles = frame.angles[:sc.angles] or ["general web search for the candidates"]
    # кто ищет: модель поиска и (по умолчанию) все участники — каждый по всем направлениям, своим поисковиком
    hunters = [searcher(c)] + (list(c.cfg.active_members) if sc.discover_by == "all" else [])
    c.stage_search_engine["discover"] = sc.discover_engine
    found: list[tuple[str, S.Candidate]] = []
    used: list[str] = []
    rounds_meta = []
    for rnd in range(sc.rounds):
        if rnd:
            comp = S.completeness(S.merge_candidates(found), len(used))
            if comp["share"] is None or comp["share"] >= sc.saturation:
                break
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
        known = [e["name"] for e in S.merge_candidates(found)] if rnd else []
        c._emit("stage", stage="discover", title=f"Поиск кандидатов{' — ещё круг' if rnd else ''}: "
                                                 f"{len(angles)} направлений × {len(hunters)} "
                                                 f"{'модель' if len(hunters) == 1 else 'моделей' if len(hunters) > 4 else 'модели'}"
                                                 f" ({', '.join(m.name for m in hunters)})")
        plan = [(a, m) for a in angles for m in hunters]
        jobs = [c._ask(m, "discover", S.discover_prompt(frame, a, sc.per_angle, known), S.Candidates,
                       meta={**META, "angle": a}, web_search=True) for a, m in plan]
        got = await asyncio.gather(*jobs, return_exceptions=True)
        for (a, m), r in zip(plan, got):
            if isinstance(r, (BudgetExceeded, OutOfCredits)):
                raise r
            if isinstance(r, BaseException):
                if not _soft(r):
                    raise r
                c.stats.setdefault(m.name, MemberStats()).failures.append(f"discover: {a}: {r}")
                c._emit("member_failed", stage="discover", member=f"{m.name} ({a[:40]})", error=str(r))
                continue
            # источник находки — пара «направление · модель»: полнота считается по независимым находкам
            found += [(f"{a}{S.BY}{m.name}", x) for x in r.items]
            c._emit("candidates_found", angle=a, by=m.name, count=len(r.items))
        used += angles
        rounds_meta.append({"angles": angles, "found": len(S.merge_candidates(found))})
    first_angles = set(frame.angles[:sc.angles] or used)
    first = [x for x in found if x[0].split(S.BY)[0] in first_angles]
    comp = S.completeness(S.merge_candidates(first), min(len(used), sc.angles) * len(hunters))
    entities = S.merge_candidates(found, seeds)
    for e in entities:  # какие модели нашли кандидата
        e["models"] = sorted({a.split(S.BY)[-1] for a in e["angles"] if S.BY in a})
    entities.sort(key=lambda e: (not e["seed"], -len(e["angles"]), e["name"].lower()))
    for i, e in enumerate(entities, 1):
        e["id"] = f"O{i}"
    c._emit("candidates", count=len(entities), completeness=comp, merged=sum(1 for e in entities if e["merged"]))
    log.info("%sотбор: найдено %d кандидатов (оценка полноты %s)", c._tag, len(entities), comp.get("share"))
    return entities, {"completeness": comp, "rounds": rounds_meta, "angles": used}


async def screen(c: Council, frame: S.SelectionFrame, attrs: list[S.Attribute], entities: list[dict[str, Any]]
                 ) -> dict[str, dict[str, Any]]:
    """Отсев: короткая проверка признаков отсева у всех кандидатов пачками (поиск)."""
    sc = c.cfg.selection
    c._emit("stage", stage="screen", title=f"Отсев: {len(entities)} кандидатов по {len(attrs)} признакам "
                                          f"({', '.join(a.name for a in attrs)})")
    batches = [entities[i:i + sc.screen_batch] for i in range(0, len(entities), sc.screen_batch)]
    jobs = [c._ask(searcher(c), "screen", S.screen_prompt(frame, attrs, b), S.ScreenBatch,
                   meta={**META, "objects": [e["id"] for e in b]}, web_search=True) for b in batches]
    got = await asyncio.gather(*jobs, return_exceptions=True)
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
            out[e["id"]] = {"result": result, "reason": reason, "checks": [x.model_dump() for x in checks]}
            if row.website and not e.get("website"):
                e["website"] = row.website
    counts = {k: sum(1 for v in out.values() if v["result"] == k) for k in ("pass", "fail", "unclear")}
    c._emit("screened", **counts, total=len(entities))
    return out


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
    src = S.ObjectSources()
    try:
        src = await c._ask(searcher(c), "sources", S.sources_prompt(frame, e, attrs, sc.pages_per_object),
                           S.ObjectSources, meta=meta, web_search=True)
    except (ProviderError, InvalidResponse) as err:
        if not _soft(err):
            raise
        c.stats[FACT_CHECKER].failures.append(f"sources {e['name']}: {err}")
        c._emit("member_failed", stage="sources", member=f"{FACT_CHECKER} ({e['name'][:40]})", error=str(err))
    if src.website and not S.site_of(e.get("website", "")):
        e["website"] = src.website
    urls = list(dict.fromkeys([s.url for s in src.sources if s.url] + [x.url for x in src.cells if x.url]
                              + ([e["url"]] if e.get("url") else [])
                              + ([_home(e["website"])] if S.site_of(e.get("website", "")) else [])))[:sc.pages_per_object + 2]
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
    if not texts:  # страницы не открылись — модели читают хотя бы цитаты, которые вернул поиск
        texts = [(x.url or "search", f"(search snippet) {x.quote}") for x in src.cells if x.quote]
    jobs = {m.name: c._ask(m, "fill", S.fill_prompt(frame, e, attrs, texts), S.ObjectCells, meta=meta)
            for m in fillers}
    got = await asyncio.gather(*jobs.values(), return_exceptions=True)
    reads: dict[str, list[S.Cell]] = {"поиск": list(src.cells)}
    for name, r in zip(jobs, got):
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
            urls=len(urls), **{f"n_{k}": v for k, v in st.items()})
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
    by_id = {e["id"]: e for e in objs}
    work = []
    for oid, row in rows.items():
        ds = [(a, row["cells"][a.id]["options"]) for a in attrs if row["cells"].get(a.id, {}).get("status") == "disputed"]
        if ds:
            work.append((oid, ds))
    n = sum(len(ds) for _, ds in work)
    c._emit("stage", stage="dispute", title=f"Разбор спорного: {n} ячеек у {len(work)} {_obj_word(len(work))}; "
                                            f"решают {', '.join(m.name for m in judges)}")
    chair = c.cfg.chair

    async def one(oid: str, ds: list[tuple[S.Attribute, list[dict[str, Any]]]]) -> None:
        prompt = S.dispute_prompt(frame, by_id[oid], ds)
        jobs = {m.name: c._ask(m, "dispute", prompt, S.Rulings, meta={**META, "object": oid}) for m in judges}
        got = await asyncio.gather(*jobs.values(), return_exceptions=True)
        votes: dict[str, dict[str, tuple[str, str]]] = {}
        for name, r in zip(jobs, got):
            if isinstance(r, (BudgetExceeded, OutOfCredits)):
                raise r
            if isinstance(r, BaseException):
                if not _soft(r):
                    raise r
                c.stats.setdefault(name, MemberStats()).failures.append(f"dispute {oid}: {r}")
                continue
            for it in r.items:
                a = S.match_attr(it.key, [x for x, _ in ds])
                if a:
                    votes.setdefault(a.id, {})[name] = (_choice(it.choice), it.reason)
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

    res = await asyncio.gather(*(one(oid, ds) for oid, ds in work), return_exceptions=True)
    for r in res:
        if isinstance(r, BaseException):
            raise r
    c._emit("disputes_done", cells=n)


def _choice(raw: str) -> str:
    """Выбор судьи: буква варианта («a», «(b)», «Option C», «вариант c») или «none»."""
    t = (raw or "").strip().lower()
    if not t or re.match(r"^\W*(none|no option|нет|ни один|neither)", t):
        return "none"
    m = re.match(r"^\W*(?:option|вариант)?\s*\(?([a-z])\)?(?![a-z])", t)
    return m.group(1) if m else "none"


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
            "weights": {a.id: a.weight for a in attrs}}

