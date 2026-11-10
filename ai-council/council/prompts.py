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
- You have no tools, code execution or file system: you cannot create, save or attach files. Put the full content (text, tables, data, code) directly into the JSON fields; never refer the user to a file, attachment or download.
- Reply with ONE valid JSON object only. No markdown fences, no text before or after it."""

DEVIL = """
Special role: you are the council's devil's advocate. Look for non-obvious alternatives, hidden assumptions and weak points that others miss. Stay constructive: the goal is a better final answer, not contrarianism."""

CHAIR = """
Special role: you chair the council. You must be neutral and fair toward all proposals, including any that may be your own."""

CHAIR_JUDGE = """
Special role: you chair the council. You do not submit a proposal of your own: your job is to frame the task, judge fairly and write the final decision. Stay neutral toward all proposals."""


def system(name: str, language: str, *, devil: bool = False, chair: bool = False, judge_only: bool = False) -> str:
    text = SYSTEM.format(name=name, language=language)
    if devil:
        text += DEVIL
    if chair:
        text += CHAIR_JUDGE if judge_only else CHAIR
    return text


def _criteria(criteria: list[str]) -> str:
    return "\n".join(f"- {c}" for c in criteria)


def _context(context: str | None) -> str:
    return f"\nAdditional context from the user:\n{context}\n" if context else ""


def _original(question: str, original: str | None) -> str:
    """Исходный запрос дословно: в нём могут быть требования к формату и структуре ответа,
    которые теряются в уточнённой формулировке."""
    if not original or original.strip() == question.strip():
        return ""
    return ("\nThe user's original request, verbatim (it may contain requirements for the structure, sections and "
            f"format of the answer — they apply to your answer):\n<<<\n{original}\n>>>\n")


FORMAT_RULE = ("If the user's request specifies a structure, sections, a table or another format for the answer, "
               "follow it exactly and cover every requested item.")

SCALE = """Scoring scale (use the whole range; most real answers are not 9-10):
- 1-2: wrong, irrelevant or unusable for this criterion;
- 3-4: serious flaws or important gaps;
- 5-6: acceptable, but with notable weaknesses;
- 7-8: good, only minor issues;
- 9-10: excellent, hard to improve (rare).
Judge substance, not length or style: a longer or more polished answer is not better by itself, and missing a requirement of the request is a weakness. A proposal that points to a file, attachment or download instead of giving the content is missing that content."""


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


def proposal(question: str, criteria: list[str], context: str | None, original: str | None = None) -> str:
    return f"""Question for the council:
{question}
{_original(question, original)}{_context(context)}
The answers will be judged by these criteria:
{_criteria(criteria)}

Propose YOUR best answer/option. Other members answer independently; you will not see them yet.
{FORMAT_RULE} The "summary" field holds the answer itself.

JSON format:
{{"title": "short name of your option",
  "summary": "the option itself, complete and concrete (can be several paragraphs)",
  "reasoning": "why this is the best choice",
  "pros": ["..."],
  "risks": ["..."],
  "confidence": 0.0-1.0}}"""


def critique(question: str, criteria: list[str], proposals: dict[str, Proposal], original: str | None = None) -> str:
    labels = ", ".join(proposals)
    example_scores = ", ".join(f'"{c}": 1-10' for c in criteria)
    return f"""Question for the council:
{question}
{_original(question, original)}
Review each of these anonymous proposals:

{proposals_block(proposals)}

Score every proposal on every criterion from 1 to 10:
{_criteria(criteria)}

{SCALE}

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
    original: str | None = None,
) -> str:
    fb = json.dumps(feedback, ensure_ascii=False, indent=1) if feedback else "(no feedback received)"
    others_full = "\n\n".join(_proposal_block(l, p) for l, p in others.items())  # целиком, без обрезки
    return f"""Question for the council:
{question}
{_original(question, original)}
Criteria:
{_criteria(criteria)}

Your current proposal (label {own_label}):
{_proposal_block(own_label, own)}

Anonymous feedback other members gave on your proposal:
{fb}

Other proposals, in full (anonymous):

{others_full}

Do this in order:
1. Compare honestly: where is each other proposal stronger than yours, and where is yours stronger? Be specific.
2. Revise: fix the real weaknesses named by critics or revealed by the comparison, keep what is strong, and take
   specific ideas from other proposals when they make yours better (say which ideas and from which label).
   List the criticism you do not accept and why: disagreement stated openly is more useful than silent agreement.
3. Keep your independence: change your core approach only if a specific argument, fact or piece of evidence shows
   it is wrong or clearly inferior — name it. That others agree with each other is not a reason by itself, and do
   not change things just to please critics. If another proposal is clearly better overall, you may rebuild yours
   on it — then say so.
{FORMAT_RULE}

JSON format:
{{"title": "...", "summary": "the full revised answer", "reasoning": "...", "pros": ["..."], "risks": ["..."],
  "confidence": 0.0-1.0,
  "comparison": "where other proposals are stronger than yours and where yours is stronger",
  "borrowed": ["idea taken from another proposal (label)"],
  "not_accepted": ["critique you did NOT accept, and why (keep real disagreements visible)"],
  "position": "kept | improved | switched",
  "changes": "what you changed and why; if position changed, the specific argument that convinced you"}}
"position": kept — same approach, only small fixes; improved — same core approach, substantially strengthened;
switched — core approach replaced (for example, rebuilt on another proposal)."""


def vote(question: str, criteria: list[str], proposals: dict[str, Proposal]) -> str:
    labels = ", ".join(proposals)
    return f"""Question for the council:
{question}

Criteria:
{_criteria(criteria)}

Final proposals:

{proposals_block(proposals)}

Rank ALL of these proposals from best to worst. Include every label exactly once: {labels}.
Judge substance against the criteria and the request, not length or style.

JSON format:
{{"ranking": ["best label", "...", "worst label"], "rationale": "why the top choice wins"}}"""


def verdict(
    question: str,
    criteria: list[str],
    proposals: dict[str, Proposal],
    tally_text: str,
    critique_notes: str,
    original: str | None = None,
    agreement: str = "",
    facts: str = "",
    ballot_notes: str = "",
    leader: str = "",
    follow_vote: bool = False,
) -> str:
    labels = ", ".join(proposals)
    if follow_vote and leader:
        winner_rule = (
            f'- "winner": the council\'s decision is the voting leader, proposal {leader}. Keep it as the winner.\n'
            f'  You may name a different winner ({labels}) ONLY for one of these reasons, and then fill "deviation":\n'
            f'    fact: the fact-check refuted claims that are central to {leader};\n'
            f'    risk: {leader} carries a serious risk named in the critique that the others avoid;\n'
            f'    process: {leader} does not answer the question or ignores explicit requirements of the request.\n'
            '  "deviation" format: "<fact|risk|process>: specific explanation with references". Taste, style or\n'
            '  "slightly better" is not a reason. If you keep the leader, leave "deviation" empty.')
    else:
        winner_rule = (f'- "winner": the label of the best proposal ({labels}). The vote is strong evidence; if you pick\n'
                       '  differently, justify it in "why" and fill "deviation" with the reason.')
    agree = f"\nCouncil agreement on the ranking: {agreement}\n" if agreement else ""
    if ballot_notes:
        agree += f"\nBallot check: {ballot_notes}\n"
    if facts:
        agree += ("\nIndependent web fact-check of the leading proposals:\n" + facts + "\n"
                  "Do not repeat refuted claims in final_answer (correct them using the fact-check); present unclear "
                  "ones as uncertain. A proposal resting on refuted facts is a weaker candidate.\n")
    return f"""Question for the council:
{question}
{_original(question, original)}
Criteria:
{_criteria(criteria)}

Final proposals:

{proposals_block(proposals)}

Voting results, in final order (schulze wins = how many rivals the proposal beats by the Schulze method, which decides the order; borda = Borda score 0..1, 1 is the maximum, first tie-break; pairwise = wins minus losses in head-to-head comparisons; critique = average critique score 1..10 given to the version BEFORE revision, for information only; first-place votes):
{tally_text}
{agree}
Key critique points:
{critique_notes or "(no critique round)"}

Write the council's final verdict.
{winner_rule}
- "final_answer": the complete, ready-to-use final answer for the user, based on the winner and improved with the best ideas from other proposals (markdown allowed). {FORMAT_RULE} Do not drop requested sections even if the proposals skipped them — write them yourself.
- If you mention voting numbers anywhere, copy them exactly as given above; do not estimate or round them differently.
- If agreement is low, say in "why" that the council was divided and what the main split is.
- "why": why this option won.
- "dissent": notable minority opinions or strong alternatives worth knowing.
- "risks": main risks and how to mitigate them.

JSON format:
{{"winner": "A", "final_answer": "...", "why": "...", "dissent": ["..."], "risks": ["..."], "deviation": ""}}"""


FACTCHECK_SYSTEM = """You are an independent fact-checker for an expert council of AI models. Use web search.
Check factual claims only (numbers, dates, names, laws, prices, technical facts, studies) — not opinions or recommendations.
Be strict: "confirmed" only if reliable sources support the claim; "refuted" if reliable sources contradict it;
otherwise "unclear". Write human-readable text in {language}. Reply with ONE valid JSON object only."""


def factcheck(question: str, label: str, p: Proposal, max_claims: int) -> str:
    return f"""Question the council is answering:
{question}

Proposal {label} (one of the leading options after voting):
{_proposal_block(label, p)}

Pick up to {max_claims} key factual claims in this proposal that matter most for the decision and are checkable
in public sources. Verify each with web search. Claims based only on the user's private documents cannot be
checked publicly — skip them or mark them "unclear" with a note.

JSON format:
{{"claims": [{{"claim": "the claim, short", "status": "confirmed|refuted|unclear",
              "note": "what the sources say, one or two sentences", "sources": ["https://..."]}}],
  "summary": "one sentence: how reliable the factual basis of this proposal is"}}"""


def factcheck_block(results: dict[str, dict]) -> str:
    """Итоги проверки фактов для председателя."""
    if not results:
        return ""
    lines = []
    for label, r in results.items():
        lines.append(f"Proposal {label}: {r.get('summary', '')}")
        for c in r.get("claims", []):
            lines.append(f"  - [{c.get('status')}] {c.get('claim')} — {c.get('note', '')}")
    return "\n".join(lines)


def history_block(history: list[dict]) -> str:
    """Контекст уточняющего вопроса: что совет уже обсуждал и решил (от старых к новым)."""
    if not history:
        return ""
    parts = ["This is a FOLLOW-UP question. Earlier the council already discussed this topic "
             "(oldest first). Build on these conclusions; revise them if the new question requires it:"]
    for i, h in enumerate(history, 1):
        parts.append(f"\n--- Earlier session {i} ---\nQuestion: {h.get('question', '')}\n"
                     f"Council decision: {h.get('answer', '')}\n--- End of session {i} ---")
    return "\n".join(parts)


def describe_images(question: str, names: list[str]) -> str:
    listed = "\n".join(f"- {n}" for n in names)
    return f"""The user attached images to a question for the council. Some council members cannot see images,
so they will rely ONLY on your description. Describe every image exhaustively and objectively:
transcribe all visible text verbatim, all numbers, table contents (as rows), chart axes and values,
diagram elements and their connections, important visual details. Do not answer the question itself.

User question (for context): {question}

Images, in the order they are attached:
{listed}

JSON format:
{{"images": [{{"name": "exact image name", "description": "detailed description"}}]}}"""


def continue_answer() -> str:
    return ("Your reply was cut off by the length limit. Continue EXACTLY from the last character you wrote: "
            "do not repeat anything, do not restart the JSON and do not add any comments — just the rest of the text, "
            "so that the previous part plus this continuation form one valid JSON object.")


def shorter_answer() -> str:
    return ("Your previous reply was too long and was cut off several times. Write the answer again as ONE valid "
            "JSON object, more compact: keep every required section and the key content, but shorten wording, "
            "examples and repetition.")


def fix_json(error: str) -> str:
    return f"""Your previous reply could not be accepted: {error}
Reply again with ONE valid JSON object in the required format, and nothing else."""
