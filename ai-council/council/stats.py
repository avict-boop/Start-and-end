"""Статистика по архиву заседаний: расходы, длительность, какие модели чаще побеждают и чаще сбоят."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from .archive import Archive
from .reports.view import MODE_NAME, View


def _month(d: dict[str, Any], stem: str) -> str:
    raw = d.get("started_at") or ""
    try:
        return datetime.fromisoformat(raw).astimezone().strftime("%Y-%m")
    except ValueError:
        return f"{stem[:4]}-{stem[4:6]}" if stem[:6].isdigit() else "—"


def compute(archive: Archive) -> dict[str, Any]:
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
        for name, s in v.stats.items():
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
    }


def fmt_duration(sec: float) -> str:
    return f"{int(sec // 60)} мин {int(sec % 60)} с" if sec >= 60 else f"{sec:.0f} с"
