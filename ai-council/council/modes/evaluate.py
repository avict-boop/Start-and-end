"""Режим «Выбор из вариантов»: постановка → варианты пользователя → критика (один раунд, без доработки:
варианты чужие) → голосование → проверка фактов лидеров (если включена) → итог председателя, который может
честно сказать «ни один вариант не подходит»."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..session import SessionResult, collect_ideas
from . import common

if TYPE_CHECKING:
    from ..session import Council


async def run(c: Council, question: str, *, options: list[str], criteria: list[str] | None, context: str | None,
              followup_of: str, started: str, t0: float) -> SessionResult:
    b = await common.frame(c, question, criteria, context, "evaluate")
    common.user_options(c, b, options)
    await common.critique(c, b, "Критика, раунд 1/1")
    k = await common.vote(c, b)
    facts = await common.fact_check(c, b.refined, b.proposals, k.rows)
    ideas = collect_ideas(b.critiques, exclude={k.leader})
    v = await common.verdict(c, b, k, facts, ideas, allow_none=c.cfg.evaluate_allow_none)
    return common.result(c, b, k, v, ideas, facts=facts, started=started, t0=t0, followup_of=followup_of)
