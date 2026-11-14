"""Режим «Подробное обсуждение»: постановка → предложения вслепую → раунды «критика → доработка» →
голосование → проверка фактов лидеров (если включена) → итог председателя."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..session import SessionResult, collect_ideas
from . import common

if TYPE_CHECKING:
    from ..session import Council


async def run(c: Council, question: str, *, criteria: list[str] | None, context: str | None, rounds: int,
              followup_of: str, started: str, t0: float) -> SessionResult:
    b = await common.frame(c, question, criteria, context, "debate")
    await common.propose(c, b)
    for r in range(rounds):
        await common.critique(c, b, f"Критика, раунд {r + 1}/{rounds}")
        await common.revise(c, b, f"Доработка, раунд {r + 1}/{rounds}")
    k = await common.vote(c, b)
    facts = await common.fact_check(c, b.refined, b.proposals, k.rows)
    ideas = collect_ideas(b.critiques, exclude={k.leader})
    v = await common.verdict(c, b, k, facts, ideas)
    return common.result(c, b, k, v, ideas, facts=facts, started=started, t0=t0, followup_of=followup_of)
