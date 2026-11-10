"""Строгие схемы ответов моделей на каждом этапе заседания."""
from __future__ import annotations

from pydantic import BaseModel, Field


class Framing(BaseModel):
    """Шаг 0: председатель уточняет вопрос и формирует критерии."""

    refined_question: str
    criteria: list[str] = Field(min_length=1, max_length=7)


class Proposal(BaseModel):
    """Шаг 1: независимое предложение участника."""

    title: str
    summary: str
    reasoning: str = ""
    pros: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0, le=1)


class Revision(Proposal):
    """Шаг 4: доработанное предложение после критики и сравнения с чужими вариантами."""

    changes: str = ""
    comparison: str = ""                                  # честное сравнение своего варианта с чужими
    borrowed: list[str] = Field(default_factory=list)     # что взято у других (с меткой варианта)
    position: str = "improved"                            # kept | improved | switched (нормализуется в session)


class Review(BaseModel):
    label: str
    scores: dict[str, float]
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)


class CritiqueSet(BaseModel):
    """Шаг 3: оценки участником чужих вариантов."""

    reviews: list[Review]


class Ballot(BaseModel):
    """Шаг 5: ранжирование вариантов от лучшего к худшему."""

    ranking: list[str]
    rationale: str = ""


class ImageNote(BaseModel):
    name: str
    description: str


class ImageNotes(BaseModel):
    """Описание картинок для моделей, которые не умеют смотреть на изображения."""

    images: list[ImageNote]


class Claim(BaseModel):
    claim: str
    status: str = "unclear"  # confirmed | refuted | unclear (нормализуется в session)
    note: str = ""
    sources: list[str] = Field(default_factory=list)


class FactCheck(BaseModel):
    """Проверка ключевых фактических утверждений одного варианта."""

    claims: list[Claim] = Field(default_factory=list)
    summary: str = ""


class Verdict(BaseModel):
    """Шаг 6: итоговое решение председателя."""

    winner: str
    final_answer: str
    why: str
    dissent: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
