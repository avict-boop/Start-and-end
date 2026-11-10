"""Анонимизация, разбор JSON из ответов и подсчёт голосов."""
from __future__ import annotations

import json
import random
import re
import string
from dataclasses import dataclass
from statistics import mean

from .schemas import CritiqueSet

# strict=False: модели часто ставят обычные переводы строк внутри длинных строк — это не ошибка по смыслу.
_decoder = json.JSONDecoder(strict=False)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _first_object(chunk: str) -> dict | None:
    for m in re.finditer(r"\{", chunk):
        try:
            obj, _ = _decoder.raw_decode(chunk, m.start())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def extract_json(text: str) -> dict:
    """Достаёт первый JSON-объект из ответа модели.

    Терпимо к типичным вольностям моделей: ```json```-обёртка, текст до и после,
    переводы строк внутри строк, лишняя запятая перед } или ].
    """
    fence = re.search(r"```(?:json|JSON)?\s*(\{.*\})\s*```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    candidates.append(text)
    for chunk in candidates:
        for variant in (chunk, _TRAILING_COMMA.sub(r"\1", chunk)):
            obj = _first_object(variant)
            if obj is not None:
                return obj
    if text.count("{") > text.count("}"):
        raise ValueError("JSON-объект оборван (похоже, ответ обрезан по лимиту токенов)")
    raise ValueError("в ответе не найден корректный JSON-объект")


def assign_labels(names: list[str], rng: random.Random) -> dict[str, str]:
    """Перемешивает авторов и выдаёт им метки A, B, C… Возвращает label -> author."""
    shuffled = names[:]
    rng.shuffle(shuffled)
    return {string.ascii_uppercase[i]: name for i, name in enumerate(shuffled)}


def normalize_label(raw: str) -> str:
    s = str(raw).strip().upper()
    s = re.sub(r"^(PROPOSAL|OPTION|ВАРИАНТ|ПРЕДЛОЖЕНИЕ)\s*", "", s)
    return s.strip(" .:()[]\"'")


def normalize_ranking(ranking: list[str], expected: list[str]) -> list[str]:
    out: list[str] = []
    for r in ranking:
        label = normalize_label(r)
        if label in expected and label not in out:
            out.append(label)
    missing = [l for l in expected if l not in out]
    if missing:
        raise ValueError(f"в ранжировании нет вариантов {missing}; нужно ранжировать ровно {expected}")
    return out


def normalize_critique(cs: CritiqueSet, expected: list[str], criteria: list[str]) -> CritiqueSet:
    """Проверяет, что оценены все нужные варианты по всем критериям, и приводит ключи к эталону."""
    by_label = {}
    for review in cs.reviews:
        label = normalize_label(review.label)
        if label not in expected:
            continue
        keys = list(review.scores)
        lower = {k.strip().lower(): k for k in keys}
        fixed: dict[str, float] = {}
        for i, crit in enumerate(criteria):
            if crit.lower() in lower:
                fixed[crit] = review.scores[lower[crit.lower()]]
            elif len(keys) == len(criteria):
                fixed[crit] = review.scores[keys[i]]  # совпадает число критериев — сопоставляем по порядку
            else:
                raise ValueError(f"для варианта {label} нет оценки по критерию '{crit}'")
        for crit, v in fixed.items():
            if not 1 <= v <= 10:
                raise ValueError(f"оценка {v} по '{crit}' для {label} вне диапазона 1-10")
        by_label[label] = review.model_copy(update={"label": label, "scores": fixed})
    missing = [l for l in expected if l not in by_label]
    if missing:
        raise ValueError(f"нет отзывов на варианты {missing}; нужно оценить ровно {expected}")
    return CritiqueSet(reviews=[by_label[l] for l in expected])


@dataclass
class TallyRow:
    label: str
    borda: float  # 0..1, среднее по бюллетеням
    avg_score: float | None  # средняя оценка в критике 1..10
    first_places: int
    ballots: int


def tally(
    candidates: list[str],
    ballots: dict[str, list[str]],
    critiques: dict[str, CritiqueSet],
) -> list[TallyRow]:
    """Нормированный подсчёт Борда: в каждом бюллетене 1-е место = 1, последнее = 0.

    Нормировка нужна, потому что участник не голосует за себя и бюллетени имеют разную длину.
    При равенстве решает средняя оценка критики, затем метка.
    """
    points: dict[str, list[float]] = {c: [] for c in candidates}
    firsts = {c: 0 for c in candidates}
    for ranking in ballots.values():
        n = len(ranking)
        for i, label in enumerate(ranking):
            if label in points:
                points[label].append(1.0 if n == 1 else (n - 1 - i) / (n - 1))
        if ranking and ranking[0] in firsts:
            firsts[ranking[0]] += 1

    scores: dict[str, list[float]] = {c: [] for c in candidates}
    for cs in critiques.values():
        for r in cs.reviews:
            if r.label in scores and r.scores:
                scores[r.label].append(mean(r.scores.values()))

    rows = [
        TallyRow(
            label=c,
            borda=mean(points[c]) if points[c] else 0.0,
            avg_score=mean(scores[c]) if scores[c] else None,
            first_places=firsts[c],
            ballots=len(points[c]),
        )
        for c in candidates
    ]
    rows.sort(key=lambda r: (-r.borda, -(r.avg_score or 0), r.label))
    return rows


def tally_text(rows: list[TallyRow]) -> str:
    lines = []
    for i, r in enumerate(rows, 1):
        avg = f"{r.avg_score:.1f}" if r.avg_score is not None else "—"
        lines.append(f"{i}. {r.label}: borda={r.borda:.2f}, critique={avg}, first places={r.first_places}/{r.ballots}")
    return "\n".join(lines)
