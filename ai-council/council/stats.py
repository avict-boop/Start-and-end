"""Статистика по архиву заседаний: расходы, длительность, какие модели чаще побеждают и чаще сбоят."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from .archive import Archive
from .reports.view import MODE_NAME, View

FACT_CHECKER = "факт-чек"  # как в session.FACT_CHECKER (не участник совета — в таблицу моделей не попадает)


def _month(d: dict[str, Any], stem: str) -> str:
    raw = d.get("started_at") or ""
    try:
        return datetime.fromisoformat(raw).astimezone().strftime("%Y-%m")
    except ValueError:
        return f"{stem[:4]}-{stem[4:6]}" if stem[:6].isdigit() else "—"


def _stages(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .costs import stage_summary
    return stage_summary(sessions)


def _qkey(q: str) -> str:
    """Один и тот же вопрос в разных режимах — для сравнения пилота."""
    return re.sub(r"\s+", " ", str(q or "").lower()).strip()[:400]


def research_metrics(stem: str, d: dict[str, Any], v: View, rating: dict[str, Any] | None) -> dict[str, Any]:
    """Показатели одного заседания для таблицы пилота: что подтверждено, ссылки, покрытие, деньги, ваша оценка."""
    led = (d.get("research") or {}).get("ledger") or {}
    claims = led.get("claims") or []
    st = Counter(c.get("status", "") for c in claims)
    ev = led.get("evidence") or []
    links = Counter(e.get("link") for e in ev if e.get("link"))
    cover = Counter(str(r.get("status") or "") for r in v.coverage_rows) if v.is_research else Counter()
    checked = sum(n for k, n in st.items() if k not in ("not_checked", ""))
    return {
        "id": stem, "title": v.title_text, "mode": d.get("mode", ""), "date": (d.get("started_at") or "")[:10],
        "cost": round(v.cost, 4), "duration": float(d.get("duration_s") or 0),
        "failures": sum(len((s or {}).get("failures") or []) for s in (d.get("stats") or {}).values()),
        "claims": len(claims), "checked": checked, "confirmed": st.get("confirmed", 0), "refuted": st.get("refuted", 0),
        "confirmed_share": round(st.get("confirmed", 0) / checked, 2) if checked else None,
        "known": len(v.known) if v.is_research else None, "contested": len(v.contested) if v.is_research else None,
        "unknown": len(v.unknown) if v.is_research else None,
        "sources": len(ev), "links_checked": sum(links.values()), "links_ok": links.get("ok", 0),
        "links_dead": links.get("dead", 0), "links_blocked": links.get("blocked", 0),
        "subq": len(v.coverage_rows) if v.is_research else 0, "covered": cover.get("covered", 0),
        "partial": cover.get("partial", 0),
        "rating": (rating or {}).get("rating"), "note": (rating or {}).get("note", ""),
    }


def compute(archive: Archive, ratings: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    ratings = ratings or {}
    sessions = []
    for stem in archive.stems():
        try:
            sessions.append((stem, archive.load(stem)))
        except (KeyError, OSError, ValueError):
            continue

    now_month = datetime.now(timezone.utc).astimezone().strftime("%Y-%m")
    months: dict[str, dict[str, float]] = defaultdict(lambda: {"sessions": 0, "cost": 0.0})
    modes: Counter[str] = Counter()
    costs, durations = [], []
    with_files = followups = 0
    models: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "sessions": 0, "proposals": 0, "wins": 0, "cost": 0.0, "failures": 0,
        "scores": [], "borda": [], "model": Counter()})
    fact = {"sessions": 0, "confirmed": 0, "refuted": 0, "unclear": 0}
    chairs: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "sessions": 0, "decisions": 0, "overrides": 0, "fallbacks": 0, "agreement": [], "examples": []})

    pilot: list[dict[str, Any]] = []
    pilot_q: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for stem, d in sessions:
        v = View(d, stem)
        cost = v.cost
        costs.append(cost)
        durations.append(float(d.get("duration_s") or 0))
        m = _month(d, stem)
        months[m]["sessions"] += 1
        months[m]["cost"] += cost
        modes[d.get("mode", "")] += 1
        with_files += bool(v.attachments)
        followups += bool(d.get("followup_of"))
        tally = {r.get("label"): r for r in v.tally}
        _facts(fact, d)
        for name, s in v.stats.items():
            if name == FACT_CHECKER:
                continue
            st = models[name]
            st["sessions"] += 1
            st["cost"] += float((s or {}).get("cost") or 0)
            st["failures"] += len((s or {}).get("failures") or [])
        for label, author in (d.get("authors") or {}).items():
            if not author:
                continue
            st = models[author]
            st["proposals"] += 1
            st["wins"] += int(label == v.winner)
            row = tally.get(label) or {}
            if row.get("avg_score") is not None:
                st["scores"].append(float(row["avg_score"]))
            if row.get("borda") is not None:
                st["borda"].append(float(row["borda"]))
        for e in d.get("transcript") or []:
            if e.get("member") in models and e.get("model"):
                models[e["member"]]["model"][e["model"]] += 1
        _chair_stats(chairs, stem, d, v)
        pilot.append(research_metrics(stem, d, v, ratings.get(stem)))
        pilot[-1]["solo_rating"] = (ratings.get(f"{stem}#solo") or {}).get("rating")
        pilot_q[_qkey(d.get("question", ""))].append(pilot[-1])

    model_rows = []
    for name, st in models.items():
        model_rows.append({
            "name": name,
            "model": st["model"].most_common(1)[0][0] if st["model"] else "",
            "sessions": st["sessions"],
            "proposals": st["proposals"],
            "wins": st["wins"],
            "win_rate": st["wins"] / st["proposals"] if st["proposals"] else None,
            "avg_score": mean(st["scores"]) if st["scores"] else None,
            "avg_borda": mean(st["borda"]) if st["borda"] else None,
            "failures": st["failures"],
            "cost": round(st["cost"], 4),
            "cost_per_session": round(st["cost"] / st["sessions"], 4) if st["sessions"] else 0,
        })
    model_rows.sort(key=lambda r: (-(r["win_rate"] or 0), -r["wins"], r["name"]))

    chair_rows = []
    for name, st in chairs.items():
        chair_rows.append({
            "name": name,
            "sessions": st["sessions"],
            "decisions": st["decisions"],
            "overrides": st["overrides"],
            "override_rate": st["overrides"] / st["decisions"] if st["decisions"] else None,
            "fallbacks": st["fallbacks"],
            "avg_agreement": round(mean(st["agreement"]), 2) if st["agreement"] else None,
            "examples": st["examples"][-5:],
        })
    chair_rows.sort(key=lambda r: (-r["sessions"], r["name"]))

    month_rows = [{"month": k, "sessions": int(v["sessions"]), "cost": round(v["cost"], 4)}
                  for k, v in sorted(months.items())][-12:]
    return {
        "sessions": len(sessions),
        "total_cost": round(sum(costs), 4),
        "month_cost": round(months[now_month]["cost"], 4) if now_month in months else 0.0,
        "month_sessions": int(months[now_month]["sessions"]) if now_month in months else 0,
        "avg_cost": round(mean(costs), 4) if costs else 0.0,
        "avg_duration": round(mean(durations), 1) if durations else 0.0,
        "with_files": with_files,
        "followups": followups,
        "modes": [{"mode": k, "name": MODE_NAME.get(k, k), "count": c} for k, c in modes.most_common()],
        "months": month_rows,
        "models": model_rows,
        "chairs": chair_rows,
        "fact_check": fact,
        # пилот: исследования и тот же вопрос в других режимах (рядом — чтобы сравнить)
        "research": [r for r in pilot if r["mode"] == "research"][-30:],
        "pairs": [{"title": rows[-1]["title"], "runs": rows} for rows in pilot_q.values()
                  if len({r["mode"] for r in rows}) > 1 and any(r["mode"] == "research" for r in rows)][-15:],
        "rated": sum(1 for r in pilot if r["rating"]),
        "stages": _stages([d for _, d in sessions]),
    }


def _facts(acc: dict[str, int], d: dict[str, Any]) -> None:
    fc = d.get("fact_check") or {}
    if not fc:
        return
    acc["sessions"] += 1
    for r in fc.values():
        for c in r.get("claims", []):
            acc[c.get("status") if c.get("status") in ("confirmed", "refuted") else "unclear"] += 1


FALLBACK_WHY = "Председатель не ответил"


def _chair_of(d: dict[str, Any]) -> str:
    """Кто был председателем: из настроек запуска, иначе — кто писал вердикт по стенограмме."""
    chair = (d.get("request") or {}).get("chair")
    if chair:
        return chair
    for e in reversed(d.get("transcript") or []):
        if e.get("stage") == "verdict" and e.get("member"):
            return e["member"]
    return ""


def _chair_stats(chairs: dict[str, dict[str, Any]], stem: str, d: dict[str, Any], v: View) -> None:
    """Насколько председатель самостоятелен: как часто он выбирает не лидера голосования."""
    chair = _chair_of(d)
    tally = v.tally
    if not chair or not tally:
        return
    st = chairs[chair]
    st["sessions"] += 1
    a = v.agreement
    if a:
        st["agreement"].append(float(a["tau"]))
    if str((d.get("verdict") or {}).get("why", "")).startswith(FALLBACK_WHY):
        st["fallbacks"] += 1  # председатель не ответил — итог по голосованию, это не его решение
        return
    st["decisions"] += 1
    leader = tally[0].get("label")
    if v.winner and leader and v.winner != leader and v.winner not in v.top_tie:
        st["overrides"] += 1
        st["examples"].append({"id": stem, "title": v.title_text[:80], "leader": leader, "winner": v.winner})


def fmt_duration(sec: float) -> str:
    return f"{int(sec // 60)} мин {int(sec % 60)} с" if sec >= 60 else f"{sec:.0f} с"
