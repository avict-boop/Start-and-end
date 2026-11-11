"""Куда ушли деньги заседания: по этапам и участникам — из стенограммы (каждая запись — одно обращение к модели).

Обращение «принято», если из него получен ответ (в записи есть разобранный JSON). Остальные — повторы (ответ не
по схеме, обрезан, ссылался на файл) и части длинных ответов, которые дописывались продолжениями.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

STAGE_NAME = {"framing": "Постановка", "describe": "Описание изображений", "proposal": "Предложения",
              "critique": "Критика", "arbitration": "Арбитраж доказательств", "revision": "Доработка",
              "vote": "Голосование", "factcheck": "Проверка фактов", "verdict": "Итог", "recheck": "Повторная проверка"}
ORDER = list(STAGE_NAME)


def _blank() -> dict[str, Any]:
    return {"requests": 0, "accepted": 0, "extra": 0, "prompt": 0, "completion": 0, "cached": 0, "cost": 0.0}


def _add(acc: dict[str, Any], e: dict[str, Any]) -> None:
    tok = e.get("tokens") or [0, 0]
    acc["requests"] += 1
    acc["accepted"] += int(e.get("parsed") is not None)
    acc["extra"] += int(e.get("parsed") is None)
    acc["prompt"] += int(tok[0] or 0)
    acc["completion"] += int(tok[1] or 0) if len(tok) > 1 else 0
    acc["cached"] += int(e.get("cached") or 0)
    acc["cost"] += float(e.get("cost") or 0)


def stage_costs(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    """{"stages": [...], "members": [...], "total": $, "cache_share": доля входа из кэша, "extra_cost": $ на повторы}."""
    by_stage: dict[str, dict[str, Any]] = defaultdict(_blank)
    by_member: dict[tuple[str, str], dict[str, Any]] = defaultdict(_blank)
    extra_cost = 0.0
    for e in transcript or []:
        if not isinstance(e, dict) or "tokens" not in e:
            continue
        st = e.get("stage") or "?"
        _add(by_stage[st], e)
        _add(by_member[(e.get("member") or "?", st)], e)
        if e.get("parsed") is None:
            extra_cost += float(e.get("cost") or 0)
    total = sum(r["cost"] for r in by_stage.values())
    prompt = sum(r["prompt"] for r in by_stage.values())
    cached = sum(r["cached"] for r in by_stage.values())

    def row(name: str, r: dict[str, Any], **kw: Any) -> dict[str, Any]:
        return {**kw, "stage": name, "stage_name": STAGE_NAME.get(name, name), **r, "cost": round(r["cost"], 4),
                "share": round(r["cost"] / total, 3) if total else 0.0,
                "cache_share": round(r["cached"] / r["prompt"], 3) if r["prompt"] else 0.0}

    stages = [row(s, by_stage[s]) for s in sorted(by_stage, key=lambda x: ORDER.index(x) if x in ORDER else 99)]
    members = sorted((row(st, r, member=m) for (m, st), r in by_member.items()), key=lambda x: -x["cost"])
    return {"stages": stages, "members": members, "total": round(total, 4),
            "cache_share": round(cached / prompt, 3) if prompt else 0.0, "extra_cost": round(extra_cost, 4)}


def stage_summary(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Для статистики: доля каждого этапа в расходах по всем заседаниям и доля входа из кэша."""
    acc: dict[str, dict[str, Any]] = defaultdict(_blank)
    for d in sessions:
        for r in stage_costs(d.get("transcript") or [])["stages"]:
            a = acc[r["stage"]]
            for k in ("requests", "accepted", "extra", "prompt", "completion", "cached"):
                a[k] += r[k]
            a["cost"] += r["cost"]
    total = sum(r["cost"] for r in acc.values())
    return [{"stage": s, "stage_name": STAGE_NAME.get(s, s), **{k: v for k, v in acc[s].items() if k != "cost"},
             "cost": round(acc[s]["cost"], 4), "share": round(acc[s]["cost"] / total, 3) if total else 0.0,
             "cache_share": round(acc[s]["cached"] / acc[s]["prompt"], 3) if acc[s]["prompt"] else 0.0}
            for s in sorted(acc, key=lambda x: ORDER.index(x) if x in ORDER else 99)]
