"""Проверка моделей и грубая оценка стоимости заседания до запуска."""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from .config import CouncilConfig, Mode

# Как считается оценка.
# Вход каждого запроса = инструкции этапа + вопрос (уточнённый и исходный) + вложения + то, что модель читает
# на этом этапе: на критике, доработке, голосовании и итоге — ВСЕ чужие варианты целиком. Поэтому главное,
# от чего зависит цена, — длина вариантов. Её программа берёт из ваших прошлых заседаний (сколько токенов модели
# реально пишут на каждом этапе, включая продолжения длинных ответов, повторы и скрытые рассуждения);
# если истории нет — значения по умолчанию ниже.
INSTR = {"framing": 500, "proposal": 800, "critique": 1100, "revision": 1400, "vote": 700, "verdict": 1400,
         "factcheck": 700, "describe": 500}
DEFAULT_OUT = {"framing": 300, "proposal": 2500, "critique": 350, "revision": 3000, "vote": 500,
               "verdict": 4000, "factcheck": 900}   # critique — на ОДИН оцениваемый вариант
FEEDBACK_PER_REVIEW = 250   # отзыв одного рецензента на доработке
NOTES_PER_PROPOSAL = 150    # сводка критики для председателя
FACT_CHECKER = "факт-чек"  # как в session.FACT_CHECKER
HIGH_FACTOR = 2.5           # верхняя граница без истории
HIGH_FACTOR_LEARNED = 1.6   # верхняя граница, когда длины взяты из прошлых заседаний
ATTACHMENT_STAGES = {"framing", "proposal", "critique", "revision", "verdict"}  # как в session.Council
VOTE_DOC_CHARS = 20_000  # как Council.VOTE_DOC_CHARS


@dataclass
class Price:
    prompt: float  # $ за токен
    completion: float
    request: float = 0.0  # $ за запрос (у моделей с поиском, например Perplexity)


@dataclass
class MemberEstimate:
    name: str
    model: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float | None = 0.0  # None — цена неизвестна


@dataclass
class Estimate:
    members: list[MemberEstimate] = field(default_factory=list)
    learned_from: int = 0          # по скольким прошлым заседаниям взяты длины ответов
    lengths: dict[str, int] = field(default_factory=dict)
    question_tokens: int = 0

    @property
    def low(self) -> float:
        return sum(m.cost or 0 for m in self.members)

    @property
    def high(self) -> float:
        return self.low * (HIGH_FACTOR_LEARNED if self.learned_from >= 3 else HIGH_FACTOR)

    @property
    def basis(self) -> str:
        """Пояснение, на чём основана оценка."""
        src = (f"длина ответов — по {self.learned_from} последним заседаниям" if self.learned_from
               else "длина ответов — по умолчанию (истории заседаний пока нет)")
        def n(x: int) -> str:
            return f"{x:,}".replace(",", " ")
        return (f"{src}: вариант ≈ {n(self.lengths.get('proposal', 0))} ток., после доработки ≈ "
                f"{n(self.lengths.get('revision', 0))}, итог ≈ {n(self.lengths.get('verdict', 0))}; "
                f"вопрос ≈ {n(self.question_tokens)} ток. в каждом запросе")

    @property
    def calls(self) -> int:
        return sum(m.calls for m in self.members)


def parse_pricing(models: list[dict[str, Any]]) -> dict[str, Price]:
    out = {}
    for m in models:
        pr = m.get("pricing") or {}
        try:
            out[m["id"]] = Price(float(pr.get("prompt") or 0), float(pr.get("completion") or 0),
                                 float(pr.get("request") or 0))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def unknown_models(cfg: CouncilConfig, known: set[str]) -> dict[str, list[str]]:
    """Модели из конфига, которых нет на OpenRouter, с похожими вариантами названий."""
    result = {}
    for m in cfg.active_members:
        if m.model not in known:
            result[m.model] = difflib.get_close_matches(m.model, sorted(known), n=3, cutoff=0.5)
    return result


def plan_calls(
    cfg: CouncilConfig,
    mode: Mode,
    rounds: int,
    n_options: int | None,
    has_criteria: bool,
) -> dict[str, list[tuple[str, int]]]:
    """Сколько вызовов каждого этапа сделает каждый участник: name -> [(stage, n_proposals)]."""
    members = [m.name for m in cfg.active_members]
    # председатель без своего варианта, если без него хватает участников для кворума (как в session.Council)
    judge = not n_options and not cfg.chair_proposes and len(members) - 1 >= cfg.min_quorum
    proposers = [m for m in members if not (judge and m == cfg.chair)]
    n = n_options or len(proposers)
    own = 0 if n_options else 1  # своего варианта участник не оценивает
    critique_rounds = 0 if mode == "quick" else (1 if mode == "evaluate" else rounds)
    plan: dict[str, list[tuple[str, int]]] = {name: [] for name in members}
    if not has_criteria:
        plan[cfg.chair].append(("framing", 0))
    for name in members:
        mine = own if name in proposers else 0
        if not n_options and name in proposers:
            plan[name].append(("proposal", 0))
        for _ in range(critique_rounds):
            plan[name].append(("critique", n - mine))
            if mode == "debate" and name in proposers:
                plan[name].append(("revision", n - mine))
        plan[name].append(("vote", n - mine))
    plan[cfg.chair].append(("verdict", n))
    if cfg.fact_check.enabled:
        plan[FACT_CHECKER] = [("factcheck", 1)] * min(cfg.fact_check.top, n)
    return plan


def estimate(
    cfg: CouncilConfig,
    pricing: dict[str, Price],
    *,
    mode: Mode,
    rounds: int,
    n_options: int | None = None,
    has_criteria: bool = False,
    doc_tokens: int = 0,
    images: int = 0,
    vision: dict[str, bool | None] | None = None,
    question_chars: int = 0,
    options_chars: int = 0,
    lengths: dict[str, Any] | None = None,
) -> Estimate:
    """doc_tokens/images — объём вложений: они добавляются к запросам этапов, где модели видят материалы.
    question_chars — длина вопроса; options_chars — суммарная длина вариантов пользователя (режим «Оценка»);
    lengths — длины ответов по этапам из прошлых заседаний (learn_lengths), иначе значения по умолчанию."""
    from .attachments import IMAGE_TOKENS

    learned = dict(lengths or {})
    out = {k: int(learned.get(k) or v) for k, v in DEFAULT_OUT.items()}
    est = Estimate(learned_from=int(learned.get("sessions") or 0), lengths=out)
    q = int(question_chars / 3 * 1.3)  # уточнённый вопрос + исходный дословно
    est.question_tokens = q
    plan = plan_calls(cfg, mode, rounds, n_options, has_criteria)
    opt_len = int(options_chars / 3 / n_options) + 100 if n_options and options_chars else out["proposal"]
    first_len = opt_len if n_options else out["proposal"]           # вариант до доработки
    final_len = out["revision"] if mode == "debate" else first_len  # вариант, который читают голосующие и председатель
    small_docs = 0 < doc_tokens * 3 <= VOTE_DOC_CHARS

    def call_tokens(stage: str, n: int, att: int, critique_round: int) -> tuple[int, int]:
        cur = first_len if critique_round == 0 else out["revision"]  # на втором раунде критикуют доработанные
        base = INSTR[stage] + q
        if stage == "framing":
            return base + att, out["framing"]
        if stage == "proposal":
            return base + att, out["proposal"]
        if stage == "critique":
            return base + n * cur + att, out["critique"] * n
        if stage == "revision":
            return base + cur + n * FEEDBACK_PER_REVIEW + n * cur + att, out["revision"]
        if stage == "vote":
            return base + n * final_len + (doc_tokens if small_docs else 0), out["vote"]
        if stage == "verdict":
            return base + n * final_len + n * NOTES_PER_PROPOSAL + att, out["verdict"]
        return base, out.get(stage, 500)
    vision = vision or {}
    blind = [m for m in cfg.active_members if vision.get(m.model) is False]
    describer = None
    if images and blind:
        seers = [m.name for m in cfg.active_members if vision.get(m.model) is not False]
        describer = cfg.chair if cfg.chair in seers else (seers[0] if seers else None)
    for member in cfg.active_members:
        me = MemberEstimate(member.name, member.model)
        sees = vision.get(member.model) is not False
        att = doc_tokens + (images * IMAGE_TOKENS if sees else images * 350)  # «незрячим» — описание текстом
        rounds_seen = 0
        for stage, n in plan[member.name]:
            me.calls += 1
            inp, outp = call_tokens(stage, n, att if stage in ATTACHMENT_STAGES else 0, rounds_seen)
            if stage == "critique":
                rounds_seen += 1
            me.prompt_tokens += inp
            me.completion_tokens += outp  # длинные ответы продолжаются, поэтому лимит длины их не обрезает
        if member.name == describer:
            me.calls += 1
            me.prompt_tokens += 500 + images * IMAGE_TOKENS
            me.completion_tokens += 400 * images
        price = pricing.get(member.model)
        me.cost = (
            me.prompt_tokens * price.prompt + me.completion_tokens * price.completion if price else None
        )
        est.members.append(me)
    if FACT_CHECKER in plan:
        fc = cfg.fact_check
        me = MemberEstimate(FACT_CHECKER, fc.model)
        for _stage, _ in plan[FACT_CHECKER]:
            me.calls += 1
            me.prompt_tokens += INSTR["factcheck"] + q + final_len
            me.completion_tokens += min(out["factcheck"], fc.max_tokens)
        price = pricing.get(fc.model)
        me.cost = (me.prompt_tokens * price.prompt + me.completion_tokens * price.completion + me.calls * price.request
                   if price else None)
        est.members.append(me)
    return est


# Запасной вариант, если каталог OpenRouter недоступен: известные модели без зрения
_BLIND_HINTS = ("deepseek", "kimi-k2", "qwen3-max", "mistral-large")


def vision_map(cfg: CouncilConfig, models: list[dict[str, Any]] | None) -> dict[str, bool | None]:
    """Модель -> умеет ли принимать картинки (по каталогу OpenRouter; без каталога — по известным названиям)."""
    from .attachments import supports_images

    info = {m.get("id"): m for m in (models or [])}
    out: dict[str, bool | None] = {}
    for m in cfg.active_members:
        v = supports_images(info.get(m.model))
        if v is None and any(h in m.model for h in _BLIND_HINTS):
            v = False
        out[m.model] = v
    return out


class ModelCatalog:
    """Кэшированный список моделей OpenRouter (для проверки id и цен)."""

    def __init__(self, ttl: float = 600) -> None:
        self.ttl = ttl
        self._ts = 0.0
        self._models: list[dict[str, Any]] | None = None

    async def get(self) -> list[dict[str, Any]] | None:
        import logging
        import os
        import time

        from .client import OpenRouterClient

        if self._models is not None and time.time() - self._ts < self.ttl:
            return self._models
        client = OpenRouterClient(os.environ.get("OPENROUTER_API_KEY", ""), timeout=20)
        try:
            models = await client.list_models()
        except Exception as e:  # noqa: BLE001 — проверка необязательна
            logging.getLogger("council").warning("не удалось получить список моделей: %r", e)
            return None
        finally:
            await client.aclose()
        self._models, self._ts = models, time.time()
        return models


# ------------------------------------------------------------------ длины ответов по прошлым заседаниям

_LEARN_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def learn_lengths(runs_dir: str | Any, limit: int = 30) -> dict[str, Any]:
    """Сколько токенов модели реально пишут на каждом этапе — по стенограммам последних заседаний.

    Для каждого участника и этапа суммируются выходные токены ВСЕХ попыток (продолжения длинных ответов,
    повторы, скрытые рассуждения) и делятся на число принятых ответов. Критика — на один оцениваемый вариант.
    Берётся медиана по заседаниям. Возвращает {"proposal": …, "revision": …, …, "sessions": N, "recent": [...]}.
    """
    import json
    from pathlib import Path
    from statistics import median

    d = Path(runs_dir)
    files = sorted((p for p in d.glob("*.json") if not p.name.endswith(".events.json")), reverse=True)[:limit] \
        if d.is_dir() else []
    stamp = max((p.stat().st_mtime for p in files), default=0.0)
    key = str(d.resolve()) if d.exists() else str(d)
    hit = _LEARN_CACHE.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    per_stage: dict[str, list[float]] = {}
    recent = []
    used = 0
    for p in files:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tr = data.get("transcript") or []
        if not tr:
            continue
        groups: dict[tuple[str, str], dict[str, float]] = {}
        for e in tr:
            g = groups.setdefault((e.get("member", ""), e.get("stage", "")), {"out": 0.0, "ok": 0, "labels": 0})
            toks = e.get("tokens") or [0, 0]
            g["out"] += float(toks[1] if len(toks) > 1 else 0)
            if "parsed" in e:
                g["ok"] += 1
                if e.get("stage") == "critique":
                    g["labels"] += len((e.get("parsed") or {}).get("reviews") or [])
        session_vals: dict[str, list[float]] = {}
        for (_, stage), g in groups.items():
            if not g["ok"] or not g["out"]:
                continue
            val = g["out"] / (g["labels"] if stage == "critique" and g["labels"] else g["ok"])
            session_vals.setdefault(stage, []).append(val)
        for stage, vals in session_vals.items():
            per_stage.setdefault(stage, []).append(median(vals))
        used += 1
        recent.append({"cost": float(data.get("total_cost_usd") or 0), "mode": data.get("mode", ""),
                       "members": len(data.get("stats") or {}), "duration": float(data.get("duration_s") or 0)})
    out: dict[str, Any] = {stage: int(median(v)) for stage, v in per_stage.items() if v and stage in DEFAULT_OUT}
    out["sessions"] = used
    out["recent"] = recent
    _LEARN_CACHE[key] = (stamp, out)
    return out


def similar_actual(lengths: dict[str, Any] | None, mode: str, members: int) -> dict[str, Any] | None:
    """Сколько реально стоили последние похожие заседания (тот же режим, похожее число участников)."""
    rec = [r for r in (lengths or {}).get("recent", []) if r["mode"] == mode and abs(r["members"] - members) <= 1
           and r["cost"] > 0]
    if not rec:
        return None
    costs = sorted(r["cost"] for r in rec)
    return {"count": len(costs), "min": round(costs[0], 4), "max": round(costs[-1], 4),
            "avg": round(sum(costs) / len(costs), 4)}
