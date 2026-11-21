"""Режим «Быстрый ответ»: постановка → предложения вслепую → голосование (без критики и доработки; ценные идеи
проигравших называют сами голосующие) → проверка фактов лидеров (если включена) → итог председателя."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..session import SessionResult, collect_ideas, ideas_from_ballots
from . import common

if TYPE_CHECKING:
    from ..session import Council


async def run(c: Council, question: str, *, criteria: list[str] | None, context: str | None,
              followup_of: str, started: str, t0: float) -> SessionResult:
    b = await common.frame(c, question, criteria, context, "quick")
    await common.propose(c, b)
    keep = c.cfg.quick_keep  # без критики идеи проигравших называют голосующие
    k = await common.vote(c, b, keep=keep)
    facts = await common.fact_check(c, b.refined, b.proposals, k.rows)
    ideas = (ideas_from_ballots(b.ballots, exclude={k.leader}, valid=set(b.proposals)) if keep
             else collect_ideas(b.critiques, exclude={k.leader}))
    v = await common.verdict(c, b, k, facts, ideas, from_voters=True)
    return common.result(c, b, k, v, ideas, facts=facts, started=started, t0=t0, followup_of=followup_of)
