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
CLAIM_STATUS = ("confirmed", "partially_confirmed", "refuted", "contested_in_sources", "unclear", "user_doc",
                "search_unavailable")
STATUS_RANK = {"confirmed": 0, "refuted": 1, "contested_in_sources": 2, "user_doc": 3, "unclear": 4,
               "search_unavailable": 5, "not_checked": 6}
DEFAULT_LENSES = [  # по-русски: их видит и пользователь; ключ — чтобы не добавлять угол, который уже есть
    ("рынок", "рынок и первичные данные: цифры, размеры, цены, кто что делает"),
    ("технолог", "технология и ограничения: как это устроено на деле, пределы, зависимости"),
    ("эконом", "экономика и расчёты: затраты, отдача, юнит-экономика, явные формулы"),
    ("скептик", "скептик и разбор провала: где вывод может быть неверен, слабые данные, сценарии неудачи"),
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
    input_gaps: list[str] = Field(default_factory=list)

    @field_validator("as_of", "scope", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("subquestions", "lenses", "input_gaps", mode="before")
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
    subquestions: list[str] = Field(default_factory=list)  # к каким подвопросам относится: ["Q2"]
    checkability: str = "public"   # public | registry | private — можно ли проверить открытым поиском
    check_path: str = ""           # где проверять (реестр, документ, правообладатель)
    decisive: bool = False         # вывод или рекомендация изменились бы, окажись утверждение ложным
    entity: str = ""               # о ком или о чём утверждение (компания, продукт) — для охвата каталога

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"text": data}
        if isinstance(data, dict) and not data.get("text"):
            data = {**data, "text": _pick(data, "claim", "statement", "fact") or ""}
        return data

    @field_validator("subquestions", mode="before")
    @classmethod
    def _sq(cls, v: Any) -> list[str]:
        return [t.upper() for t in re.findall(r"[Qq]\d+", " ".join(to_list(v)))]

    @field_validator("checkability", mode="before")
    @classmethod
    def _chk(cls, v: Any) -> str:
        return _norm(v, ("public", "registry", "private"), "public")

    @field_validator("decisive", mode="before")
    @classmethod
    def _dec(cls, v: Any) -> bool:
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "1", "да", "high", "critical")
        return bool(v)

    @field_validator("text", "scope", "why_it_matters", "id", "check_path", "entity", mode="before")
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
    omitted_for_length: list[str] = Field(default_factory=list)  # что автор опустил ради длины

    @field_validator("unknowns", "what_would_change", "omitted_for_length", mode="before")
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
    quote: str = ""   # дословная фраза со страницы (до 25 слов) — программа ищет её на скачанной странице

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"url": data}
        return data

    @field_validator("url", "title", "publisher", "date", "source_type", "material_seen", "relation", "quote",
                     mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return "" if v is None else to_text(v).strip()


class CheckedClaim(BaseModel):
    claim_id: str
    status: str = "unclear"
    note: str = ""
    sources: list[ESource] = Field(default_factory=list)
    verified_parts: list[str] = Field(default_factory=list)    # какие части утверждения подтверждены
    unverified_parts: list[str] = Field(default_factory=list)  # какие — нет
    queries_used: list[str] = Field(default_factory=list)      # каким запросами искали (для второго поиска)
    verified_claim_text: str = ""  # частичное подтверждение: самостоятельная формулировка только доказанной части

    @field_validator("verified_parts", "unverified_parts", "queries_used", mode="before")
    @classmethod
    def _parts(cls, v: Any) -> list[str]:
        return to_list(v)

    @field_validator("claim_id", "note", "verified_claim_text", mode="before")
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
    claim_ids: list[str] = Field(default_factory=list)   # на какие проверенные утверждения реестра опирается
    evidence_ids: list[str] = Field(default_factory=list)
    scope: str = ""
    volatility: str = "unknown"
    item_id: str = ""     # K1, K2… — ставит программа по порядку пунктов председателя (карточка ссылается на них)
    doc_ref: str = ""     # для данных из документов пользователя: файл и место
    doc_quote: str = ""   # дословная короткая цитата из документа — программа ищет её в тексте вложений
    verified: list[str] = Field(default_factory=list)  # ставит программа: текст проверенных утверждений реестра

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"statement": data, "evidence_ids": re.findall(r"\bE\d+\b|\[DOC", data)}
        return data

    @field_validator("statement", "scope", "doc_ref", "doc_quote", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("evidence_ids", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> list[str]:
        return [("DOC" if "DOC" in str(x).upper() else str(x).strip().strip("[]").upper()) for x in to_list(v)]

    @field_validator("claim_ids", mode="before")
    @classmethod
    def _cids(cls, v: Any) -> list[str]:
        return [str(x).strip().strip("[]").upper() for x in to_list(v) if str(x).strip()]


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
    item_id: str = ""     # U1, U2… — ставит программа (карточка решения ссылается на них)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        return {"gap": data} if isinstance(data, str) else data

    @field_validator("gap", "reason", "how_to_check", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return to_text(v)


class DecisionCard(BaseModel):
    """Карточка решения на одну страницу — первой в итоге исследования. Председатель выбирает факты по номерам
    пунктов «известно» (K1…) и пробелы по номерам «неизвестно» (U1…); их текст подставляет программа из
    проверенных пунктов — пересказать или дописать факт в карточке нельзя."""

    summary_decision: str = ""
    known_item_ids: list[str] = Field(default_factory=list)
    key_unknown_ids: list[str] = Field(default_factory=list)
    top_known: list[str] = Field(default_factory=list)      # заполняет программа (старые итоги — текст председателя)
    key_unknowns: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    unverified: list[str] = Field(default_factory=list)     # программа: что в карточке не подтверждено

    @field_validator("known_item_ids", "key_unknown_ids", mode="before")
    @classmethod
    def _ids(cls, v: Any) -> list[str]:
        return [str(x).strip().strip("[]").upper() for x in to_list(v) if str(x).strip()][:8]

    @field_validator("summary_decision", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("top_known", "key_unknowns", "next_steps", mode="before")
    @classmethod
    def _l(cls, v: Any) -> list[str]:
        return to_list(v)[:8]

    @field_validator("unverified", mode="before")
    @classmethod
    def _u(cls, v: Any) -> list[str]:
        return to_list(v)


class ResearchVerdict(Verdict):
    """Этап 6: синтез исследования. «winner» необязателен: итог собирается по подвопросам."""

    winner: str = ""
    why: str = ""
    decision_card: DecisionCard = Field(default_factory=DecisionCard)

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

def fill_lenses(lenses: list[str], authors: int) -> list[str]:
    """Углов зрения не меньше, чем авторов: если председатель придумал меньше, добавляем стандартные."""
    out = [l for l in lenses if l.strip()]
    for key, text in DEFAULT_LENSES:
        if len(out) >= authors:
            break
        if not any(key in l.lower() for l in out):
            out.append(text)
    for _, text in DEFAULT_LENSES:  # всё равно меньше авторов — повторяем стандартные
        if len(out) >= authors:
            break
        if text not in out:
            out.append(text)
    return out


def subq_block(subquestions: list[str]) -> str:
    return "\n".join(f"Q{i}. {q}" for i, q in enumerate(subquestions, 1))


def research_head(question: str, original: str | None, req: list[str] | None, scope: str, as_of: str,
                  subquestions: list[str]) -> str:
    """Общее начало всех запросов исследования: одинаковое у всех — его кэширует провайдер."""
    return (head(question, original, req)
            + f"\nResearch scope{f' (as of {as_of})' if as_of else ''}: {scope or 'as stated in the question'}\n"
            + f"Subquestions the research must cover:\n{subq_block(subquestions)}\n")


def framing(question: str, context: str | None, members: int, max_req: int = 20) -> str:
    lenses = max(2, members)
    return f"""Before the council starts, prepare a RESEARCH task (not a choice between options).

User question:
{question}
{_context(context)}
1. Restate the research question precisely and neutrally (keep the user's intent; do not answer it).
2. Define 3-5 short evaluation criteria (1-4 words each). For research the defaults are: coverage completeness,
   quality of evidence, calibration of uncertainty, practical value — adapt them to the question if needed.
3. List the HARD REQUIREMENTS the user stated explicitly (structure, sections, length, language, constraints),
   faithfully, one per item, without inventing new ones (at most {max_req}, merging only exact duplicates; an
   empty list if none). If any input looks truncated or unavailable, say so in "input_gaps".
4. "as_of": the date or period the facts must be current for (e.g. "2026-11" or "2023-2025").
   "scope": geography, period, units, definitions and what is out of scope, in one or two sentences.
5. "subquestions": 3-5 subquestions that together cover the research question (a checklist, not separate tasks).
6. "lenses": {lenses} different angles for independent authors, adapted to this topic, e.g. first-hand data and
   market facts; technology and constraints; economics and calculations; methodology skeptic / pre-mortem.

JSON format:
{{"refined_question": "...", "criteria": ["..."], "requirements": ["..."], "as_of": "...", "scope": "...",
  "subquestions": ["...", "..."], "lenses": ["...", "..."], "input_gaps": ["..."]}}"""


def _claims_rule(max_claims: int) -> str:
    return f"""Epistemic discipline:
- Tag facts from attached files as [DOC: file, place]. Tag your own estimates or calculations as [CALC] and show
  inputs and formula. Tag facts, numbers or dates recalled from training as [MEM].
- A URL recalled from memory is only a lead for later search ("source_leads"), never a verification. NEVER invent
  URLs, quotations, dates or numbers: an honest [MEM] is better than a confident guess.
- State period, geography and units for every number. Separate facts, estimates and recommendations.
- If a subquestion cannot be answered, say so in "unknowns" instead of filling the gap. For every major conclusion
  state what evidence would change it.
List up to {max_claims} decision-critical, checkable claims in "claims": POSITIVE statements that public
sources (registries of FDA/EMA/PMDA, official sites, publications) can confirm or refute — not absence claims like
"X is not registered anywhere" (put those in "unknowns"). Mark fast-changing ones (prices, market shares, regulation,
versions) as "changes_fast".
Make every claim ATOMIC: one entity, one fact or number, one period, one geography — if a sentence holds several
facts, write several claims. Name the subquestions each claim supports ("subquestions": ["Q2"]). Set "checkability":
public (a primary or reputable public source can confirm it), registry (only a named registry or database can; name
it in "check_path"), private (needs the rights holder, a contract or an internal document). Who publicly holds the
rights to a product, or whether it is in a public registry, is public or registry, not private; only the terms a
company would offer YOU are private. Set "decisive": true if your conclusion or recommendation would change were
the claim false."""


CLAIM_JSON = ('{"text": "one atomic checkable statement", "basis": "user_doc|calc|memory|search_reported", '
              '"scope": "period, geography, units", "volatility": "stable|changes_fast|unknown", '
              '"subquestions": ["Q1"], "checkability": "public|registry|private", "check_path": "", "decisive": true|false, '
              '"entity": "company or product the claim is about", '
              '"source_leads": ["..."], "why_it_matters": "..."}')


DOSSIER_RULE = ("IMPORTANT: your output is working material for the council chair, NOT the final formatted "
                "deliverable. The chair applies the user's final structure and format. Do not write generic "
                "introductions, conclusions or decorative prose.")


def dossier_rule(max_words: int) -> str:
    return (DOSSIER_RULE + f"\nHard budget: the \"summary\" is at most {max_words} words; \"claims\", \"coverage\" and "
            "\"unknowns\" are separate fields and do NOT count toward it. Put the most decision-critical claims in "
            "\"claims\" first and do not repeat their text in the summary beyond the claim. Present catalogues and "
            "comparisons as compact Markdown tables only when a table replaces at least three sentences. If you run out "
            "of space, drop background, never claims or unknowns; list anything you left out in \"omitted_for_length\".")


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
Cover ALL subquestions: dense substance, no generic introductions.{lens_txt}
{dossier_rule(max_words)}
{_claims_rule(max_claims)}
The "summary" field holds your research findings (the chair writes the final report).

JSON format:
{{"title": "short descriptive angle",
 "summary": "structured Markdown covering Q1..Qk with inline tags [DOC: ...], [CALC], [MEM]",
 "reasoning": "core logic and assumptions",
 "claims": [{CLAIM_JSON}],
 "pros": ["..."], "risks": ["..."],
 "coverage": [{{"subquestion_id": "Q1", "status": "covered|partial|unknown", "note": "..."}}],
 "unknowns": ["what is not known and why it matters"],
 "what_would_change": ["evidence that would change the main conclusions"],
 "omitted_for_length": ["what you left out to keep the budget"],
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
    added = getattr(p, "new_after_ledger", None) or []
    if added:
        lines.append("Added after the evidence ledger (UNVERIFIED): " + "; ".join(added))
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
"decisive_claims": up to 3 claim ids per proposal on which its main conclusion or recommendation rests (if one is
false, the conclusion changes) — whether or not you doubt them.
"missing_subquestions": subquestions or important aspects this proposal did not cover (ids like "Q2" or text).

JSON format:
{{"reviews": [
  {{"label": "A", "scores": {{{example_scores}}},
    "strengths": ["..."], "weaknesses": ["..."], "keep": ["..."],
    "disputed_claims": [{{"claim_id": "A-C1", "reason": "..."}}], "decisive_claims": ["A-C2"], "missing_subquestions": ["Q3"]}}
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


SNIPPET_RULE = ("For a claim marked CRITICAL do NOT return \"confirmed\" solely from a snippet or a search answer: open "
                "and read the page; if you could not, return \"unclear\" and name the missing part. ")
RECHECK_NOTE = ("SECOND PASS: these claims were \"unclear\" after a first search. Do NOT repeat obvious queries: write 2-3 "
                "new ones in other wording or languages (legal entity name, brand, regulator or registry, official site) "
                "and list them in \"queries_used\". ")


def arbitration(question: str, as_of: str, scope: str, mission: str, claims_batch: str, max_evidence: int,
                snippet: bool = True, recheck: bool = False) -> str:
    return f"""Question (as of {as_of or 'today'}): {question}
Required scope: {scope or 'as stated in the question'}
Assigned mission: {mission}

Claims selected from anonymous proposals (contradictions, key numbers, fast-changing or memory-only claims). IDs are
fixed — use them exactly:
{claims_batch}

{RECHECK_NOTE if recheck else ""}Investigate EVERY claim. Look for evidence both for and against. Prefer primary sources for numbers, dates, laws and
technical facts; check dates, geography, units and definitions. Where estimates differ, find the reason. Two articles
restating one underlying report count as ONE source (mark the later one "secondary"). Record only sources your search
actually returned; never invent a URL, date or quotation. If you saw only a snippet or a search summary, say so in
"material_seen" — a snippet or a search answer is a lead, not a reading of the document. A live URL proves only
availability. A related page is not a confirmation. Absence of a found source is not a refutation.
{SNIPPET_RULE if snippet else ""}Test subject, value, date, geography, units and every qualifier separately; list
them in "verified_parts" and "unverified_parts".
For every source give "quote": ONE verbatim phrase (at most 25 words) copied from the page text you actually read
that bears on the claim; leave it empty if you saw only a snippet or a search summary; never paraphrase. The
application looks for this phrase on the page itself.
Statuses: confirmed (a source directly supports EVERY part of the claim in its stated scope — each number, date,
age, dose, name and owner); partially_confirmed (sources directly support only a separable part: do not confirm the
original claim — write a standalone "verified_claim_text" with ONLY the directly supported subject, value, units,
period, geography and qualifiers, and put the rest in "unverified_parts"); refuted (a reliable source directly
contradicts it); contested_in_sources (reputable sources conflict; report both); unclear (no reliable evidence found);
user_doc (checkable only from the user's own documents); search_unavailable.
Return at most {max_evidence} sources in total.

JSON format:
{{"checked_claims": [{{"claim_id": "A-C1", "status": "confirmed|partially_confirmed|refuted|contested_in_sources|unclear|user_doc|search_unavailable",
   "note": "what the sources state, with exact figures, scope and caveats", "verified_claim_text": "",
   "verified_parts": ["..."], "unverified_parts": ["..."], "queries_used": ["..."],
   "sources": [{{"url": "https://...", "title": "...", "publisher": "...", "date": "YYYY-MM-DD or empty",
                "source_type": "primary|secondary|unknown", "material_seen": "page|snippet|search_answer",
                "relation": "supports|contradicts|context", "quote": "verbatim words from the page"}}]}}],
 "gaps": [{{"question": "...", "searched_for": "...", "why_unresolved": "..."}}]}}"""


def _table_rows(md: str, limit: int) -> list[str]:
    """Строки таблиц Markdown из текста варианта (каталоги кандидатов, сравнения) — до limit строк."""
    rows = [ln.strip() for ln in md.splitlines() if ln.strip().startswith("|") and not re.fullmatch(r"\|[\s:|-]+\|?", ln.strip())]
    return rows[:limit]


def digest(label: str, p: Proposal, table_rows: int = 12) -> str:
    """Выжимка чужого варианта для доработки: утверждения, покрытие подвопросов, строки таблиц — без полного текста."""
    lines = [f"### Proposal {label}: {p.title}"]
    claims = getattr(p, "claims", None) or []
    if claims:
        lines += [f"- {c.id} [{c.basis}] {c.text}" for c in claims]
    cov = getattr(p, "coverage", None) or []
    if cov:
        lines.append("Coverage: " + "; ".join(f"{c.subquestion_id} {c.status}" + (f" ({c.note[:80]})" if c.note else "")
                                              for c in cov))
    rows = _table_rows(p.summary, table_rows) if table_rows else []
    if rows:
        lines.append("Table rows from this proposal (first ones):")
        lines += rows
    if p.pros:
        lines.append("Strengths claimed by the author: " + "; ".join(p.pros[:5]))
    unknowns = getattr(p, "unknowns", None) or []
    if unknowns:
        lines.append("Unknowns: " + "; ".join(unknowns[:5]))
    return "\n".join(lines)


def revision(rhead: str, criteria: list[str], own_label: str, own: Proposal, feedback: list[dict],
             others: dict[str, Proposal], ledger: str, lens: str, max_words: int, max_claims: int,
             full_others: bool, table_rows: int = 12) -> str:
    fb = json.dumps(feedback, ensure_ascii=False, indent=1) if feedback else "(no feedback received)"
    if full_others:
        others_txt = "Other proposals, in full (anonymous):\n\n" + "\n\n".join(proposal_block(l, p) for l, p in others.items())
    else:
        others_txt = ("Other proposals, as digests (claims, coverage and table rows — keep your own line of reasoning):\n\n"
                      + "\n\n".join(digest(l, p, table_rows) for l, p in others.items()))
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
{dossier_rule(max_words)}
{_claims_rule(max_claims)}

JSON format:
{{"title": "...", "summary": "the full revised research answer with [E#]/[DOC]/[CALC]/[MEM] tags",
 "reasoning": "...", "claims": [{CLAIM_JSON}], "pros": ["..."], "risks": ["..."],
 "coverage": [{{"subquestion_id": "Q1", "status": "covered|partial|unknown", "note": "..."}}],
 "unknowns": ["..."], "what_would_change": ["..."], "confidence": 0.0-1.0,
 "comparison": "where other proposals are stronger and where yours is", "borrowed": ["idea (label)"],
 "not_accepted": ["critique not accepted and why"], "position": "kept | improved | switched",
 "changes": "what you changed and why", "new_after_ledger": ["claim text"],
 "omitted_for_length": ["..."]}}"""


VOTE_LEDGER_NOTE = ("A compact evidence digest follows (claim id, status, evidence ids, material seen). It is evidence about "
                    "claims, not a vote for a proposal. Claims added after the ledger and memory-only claims are unverified "
                    "however persuasive they read. Prefer coverage supported by the ledger and honestly labelled "
                    "uncertainty; do not penalise an unknown that no public source can resolve. Agreement between authors "
                    "is not evidence.")


def vote(rhead: str, criteria: list[str], proposals: dict[str, Proposal], subq: list[str], req: list[str] | None,
         section_vote: bool, ledger_digest: str = "") -> str:
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
{f"{chr(10)}{VOTE_LEDGER_NOTE}{chr(10)}Evidence digest:{chr(10)}{ledger_digest}{chr(10)}" if ledger_digest else ""}
Rank ALL of these proposals from best to worst as research: coverage, quality of evidence, honest uncertainty,
practical value — not length or style. Include every label exactly once: {labels}.{REQ_VOTE if req else ""}{sec}

JSON format:
{{"ranking": ["best label", "...", "worst label"], "rationale": "why the top choice wins"{sec_json}}}"""


from .prompts import BRIEF_RULE  # noqa: E402 — общий текст «краткого итога»

DECISION_CARD_NOTE = ("\n- \"decision_card\": a one-page executive brief shown FIRST to the user. Your \"known\" items are "
                      "numbered K1, K2, … in the order you list them, your \"unknown\" items U1, U2, …. Select facts ONLY by "
                      "these ids: known_item_ids (at most 5) and key_unknown_ids (at most 5) — do not write or paraphrase facts "
                      "in the card, the application renders them from the validated items. summary_decision (at most 60 "
                      "words) is conditional advice, not a verified fact: add no numbers, names or sources that are not in "
                      "the selected known items, and say which assumptions it depends on; if no decisive assumption is "
                      "verified, say so. next_steps: at most 5 actions.")
DECISION_CARD_JSON = ('\n "decision_card": {{"summary_decision": "...", "known_item_ids": ["K1"], '
                      '"key_unknown_ids": ["U1"], "next_steps": ["..."]}},').replace("{{", "{").replace("}}", "}")


def verdict(rhead: str, criteria: list[str], proposals_txt: str, ledger: str, section_tally: str, overall_tally: str,
            critique_notes: str, ideas: str, leader: str, labels: list[str], agreement: str, ballot_notes: str,
            req: list[str] | None, co_leaders: list[str] | None, subq: list[str], card: bool = True,
            brief: bool = False) -> str:
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
- "known": ONLY claims with status confirmed in the ledger whose text and scope entail the statement. EVERY known
  item MUST cite the confirmed ledger claim ids it rests on in "claim_ids" and their E-ids in "evidence_ids"; the
  statement must not go beyond those claims (no new numbers, dates, entities, places or broader scope — "the
  product exists" does not support "the company grants exclusive rights"). Direct user-document data: evidence id
  "DOC", "doc_ref" = file and place, "doc_quote" = a short VERBATIM quote (at most 25 words) from that document; it
  is user data, not independent verification. The program checks all of this and moves anything else to unknown.
  Model agreement and [MEM] claims NEVER qualify as known. Claims added after the ledger (new_after_ledger) cannot
  be known unless the ledger re-checked them.
- In "final_answer" itself, label memory-only, new_after_ledger and unresolved statements as uncertain; a vote or
  agreement is not evidence. Claims the ledger lists as "not checkable by public search" go to "unknown" with how
  to check them.{DECISION_CARD_NOTE if card else ""}{(chr(10) + "- " + BRIEF_RULE) if brief else ""}
- "contested": conflicting sources or genuine methodological splits; give both positions, E-ids, why they differ
  (definitions, dates, methods, sources, values) and what would resolve it.
- "unknown": memory-only claims, unresolved gaps, data not found; a subquestion without an answer goes here with the reason.
- "coverage_audit": one entry per subquestion ({qids}) — covered, partial or unknown, and where in the answer.
- Never repeat refuted claims as true. Do not introduce facts, numbers or sources absent from the proposals, documents
  and ledger; if essential, mark "(добавлено председателем)".
- For recommendations state assumptions, risks and what would change them. End with the highest-value next research
  steps in "next_questions". The final_answer follows the structure and length the user's request demands; do
  not drop requested items and do not reproduce the transcript.
- If hard requirements R1, R2, … are listed, "final_answer" must satisfy every one of them. {FORMAT_RULE}
- Decide on EVERY idea worth keeping listed above, in "ideas".

JSON format:
{{"winner": "{labels[0] if labels else 'A'}", "section_backbone": {{"Q1": "B"}},
 "final_answer": "concise structured Markdown with [E1]/[DOC]/[CALC]/[MEM] tags and a Sources list with URLs",
 "why": "how the synthesis was built", "known": [{{"statement": "...", "claim_ids": ["A-C1"], "evidence_ids": ["E1"], "scope": "...", "volatility": "stable|changes_fast|unknown"}}, {{"statement": "...", "evidence_ids": ["DOC"], "doc_ref": "file.pdf, p. 3", "doc_quote": "verbatim words"}}],{DECISION_CARD_JSON if card else ""}
 "contested": [{{"issue": "...", "positions": ["..."], "evidence_ids": ["E2"], "why_they_differ": "...", "what_would_resolve": "..."}}],
 "unknown": [{{"gap": "...", "reason": "unverified_memory|no_public_data|not_checked", "how_to_check": "..."}}],
 "coverage_audit": [{{"subquestion_id": "Q1", "status": "covered|partial|unknown", "note": "where"}}],
 "what_would_change": ["..."], "next_questions": ["..."], "dissent": ["..."], "risks": ["..."], "deviation": "",
 "ideas": [{{"id": "I1", "decision": "included|rejected", "reason": "..."}}]}}"""


# ---------------------------------------------------------------- утверждения и реестр

MEMORY_URL_NOTE = " *(ссылка из памяти модели — не проверена)*"
_URL_RE = re.compile(r"(?<![(<\[])\bhttps?://[^\s)\]>*]+")


def mark_memory_urls(p: ResearchProposal, allowed: set[str]) -> ResearchProposal:
    """Ссылки в тексте участника без поиска — из памяти: помечаем, что они не проверены (кроме ссылок из реестра)."""
    keys = {_url_key(u) for u in allowed}

    def mark(m: re.Match) -> str:
        url = m.group(0).rstrip(".,;:")
        tail = m.group(0)[len(url):]
        if _url_key(url) in keys or MEMORY_URL_NOTE.strip() in p.summary[m.end():m.end() + 60]:
            return m.group(0)
        return url + MEMORY_URL_NOTE + tail
    return p.model_copy(update={"summary": _URL_RE.sub(mark, p.summary)})


def number_claims(label: str, p: ResearchProposal, start: int = 1) -> ResearchProposal:
    """Сквозные id утверждений присваивает программа после анонимизации: A-C1, A-C2…"""
    claims = [c.model_copy(update={"id": f"{label}-C{i}"}) for i, c in enumerate(p.claims, start)]
    return p.model_copy(update={"claims": claims})


# правообладатель, регистрация и патенты видны в публичных реестрах — «непубличным» это не считается
PUBLIC_REGISTRY_RE = re.compile(r"\b(FDA|EMA|PMDA|MHRA|ГРЛС|ЕАЭС|Росздравнадзор|Роспатент|патент\w*|patent\w*|"
                                r"зарегистрир\w*|регистрац\w*|registered|approv\w*|одобрен\w*|правообладател\w*|"
                                r"marketing authori[sz]ation|license holder|MAH)\b", re.I)
NEGATIVE_RE = re.compile(r"\b(ни один|ни одна|не зарегистрир|не имеет|нет\s+(?:ни|в|на|зарегистр)|отсутству|"
                         r"не представлен|никто|no\s+\w+\s+(?:is|has)|not\s+registered|none\s+of)", re.I)
ARBITRATION_BATCH = 5  # утверждений в одном поисковом запросе: на 10 сразу поиск находит слишком мало источников


def select_claims(proposals: dict[str, ResearchProposal], critiques: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Что проверять поиском: спорное по мнению критиков, быстро меняющееся, числа «из памяти»; минимум по одному
    утверждению от каждого автора, затем самые важные."""
    disputed: dict[str, int] = {}
    reasons: dict[str, list[str]] = {}
    decisive_by: set[str] = set()   # критики назвали посылкой вывода
    for cs in critiques.values():
        for r in cs.reviews:
            decisive_by |= {str(x).strip().upper() for x in getattr(r, "decisive_claims", None) or []}
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
            s += 3.0 if (c.decisive or c.id.upper() in decisive_by) else 0.0  # от него зависит вывод
            s += 0.5 / (i + 1)  # автор ставит важное выше
            if NEGATIVE_RE.search(c.text):
                s -= 4.0  # «нет / не зарегистрирован / ни один» поиском не доказать: проверяем, только если места хватит
            scored.append((s, label, {"id": c.id, "label": label, "text": c.text, "scope": c.scope,
                                      "basis": c.basis, "volatility": c.volatility,
                                      "disputed": disputed.get(c.id.upper(), 0),
                                      "decisive_by_critics": c.id.upper() in decisive_by, "entity": c.entity,
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


def _claim_key(text: str) -> tuple[str, frozenset[str], frozenset[str]]:
    """Ключ для поиска дублей: слова без пунктуации, числа отдельно (числа должны совпасть)."""
    t = re.sub(r"[«»\"'“”„(),;:.!?]", " ", text.lower().replace("ё", "е"))
    nums = frozenset(re.findall(r"\d+(?:[.,]\d+)?", t))
    words = frozenset(w for w in t.split() if len(w) > 2 and not re.fullmatch(r"\d+(?:[.,]\d+)?", w))
    return " ".join(t.split()), nums, words


def _same_claim(a: tuple, b: tuple) -> bool:
    """Дубль: тот же текст или почти те же слова при тех же числах (разные даты, дозы, территории — не дубль)."""
    if a[0] == b[0]:
        return True
    if a[1] != b[1] or not a[2] or not b[2]:
        return False
    return len(a[2] & b[2]) / len(a[2] | b[2]) >= 0.8


def prepare_claims(proposals: dict[str, ResearchProposal], critiques: dict[str, Any], limit: int, *, n_subq: int = 0,
                   dedupe: bool = True, defer_private: bool = True, coverage_first: bool = True,
                   check_decisive_private: bool = True) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Что проверять поиском — и что сразу в «неизвестно».

    1. Дубли разных авторов склеиваются (проверяются один раз; статус получат все копии — «dup_ids»).
    2. Непубличное (права, внутренние цены — checkability private) и утверждения об отсутствии («нигде нет») поиском
       не решить — они уходят в «неизвестно» с тем, где проверять («deferred»), и не тратят проверки. Но метку
       «private» ставит автор: решающее для вывода утверждение с такой меткой один раз проверяется публичным
       поиском (check_decisive_private) — автор мог ошибиться, а правообладатель или регистрация часто публичны.
    3. Отбор: сначала по утверждению на каждый подвопрос, затем по одному от каждого автора, затем самые важные
       (спорные по мнению критиков, быстро меняющиеся, числа «из памяти»)."""
    base = select_claims(proposals, critiques, 10 ** 6)  # все кандидаты, по важности
    by_id = {c.id.upper(): c for p in proposals.values() for c in p.claims}
    deferred: list[dict[str, Any]] = []
    pool: list[dict[str, Any]] = []
    keys: list[tuple] = []
    for c in base:
        rc = by_id.get(c["id"].upper())
        c = {**c, "subquestions": list(rc.subquestions) if rc else [], "checkability": rc.checkability if rc else "public",
             "check_path": rc.check_path if rc else "", "dup_ids": [],
             "decisive": bool(rc and rc.decisive) or bool(c.get("decisive_by_critics")),
             "critical": bool(c.get("disputed")) or c.get("volatility") == "changes_fast" or bool(rc and rc.decisive)
             or bool(c.get("decisive_by_critics"))}
        if c["checkability"] == "private" and PUBLIC_REGISTRY_RE.search(c["text"]):
            c["checkability"] = "registry"  # регистрация, патент, правообладатель в реестре — проверяется публично
        private = c["checkability"] == "private" and not (check_decisive_private and c["decisive"])
        if c["checkability"] == "private" and not private:
            c["private_checked"] = True  # автор считал непубличным, но от него зависит вывод — один публичный поиск
        if defer_private and (private or NEGATIVE_RE.search(c["text"])):
            deferred.append({"ids": [c["id"]], "text": c["text"], "reason": "absence" if NEGATIVE_RE.search(c["text"]) else "private",
                             "how_to_check": c["check_path"] or ("запрос правообладателю" if c["checkability"] == "private"
                                                                 else "проверка по реестрам и запрос — отсутствие поиском не доказать")})
            continue
        k = _claim_key(c["text"])
        dup = next((i for i, kk in enumerate(keys) if _same_claim(kk, k)), None) if dedupe else None
        if dup is not None:
            keep = pool[dup]
            keep["dup_ids"].append(c["id"])
            keep["disputed"] = keep.get("disputed", 0) + c.get("disputed", 0)
            keep["critical"] = keep["critical"] or c["critical"]
            keep["subquestions"] = sorted(set(keep["subquestions"]) | set(c["subquestions"]))
            continue
        keys.append(k)
        pool.append(c)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def take(c: dict[str, Any]) -> None:
        if c["id"] not in seen and len(out) < limit:
            out.append(c)
            seen.add(c["id"])
    if coverage_first:
        for q in [f"Q{i}" for i in range(1, n_subq + 1)]:  # по утверждению на подвопрос
            c = next((c for c in pool if q in c["subquestions"] and c["id"] not in seen), None)
            if c:
                take(c)
    for label in proposals:  # по одному от каждого автора
        if label not in {x["label"] for x in out}:
            c = next((c for c in pool if c["label"] == label and c["id"] not in seen), None)
            if c:
                take(c)
    for c in pool:
        take(c)
    return out, deferred


def claims_batch(selected: list[dict[str, Any]]) -> str:
    lines = []
    for c in selected:
        why = f" | critics: {'; '.join(c['reasons'])}" if c.get("reasons") else ""
        lines.append(f"{c['id']}{' CRITICAL' if c.get('critical') else ''}{' DECISIVE' if c.get('decisive') else ''}: {c['text']}"
                     + (f" (scope: {c['scope']})" if c.get("scope") else "")
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


def build_ledger(selected: list[dict[str, Any]], results: list[EvidenceResult], model: str, *,
                 snippet_rule: str = "off", deferred: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Склеивает ответы поисковых вызовов в реестр: статус по каждому утверждению и источники E1..En.

    Защита от выдуманных ссылок: если поиск вернул список найденных ссылок (citations), источник, которого в нём
    нет, помечается «не из результатов поиска» и статус «подтверждено» не даёт."""
    by_id = {c["id"].upper(): c for c in selected}
    statuses: dict[str, list[str]] = {k: [] for k in by_id}
    notes: dict[str, list[str]] = {k: [] for k in by_id}
    evid: dict[str, dict[str, Any]] = {}
    claim_ev: dict[str, list[str]] = {k: [] for k in by_id}
    derived: dict[str, dict[str, Any]] = {}   # частичное подтверждение: доказанная часть — отдельным утверждением
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
            read_page = False
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
                if src.quote and src.material_seen == "page":
                    e.setdefault("quotes", {})[cid] = src.quote[:400]
                if k not in claim_ev[cid]:
                    claim_ev[cid].append(k)
                if src.relation != "contradicts" and returned is not False:
                    supported = True
                    read_page = read_page or src.material_seen == "page"
            if status == "partially_confirmed":
                # исходное утверждение не повышается; доказанная часть — новым утверждением A-C1-V1 с теми же источниками
                if cc.verified_claim_text and supported and (read_page or not (
                        snippet_rule == "all" or (snippet_rule == "critical" and by_id[cid].get("critical")))):
                    derived[cid] = {"text": cc.verified_claim_text,
                                    "keys": [_url_key(s.url) for s in cc.sources if s.url.startswith("http")
                                             and s.relation != "contradicts"]}
                    notes[cid].append("подтверждена только часть: " + cc.verified_claim_text[:160])
                status = "unclear"
            if status == "confirmed" and not supported:
                status = "unclear"  # «подтверждено» без ссылки из результатов поиска не принимается
                notes[cid].append("подтверждение без ссылки из результатов поиска — не засчитано")
            elif status == "confirmed" and not read_page and (
                    snippet_rule == "all" or (snippet_rule == "critical" and by_id[cid].get("critical"))):
                status = "unclear"  # для важного утверждения фрагмент страницы — не подтверждение
                notes[cid].append("видели только фрагмент или ответ поиска, а не саму страницу — не засчитано")
            if cc.unverified_parts:
                notes[cid].append("не подтверждено: " + "; ".join(cc.unverified_parts[:3]))
            statuses[cid].append(status)
            if cc.note:
                notes[cid].append(cc.note)
    for i, e in enumerate(evid.values(), 1):
        e["id"] = f"E{i}"
    key_to_id = {k: e["id"] for k, e in evid.items()}
    claims = []
    for cid, c in by_id.items():
        row = {**c, "status": merge_status(statuses[cid]), "notes": list(dict.fromkeys(notes[cid]))[:4],
               "evidence": [key_to_id[k] for k in claim_ev[cid]]}
        claims.append(row)
        for dup in c.get("dup_ids") or []:  # копии того же утверждения у других авторов — тот же статус
            claims.append({**row, "id": dup, "duplicate_of": c["id"], "dup_ids": []})
        if cid in derived:
            d = derived[cid]
            claims.append({**c, "id": f"{c['id']}-V1", "text": d["text"], "status": "confirmed", "derived_from": c["id"],
                           "dup_ids": [], "notes": ["доказанная часть утверждения " + c["id"]],
                           "evidence": [key_to_id[k] for k in d["keys"] if k in key_to_id]})
    for e in evid.values():
        e["claims"] = [x for x in e["claims"]]
        e.pop("relations", None)
    return {"model": model, "missions": len(results), "claims": claims, "evidence": list(evid.values()),
            "gaps": gaps[:10], "deferred": list(deferred or [])}


LINK_TEXT = {"ok": "открывается", "dead": "страница не найдена", "blocked": "сайт не пускает автоматическую проверку",
             "error": "не удалось открыть", "refused": "адрес внутри сети — не открывался"}


QUOTE_MIN_TEXT = 1500  # меньше текста на странице — скорее всего, содержимое рисует JavaScript: цитату не судим


def apply_links(ledger: dict[str, Any], results: dict[str, dict[str, Any]], quote_check: bool = True) -> dict[str, Any]:
    """Итог проверки ссылок — в реестр. Утверждение, подтверждённое только мёртвыми ссылками (404/410),
    больше не «подтверждено». Для важных утверждений (спорных, быстро меняющихся, решающих) — цитата источника:
    если страница открылась, текста на ней достаточно, а дословной фразы, которую назвал поиск, там нет ни в одном
    источнике, подтверждение не засчитывается. Нашлась — у источника отметка «цитата найдена на странице»."""
    if not ledger or not results:
        return ledger
    for e in ledger.get("evidence", []):
        r = results.get(e.get("url", "")) or {}
        if r.get("quotes") is not None and e.get("quotes"):
            hits = r["quotes"]
            e["quote_hits"] = {cid: hits.get(q) for cid, q in e["quotes"].items()}
            e["page_text"] = r.get("text_len", 0)
    if quote_check:
        ev_by = {e["id"]: e for e in ledger.get("evidence", [])}
        for c in ledger.get("claims", []):
            if c.get("status") != "confirmed" or not (c.get("critical") or c.get("decisive")) or c.get("duplicate_of"):
                continue
            cid = c.get("derived_from") or c["id"]
            judged = [ev_by[i] for i in c.get("evidence") or [] if i in ev_by and cid in (ev_by[i].get("quote_hits") or {})
                      and (ev_by[i].get("page_text") or 0) >= QUOTE_MIN_TEXT]
            if any(e["quote_hits"][cid] for e in judged):
                c["quote_found"] = True
            elif judged and len(judged) == len([i for i in c.get("evidence") or [] if i in ev_by]):
                c["status"] = "unclear"
                c.setdefault("notes", []).append("цитата, которую привёл поиск, на странице источника не найдена — не засчитано")
    counts = {"ok": 0, "dead": 0, "blocked": 0, "error": 0}
    by_id = {}
    for e in ledger.get("evidence", []):
        r = results.get(e.get("url", ""))
        if not r:
            continue
        e["link"] = r["status"]
        if r.get("code"):
            e["link_code"] = r["code"]
        counts[r["status"]] = counts.get(r["status"], 0) + 1
        by_id[e["id"]] = r["status"]
    for c in ledger.get("claims", []):
        ev = [i for i in c.get("evidence") or [] if i in by_id]
        if c.get("status") == "confirmed" and ev and all(by_id[i] == "dead" for i in ev):
            c["status"] = "unclear"
            c.setdefault("notes", []).append("ссылки на источники не открываются (страница не найдена)")
    ledger["links"] = counts
    return ledger


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
            if e.get("link") == "dead":
                flag += " (LINK IS DEAD: page not found — do not rely on it)"
            lines.append(f"- {e['id']}: {e['url']} — {e.get('title') or ''} ({meta}){flag}")
    if ledger.get("gaps"):
        lines.append("Unresolved by search: " + "; ".join(to_text(g.get("question") or g) for g in ledger["gaps"][:6]))
    if ledger.get("deferred"):
        lines.append("Not checkable by public search (put into \"unknown\" with how to check): "
                     + "; ".join(f"{', '.join(d['ids'])} {d['text']} — {d['how_to_check']}" for d in ledger["deferred"][:10]))
    return "\n".join(lines)


def ledger_digest(ledger: dict[str, Any] | None) -> str:
    """Краткий реестр для голосующих: статус каждого проверенного утверждения и что видел поиск."""
    if not ledger or not ledger.get("claims"):
        return ""
    seen = {e["id"]: e.get("material_seen", "") for e in ledger.get("evidence", [])}
    out = []
    for c in ledger["claims"]:
        if c.get("duplicate_of"):
            continue
        ev = ", ".join(c.get("evidence") or [])
        mat = ", ".join(sorted({seen.get(i, "") for i in c.get("evidence") or []} - {""}))
        dups = f" (= {', '.join(c['dup_ids'])})" if c.get("dup_ids") else ""
        out.append(f"- {c['id']}{dups} {c['status'].upper()}" + (f" [{ev}; seen: {mat}]" if ev else ""))
    for d in ledger.get("deferred") or []:
        out.append(f"- {', '.join(d['ids'])} NOT CHECKABLE PUBLICLY")
    return "\n".join(out)


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


def check_length(p: Proposal, max_words: int, factor: float) -> Proposal:
    """Вариант намного длиннее заданного — вернуть автору на сокращение (ошибка уходит модели)."""
    n = len(str(p.summary or "").split())
    if n > max_words * factor:
        raise ValueError(f'"summary" is {n} words, but the limit is about {max_words} words. Rewrite it to about '
                         f"{max_words} words: keep every claim, number, source and recommendation, drop repetition, "
                         "generic text and long tables; reply with the full JSON again")
    return p


_IDS_RE = re.compile(r"\[[^\]]*\]|\b[A-Z]-C\d+(?:-V\d+)?\b|\b[EQRKU]\d+\b")
_NUM_RE = re.compile(r"\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?")


def _num_forms(n: str) -> frozenset[str]:
    """Каноническая форма числа: «1 360» → 1360, «1,4» → 1.4, «1,40» → 1.4. Неоднозначное «1,360» (тысячи или
    дробь?) даёт обе формы — совпадение с любой из них засчитывается."""
    n = re.sub(r"[ \u00a0\u202f]", "", n)
    m = re.fullmatch(r"(\d+)[.,](\d+)", n)

    def dec(i: str, f: str) -> str:
        f = f.rstrip("0")
        return (i.lstrip("0") or "0") + (f".{f}" if f else "")
    if not m:
        return frozenset({n.lstrip("0") or "0"})
    i, f = m.groups()
    forms = {dec(i, f)}
    if len(f) == 3:
        forms.add((i + f).lstrip("0") or "0")
    return frozenset(forms)


def _numbers(text: str) -> list[frozenset[str]]:
    """Все числа текста (и однозначные: «2 компании» ≠ «3 компании»), без номеров E1, A-C2, Q3, K1."""
    t = _IDS_RE.sub(" ", text or "")
    return [_num_forms(x) for x in _NUM_RE.findall(t)]


def _extra_numbers(statement: str, allowed_texts: list[str]) -> list[str]:
    allowed = set().union(*(f for t in allowed_texts for f in _numbers(t))) if allowed_texts else set()
    return sorted({min(f) for f in _numbers(statement) if not (f & allowed)})


def _plain(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)          # перенос слова в PDF: «регистра-\nция»
    t = re.sub(r"[«»\"“”„'’`*_>#|]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


DOC_QUOTE_WORDS = 30  # председателя просят до 25 слов; небольшой запас на союзы и числа


def _doc_files(ref: str, docs: dict[str, str]) -> list[str]:
    """Какие файлы называет ссылка «файл и место»; единственный файл — он и есть."""
    r = _plain(ref)
    named = [n for n in docs if _plain(n) in r or _plain(n.rsplit(".", 1)[0]) in r]
    return named or (list(docs) if len(docs) == 1 else [])


def _known_problem(k: KnownItem, *, ok_ids: set[str], claim_by: dict[str, Any], require_claims: bool,
                   numbers_check: bool, docs: dict[str, str]) -> str:
    """Почему пункт нельзя оставить в «известно» (пустая строка — можно)."""
    ids = [i for i in k.evidence_ids if i]
    ext = [i for i in ids if i != "DOC"]
    allowed: list[str] = []
    if "DOC" in ids:
        if not docs:
            return "ссылка на документ пользователя, но документов нет"
        q = _plain(k.doc_quote)
        if len(q.split()) < 2:
            return "ссылка на документ без дословной цитаты (doc_quote)"
        if len(q.split()) > DOC_QUOTE_WORDS:
            return f"цитата из документа длиннее {DOC_QUOTE_WORDS} слов — нужна короткая дословная"
        files = _doc_files(k.doc_ref, docs)
        if not files:
            return "не указан файл документа (doc_ref: файл и место)"
        if not any(q in docs[f] for f in files):
            return "цитата не найдена в указанном документе"
        allowed.append(k.doc_quote)
    if not ext and "DOC" not in ids:
        return "нет источника"
    cl: list[Any] = []
    if ext:
        if not all(i in ok_ids for i in ext):
            return "источник не подтверждён в реестре"
        if not k.claim_ids:
            if require_claims:
                return "не указано, какое проверенное утверждение реестра его подтверждает"
        else:
            cl = [claim_by.get(c) for c in k.claim_ids]
            if not all(c and c.get("status") == "confirmed" for c in cl):
                return "опорное утверждение не подтверждено"
            if not set(ext) <= {e for c in cl for e in c.get("evidence") or []}:
                return "источник не из подтверждения названного утверждения"
            allowed += [f"{c.get('text', '')} {c.get('scope', '')}" for c in cl]
    if numbers_check and allowed:
        extra = _extra_numbers(k.statement, allowed)
        if extra:
            return "в формулировке есть числа, которых нет в проверенном: " + ", ".join(extra[:5])
    return ""


def _mark_answer(answer: str, bad: list[tuple[KnownItem, str]], all_ids: set[str], ok_ids: set[str],
                 mark: bool) -> str:
    """Неподтверждённое — видно и там, где читают сначала: предупреждение в начале, пометки в тексте, полный список.
    Ссылка [E…] на источник, которого нет в реестре или который ничего не подтвердил, помечается на месте."""
    if all_ids:
        def fix(m: re.Match) -> str:
            ids = re.findall(r"\bE\d+\b", m.group(0))
            if not ids or "⚠" in m.group(0):
                return m.group(0)
            if any(i not in all_ids for i in ids):
                return m.group(0)[:-1] + " ⚠ нет в реестре]"
            if all(i not in ok_ids for i in ids):
                return m.group(0)[:-1] + " · не подтверждено]"
            return m.group(0)
        answer = re.sub(r"\[[^\]\n]{0,60}\]", fix, answer)
    if not (bad and mark):
        return answer
    for k, _ in bad:
        st = k.statement.strip()
        if len(st) >= 20 and st in answer:
            answer = answer.replace(st, st + " ⚠ *(не подтверждено источниками)*", 1)
    head = (f"> ⚠ **{len(bad)} утв. из «известно» не подтверждены источниками** и перенесены в «неизвестно» — "
            "список в конце итога. Не опирайтесь на них без проверки.\n\n")
    tail = ("\n\n> ⚠ **Не подтверждено источниками** — председатель указал эти утверждения как известные, но опоры в "
            "реестре проверенных источников для них нет (перенесены в «неизвестно»):\n"
            + "\n".join(f"> - {k.statement} — *{why}*" for k, why in bad))
    return head + answer + tail


def _refs(k: KnownItem) -> str:
    return f" [{', '.join(k.evidence_ids)}]" if k.evidence_ids else ""


def _check_card(card: DecisionCard, all_known: list[KnownItem], passed: list[KnownItem],
                unknown: list[Any], claim_by: dict[str, Any]) -> DecisionCard:
    """Карточку решения читают первой. «Известно» в ней — только тексты пунктов, прошедших проверку (по номерам
    K…; у старых итогов — точное совпадение текста), пробелы — по номерам U…; числа в «решении» — только из
    выбранных фактов. Всё снятое — в «не подтверждено», ничего не теряется."""
    if not any((card.summary_decision, card.known_item_ids, card.top_known, card.key_unknowns, card.key_unknown_ids,
                card.next_steps)):
        return card
    ok = {k.item_id: k for k in passed}
    named = {k.item_id: k for k in all_known}
    unverified: list[str] = []
    chosen: list[KnownItem] = []
    for i in card.known_item_ids:
        if i in ok:
            chosen.append(ok[i])
        elif i in named:
            unverified.append(f"{named[i].statement} — не прошло проверку")
        else:
            unverified.append(f"{i} — такого пункта «известно» нет")
    if not card.known_item_ids:  # итог в старом формате: текст председателя — только при точном совпадении
        texts = {_plain(_IDS_RE.sub(" ", k.statement)): k for k in passed}
        for t in card.top_known:
            k = texts.get(_plain(_IDS_RE.sub(" ", t)))
            if k:
                chosen.append(k)
            else:
                unverified.append(f"{t} — нет среди проверенного «известно»")
    chosen = list({k.item_id: k for k in chosen}.values())
    top = [k.statement + _refs(k) for k in chosen]
    un_by = {getattr(u, "item_id", ""): u for u in unknown}
    gaps = [un_by[i].gap for i in card.key_unknown_ids if i in un_by]
    gaps += [g for g in card.key_unknowns if g not in gaps]
    basis = [k.statement for k in chosen] + [t for k in chosen for t in k.verified]
    basis += [f"{claim_by[c].get('text', '')} {claim_by[c].get('scope', '')}" for k in chosen for c in k.claim_ids if c in claim_by]
    extra = _extra_numbers(card.summary_decision, basis) if card.summary_decision else []
    if extra:
        unverified.append("в решении есть числа, которых нет в выбранных проверенных фактах: " + ", ".join(extra[:6]))
    return card.model_copy(update={"top_known": top, "key_unknowns": gaps, "unverified": unverified})


def check_verdict(v: ResearchVerdict, *, ledger: dict[str, Any] | None, n_subq: int, strict: bool,
                  mark: bool = False, require_claims: bool = True, numbers_check: bool = True,
                  docs_text: str | dict[str, str] = "") -> ResearchVerdict:
    """«Известно» — только с опорой на подтверждённые утверждения реестра или на цитату из документов пользователя.
    Каждый пункт называет опорные утверждения (claim_ids, require_claims), источники — из их подтверждений, числа
    формулировки — из проверенного текста или цитаты (numbers_check). Для данных из документов («DOC») — файл и
    место, короткая дословная цитата, найденная в этом файле. Что не прошло — в «неизвестно» с причиной; в тексте
    итога это видно сразу (mark), карточка решения — только из прошедшего."""
    ok_ids = confirmed_ids(ledger)
    all_ids = {e["id"] for e in (ledger or {}).get("evidence", [])}
    claim_by = {c["id"].upper(): c for c in (ledger or {}).get("claims", [])}
    if isinstance(docs_text, str):
        docs = {"документ": _plain(docs_text)} if docs_text.strip() else {}
    else:
        docs = {n: _plain(t) for n, t in docs_text.items() if t and t.strip()}
    all_known = [k.model_copy(update={"item_id": f"K{i}"}) for i, k in enumerate(v.known, 1)]
    bad: list[tuple[KnownItem, str]] = []
    known: list[KnownItem] = []
    for k in all_known:
        why = _known_problem(k, ok_ids=ok_ids, claim_by=claim_by, require_claims=require_claims,
                             numbers_check=numbers_check, docs=docs)
        if why:
            bad.append((k, why))
        else:
            verified = [claim_by[c]["text"] for c in k.claim_ids if c in claim_by and claim_by[c].get("text")]
            known.append(k.model_copy(update={"verified": verified}))
    contested = [c.model_copy(update={"evidence_ids": [i for i in c.evidence_ids if i in all_ids]}) for c in v.contested]
    have = {c.subquestion_id for c in v.coverage_audit}
    missing = [f"Q{i}" for i in range(1, n_subq + 1) if f"Q{i}" not in have]
    if strict and (bad or missing):
        parts = []
        if bad:
            parts.append('"known" may contain ONLY items resting on CONFIRMED ledger claims (claim_ids + their E-ids, '
                         'no new numbers) or on a verbatim user-document quote ("DOC"); move these to "contested" or '
                         '"unknown": ' + "; ".join(f'"{k.statement[:80]}" ({why})' for k, why in bad[:5]))
        if missing:
            parts.append(f'"coverage_audit" must have one entry per subquestion; missing: {", ".join(missing)}')
        raise ValueError(" ".join(parts))
    unknown = [u.model_copy(update={"item_id": f"U{i}"}) for i, u in enumerate(v.unknown, 1)]
    unknown += [UnknownItem(gap=k.statement, reason="not_checked", how_to_check=f"не подтверждено: {why}",
                            item_id=f"U{len(unknown) + n}") for n, (k, why) in enumerate(bad, 1)]
    audit = list(v.coverage_audit) + [CoverageItem(subquestion_id=q, status="unknown", note="председатель не указал")
                                      for q in missing]
    answer = _mark_answer(v.final_answer, bad, all_ids, ok_ids, mark)
    card = _check_card(v.decision_card, all_known, known, unknown, claim_by)
    return v.model_copy(update={"known": known, "unknown": unknown, "contested": contested, "coverage_audit": audit,
                                "final_answer": answer, "decision_card": card})


def fallback_verdict(leader: str, proposals: dict[str, Proposal], ledger: dict[str, Any] | None,
                     subq: list[str]) -> ResearchVerdict:
    """Председатель не ответил: не выдаём текст лидера за исследование — показываем реестр и лучший вариант с пометкой."""
    known = [KnownItem(statement=c["text"], claim_ids=[c["id"]], evidence_ids=c["evidence"], scope=c.get("scope", ""),
                       volatility=c.get("volatility", "unknown"), verified=[c["text"]])
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


# ---------------------------------------------------------------- теневая смысловая сверка «известно»

class SemanticItem(BaseModel):
    item_id: str
    status: str = "insufficient"      # supported | contradicted | insufficient
    unsupported_parts: list[str] = Field(default_factory=list)
    reason: str = ""

    @field_validator("item_id", "reason", mode="before")
    @classmethod
    def _t(cls, v: Any) -> str:
        return to_text(v).strip()

    @field_validator("status", mode="before")
    @classmethod
    def _s(cls, v: Any) -> str:
        return _norm(v, ("supported", "contradicted", "insufficient"), "insufficient")

    @field_validator("unsupported_parts", mode="before")
    @classmethod
    def _p(cls, v: Any) -> list[str]:
        return to_list(v)


class SemanticAudit(BaseModel):
    items: list[SemanticItem] = Field(default_factory=list)


def semantic_items(known: list[KnownItem], ledger: dict[str, Any] | None) -> str:
    claim_by = {c["id"].upper(): c for c in (ledger or {}).get("claims", [])}
    out = []
    for k in known:
        basis = [f"- verified claim: {claim_by[c].get('text', '')} (scope: {claim_by[c].get('scope', '') or '—'})"
                 for c in k.claim_ids if c in claim_by]
        if "DOC" in k.evidence_ids and k.doc_quote:
            basis.append(f"- user-document quotation ({k.doc_ref or 'file not named'}): \"{k.doc_quote}\"")
        out.append(f"{k.item_id}: {k.statement}\n" + ("\n".join(basis) or "- (no verified basis)"))
    return "\n\n".join(out)


def semantic_audit(items: str) -> str:
    return f"""Check whether EACH statement below is fully entailed ONLY by its supplied verified claim texts or the exact
user-document quotation. Check subject, polarity, modality, quantities, units, period, geography and every qualifier.
Do not use outside knowledge. A matching number, an existing evidence id or a related quotation is not proof. Treat
user documents as user-provided evidence, not independent verification. Return "supported" only if every part is
directly supported; otherwise "contradicted" or "insufficient", and quote the unsupported words. Do not rewrite
statements and never mark anything confirmed.

{items}

JSON format:
{{"items": [{{"item_id": "K1", "status": "supported|contradicted|insufficient", "unsupported_parts": ["..."], "reason": "..."}}]}}"""
