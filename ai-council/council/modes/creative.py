"""Режим «Креатив»: названия, концепции, идеи, слоганы — всё, где нужна широта и смелость, а не одна «правильная»
версия.

Отличия от подробного обсуждения:
- каждый автор получает свою творческую роль (поэт, провокатор, словотворец…) и выдаёт много разных идей по
  нескольким направлениям, а не один отшлифованный ответ; температура выше;
- критика — «да, и…»: сильные идеи, риски (созвучия, переводы, похожие бренды) и как усилить, смелость не наказывается;
- доработка — ремикс: своё лучшее + сильное у других + новые идеи из критики;
- голосование — по силе лучших идей, а не по «безопасности» среднего;
- итог — шорт-лист из ВСЕХ вариантов (а не только победителя): лучшие, смелые, гибриды и что проверить;
- без проверки фактов и поиска: проверять здесь нечего, а платить пришлось бы.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..session import SessionResult, collect_ideas
from . import common

if TYPE_CHECKING:
    from ..session import Council

# Творческие роли авторов: у каждого своя, чтобы идеи не сходились к одному «среднему»
ROLES = [
    ("Poet", "sound, rhythm, imagery and metaphor; names and ideas that feel good to say and evoke a picture"),
    ("Provocateur", "break the category's conventions; bold, unexpected, slightly risky ideas that get noticed"),
    ("Wordsmith", "neologisms, wordplay, compounds, roots from Latin, Greek and other languages, clever spellings"),
    ("Strategist", "audience insight and positioning: what makes it stand out against competitors and why it sells"),
    ("Storyteller", "ideas that carry a story, a character, a myth or a ritual people can retell"),
    ("Minimalist", "short, simple, instantly memorable; one strong word or one clear idea"),
    ("Cross-pollinator", "borrow from unrelated worlds — nature, science, art, sport, travel, history"),
]

FRAMING_NOTE = """

This is a CREATIVE task (names, concepts, ideas, slogans and the like). The criteria must reward originality and
memorability as well as fit to the task — e.g. "Оригинальность", "Запоминаемость", "Соответствие задаче",
"Потенциал развития". Keep any constraints the user gave (language, length, style, things to avoid) as requirements."""

PROPOSAL_NOTE = """

CREATIVE MODE — divergent phase. Your creative role: {role} — {focus}.
- Generate MANY distinct ideas: 12-20, spread over at least 4 clearly different directions (approaches, tones,
  metaphors). Quantity and variety matter more than polish at this stage; do not self-censor.
- For every idea: the idea itself (name / concept / line) in bold, then one short line on why it works.
- Mark 3 ideas as **[смело]** — unexpected, provocative or unconventional; they may be risky.
- Avoid the category's clichés: name 3-5 clichés you deliberately avoided.
- In "summary": group ideas by direction (a short heading per direction), then «Мой топ-3» with one line each.
- Keep the user's constraints (language, length, style, things to avoid)."""

CRITIQUE_NOTE = """

CREATIVE MODE — "yes, and" critique. Be generous and specific, not cautious:
- In "strengths" name the 2-4 strongest ideas of the proposal by their text and say why they stand out.
- In "weaknesses" give concrete risks only: negative or comic associations, hard pronunciation, unfortunate meaning
  in other languages, likely similarity to an existing brand or trademark, unclear meaning, clichés.
- Also give one concrete way to make a good idea stronger, or a promising combination with another proposal's idea.
- Score originality honestly and do NOT punish boldness: a daring idea with a fixable risk beats a safe, forgettable one."""

REVISION_NOTE = """

CREATIVE MODE — remix. Improve your list instead of polishing one answer:
- keep your strongest ideas, drop the weak ones;
- take the strongest ideas or combinations from other proposals and from the critique (mark them «по мотивам X»);
- add at least 5 NEW ideas the critique or the other proposals inspired;
- keep 10-15 ideas grouped by direction, keep 2-3 marked **[смело]**, end with «Мой топ-3»."""

VOTE_NOTE = """

CREATIVE MODE: rank proposals by the strength and originality of their BEST ideas — how good the top 3 are and
whether they would stand out — not by the average idea, length or safety. A list with two brilliant ideas beats a
list of fifteen acceptable ones."""

VERDICT_NOTE = """

CREATIVE MODE — the final answer is a curated SHORTLIST built from ALL proposals, not only the winner (credit each
idea with its proposal label). "final_answer" structure:
1. «Шорт-лист» — the 5-7 best ideas: each with why it works, the risk to keep in mind and the label it came from.
2. «Смелые варианты» — 2-3 bold ideas worth considering even if risky.
3. «Гибриды» — 2-4 combinations or improved versions that the council's ideas suggest.
4. «Отброшенные направления» — briefly, which directions did not work and why.
5. «Что проверить перед выбором» — trademark availability (ФИПС, WIPO), domains and social handles, sound and meaning
   in other relevant languages, a quick test with the target audience. Say plainly that the council did not check
   trademark registries.
6. «Следующий шаг» — e.g. test 3-5 favourites with the audience, or run «Сравнить ваши варианты» on the shortlist.
Keep the user's constraints and requirements."""

# температура «творческих» этапов (если у участника своя температура не задана выше)
PROPOSAL_TEMPERATURE = 1.0
REVISION_TEMPERATURE = 0.9


def roles_for(names: list[str], seed: int | None) -> dict[str, tuple[str, str]]:
    """Роль каждому автору: по кругу, с перемешиванием по номеру заседания (воспроизводимо при продолжении)."""
    import random
    order = ROLES[:]
    random.Random(seed).shuffle(order)  # seed не задан — от вопроса: при продолжении роли те же (ответы из журнала)
    return {n: order[i % len(order)] for i, n in enumerate(names)}


async def run(c: Council, question: str, *, criteria: list[str] | None, context: str | None,
              followup_of: str, started: str, t0: float) -> SessionResult:
    c.stage_temperature = {"proposal": PROPOSAL_TEMPERATURE, "revision": REVISION_TEMPERATURE}
    b = await common.frame(c, question, criteria, context, "creative", note=FRAMING_NOTE,
                           default_criteria=["Оригинальность", "Запоминаемость", "Соответствие задаче",
                                             "Потенциал развития"])
    b.web = False  # поиск в «Креативе» не нужен
    import zlib
    roles = roles_for([m.name for m in c.cfg.active_members],
                      c.seed if c.seed is not None else zlib.crc32(question.encode("utf-8")))
    b.notes = {"proposal": PROPOSAL_NOTE, "critique": CRITIQUE_NOTE, "revision": REVISION_NOTE,
               "vote": VOTE_NOTE, "verdict": VERDICT_NOTE}
    b.persona = {n: {"role": r, "focus": f} for n, (r, f) in roles.items()}
    await common.propose(c, b)
    await common.critique(c, b, "Критика: сильные идеи, риски, как усилить")
    await common.revise(c, b, "Ремикс: лучшее своё и чужое, новые идеи")
    k = await common.vote(c, b)
    ideas = collect_ideas(b.critiques, exclude={k.leader})
    v = await common.verdict(c, b, k, {}, ideas)
    return common.result(c, b, k, v, ideas, facts={}, started=started, t0=t0, followup_of=followup_of)
