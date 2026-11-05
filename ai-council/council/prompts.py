"""Шаблоны промптов для каждой роли и этапа.

Промпты на английском (модели следуют им точнее), а весь человекочитаемый
текст в ответах модели пишут на языке из конфига.
"""
from __future__ import annotations

import json

from .schemas import Proposal

SYSTEM = """You are "{name}", a member of an expert council made up of AI models from different labs.
The council's goal is to find the best possible answer to the user's question through independent proposals, honest critique, revision and voting.

Rules:
- Think independently. Do not defer to others just because they agree with each other.
- Be concrete and practical. Back claims with reasoning; name trade-offs and risks.
- Critique honestly: no flattery, no false modesty. Judge ideas, not authors (proposals are anonymous).
- Write all human-readable text in {language}.
- Reply with ONE valid JSON object only. No markdown fences, no text before or after it."""

DEVIL = """
Special role: you are the council's devil's advocate. Look for non-obvious alternatives, hidden assumptions and weak points that others miss. Stay constructive: the goal is a better final answer, not contrarianism."""

CHAIR = """
Special role: you chair the council. You must be neutral and fair toward all proposals, including any that may be your own."""


def system(name: str, language: str, *, devil: bool = False, chair: bool = False) -> str:
    text = SYSTEM.format(name=name, language=language)
    if devil:
        text += DEVIL
    if chair:
        text += CHAIR
    return text


def _criteria(criteria: list[str]) -> str:
    return "\n".join(f"- {c}" for c in criteria)


def _context(context: str | None) -> str:
    return f"\nAdditional context from the user:\n{context}\n" if context else ""


def _proposal_block(label: str, p: Proposal) -> str:
    lines = [f"### Proposal {label}: {p.title}", p.summary]
    if p.reasoning:
        lines.append(f"Reasoning: {p.reasoning}")
    if p.pros:
        lines.append("Pros: " + "; ".join(p.pros))
    if p.risks:
        lines.append("Risks: " + "; ".join(p.risks))
    return "\n".join(lines)


def proposals_block(proposals: dict[str, Proposal]) -> str:
    return "\n\n".join(_proposal_block(label, p) for label, p in proposals.items())


def framing(question: str, context: str | None) -> str:
    return f"""Before the council starts, prepare the task.

User question:
{question}
{_context(context)}
1. Restate the question precisely and neutrally (keep the user's intent; do not answer it).
2. Define 3-5 short evaluation criteria that matter most for choosing the best answer (e.g. effectiveness, cost, risk, feasibility). Each criterion is 1-4 words.

JSON format:
{{"refined_question": "...", "criteria": ["...", "..."]}}"""


def proposal(question: str, criteria: list[str], context: str | None) -> str:
    return f"""Question for the council:
{question}
{_context(context)}
The answers will be judged by these criteria:
{_criteria(criteria)}

Propose YOUR best answer/option. Other members answer independently; you will not see them yet.

JSON format:
{{"title": "short name of your option",
  "summary": "the option itself, complete and concrete (can be several paragraphs)",
  "reasoning": "why this is the best choice",
  "pros": ["..."],
  "risks": ["..."],
  "confidence": 0.0-1.0}}"""


def critique(question: str, criteria: list[str], proposals: dict[str, Proposal]) -> str:
    labels = ", ".join(proposals)
    example_scores = ", ".join(f'"{c}": 1-10' for c in criteria)
    return f"""Question for the council:
{question}

Review each of these anonymous proposals:

{proposals_block(proposals)}

Score every proposal on every criterion from 1 (very bad) to 10 (excellent):
{_criteria(criteria)}

You must review exactly these labels: {labels}. Use the criteria names exactly as written.

JSON format:
{{"reviews": [
  {{"label": "A", "scores": {{{example_scores}}},
    "strengths": ["..."], "weaknesses": ["..."]}}
]}}"""


def revision(
    question: str,
    criteria: list[str],
    own_label: str,
    own: Proposal,
    feedback: list[dict],
    others: dict[str, Proposal],
) -> str:
    fb = json.dumps(feedback, ensure_ascii=False, indent=1) if feedback else "(no feedback received)"
    others_short = "\n".join(f"- {l}: {p.title} — {p.summary[:400]}" for l, p in others.items())
    return f"""Question for the council:
{question}

Criteria:
{_criteria(criteria)}

Your current proposal (label {own_label}):
{_proposal_block(own_label, own)}

Anonymous feedback other members gave on your proposal:
{fb}

Other proposals (short):
{others_short}

Revise your proposal: fix real weaknesses, keep what is strong, borrow good ideas from others if they make it better.
If another proposal is clearly better, you may rework yours to build on it. Do not change things just to please critics.

JSON format:
{{"title": "...", "summary": "...", "reasoning": "...", "pros": ["..."], "risks": ["..."],
  "confidence": 0.0-1.0, "changes": "what you changed and why"}}"""


def vote(question: str, criteria: list[str], proposals: dict[str, Proposal]) -> str:
    labels = ", ".join(proposals)
    return f"""Question for the council:
{question}

Criteria:
{_criteria(criteria)}

Final proposals:

{proposals_block(proposals)}

Rank ALL of these proposals from best to worst. Include every label exactly once: {labels}.

JSON format:
{{"ranking": ["best label", "...", "worst label"], "rationale": "why the top choice wins"}}"""


def verdict(
    question: str,
    criteria: list[str],
    proposals: dict[str, Proposal],
    tally_text: str,
    critique_notes: str,
) -> str:
    labels = ", ".join(proposals)
    return f"""Question for the council:
{question}

Criteria:
{_criteria(criteria)}

Final proposals:

{proposals_block(proposals)}

Voting results (Borda score 0..1, average critique score 1..10, first-place votes):
{tally_text}

Key critique points:
{critique_notes or "(no critique round)"}

Write the council's final verdict.
- "winner": the label of the best proposal ({labels}). The vote is strong evidence; if you pick differently, justify it in "why".
- "final_answer": the complete, ready-to-use final answer for the user, based on the winner and improved with the best ideas from other proposals (markdown allowed).
- "why": why this option won.
- "dissent": notable minority opinions or strong alternatives worth knowing.
- "risks": main risks and how to mitigate them.

JSON format:
{{"winner": "A", "final_answer": "...", "why": "...", "dissent": ["..."], "risks": ["..."]}}"""


def fix_json(error: str) -> str:
    return f"""Your previous reply could not be accepted: {error}
Reply again with ONE valid JSON object in the required format, and nothing else."""
