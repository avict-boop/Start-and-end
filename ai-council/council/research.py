"""Режим «Исследование»: схемы ответов, тексты запросов и реестр доказательств.

Устроен по ТЗ, которое совет написал сам (docs/research-tz). Главное отличие от «Дебатов»: итог —
не выбор победителя, а синтез по подвопросам с честным делением на «известно / спорно / неизвестно».
Статус факта определяют найденные источники, а не число согласных моделей.

Ход заседания:
 0. постановка: рамки (период, география, единицы), 3–5 подвопросов, линзы для авторов;
 1. слепые предложения без поиска: у каждого факта пометка [DOC] / [CALC] / [MEM], список утверждений;
 2. критика: оценки, спорные утверждения, пропущенные подвопросы;
 3. арбитраж доказательств: поисковая модель проверяет важные утверждения всех вариантов двумя
    вызовами с противоположными заданиями; программа собирает реестр источников E1..En;
 4. доработка по реестру и выжимкам чужих вариантов (а не по их полным текстам);
 5. голосование: общий порядок и лучший вариант по каждому подвопросу (это выбор каркаса, не истины);
 6. итог: синтез по подвопросам; «известно» — только подтверждённое реестром или документами пользователя.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .prompts import FORMAT_RULE, KEEP_RULE, REQ_CHECK, REQ_VOTE, SCALE, _context, _criteria, _ideas_block, head
from .schemas import Ballot, Framing, Proposal, Verdict, _pick, to_list, to_text

BASIS = ("user_doc", "calc", "memory", "search_reported")
VOLATILITY = ("stable", "changes_fast", "unknown")
CLAIM_STATUS = ("confirmed", "refuted", "contested_in_sources", "unclear", "user_doc", "search_unavailable")
STATUS_RANK = {"confirmed": 0, "refuted": 1, "contested_in_sources": 2, "user_doc": 3, "unclear": 4,
               "search_unavailable": 5, "not_checked": 6}
DEFAULT_LENSES = [
    "first-hand data and market facts: numbers, sizes, prices, who does what",
    "technology and constraints: how it actually works, limits, dependencies",
    "economics and calculations: costs, returns, unit economics, explicit formulas",
    "methodology skeptic and pre-mortem: how this could be wrong, weak evidence, failure scenarios",
]
DEFAULT_CRITERIA = ["Полнота покрытия", "Качество доказательств", "Калибровка неопределённости", "Практическая ценность"]


def _norm(v: Any, allowed: tuple[str, ...], default: str) -> str:
    t = str(v or "").strip().lower().replace("-", "_").replace(" ", "_")
    for a in allowed:
        if t == a or t.startswith(a):
            return a
    aliases = {"memory_only": "memory", "mem": "memory", "doc": "user_doc", "document": "user_doc",
               "calculation": "calc", "estimate": "calc", "search": "search_reported", "web": "search_reported",
               "volatile": "changes_fast", "fast": "changes_fast", "contested": "contested_in_sources",
               "disputed": "contested_in_sources", "verified": "confirmed", "true": "confirmed", "false": "refuted",
               "unknown": "unclear", "unavailable": "search_unavailable"}
    a = aliases.get(t)
    return a if a in allowed else default


# ---------------------------------------------------------------- схемы

class ResearchFraming(Framing):
    """Этап 0: постановка исследования."""

    as_of: str = ""
    scope: str = ""
    subquestions: list[str] = Field(default_factory=list)
    lenses: list[str] = Field(default_factory=list)

    @field_validator("as_of", "scope", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("subquestions", "lenses", mode="before")
    @classmethod
    def _lst(cls, v: Any) -> list[str]:
        out = []
        for x in v if isinstance(v, list) else to_list(v):
            t = to_text(_pick(x, "question", "text", "title", "lens") if isinstance(x, dict) else x).strip()
            t = re.sub(r"^Q\d+[.:)\s-]+", "", t).strip()
            if t:
                out.append(t)
        return out


class RClaim(BaseModel):
    """Проверяемое утверждение варианта. id присваивает программа (A-C1, B-C2…), не модель."""

    id: str = ""
    text: str
    basis: str = "memory"
    scope: str = ""
    volatility: str = "unknown"
    source_leads: list[str] = Field(default_factory=list)
    why_it_matters: str = ""

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"text": data}
        if isinstance(data, dict) and not data.get("text"):
            data = {**data, "text": _pick(data, "claim", "statement", "fact") or ""}
        return data

    @field_validator("text", "scope", "why_it_matters", "id", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("basis", mode="before")
    @classmethod
    def _basis(cls, v: Any) -> str:
        return _norm(v, BASIS, "memory")

    @field_validator("volatility", mode="before")
    @classmethod
    def _vol(cls, v: Any) -> str:
        return _norm(v, VOLATILITY, "unknown")

    @field_validator("source_leads", mode="before")
    @classmethod
    def _leads(cls, v: Any) -> list[str]:
        return to_list(v)


class CoverageItem(BaseModel):
    subquestion_id: str
    status: str = "covered"
    note: str = ""

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("subquestion_id"):
            data = {**data, "subquestion_id": _pick(data, "id", "q", "subquestion") or ""}
        if isinstance(data, dict):
            data = {**data, "note": data.get("note") or data.get("where") or ""}
        return data

    @field_validator("subquestion_id", "status", "note", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        t = to_text(v).strip()
        return t.upper() if re.fullmatch(r"q\d+", t, re.I) else t


class ResearchProposal(Proposal):
    """Этап 1: вариант исследования с утверждениями, покрытием подвопросов и пробелами."""

    claims: list[RClaim] = Field(default_factory=list)
    coverage: list[CoverageItem] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    what_would_change: list[str] = Field(default_factory=list)

    @field_validator("unknowns", "what_would_change", mode="before")
    @classmethod
    def _lst(cls, v: Any) -> list[str]:
        out = []
        for x in v if isinstance(v, list) else to_list(v):
            if isinstance(x, dict):
                q, why = to_text(_pick(x, "question", "gap", "text") or ""), to_text(x.get("why_it_matters") or "")
                out.append(f"{q} — {why}" if why else q)
            elif to_text(x).strip():
                out.append(to_text(x))
        return out

    @field_validator("claims", mode="before")
    @classmethod
    def _claims(cls, v: Any) -> Any:
        return v if isinstance(v, list) else ([v] if v else [])


class ResearchRevision(ResearchProposal):
    """Этап 4: доработка по реестру доказательств."""

    changes: str = ""
    comparison: str = ""
    borrowed: list[str] = Field(default_factory=list)
    not_accepted: list[str] = Field(default_factory=list)
    position: str = "improved"
    new_after_ledger: list[str] = Field(default_factory=list)  # утверждения, добавленные после реестра

    @field_validator("changes", "comparison", mode="before")
    @classmethod
    def _rtxt(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("borrowed", "not_accepted", "new_after_ledger", mode="before")
    @classmethod
    def _rlst(cls, v: Any) -> list[str]:
        return to_list(v)


class ESource(BaseModel):
    url: str
    title: str = ""
    publisher: str = ""
    date: str = ""
    source_type: str = "unknown"
    material_seen: str = "snippet"
    relation: str = "supports"

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"url": data}
        return data

    @field_validator("url", "title", "publisher", "date", "source_type", "material_seen", "relation", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return "" if v is None else to_text(v).strip()


class CheckedClaim(BaseModel):
    claim_id: str
    status: str = "unclear"
    note: str = ""
    sources: list[ESource] = Field(default_factory=list)

    @field_validator("claim_id", "note", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return to_text(v).strip()

    @field_validator("status", mode="before")
    @classmethod
    def _st(cls, v: Any) -> str:
        return _norm(v, CLAIM_STATUS, "unclear")


class EvidenceResult(BaseModel):
    """Этап 3: ответ поисковой модели. search_citations заполняет программа — ссылки, которые реально вернул поиск."""

    checked_claims: list[CheckedClaim] = Field(default_factory=list)
    gaps: list[dict[str, Any]] = Field(default_factory=list)
    search_citations: list[str] = Field(default_factory=list)

    @field_validator("gaps", mode="before")
    @classmethod
    def _gaps(cls, v: Any) -> list[dict[str, Any]]:
        return [x if isinstance(x, dict) else {"question": to_text(x)} for x in (v or []) if x]


class ResearchBallot(Ballot):
    best_by_subquestion: dict[str, str] = Field(default_factory=dict)

    @field_validator("best_by_subquestion", mode="before")
    @classmethod
    def _best(cls, v: Any) -> dict[str, str]:
        if isinstance(v, list):
            v = {to_text(_pick(x, "subquestion_id", "q", "id") or ""): to_text(_pick(x, "label", "best") or "")
                 for x in v if isinstance(x, dict)}
        if not isinstance(v, dict):
            return {}
        return {str(k).strip().upper(): to_text(x).strip().upper()[:1] for k, x in v.items() if k and x}


class KnownItem(BaseModel):
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    scope: str = ""
    volatility: str = "unknown"

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"statement": data, "evidence_ids": re.findall(r"\bE\d+\b|\[DOC", data)}
        return data

    @field_validator("statement", "scope", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> list[str]:
        return [("DOC" if "DOC" in str(x).upper() else str(x).strip().strip("[]").upper()) for x in to_list(v)]


class ContestedItem(BaseModel):
    issue: str
    positions: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    why_they_differ: str = ""
    what_would_resolve: str = ""

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        return {"issue": data} if isinstance(data, str) else data

    @field_validator("issue", "why_they_differ", "what_would_resolve", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("positions", mode="before")
    @classmethod
    def _pos(cls, v: Any) -> list[str]:
        return to_list(v)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> list[str]:
        return [str(x).strip().strip("[]").upper() for x in to_list(v)]


class UnknownItem(BaseModel):
    gap: str
    reason: str = ""
    how_to_check: str = ""

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        return {"gap": data} if isinstance(data, str) else data

    @field_validator("gap", "reason", "how_to_check", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return to_text(v)


class ResearchVerdict(Verdict):
    """Этап 6: синтез исследования."""

    section_backbone: dict[str, str] = Field(default_factory=dict)
    known: list[KnownItem] = Field(default_factory=list)
    contested: list[ContestedItem] = Field(default_factory=list)
    unknown: list[UnknownItem] = Field(default_factory=list)
    coverage_audit: list[CoverageItem] = Field(default_factory=list)
    what_would_change: list[str] = Field(default_factory=list)
    next_questions: list[str] = Field(default_factory=list)

    @field_validator("what_would_change", "next_questions", mode="before")
    @classmethod
    def _lst2(cls, v: Any) -> list[str]:
        return to_list(v)

    @field_validator("section_backbone", mode="before")
    @classmethod
    def _bb(cls, v: Any) -> dict[str, str]:
        return {str(k).strip().upper(): to_text(x).strip().upper()[:1] for k, x in (v or {}).items()} \
            if isinstance(v, dict) else {}


# ---------------------------------------------------------------- запросы

def subq_block(subquestions: list[str]) -> str:
    return "\n".join(f"Q{i}. {q}" for i, q in enumerate(subquestions, 1))


def research_head(question: str, original: str | None, req: list[str] | None, scope: str, as_of: str,
                  subquestions: list[str]) -> str:
    """Общее начало всех запросов исследования: одинаковое у всех — его кэширует провайдер."""
    return (head(question, original, req)
            + f"\nResearch scope{f' (as of {as_of})' if as_of else ''}: {scope or 'as stated in the question'}\n"
            + f"Subquestions the research must cover:\n{subq_block(subquestions)}\n")


def framing(question: str, context: str | None, members: int) -> str:
    lenses = max(2, members)
    return f"""Before the council starts, prepare a RESEARCH task (not a choice between options).

User question:
{question}
{_context(context)}
1. Restate the research question precisely and neutrally (keep the user's intent; do not answer it).
2. Define 3-5 short evaluation criteria (1-4 words each). For research the defaults are: coverage completeness,
   quality of evidence, calibration of uncertainty, practical value — adapt them to the question if needed.
3. List the HARD REQUIREMENTS the user stated explicitly (structure, sections, length, language, constraints),
   faithfully, one per item, without inventing new ones (at most 10; an empty list if none).
4. "as_of": the date or period the facts must be current for (e.g. "2026-11" or "2023-2025").
   "scope": geography, period, units, definitions and what is out of scope, in one or two sentences.
5. "subquestions": 3-5 subquestions that together cover the research question (a checklist, not separate tasks).
6. "lenses": {lenses} different angles for independent authors, adapted to this topic, e.g. first-hand data and
   market facts; technology and constraints; economics and calculations; methodology skeptic / pre-mortem.

JSON format:
{{"refined_question": "...", "criteria": ["..."], "requirements": ["..."], "as_of": "...", "scope": "...",
  "subquestions": ["...", "..."], "lenses": ["...", "..."]}}"""


def _claims_rule(max_claims: int) -> str:
    return f"""Epistemic discipline:
- Tag facts from attached files as [DOC: file, place]. Tag your own estimates or calculations as [CALC] and show
  inputs and formula. Tag facts, numbers or dates recalled from training as [MEM].
- A URL recalled from memory is only a lead for later search ("source_leads"), never a verification. NEVER invent
  URLs, quotations, dates or numbers: an honest [MEM] is better than a confident guess.
- State period, geography and units for every number. Separate facts, estimates and recommendations.
- If a subquestion cannot be answered, say so in "unknowns" instead of filling the gap. For every major conclusion
  state what evidence would change it.
List up to {max_claims} decision-critical, checkable atomic claims in "claims"; mark fast-changing ones (prices,
market shares, regulation, versions) as "changes_fast"."""


CLAIM_JSON = ('{"text": "one atomic checkable statement", "basis": "user_doc|calc|memory|search_reported", '
              '"scope": "period, geography, units", "volatility": "stable|changes_fast|unknown", '
              '"source_leads": ["..."], "why_it_matters": "..."}')


def proposal(rhead: str, criteria: list[str], context: str | None, lens: str, max_words: int, max_claims: int,
             web: bool = False) -> str:
    access = ("Web search IS enabled for you in this step: use it for fresh numbers and primary sources, and tag "
              "what you found as [SRC: url]; put such claims in \"claims\" with basis \"search_reported\"."
              if web else "You have no web access in this step.")
    lens_txt = f"\nDevote at least 40% of your analytical depth to your assigned lens (a starting emphasis, not a limit):\n>>> {lens} <<<\n" if lens else ""
    return f"""{rhead}{_context(context)}
Evaluation criteria:
{_criteria(criteria)}

Work independently; you have not seen other answers. {access}
Cover ALL subquestions in about {max_words} words at most: dense substance, no generic introductions.{lens_txt}
{_claims_rule(max_claims)}
{FORMAT_RULE} The "summary" field holds the research answer itself.

JSON format:
{{"title": "short descriptive angle",
 "summary": "structured Markdown covering Q1..Qk with inline tags [DOC: ...], [CALC], [MEM]",
 "reasoning": "core logic and assumptions",
 "claims": [{CLAIM_JSON}],
 "pros": ["..."], "risks": ["..."],
 "coverage": [{{"subquestion_id": "Q1", "status": "covered|partial|unknown", "note": "..."}}],
 "unknowns": ["what is not known and why it matters"],
 "what_would_change": ["evidence that would change the main conclusions"],
 "confidence": 0.0-1.0}}"""


def proposal_block(label: str, p: Proposal, claim_ids: bool = True) -> str:
    lines = [f"### Proposal {label}: {p.title}", p.summary]
    if p.reasoning:
        lines.append(f"Reasoning: {p.reasoning}")
    claims = getattr(p, "claims", None) or []
    if claims:
        lines.append("Claims:")
        lines += [f"- {c.id if claim_ids and c.id else '-'} [{c.basis}, {c.volatility}] {c.text}"
                  + (f" ({c.scope})" if c.scope else "") for c in claims]
    unknowns = getattr(p, "unknowns", None) or []
    if unknowns:
        lines.append("Unknowns: " + "; ".join(unknowns))
    if p.risks:
        lines.append("Risks: " + "; ".join(p.risks))
    return "\n".join(lines)


def critique(rhead: str, criteria: list[str], proposals: dict[str, Proposal], req: list[str] | None) -> str:
    labels = ", ".join(proposals)
    example_scores = ", ".join(f'"{c}": 1-10' for c in criteria)
    blocks = "\n\n".join(proposal_block(l, p) for l, p in proposals.items())
    return f"""{rhead}
Review each of these anonymous research proposals:

{blocks}

Score every proposal on every criterion from 1 to 10:
{_criteria(criteria)}

{SCALE}
For research, a confident [MEM] number presented as fact, an invented-looking source or an uncovered subquestion is a
weakness; an honest "unknown" is not.
{REQ_CHECK if req else ""}
You must review exactly these labels: {labels}. Use the criteria names exactly as written.
{KEEP_RULE}
"disputed_claims": 0-4 claims per proposal that most need checking: contradictions between proposals, central [MEM]
numbers, doubtful sources. Use the claim ids shown above (e.g. "B-C2") and give the reason.
"missing_subquestions": subquestions or important aspects this proposal did not cover (ids like "Q2" or text).

JSON format:
{{"reviews": [
  {{"label": "A", "scores": {{{example_scores}}},
    "strengths": ["..."], "weaknesses": ["..."], "keep": ["..."],
    "disputed_claims": [{{"claim_id": "A-C1", "reason": "..."}}], "missing_subquestions": ["Q3"]}}
]}}"""


ARBITRATION_SYSTEM = """You are an independent evidence investigator for an expert research council of AI models.
Web search is enabled for this request by the calling program; use it. If you cannot actually search, mark every
item "search_unavailable" — never pretend to have searched. Write human-readable text in {language}.
Reply with ONE valid JSON object only."""

MISSIONS = [
    "PRIMARY: find primary sources — official documents, statistics, filings, price lists, original studies — that "
    "confirm or correct each claim; prefer the most recent authoritative data.",
    "CHALLENGE: look for refutations, counterexamples, alternative estimates and criticism of the methodology behind "
    "each claim; check whether definitions, dates or geography differ.",
]


def arbitration(question: str, as_of: str, scope: str, mission: str, claims_batch: str, max_evidence: int) -> str:
    return f"""Question (as of {as_of or 'today'}): {question}
Required scope: {scope or 'as stated in the question'}
Assigned mission: {mission}

Claims selected from anonymous proposals (contradictions, key numbers, fast-changing or memory-only claims). IDs are
fixed — use them exactly:
{claims_batch}

Investigate EVERY claim. Look for evidence both for and against. Prefer primary sources for numbers, dates, laws and
technical facts; check dates, geography, units and definitions. Where estimates differ, find the reason. Two articles
restating one underlying report count as ONE source (mark the later one "secondary"). Record only sources your search
actually returned; never invent a URL, date or quotation. If you saw only a snippet or a search summary, say so in
"material_seen". A related page is not a confirmation. Absence of a found source is not a refutation.
Statuses: confirmed (a source directly supports the claim in its stated scope); refuted (a reliable source directly
contradicts it); contested_in_sources (reputable sources conflict; report both); unclear (no reliable evidence found);
user_doc (checkable only from the user's own documents); search_unavailable.
Return at most {max_evidence} sources in total.

JSON format:
{{"checked_claims": [{{"claim_id": "A-C1", "status": "confirmed|refuted|contested_in_sources|unclear|user_doc|search_unavailable",
   "note": "what the sources state, with exact figures, scope and caveats",
   "sources": [{{"url": "https://...", "title": "...", "publisher": "...", "date": "YYYY-MM-DD or empty",
                "source_type": "primary|secondary|unknown", "material_seen": "page|snippet|search_answer",
                "relation": "supports|contradicts|context"}}]}}],
 "gaps": [{{"question": "...", "searched_for": "...", "why_unresolved": "..."}}]}}"""


def digest(label: str, p: Proposal) -> str:
    """Выжимка чужого варианта для доработки: тезисы и утверждения, без полного текста."""
    lines = [f"### Proposal {label}: {p.title}"]
    claims = getattr(p, "claims", None) or []
    if claims:
        lines += [f"- {c.id} [{c.basis}] {c.text}" for c in claims]
    if p.pros:
        lines.append("Strengths claimed by the author: " + "; ".join(p.pros[:5]))
    unknowns = getattr(p, "unknowns", None) or []
    if unknowns:
        lines.append("Unknowns: " + "; ".join(unknowns[:5]))
    return "\n".join(lines)


def revision(rhead: str, criteria: list[str], own_label: str, own: Proposal, feedback: list[dict],
             others: dict[str, Proposal], ledger: str, lens: str, max_words: int, max_claims: int,
             full_others: bool) -> str:
    fb = json.dumps(feedback, ensure_ascii=False, indent=1) if feedback else "(no feedback received)"
    if full_others:
        others_txt = "Other proposals, in full (anonymous):\n\n" + "\n\n".join(proposal_block(l, p) for l, p in others.items())
    else:
        others_txt = ("Other proposals, as digests (claims and strengths only — keep your own line of reasoning):\n\n"
                      + "\n\n".join(digest(l, p) for l, p in others.items()))
    lens_txt = f"\nYour lens remains: {lens}\n" if lens else ""
    return f"""{rhead}
Criteria:
{_criteria(criteria)}

Your current research proposal (label {own_label}):
{proposal_block(own_label, own)}

Anonymous feedback other members gave on your proposal:
{fb}

Evidence ledger built by an independent web search after the critique (statuses come ONLY from sources):
{ledger or "(no evidence ledger: search was not run)"}

{others_txt}
{lens_txt}
Do this in order:
1. Fix every claim of yours the ledger marks refuted; use confirmed evidence and cite it as [E1], [E2]…
2. Address the real weaknesses and missing subquestions named by critics; deepen your lens.
3. Change your core approach only for a specific evidence id or argument — name it. Agreement of others is not a
   reason by itself. List criticism you do not accept in "not_accepted".
4. Any claim you ADD or CHANGE that is not covered by the ledger goes to "new_after_ledger" (by its text).
About {max_words} words at most. {_claims_rule(max_claims)}

JSON format:
{{"title": "...", "summary": "the full revised research answer with [E#]/[DOC]/[CALC]/[MEM] tags",
 "reasoning": "...", "claims": [{CLAIM_JSON}], "pros": ["..."], "risks": ["..."],
 "coverage": [{{"subquestion_id": "Q1", "status": "covered|partial|unknown", "note": "..."}}],
 "unknowns": ["..."], "what_would_change": ["..."], "confidence": 0.0-1.0,
 "comparison": "where other proposals are stronger and where yours is", "borrowed": ["idea (label)"],
 "not_accepted": ["critique not accepted and why"], "position": "kept | improved | switched",
 "changes": "what you changed and why", "new_after_ledger": ["claim text"]}}"""


def vote(rhead: str, criteria: list[str], proposals: dict[str, Proposal], subq: list[str], req: list[str] | None,
         section_vote: bool) -> str:
    labels = ", ".join(proposals)
    blocks = "\n\n".join(proposal_block(l, p) for l, p in proposals.items())
    qids = ", ".join(f"Q{i}" for i in range(1, len(subq) + 1))
    sec = (f'\nAlso pick, for each subquestion ({qids}), which proposal covers it best with evidence — this chooses the '
           'backbone of that section of the final report, not the truth of facts: "best_by_subquestion".') if section_vote else ""
    sec_json = ', "best_by_subquestion": {"Q1": "B", "Q2": "A"}' if section_vote else ""
    return f"""{rhead}
Criteria:
{_criteria(criteria)}

Final research proposals (anonymous):

{blocks}

Rank ALL of these proposals from best to worst as research: coverage, quality of evidence, honest uncertainty,
practical value — not length or style. Include every label exactly once: {labels}.{REQ_VOTE if req else ""}{sec}

JSON format:
{{"ranking": ["best label", "...", "worst label"], "rationale": "why the top choice wins"{sec_json}}}"""


def verdict(rhead: str, criteria: list[str], proposals_txt: str, ledger: str, section_tally: str, overall_tally: str,
            critique_notes: str, ideas: str, leader: str, labels: list[str], agreement: str, ballot_notes: str,
            req: list[str] | None, co_leaders: list[str] | None, subq: list[str]) -> str:
    qids = ", ".join(f"Q{i}" for i in range(1, len(subq) + 1))
    tie = (f"\nTie for first place overall: {', '.join(co_leaders)} — you may name any of them as \"winner\".\n"
           if co_leaders and len(co_leaders) > 1 else "")
    return f"""{rhead}
Criteria:
{_criteria(criteria)}

Final proposals (after revision; "Changes after critique" shows what each author fixed):
{proposals_txt}

Evidence ledger (global ids E1..En with URLs; a claim's status comes ONLY from evidence, never from how many members agree):
{ledger or "(no ledger: evidence search was not run — then nothing can be 'known' except user-document data)"}

Voting — best backbone per subquestion (preference for a backbone text only, NOT evidence of truth):
{section_tally or "(no section vote)"}
Overall ranking (diagnostic): {overall_tally or "(no vote)"}
{f"Council agreement on the ranking: {agreement}" if agreement else ""}
{f"Ballot check: {ballot_notes}" if ballot_notes else ""}{tie}
Critique gaps (they refer to versions BEFORE revision):
{critique_notes or "(none)"}
{_ideas_block(ideas)}
Write a RESEARCH SYNTHESIS, not a defence of one winner.
- Build "final_answer" over {qids} using each subquestion's backbone leader; integrate verified findings and kept ideas
  of all proposals. "winner": the overall backbone label ({', '.join(labels)}); the overall voting leader is {leader or "—"}.
  If you choose another winner or override a section leader, fill "deviation" as "<fact|risk|process>: reason".
- "known": ONLY claims with status confirmed in the ledger (cite the E-ids) or direct user-document data (evidence id
  "DOC"; it is user data, not independent verification). Model agreement and [MEM] claims NEVER qualify as known.
  Claims added after the ledger (new_after_ledger) cannot be known.
- "contested": conflicting sources or genuine methodological splits; give both positions, E-ids, why they differ
  (definitions, dates, methods, sources, values) and what would resolve it.
- "unknown": memory-only claims, unresolved gaps, data not found; a subquestion without an answer goes here with the reason.
- "coverage_audit": one entry per subquestion ({qids}) — covered, partial or unknown, and where in the answer.
- Never repeat refuted claims as true. Do not introduce facts, numbers or sources absent from the proposals, documents
  and ledger; if essential, mark "(добавлено председателем)".
- For recommendations state assumptions, risks and what would change them. End with the highest-value next research
  steps in "next_questions". Keep the report concise (about 6-12 pages); do not reproduce the transcript.
- If hard requirements R1, R2, … are listed, "final_answer" must satisfy every one of them. {FORMAT_RULE}
- Decide on EVERY idea worth keeping listed above, in "ideas".

JSON format:
{{"winner": "{labels[0] if labels else 'A'}", "section_backbone": {{"Q1": "B"}},
 "final_answer": "concise structured Markdown with [E1]/[DOC]/[CALC]/[MEM] tags and a Sources list with URLs",
 "why": "how the synthesis was built", "known": [{{"statement": "...", "evidence_ids": ["E1"], "scope": "...", "volatility": "stable|changes_fast|unknown"}}],
 "contested": [{{"issue": "...", "positions": ["..."], "evidence_ids": ["E2"], "why_they_differ": "...", "what_would_resolve": "..."}}],
 "unknown": [{{"gap": "...", "reason": "unverified_memory|no_public_data|not_checked", "how_to_check": "..."}}],
 "coverage_audit": [{{"subquestion_id": "Q1", "status": "covered|partial|unknown", "note": "where"}}],
 "what_would_change": ["..."], "next_questions": ["..."], "dissent": ["..."], "risks": ["..."], "deviation": "",
 "ideas": [{{"id": "I1", "decision": "included|rejected", "reason": "..."}}]}}"""


# ---------------------------------------------------------------- утверждения и реестр

def number_claims(label: str, p: ResearchProposal, start: int = 1) -> ResearchProposal:
    """Сквозные id утверждений присваивает программа после анонимизации: A-C1, A-C2…"""
    claims = [c.model_copy(update={"id": f"{label}-C{i}"}) for i, c in enumerate(p.claims, start)]
    return p.model_copy(update={"claims": claims})


def select_claims(proposals: dict[str, ResearchProposal], critiques: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Что проверять поиском: спорное по мнению критиков, быстро меняющееся, числа «из памяти»; минимум по одному
    утверждению от каждого автора, затем самые важные."""
    disputed: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}
    for cs in critiques.values():
        for r in cs.reviews:
            for d in r.disputed_claims or []:
                cid = (d.get("claim_id") or "").strip().upper()
                if cid:
                    disputed[cid] = disputed.get(cid, 0) + 1
                    if d.get("reason"):
                        reasons.setdefault(cid, []).append(d["reason"])
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for label, p in proposals.items():
        for i, c in enumerate(p.claims):
            if c.basis == "user_doc":
                continue  # данные пользователя поиском не проверяются
            s = 3.0 * disputed.get(c.id.upper(), 0) + (2.0 if c.volatility == "changes_fast" else 0.0)
            s += 2.0 if c.basis == "memory" and re.search(r"\d", c.text) else 0.0
            s += 1.0 if c.basis == "search_reported" else 0.0
            s += 0.5 / (i + 1)  # автор ставит важное выше
            scored.append((s, label, {"id": c.id, "label": label, "text": c.text, "scope": c.scope,
                                      "basis": c.basis, "volatility": c.volatility,
                                      "disputed": disputed.get(c.id.upper(), 0),
                                      "reasons": reasons.get(c.id.upper(), [])[:2]}))
    scored.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, label, c in scored:  # по одному от каждого автора
        if label not in {x["label"] for x in out}:
            out.append(c)
            seen.add(c["id"])
    for _, _, c in scored:
        if len(out) >= limit:
            break
        if c["id"] not in seen:
            out.append(c)
            seen.add(c["id"])
    return out[:limit]


def claims_batch(selected: list[dict[str, Any]]) -> str:
    lines = []
    for c in selected:
        why = f" | critics: {'; '.join(c['reasons'])}" if c.get("reasons") else ""
        lines.append(f"{c['id']}: {c['text']}" + (f" (scope: {c['scope']})" if c.get("scope") else "")
                     + f" [basis {c['basis']}, {c['volatility']}]{why}")
    return "\n".join(lines)


def _url_key(u: str) -> str:
    u = u.strip().lower().split("#")[0].rstrip("/")
    return re.sub(r"^https?://(www\.)?", "", u)


def merge_status(statuses: list[str]) -> str:
    s = set(statuses)
    if not s:
        return "not_checked"
    if ("confirmed" in s and "refuted" in s) or "contested_in_sources" in s:
        return "contested_in_sources"
    for st in ("refuted", "confirmed", "user_doc", "unclear"):
        if st in s:
            return st
    return "search_unavailable"


def build_ledger(selected: list[dict[str, Any]], results: list[EvidenceResult], model: str) -> dict[str, Any]:
    """Склеивает ответы поисковых вызовов в реестр: статус по каждому утверждению и источники E1..En.

    Защита от выдуманных ссылок: если поиск вернул список найденных ссылок (citations), источник, которого в нём
    нет, помечается «не из результатов поиска» и статус «подтверждено» не даёт."""
    by_id = {c["id"].upper(): c for c in selected}
    statuses: dict[str, list[str]] = {k: [] for k in by_id}
    notes: dict[str, list[str]] = {k: [] for k in by_id}
    evid: dict[str, dict[str, Any]] = {}
    claim_ev: dict[str, list[str]] = {k: [] for k in by_id}
    gaps: list[dict[str, Any]] = []
    for mi, res in enumerate(results):
        cites = {_url_key(u) for u in res.search_citations}
        gaps += res.gaps
        for cc in res.checked_claims:
            cid = cc.claim_id.strip().upper()
            if cid not in by_id:
                continue
            status = cc.status
            supported = False
            for src in cc.sources:
                if not src.url.startswith("http"):
                    continue
                k = _url_key(src.url)
                returned = (k in cites) if cites else None
                e = evid.get(k)
                if e is None:
                    e = evid[k] = {"id": "", "url": src.url, "title": src.title, "publisher": src.publisher,
                                   "date": src.date, "source_type": src.source_type, "material_seen": src.material_seen,
                                   "search_returned": returned, "claims": [], "relations": {}}
                elif returned:
                    e["search_returned"] = True
                if cid not in e["claims"]:
                    e["claims"].append(cid)
                e["relations"][cid] = src.relation
                if k not in claim_ev[cid]:
                    claim_ev[cid].append(k)
                if src.relation != "contradicts" and returned is not False:
                    supported = True
            if status == "confirmed" and not supported:
                status = "unclear"  # «подтверждено» без ссылки из результатов поиска не принимается
                notes[cid].append("подтверждение без ссылки из результатов поиска — не засчитано")
            statuses[cid].append(status)
            if cc.note:
                notes[cid].append(cc.note)
    for i, e in enumerate(evid.values(), 1):
        e["id"] = f"E{i}"
    key_to_id = {k: e["id"] for k, e in evid.items()}
    claims = []
    for cid, c in by_id.items():
        claims.append({**c, "status": merge_status(statuses[cid]), "notes": notes[cid][:3],
                       "evidence": [key_to_id[k] for k in claim_ev[cid]]})
    for e in evid.values():
        e["claims"] = [x for x in e["claims"]]
        e.pop("relations", None)
    return {"model": model, "missions": len(results), "claims": claims, "evidence": list(evid.values()),
            "gaps": gaps[:10]}


def ledger_text(ledger: dict[str, Any] | None) -> str:
    if not ledger or not ledger.get("claims"):
        return ""
    lines = ["Claims checked:"]
    for c in ledger["claims"]:
        ev = f" [{', '.join(c['evidence'])}]" if c.get("evidence") else ""
        note = f" — {' / '.join(c['notes'])}" if c.get("notes") else ""
        lines.append(f"- {c['id']} {c['status'].upper()}{ev}: {c['text']}{note}")
    if ledger.get("evidence"):
        lines.append("Sources:")
        for e in ledger["evidence"]:
            meta = ", ".join(x for x in (e.get("publisher"), e.get("date"), e.get("source_type"),
                                         f"seen: {e.get('material_seen')}") if x)
            flag = "" if e.get("search_returned") is not False else " (NOT among search results — treat as unverified)"
            lines.append(f"- {e['id']}: {e['url']} — {e.get('title') or ''} ({meta}){flag}")
    if ledger.get("gaps"):
        lines.append("Unresolved by search: " + "; ".join(to_text(g.get("question") or g) for g in ledger["gaps"][:6]))
    return "\n".join(lines)


def confirmed_ids(ledger: dict[str, Any] | None) -> set[str]:
    """Id источников, на которые можно опереться в «известно»: из подтверждённых утверждений."""
    out: set[str] = set()
    for c in (ledger or {}).get("claims", []):
        if c.get("status") == "confirmed":
            out.update(c.get("evidence") or [])
    return out


def section_tally(subq: list[str], ballots: dict[str, Any], order: list[str]) -> dict[str, dict[str, Any]]:
    """Голоса за лучший вариант по каждому подвопросу; при равенстве — по общему порядку голосования."""
    out: dict[str, dict[str, Any]] = {}
    rank = {l: i for i, l in enumerate(order)}
    for i in range(1, len(subq) + 1):
        q = f"Q{i}"
        counts: dict[str, int] = {}
        for b in ballots.values():
            lab = (getattr(b, "best_by_subquestion", {}) or {}).get(q)
            if lab and lab in rank and lab in getattr(b, "ranking", []):  # только чужие варианты (свой не в бюллетене)
                counts[lab] = counts.get(lab, 0) + 1
        leader = min(counts, key=lambda l: (-counts[l], rank.get(l, 99))) if counts else (order[0] if order else "")
        out[q] = {"counts": counts, "leader": leader, "votes": sum(counts.values())}
    return out


def section_text(subq: list[str], sections: dict[str, dict[str, Any]]) -> str:
    lines = []
    for i, q in enumerate(subq, 1):
        s = sections.get(f"Q{i}") or {}
        c = ", ".join(f"{l}: {n}" for l, n in sorted((s.get("counts") or {}).items(), key=lambda x: -x[1]))
        lines.append(f"Q{i} ({q[:80]}): backbone {s.get('leader') or '—'}" + (f" — votes {c}" if c else ""))
    return "\n".join(lines)


def check_verdict(v: ResearchVerdict, *, ledger: dict[str, Any] | None, n_subq: int, strict: bool) -> ResearchVerdict:
    """«Известно» — только с опорой на подтверждённые источники реестра или документы пользователя.
    Ошибка — ответ возвращается председателю; на последней попытке неподкреплённое переносится в «неизвестно»."""
    ok_ids = confirmed_ids(ledger)
    all_ids = {e["id"] for e in (ledger or {}).get("evidence", [])}
    bad, known = [], []
    unknown = list(v.unknown)
    for k in v.known:
        ids = [i for i in k.evidence_ids if i]
        if "DOC" in ids or (ids and all(i in ok_ids for i in ids)):
            known.append(k)
        else:
            bad.append(k)
    contested = [c.model_copy(update={"evidence_ids": [i for i in c.evidence_ids if i in all_ids]}) for c in v.contested]
    have = {c.subquestion_id for c in v.coverage_audit}
    missing = [f"Q{i}" for i in range(1, n_subq + 1) if f"Q{i}" not in have]
    if strict and (bad or missing):
        parts = []
        if bad:
            parts.append('"known" may contain ONLY claims backed by CONFIRMED ledger evidence ids or user documents '
                         '("DOC"); move these to "contested" or "unknown": '
                         + "; ".join(f'"{k.statement[:80]}" (ids: {", ".join(k.evidence_ids) or "none"})' for k in bad[:5]))
        if missing:
            parts.append(f'"coverage_audit" must have one entry per subquestion; missing: {", ".join(missing)}')
        raise ValueError(" ".join(parts))
    unknown += [UnknownItem(gap=k.statement, reason="not_checked",
                            how_to_check="нет подтверждения в реестре источников") for k in bad]
    audit = list(v.coverage_audit) + [CoverageItem(subquestion_id=q, status="unknown", note="председатель не указал")
                                      for q in missing]
    return v.model_copy(update={"known": known, "unknown": unknown, "contested": contested, "coverage_audit": audit})


def fallback_verdict(leader: str, proposals: dict[str, Proposal], ledger: dict[str, Any] | None,
                     subq: list[str]) -> ResearchVerdict:
    """Председатель не ответил: не выдаём текст лидера за исследование — показываем реестр и лучший вариант с пометкой."""
    known = [KnownItem(statement=c["text"], evidence_ids=c["evidence"], scope=c.get("scope", ""),
                       volatility=c.get("volatility", "unknown"))
             for c in (ledger or {}).get("claims", []) if c.get("status") == "confirmed" and c.get("evidence")]
    contested = [ContestedItem(issue=c["text"], evidence_ids=c["evidence"], positions=c.get("notes", []))
                 for c in (ledger or {}).get("claims", []) if c.get("status") in ("contested_in_sources", "refuted")]
    top = proposals.get(leader)
    body = ("**Синтез не выполнен:** председатель не ответил. Ниже — вариант-лидер голосования без синтеза; "
            "проверенные факты и спорное — в разделах «Известно» и «Спорно».\n\n" + (top.summary if top else ""))
    return ResearchVerdict(winner=leader, final_answer=body, why="Председатель не ответил; итог не синтезирован.",
                           known=known, contested=contested,
                           coverage_audit=[CoverageItem(subquestion_id=f"Q{i}", status="unknown", note="синтез не выполнен")
                                           for i in range(1, len(subq) + 1)],
                           risks=list(top.risks) if top else [])
