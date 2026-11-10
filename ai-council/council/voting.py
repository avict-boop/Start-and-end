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
    pairwise: float = 0.0  # попарные сравнения (Копленд): побед минус поражений над другими вариантами
    tie_break: str = ""    # чем решена ничья по Шульце с вариантом ниже: borda | pairwise | label
    schulze: int = 0       # скольких соперников вариант побеждает по методу Шульце (сильнейшие пути)


EPS = 1e-9


def pairwise_scores(candidates: list[str], ballots: dict[str, list[str]]) -> dict[str, float]:
    """Счёт Копленда по бюллетеням: для каждой пары X, Y — кто выше в большинстве бюллетеней, где есть оба.

    Победа = +1, поражение = −1, равенство = 0. Бюллетени неполные (без своего варианта),
    поэтому пара сравнивается только там, где встречаются оба варианта.
    """
    pos = [{l: i for i, l in enumerate(r)} for r in ballots.values()]
    score = {c: 0.0 for c in candidates}
    for i, x in enumerate(candidates):
        for y in candidates[i + 1:]:
            x_up = sum(1 for p in pos if x in p and y in p and p[x] < p[y])
            y_up = sum(1 for p in pos if x in p and y in p and p[y] < p[x])
            if x_up > y_up:
                score[x] += 1
                score[y] -= 1
            elif y_up > x_up:
                score[y] += 1
                score[x] -= 1
    return score


def pairwise_matrix(candidates: list[str], ballots: dict[str, list[str]]) -> dict[tuple[str, str], int]:
    """d[X, Y] — в скольких бюллетенях X стоит выше Y (учитываются только бюллетени, где есть оба)."""
    pos = [{l: i for i, l in enumerate(r)} for r in ballots.values()]
    return {(x, y): sum(1 for p in pos if x in p and y in p and p[x] < p[y])
            for x in candidates for y in candidates if x != y}


def schulze_wins(candidates: list[str], ballots: dict[str, list[str]]) -> dict[str, int]:
    """Метод Шульце: для каждого варианта — скольких соперников он побеждает.

    Сила прямой победы X над Y — d[X,Y], если d[X,Y] > d[Y,X], иначе 0. Сила пути — его самое слабое звено,
    p[X,Y] — сильнейший путь из X в Y. X выше Y, если p[X,Y] > p[Y,X]. Это отношение транзитивно,
    поэтому число побеждённых соперников задаёт порядок. Метод выбирает победителя Кондорсе, если он есть
    (вариант, который выигрывает у каждого один на один), и устойчив к похожим вариантам («клонам»).
    """
    d = pairwise_matrix(candidates, ballots)
    p = {(x, y): (d[x, y] if d[x, y] > d[y, x] else 0) for x in candidates for y in candidates if x != y}
    for k in candidates:
        for i in candidates:
            if i == k:
                continue
            for j in candidates:
                if j in (i, k):
                    continue
                p[i, j] = max(p[i, j], min(p[i, k], p[k, j]))
    return {x: sum(1 for y in candidates if y != x and p[x, y] > p[y, x]) for x in candidates}


def tally(
    candidates: list[str],
    ballots: dict[str, list[str]],
    critiques: dict[str, CritiqueSet],
) -> list[TallyRow]:
    """Итоги голосования.

    Порядок: метод Шульце (кто выигрывает в прямых сравнениях); при равенстве — нормированный балл Борда
    (1-е место в бюллетене = 1, последнее = 0, среднее по бюллетеням; нормировка нужна, потому что участник
    не голосует за себя и бюллетени разной длины), затем попарный счёт Копленда, затем метка (метки случайны —
    это жребий). Средняя оценка критики в порядке не участвует: она дана версиям до последней доработки.
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
    pair = pairwise_scores(candidates, ballots)
    sch = schulze_wins(candidates, ballots)

    rows = [
        TallyRow(
            label=c,
            borda=mean(points[c]) if points[c] else 0.0,
            avg_score=mean(scores[c]) if scores[c] else None,
            first_places=firsts[c],
            ballots=len(points[c]),
            pairwise=pair[c],
            schulze=sch[c],
        )
        for c in candidates
    ]
    rows.sort(key=lambda r: (-r.schulze, -round(r.borda, 9), -r.pairwise, r.label))
    for a, b in zip(rows, rows[1:]):
        if a.schulze == b.schulze:
            a.tie_break = ("borda" if abs(a.borda - b.borda) >= EPS else
                           "pairwise" if a.pairwise != b.pairwise else "label")
    return rows


TIE_NAME = {"borda": "балл Борда", "pairwise": "попарный счёт", "critique": "оценка критики",
            "label": "жребий (метки вариантов случайны)"}
ORDER_NOTE = ("Порядок: метод Шульце — кто выигрывает в прямых сравнениях (сколько соперников побеждает); "
              "при равенстве — балл Борда, затем попарный счёт, затем жребий. "
              "Оценка критики показана для сведения и на порядок не влияет: она дана версиям до доработки.")


def agreement(ballots: dict[str, list[str]]) -> dict | None:
    """Согласие совета: средний коэффициент Кендалла τ между бюллетенями по общим вариантам.

    τ = (согласных пар − несогласных пар) / все пары; 1 — одинаковый порядок, 0 — нет связи, −1 — обратный.
    Классический W Кендалла не подходит: в каждом бюллетене нет своего варианта, поэтому сравниваем
    бюллетени попарно только на общих вариантах (нужно хотя бы 2 общих).
    """
    lists = [r for r in ballots.values() if len(r) >= 2]
    taus: list[float] = []
    for i, a in enumerate(lists):
        for b in lists[i + 1:]:
            common = [l for l in a if l in b]
            if len(common) < 2:
                continue
            pb = {l: k for k, l in enumerate(b)}
            conc = disc = 0
            for x in range(len(common)):
                for y in range(x + 1, len(common)):
                    if pb[common[x]] < pb[common[y]]:
                        conc += 1
                    else:
                        disc += 1
            taus.append((conc - disc) / (conc + disc))
    if not taus:
        return None
    tau = mean(taus)
    level = "high" if tau >= 0.6 else "medium" if tau >= 0.3 else "low"
    return {"tau": round(tau, 3), "level": level, "pairs": len(taus), "ballots": len(lists)}


AGREEMENT_NAME = {"high": "высокое", "medium": "среднее", "low": "низкое"}
AGREEMENT_HINT = {
    "high": "модели в основном одинаково упорядочили варианты — решение устойчиво",
    "medium": "модели частично расходятся в оценке вариантов",
    "low": "мнения совета разошлись — относитесь к решению осторожно и посмотрите особые мнения",
}


def agreement_text(a: dict | None) -> str:
    if not a:
        return "—"
    return f"{AGREEMENT_NAME[a['level']]} (τ = {a['tau']:.2f})"


def tally_text(rows: list[TallyRow]) -> str:
    lines = []
    for i, r in enumerate(rows, 1):
        avg = f"{r.avg_score:.1f}" if r.avg_score is not None else "—"
        tie = f", tie broken by {r.tie_break}" if r.tie_break else ""
        lines.append(f"{i}. {r.label}: schulze wins={r.schulze}, borda={r.borda:.2f}, pairwise={r.pairwise:+.0f}, "
                     f"critique={avg} (pre-revision, info only), first places={r.first_places}/{r.ballots}{tie}")
    return "\n".join(lines)


def _tau(a: list[str], b: list[str]) -> float | None:
    """τ Кендалла двух порядков на их общих элементах (нужно хотя бы 2 общих)."""
    common = [x for x in a if x in b]
    if len(common) < 2:
        return None
    pb = {l: k for k, l in enumerate(b)}
    conc = disc = 0
    for i in range(len(common)):
        for j in range(i + 1, len(common)):
            if pb[common[i]] < pb[common[j]]:
                conc += 1
            else:
                disc += 1
    return (conc - disc) / (conc + disc)


def ballot_flags(rows: list[TallyRow], ballots: dict[str, list[str]]) -> dict[str, dict]:
    """Бюллетени, заметно расходящиеся с мнением остальных (возможное стратегическое голосование).

    Для каждого бюллетеня из 3+ вариантов строим общий порядок по ОСТАЛЬНЫМ бюллетеням (без него самого) и
    сравниваем: τ < 0 — порядок скорее обратный общему; «лидер последним» — вариант, который остальные
    ставят первым, у голосующего на последнем месте. Это только пометка: голос не отбрасывается.
    """
    del rows  # общий порядок считается без проверяемого бюллетеня, чтобы он не влиял сам на себя
    out: dict[str, dict] = {}
    for voter, ranking in ballots.items():
        if len(ranking) < 3:
            continue
        others = {v: r for v, r in ballots.items() if v != voter}
        if not others:
            continue
        order = [r.label for r in tally(sorted({l for r in others.values() for l in r} | set(ranking)), others, {})]
        order = [l for l in order if l in ranking]
        t = _tau(ranking, order)
        flags = []
        if t is not None and t < 0:
            flags.append("against")
        if order and ranking[-1] == order[0]:
            flags.append("leader_last")
        if flags:
            out[voter] = {"tau": round(t, 2) if t is not None else None, "flags": flags, "consensus_top": order[0]}
    return out


FLAG_NAME = {"against": "порядок почти обратный общему мнению",
             "leader_last": "ставит на последнее место вариант, который остальные считают лучшим"}


def leader_without(candidates: list[str], ballots: dict[str, list[str]], critiques: dict[str, CritiqueSet],
                   skip: set[str]) -> str | None:
    """Лидер голосования без указанных бюллетеней (проверка устойчивости). None — если бюллетеней не остаётся."""
    rest = {v: r for v, r in ballots.items() if v not in skip}
    if not rest:
        return None
    return tally(candidates, rest, critiques)[0].label
