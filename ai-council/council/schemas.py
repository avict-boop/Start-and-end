"""Схемы ответов моделей на каждом этапе заседания.

Схемы строгие по смыслу, но терпимые к форме: модели часто присылают раздел ответа объектом или списком
вместо строки (особенно когда запрос задаёт разделы), называют поля по-своему или заворачивают ответ
в лишний уровень ({"proposal": {...}}). Такие ответы приводятся к нужному виду, а не отбрасываются.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


def to_text(v: Any, level: int = 3) -> str:
    """Любое значение -> Markdown-текст (объект с разделами -> заголовки, список -> пункты)."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float, bool)):
        return str(v)
    if isinstance(v, dict):
        parts = []
        for k, val in v.items():
            body = to_text(val, min(level + 1, 6))
            if isinstance(val, (dict, list)):
                parts.append(f"{'#' * level} {k}\n\n{body}")
            else:
                parts.append(f"**{k}:** {body}")
        return "\n\n".join(p for p in parts if p.strip())
    if isinstance(v, (list, tuple)):
        lines = []
        for x in v:
            t = to_text(x, level)
            if t.strip():
                lines.append(f"- {t}" if "\n" not in t else t)
        return "\n".join(lines)
    return str(v)


def to_list(v: Any) -> list[str]:
    """Строка/объект/список -> список строк."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, dict):
        return [f"{k}: {to_text(val)}" for k, val in v.items()]
    if isinstance(v, (list, tuple)):
        return [t for t in (to_text(x) for x in v) if t.strip()]
    return [str(v)]


def _pick(d: dict, *names: str) -> Any:
    for n in names:
        if n in d and d[n] not in (None, "", [], {}):
            return d[n]
    return None


def _first_line(text: str) -> str:
    """Первая содержательная строка без разметки Markdown — как название."""
    for ln in text.splitlines():
        ln = re.sub(r"^[#>\-\s]+", "", ln)
        ln = re.sub(r"^\*\*[^*]{1,60}:\*\*\s*(?=\S)", "", ln)   # «**Раздел:** текст» -> «текст»
        ln = re.sub(r"^[*\s]+", "", ln)
        ln = re.sub(r"[*_`]+", "", ln).strip()
        if ln:
            return (ln[:77] + "…") if len(ln) > 80 else ln
    return "Вариант"


def _title_from(sections: dict) -> str:
    """Ответ прислан разделами {"краткий_вывод": "...", ...}: название — начало первого раздела, а не имя ключа."""
    for v in sections.values():
        t = _first_line(to_text(v))
        if t and t != "Вариант":
            return t
    k = next(iter(sections), "")
    return str(k).replace("_", " ").strip().capitalize() or "Вариант"


def _unwrap(data: Any, required: tuple[str, ...]) -> Any:
    """{"proposal": {...нужные поля...}} -> {...}."""
    if isinstance(data, dict) and not any(k in data for k in required) and len(data) == 1:
        inner = next(iter(data.values()))
        if isinstance(inner, dict):
            return inner
    return data


class Framing(BaseModel):
    """Шаг 0: председатель уточняет вопрос и формирует критерии."""

    refined_question: str
    criteria: list[str] = Field(min_length=1, max_length=7)
    requirements: list[str] = Field(default_factory=list)  # жёсткие требования запроса пользователя
    input_gaps: list[str] = Field(default_factory=list)    # что во входе обрезано или недоступно (по мнению председателя)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        data = _unwrap(data, ("refined_question", "criteria"))
        if isinstance(data, dict):
            data = dict(data)
            if "refined_question" not in data:
                data["refined_question"] = _pick(data, "question", "refined", "task") or ""
            crit = data.get("criteria")
            if isinstance(crit, str):
                crit = [c.strip(" -•\t") for c in re.split(r"[,;\n]", crit) if c.strip(" -•\t")]
            if isinstance(crit, dict):
                crit = list(crit)
            if isinstance(crit, list):
                crit = [to_text(c.get("name") if isinstance(c, dict) and c.get("name") else c) for c in crit][:7]
            data["criteria"] = crit
            data["refined_question"] = to_text(data["refined_question"])
            reqs = data.get("requirements")
            if reqs is None:
                reqs = _pick(data, "hard_requirements", "must_have", "constraints")
            data["requirements"] = [r for r in to_list(reqs) if r.strip()][:40]  # предел — max_requirements в оркестраторе
            data["input_gaps"] = [g for g in to_list(data.get("input_gaps")) if g.strip()][:10]
        return data


TEXT_ALIASES = ("answer", "content", "text", "proposal", "response", "solution", "body", "details", "description",
                "option", "result", "report")
KNOWN_PROPOSAL = {"title", "summary", "reasoning", "pros", "risks", "confidence",
                  "changes", "comparison", "borrowed", "position", "not_accepted",
                  "claims", "coverage", "unknowns", "what_would_change", "new_after_ledger"}  # + режим «Исследование»


class Proposal(BaseModel):
    """Шаг 1: независимое предложение участника."""

    title: str
    summary: str
    reasoning: str = ""
    pros: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        data = _unwrap(data, ("title", "summary"))
        if not isinstance(data, dict):
            return data
        data = dict(data)
        sections: dict = {}
        if data.get("summary") in (None, "", [], {}):
            alt = _pick(data, *TEXT_ALIASES)
            if alt is not None:
                data["summary"] = alt
            else:
                # разделы ответа лежат прямо на верхнем уровне — собираем их в текст
                sections = {k: v for k, v in data.items() if k not in KNOWN_PROPOSAL}
                if sections:
                    data["summary"] = sections
        if isinstance(data.get("summary"), dict) and not sections:
            sections = data["summary"]
        if data.get("title") in (None, "", [], {}):
            alt = _pick(data, "name", "heading", "option_title", "headline")
            if alt is not None:
                data["title"] = alt
        if data.get("title") in (None, "", [], {}) and data.get("summary") not in (None, "", [], {}):
            data["title"] = _title_from(sections) if sections else _first_line(to_text(data["summary"]))
        if "reasoning" not in data:
            alt = _pick(data, "rationale", "justification", "why")
            if alt is not None:
                data["reasoning"] = alt
        return data

    @field_validator("title", "summary", "reasoning", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("pros", "risks", mode="before")
    @classmethod
    def _list(cls, v: Any) -> list[str]:
        return to_list(v)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> float:
        if v is None or v == "":
            return 0.5
        try:
            x = float(str(v).strip().rstrip("%").replace(",", "."))
        except ValueError:
            return 0.5
        if 1 < x <= 100:
            x /= 100
        return min(max(x, 0.0), 1.0)


class Revision(Proposal):
    """Шаг 4: доработанное предложение после критики и сравнения с чужими вариантами."""

    changes: str = ""
    comparison: str = ""                                  # честное сравнение своего варианта с чужими
    borrowed: list[str] = Field(default_factory=list)     # что взято у других (с меткой варианта)
    not_accepted: list[str] = Field(default_factory=list)  # какую критику не принял и почему
    position: str = "improved"                            # kept | improved | switched (нормализуется в session)

    @field_validator("changes", "comparison", "position", mode="before")
    @classmethod
    def _rtext(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("borrowed", "not_accepted", mode="before")
    @classmethod
    def _rlist(cls, v: Any) -> list[str]:
        return to_list(v)


def _score(v: Any) -> Any:
    """«7», «7/10», «7.5 из 10» -> 7.0."""
    if isinstance(v, str):
        m = re.match(r"\s*(\d+(?:[.,]\d+)?)", v)
        if m:
            return float(m.group(1).replace(",", "."))
    if isinstance(v, dict):
        return _score(_pick(v, "score", "value", "rating"))
    return v


class Review(BaseModel):
    label: str
    scores: dict[str, float]
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    keep: list[str] = Field(default_factory=list)  # что стоит сохранить из варианта, даже если он не победит
    # режим «Исследование»: спорные утверждения (id утверждения + причина) и пропущенные подвопросы
    disputed_claims: list[dict[str, Any]] = Field(default_factory=list)
    decisive_claims: list[str] = Field(default_factory=list)  # посылки, на которых держится вывод варианта
    missing_subquestions: list[str] = Field(default_factory=list)

    @field_validator("decisive_claims", mode="before")
    @classmethod
    def _decisive(cls, v: Any) -> list[str]:
        out = []
        for x in v if isinstance(v, list) else ([v] if v else []):
            x = to_text(_pick(x, "claim_id", "id", "claim") or "") if isinstance(x, dict) else to_text(x)
            if x.strip():
                out.append(x.strip().upper())
        return out[:6]

    @field_validator("label", mode="before")
    @classmethod
    def _label(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("scores", mode="before")
    @classmethod
    def _scores(cls, v: Any) -> Any:
        if isinstance(v, list):  # [{"criterion": "Цена", "score": 7}, ...]
            v = {to_text(_pick(x, "criterion", "name", "key")): _pick(x, "score", "value", "rating")
                 for x in v if isinstance(x, dict)}
        if isinstance(v, dict):
            return {str(k): _score(x) for k, x in v.items()}
        return v

    @field_validator("disputed_claims", mode="before")
    @classmethod
    def _disputed(cls, v: Any) -> list[dict[str, Any]]:
        out = []
        for x in v if isinstance(v, list) else ([v] if v else []):
            if isinstance(x, dict):
                cid = to_text(_pick(x, "claim_id", "id", "claim") or "")
                out.append({"claim_id": cid.strip(), "reason": to_text(_pick(x, "reason", "why", "note") or "")})
            elif isinstance(x, str) and x.strip():
                m = re.match(r"\s*([A-Z]-C\d+)\W*(.*)", x)
                out.append({"claim_id": m.group(1), "reason": m.group(2)} if m else {"claim_id": "", "reason": x})
        return out

    @field_validator("strengths", "weaknesses", "keep", "missing_subquestions", mode="before")
    @classmethod
    def _list(cls, v: Any) -> list[str]:
        return to_list(v)


class CritiqueSet(BaseModel):
    """Шаг 3: оценки участником чужих вариантов."""

    reviews: list[Review]

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, list):
            return {"reviews": data}
        if isinstance(data, dict) and "reviews" not in data:
            alt = _pick(data, "critique", "critiques", "evaluations", "assessments")
            if isinstance(alt, list):
                return {"reviews": alt}
            # {"A": {...}, "B": {...}} — отзывы по меткам
            if data and all(isinstance(v, dict) for v in data.values()):
                return {"reviews": [{"label": k, **v} for k, v in data.items()]}
        if isinstance(data, dict) and isinstance(data.get("reviews"), dict):
            return {"reviews": [{"label": k, **v} if isinstance(v, dict) else v for k, v in data["reviews"].items()]}
        return data


class Ballot(BaseModel):
    """Шаг 5: ранжирование вариантов от лучшего к худшему."""

    ranking: list[str]
    rationale: str = ""
    keep_from_others: list[dict[str, str]] = Field(default_factory=list)  # «Быстрый ответ»: ценное из других вариантов

    @field_validator("keep_from_others", mode="before")
    @classmethod
    def _keep(cls, v: Any) -> list[dict[str, str]]:
        out = []
        for x in v if isinstance(v, list) else ([v] if v else []):
            if isinstance(x, dict):
                idea = to_text(_pick(x, "idea", "text", "keep") or "").strip()
                if idea:
                    out.append({"label": to_text(x.get("label") or "").strip().upper()[:1], "idea": idea})
            elif to_text(x).strip():
                out.append({"label": "", "idea": to_text(x).strip()})
        return out[:3]

    @field_validator("ranking", mode="before")
    @classmethod
    def _ranking(cls, v: Any) -> Any:
        if isinstance(v, str):  # «B > A > C», «B, A, C»
            return [x.strip() for x in re.split(r"[>,;\n]|\s+-\s+", v) if x.strip()]
        if isinstance(v, list):
            return [to_text(_pick(x, "label", "proposal", "option") if isinstance(x, dict) else x) for x in v]
        return v

    @field_validator("rationale", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return to_text(v)


class ImageNote(BaseModel):
    name: str
    description: str

    @field_validator("name", "description", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return to_text(v)


class ImageNotes(BaseModel):
    """Описание картинок для моделей, которые не умеют смотреть на изображения."""

    images: list[ImageNote]


class Claim(BaseModel):
    claim: str
    status: str = "unclear"  # confirmed | refuted | unclear (нормализуется в session)
    note: str = ""
    sources: list[str] = Field(default_factory=list)

    @field_validator("claim", "status", "note", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("sources", mode="before")
    @classmethod
    def _list(cls, v: Any) -> list[str]:
        return to_list(v)


class FactCheck(BaseModel):
    """Проверка ключевых фактических утверждений одного варианта."""

    claims: list[Claim] = Field(default_factory=list)
    summary: str = ""

    @field_validator("summary", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return to_text(v)


class IdeaDecision(BaseModel):
    """Решение председателя по идее, которую критики советовали сохранить: включена в ответ или отклонена."""

    id: str
    decision: str = "included"  # included | rejected | not_addressed (нормализуется в session)
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            if data.get("id") in (None, ""):
                data["id"] = _pick(data, "idea_id", "ref", "key", "idea") or ""
            if data.get("reason") in (None, ""):
                data["reason"] = _pick(data, "why", "note", "comment", "explanation") or ""
            if data.get("decision") in (None, ""):
                data["decision"] = _pick(data, "status", "verdict", "action") or "included"
        return data

    @field_validator("id", "decision", "reason", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return to_text(v)


class Verdict(BaseModel):
    """Шаг 6: итоговое решение председателя."""

    winner: str
    final_answer: str
    why: str
    dissent: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    deviation: str = ""   # если победитель не лидер голосования — причина (fact | risk | process) и пояснение
    ideas: list[IdeaDecision] = Field(default_factory=list)  # по каждой идее «стоит сохранить» — взята или нет
    acceptable: bool = True  # «Выбор из вариантов»: false — ни один вариант не подходит
    decisive_defects: list[str] = Field(default_factory=list)  # почему не подходит (с цитатой)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        data = _unwrap(data, ("winner", "final_answer"))
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if data.get("final_answer") in (None, "", [], {}):
            alt = _pick(data, "answer", "final", "decision", "summary", "content", "text", "result")
            if alt is not None:
                data["final_answer"] = alt
        if data.get("why") in (None, "", [], {}):
            data["why"] = _pick(data, "reasoning", "rationale", "justification") or ""
        return data

    @field_validator("winner", "final_answer", "why", "deviation", mode="before")
    @classmethod
    def _text(cls, v: Any) -> str:
        return to_text(v)

    @field_validator("dissent", "risks", mode="before")
    @classmethod
    def _list(cls, v: Any) -> list[str]:
        return to_list(v)
