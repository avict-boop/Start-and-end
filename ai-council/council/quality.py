"""Мерило качества «Отбора» на сохранённых данных — без обращений к моделям.

Берёт сырые находки поиска из журналов заседаний (`journal.jsonl`) и контрольный список компаний (кто должен
найтись) и считает по этапам:

- найдена — компания есть среди сырых находок поиска;
- одна карточка — после склейки она ровно в одной карточке (не размазана по дублям);
- чистая карточка — в её карточке нет названий другой контрольной компании (не склеена с чужой);
- свой сайт — у карточки сайт той компании (если в контрольном списке он указан);
- и, если дан журнал событий с остановкой «короткий список»: статус отсева и попала ли в предложенный список.

Контрольный список — JSON: [{"name": "BioGrowing", "patterns": ["biogrowing", "均瑶润盈"], "site": "biogrowing"}].
patterns — подстроки (без учёта регистра) названий или сайта; site — бренд сайта (без зоны), необязателен.

    python -m council.quality --journal path/journal.jsonl [--journal ...] --control control.json [--events events.jsonl]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import selection as S


def load_finds(paths: list[str | Path]) -> list[tuple[str, S.Candidate]]:
    """Сырые находки поиска кандидатов и чтения списков из журналов заседаний."""
    out: list[tuple[str, S.Candidate]] = []
    for p in paths:
        try:
            lines = Path(p).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            e = e.get("entry", e) if isinstance(e, dict) else {}
            if e.get("stage") not in ("discover", "list_read") or not e.get("parsed"):
                continue
            for it in (e["parsed"].get("items") or []):
                try:
                    c = S.Candidate.model_validate(it)
                except ValueError:
                    continue
                if c.name.strip():
                    out.append((f"{e.get('stage')}{S.BY}{e.get('member') or ''}", c))
    return out


def _blob(names: list[str], website: str = "") -> str:
    return (" ".join(names) + " " + (website or "")).lower()


def _hit(patterns: list[str], blob: str) -> bool:
    return any(p.lower() in blob for p in patterns if p)


def evaluate(finds: list[tuple[str, S.Candidate]], entities: list[dict[str, Any]], control: list[dict[str, Any]],
             review: dict[str, Any] | None = None) -> dict[str, Any]:
    """Сводка по контрольному списку (см. описание модуля)."""
    raw = [_blob([c.name, c.local_name, *c.aliases], c.website) for _, c in finds]
    cards = [(e, _blob([e.get("name") or "", *(e.get("names") or [])], e.get("website") or "")) for e in entities]
    items = {x["id"]: x for x in (review or {}).get("items") or []}
    short = set((review or {}).get("shortlist") or [])
    rows = []
    for ctl in control:
        pats = list(ctl.get("patterns") or [ctl["name"]])
        found = any(_hit(pats, b) for b in raw)
        mine = [e for e, b in cards if _hit(pats, b)]
        others = [o for o in control if o is not ctl]
        dirty = [o["name"] for e in mine for o in others
                 if _hit(list(o.get("patterns") or [o["name"]]), _blob([e.get("name") or "", *(e.get("names") or [])]))]
        site_ok = None
        if ctl.get("site") and len(mine) == 1 and S.site_of(mine[0].get("website") or ""):
            w = mine[0].get("website") or ""
            site_ok = ctl["site"].lower() in (S.site_of(w) or "").lower()
        row = {"name": ctl["name"], "found": found, "cards": len(mine), "clean": not dirty,
               "mixed_with": sorted(set(dirty)), "site_ok": site_ok,
               "card": (mine[0].get("name") if mine else "")}
        if review is not None:
            ids = [x["id"] for x in items.values() if _hit(pats, _blob([x.get("name") or "", *(x.get("names") or [])],
                                                                         x.get("website") or ""))]
            row["status"] = [(items[i].get("status") or (items[i].get("screen") or {}).get("result") or "—") for i in ids]
            row["in_shortlist"] = any(i in short for i in ids)
        rows.append(row)
    n = len(control) or 1
    summary = {
        "control": len(control),
        "found": sum(r["found"] for r in rows),
        "one_card": sum(1 for r in rows if r["cards"] == 1),
        "clean": sum(1 for r in rows if r["cards"] >= 1 and r["clean"]),
        "site_ok": sum(1 for r in rows if r["site_ok"]),
        "duplicated": sum(1 for r in rows if r["cards"] > 1),
        "mixed": sum(1 for r in rows if r["mixed_with"]),
        "entities": len(entities),
    }
    if review is not None:
        summary["in_shortlist"] = sum(1 for r in rows if r.get("in_shortlist"))
    summary["score"] = round(summary["clean"] / n, 3)
    return {"summary": summary, "rows": rows}


def apply_known_sites(entities: list[dict[str, Any]], known: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Как этап «сначала сайт»: карточкам без своего сайта — сайт, который программа нашла в заседании (по тому же
    названию), затем склейка карточек с одним сайтом."""
    site_by: dict[str, str] = {}
    for x in known:
        w = x.get("website") or ""
        if S.site_of(w) and not S.weak_source(w) and S.site_like(w):
            for n in [x.get("name") or "", *(x.get("names") or [])]:
                for part in [n, *n.split(" / ")]:
                    k = S.norm_name(part)
                    if S._name_key_ok(k):
                        site_by.setdefault(k, w)
    for e in entities:
        w = e.get("website") or ""
        if S.site_of(w) and not S.weak_source(w) and S.site_like(w):
            continue
        for n in [e["name"], *(e.get("names") or [])]:
            hit = next((site_by[k] for part in [n, *n.split(" / ")] for k in [S.norm_name(part)] if k in site_by), "")
            if hit:
                e["website"] = hit
                break
    return S.merge_by_site(entities)[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Мерило качества «Отбора» на сохранённых находках (без моделей)")
    ap.add_argument("--journal", action="append", required=True, help="journal.jsonl заседания (можно несколько)")
    ap.add_argument("--control", required=True, help="контрольный список, JSON")
    ap.add_argument("--events", help="events.jsonl с остановкой «короткий список» (статусы и предложенный список)")
    ap.add_argument("--rows", action="store_true", help="показать каждую контрольную компанию")
    ap.add_argument("--sites", action="store_true",
                    help="этап «сначала сайт»: сайты, найденные в заседании (из --events), и склейка по ним")
    a = ap.parse_args(argv)
    finds = load_finds(a.journal)
    ents = S.merge_candidates(finds)
    control = json.loads(Path(a.control).read_text(encoding="utf-8"))
    review = None
    if a.events:
        for line in Path(a.events).read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "review" and ev.get("items"):
                review = ev
    if review is not None and a.sites:
        ents = apply_known_sites(ents, review.get("items") or [])
    res = evaluate(finds, ents, control, review)
    print(json.dumps(res["summary"], ensure_ascii=False))
    if a.rows:
        for r in res["rows"]:
            flag = "OK " if r["cards"] == 1 and r["clean"] and r["site_ok"] is not False else ("—  " if not r["found"] else "!! ")
            print(flag, r["name"].ljust(28), "| карточек", r["cards"], "| смешана с", ",".join(r["mixed_with"]) or "—",
                  "| сайт", {True: "свой", False: "ЧУЖОЙ", None: "нет"}[r["site_ok"]], "|", (r["card"] or "")[:60],
                  *(["| статус", r.get("status"), "| в списке", r.get("in_shortlist")] if "status" in r else []))
    return 0


if __name__ == "__main__":
    sys.exit(main())
