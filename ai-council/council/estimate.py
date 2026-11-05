"""Проверка моделей и грубая оценка стоимости заседания до запуска."""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from .config import CouncilConfig, Mode

# Грубые оценки токенов на один вызов: (вход_база, вход_на_вариант, выход_база, выход_на_вариант).
# n — число вариантов на заседании. Reasoning-модели могут тратить заметно больше на выходе,
# поэтому верхняя граница оценки берётся с запасом (HIGH_FACTOR).
TOKENS = {
    "framing": (450, 0, 250, 0),
    "proposal": (650, 0, 900, 0),
    "critique": (650, 700, 0, 260),
    "revision": (1600, 250, 1000, 0),
    "vote": (550, 750, 200, 0),
    "verdict": (900, 850, 1500, 0),
}
HIGH_FACTOR = 2.5


@dataclass
class Price:
    prompt: float  # $ за токен
    completion: float


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

    @property
    def low(self) -> float:
        return sum(m.cost or 0 for m in self.members)

    @property
    def high(self) -> float:
        return self.low * HIGH_FACTOR

    @property
    def calls(self) -> int:
        return sum(m.calls for m in self.members)


def parse_pricing(models: list[dict[str, Any]]) -> dict[str, Price]:
    out = {}
    for m in models:
        pr = m.get("pricing") or {}
        try:
            out[m["id"]] = Price(float(pr.get("prompt") or 0), float(pr.get("completion") or 0))
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
    n = n_options or len(members)
    own = 0 if n_options else 1  # своего варианта участник не оценивает
    critique_rounds = 0 if mode == "quick" else (1 if mode == "evaluate" else rounds)
    plan: dict[str, list[tuple[str, int]]] = {name: [] for name in members}
    if not has_criteria:
        plan[cfg.chair].append(("framing", 0))
    for name in members:
        if not n_options:
            plan[name].append(("proposal", 0))
        for _ in range(critique_rounds):
            plan[name].append(("critique", n - own))
            if mode == "debate":
                plan[name].append(("revision", n - own))
        plan[name].append(("vote", n - own))
    plan[cfg.chair].append(("verdict", n))
    return plan


def estimate(
    cfg: CouncilConfig,
    pricing: dict[str, Price],
    *,
    mode: Mode,
    rounds: int,
    n_options: int | None = None,
    has_criteria: bool = False,
) -> Estimate:
    est = Estimate()
    plan = plan_calls(cfg, mode, rounds, n_options, has_criteria)
    for member in cfg.active_members:
        me = MemberEstimate(member.name, member.model)
        for stage, n in plan[member.name]:
            ib, ip, ob, op = TOKENS[stage]
            me.calls += 1
            me.prompt_tokens += ib + ip * n
            me.completion_tokens += min(ob + op * n, member.max_tokens or cfg.max_tokens)
        price = pricing.get(member.model)
        me.cost = (
            me.prompt_tokens * price.prompt + me.completion_tokens * price.completion if price else None
        )
        est.members.append(me)
    return est
