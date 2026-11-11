"""Описания моделей для выбора в настройках: понятные метки, подсказки по ролям в совете, ваша история.

Ничего не придумывается: метки — из данных каталога OpenRouter (цена, контекст, длина ответа, рассуждения,
картинки, поиск), история — из ваших заседаний, описание — от разработчика (по кнопке его можно кратко
пересказать по-русски дешёвой моделью; пересказ запоминается в runs/model_notes.json).
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any

from .catalog import is_light

CACHE = "model_notes.json"  # в runs/notes/: не в корне архива, иначе файл примут за заседание


def _k(n: int | None) -> str:
    if not n:
        return "—"
    return f"{n // 1_000_000} млн" if n >= 1_000_000 else f"{n // 1000} тыс."


def price_tier(out_price: float) -> tuple[str, str]:
    """Цена за 1M выходных токенов → (вид метки, текст)."""
    if out_price <= 0:
        return "plain", "цена неизвестна"
    if out_price < 1.5:
        return "ok", "дешёвая"
    if out_price <= 8:
        return "plain", "средняя цена"
    return "warn", "дорогая"


def tags(row: dict[str, Any]) -> list[list[str]]:
    """Короткие метки на русском: [[вид, текст], …] — вид для цвета (ok / warn / plain)."""
    out = [list(price_tier(float(row.get("completion") or 0)))]
    mid = str(row.get("id", ""))
    if row.get("reasoning"):
        out.append(["plain", "рассуждает"])
    if mid.startswith("perplexity/") or mid.endswith(":online"):
        out.append(["ok", "ищет в интернете"])
    if row.get("image"):
        out.append(["plain", "видит картинки"])
    if row.get("audio"):
        out.append(["plain", "понимает звук"])
    ctx = row.get("context") or 0
    if ctx >= 500_000:
        out.append(["ok", f"огромный контекст ({_k(ctx)})"])
    elif ctx and ctx < 64_000:
        out.append(["warn", f"маленький контекст ({_k(ctx)})"])
    mo = row.get("max_out") or 0
    if mo and mo < 8000:
        out.append(["warn", f"короткий ответ (до {_k(mo)})"])
    if is_light(mid):
        out.append(["plain", "облегчённая версия"])
    return out


def roles(row: dict[str, Any]) -> list[str]:
    """Для чего модель подходит в совете — по её характеристикам (подсказка, не гарантия качества)."""
    mid = str(row.get("id", ""))
    out_price = float(row.get("completion") or 0)
    ctx = row.get("context") or 0
    mo = row.get("max_out") or 0
    light = is_light(mid)
    res = []
    if mid.startswith("perplexity/") or mid.endswith(":online"):
        res.append("проверка фактов и арбитраж доказательств — у неё встроенный поиск; участником совета её обычно не ставят")
        return res
    if row.get("reasoning") and not light and (not mo or mo >= 16000):
        res.append("председатель: рассуждает и может написать длинный итог")
        res.append("адвокат дьявола или скептик: этой роли нужно думать, а не только писать")
    if light or (out_price and out_price < 1.5):
        res.append("недорогой участник для разнообразия мнений; в глубоких исследованиях может уступать старшим моделям")
    elif not res:
        res.append("обычный участник совета")
    if ctx >= 500_000:
        res.append("большие вложения: длинные документы помещаются целиком")
    if row.get("image"):
        res.append("вопросы с картинками и сканами")
    if mo and mo < 8000:
        res.append("не для председателя: короткий предел ответа")
    return res


def history(sessions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Ваша статистика по моделям (а не по именам участников): заседания, победы вариантов, оценки, сбои."""
    acc: dict[str, dict[str, Any]] = defaultdict(lambda: {"sessions": 0, "proposals": 0, "wins": 0, "scores": [],
                                                          "failures": 0, "cost": 0.0, "reasoning_off": 0})
    for d in sessions:
        used: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for e in d.get("transcript") or []:
            if e.get("member") and e.get("model"):
                used[e["member"]][e["model"]] += 1
        member_model = {m: max(c, key=c.get) for m, c in used.items()}
        winner = (d.get("verdict") or {}).get("winner")
        tally = {r.get("label"): r for r in d.get("tally") or []}
        for name, st in (d.get("stats") or {}).items():
            model = member_model.get(name)
            if not model:
                continue
            a = acc[model]
            a["sessions"] += 1
            a["failures"] += len((st or {}).get("failures") or [])
            a["cost"] += float((st or {}).get("cost") or 0)
            a["reasoning_off"] += int(bool((st or {}).get("reasoning_off")))
        for label, author in (d.get("authors") or {}).items():
            model = member_model.get(author or "")
            if not model:
                continue
            a = acc[model]
            a["proposals"] += 1
            a["wins"] += int(label == winner)
            sc = (tally.get(label) or {}).get("avg_score")
            if sc is not None:
                a["scores"].append(float(sc))
    out = {}
    for model, a in acc.items():
        out[model] = {"sessions": a["sessions"], "proposals": a["proposals"], "wins": a["wins"],
                      "win_rate": round(a["wins"] / a["proposals"], 2) if a["proposals"] else None,
                      "avg_score": round(mean(a["scores"]), 1) if a["scores"] else None,
                      "failures": a["failures"], "reasoning_off": a["reasoning_off"],
                      "cost_per_session": round(a["cost"] / a["sessions"], 3) if a["sessions"] else None}
    return out


def history_for(history_map: dict[str, dict[str, Any]], model: str) -> dict[str, Any] | None:
    """История модели; провайдер иногда отвечает версией с датой (claude-sonnet-5.5-20261001) — ищем и так."""
    if model in history_map:
        return history_map[model]
    base = model.split(":")[0]
    for k, v in history_map.items():
        if k.startswith(base):
            return v
    return None


def load_cache(runs_dir: Path) -> dict[str, str]:
    try:
        return json.loads((Path(runs_dir) / "notes" / CACHE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_summary(runs_dir: Path, model: str, text: str) -> None:
    data = load_cache(runs_dir)
    data[model] = text
    p = Path(runs_dir) / "notes" / CACHE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def translate_key(language: str, model: str, full: bool) -> str:
    return f"{'trfull' if full else 'tr'}:{language}:{model}"


def translate_prompt(texts: dict[str, str], language: str) -> str:
    payload = json.dumps(texts, ensure_ascii=False, indent=1)
    return f"""Translate each value of this JSON object into {language}. These are descriptions of AI models from a model
catalog. Keep model names, product names, numbers and technical terms (context, tokens, benchmarks) as they are; keep the
meaning exact, add nothing. Reply with ONE JSON object with the same keys and the translated values, nothing else.

{payload}"""


def save_many(runs_dir: Path, items: dict[str, str]) -> None:
    if not items:
        return
    data = load_cache(runs_dir)
    data.update(items)
    p = Path(runs_dir) / "notes" / CACHE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def summary_prompt(row: dict[str, Any], description: str) -> str:
    return f"""Below is the official OpenRouter description of the AI model "{row.get('id')}" and its catalog data.
Write in Russian, 3-5 short sentences for a non-technical user choosing models for an "AI council" (several models
answer, critique each other and vote; a chair writes the summary): what the developer says the model is good at,
its notable limits or trade-offs, and which council roles it suits (participant, chair, devil's advocate, fact-checker).
Use ONLY the description and the data below. Do not invent benchmarks, rankings or comparisons that are not stated.
If the description says little, say so.

Catalog data: price ${row.get('prompt')} input / ${row.get('completion')} output per 1M tokens; context {row.get('context')};
max answer {row.get('max_out') or 'unknown'}; reasoning: {'yes' if row.get('reasoning') else 'no'};
images: {'yes' if row.get('image') else 'no'}; released {row.get('date')}.

Description:
{description[:4000]}

Reply with the plain text only."""
