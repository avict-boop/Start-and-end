"""Демонстрационные заседания для примеров отчётов и проверки вёрстки (без обращений к моделям).

Каждое заседание — словарь в том же виде, что сохраняет программа (SessionResult.to_dict()),
поэтому отчёты по ним собираются тем же кодом, что и для настоящих заседаний.

    python -m council.reports.samples docs/examples      # собрать примеры всех отчётов
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

MEMBERS5 = ["claude", "chatgpt", "gemini", "kimi", "deepseek"]
MODELS = {"claude": "anthropic/claude-sonnet-4.5", "chatgpt": "openai/gpt-5", "gemini": "google/gemini-2.5-pro",
          "kimi": "moonshotai/kimi-k2", "deepseek": "deepseek/deepseek-chat", "grok": "x-ai/grok-4",
          "qwen": "qwen/qwen3-max", "mistral": "mistralai/mistral-large"}

CRM_ANSWER = """**Рекомендация:** внедрять **amoCRM** на тарифе «Расширенный» с пилотом на 4 менеджерах в течение 6 недель,
затем — на весь отдел. Bitrix24 оставить запасным вариантом, если понадобится единая система для склада и бухгалтерии.

## Почему amoCRM

- Отдел работает в основном с входящими заявками из мессенджеров — у amoCRM самая быстрая обработка этого сценария.
- Менеджеры осваивают воронку за 1–2 дня; у Bitrix24 обучение заняло бы 1–2 недели.
- Стоимость владения за первый год ниже на 18–25% при 12 пользователях.

## Сравнение кандидатов

| Критерий | amoCRM | Bitrix24 | RetailCRM |
|---|---|---|---|
| Стоимость в год, 12 мест | ≈ 215 тыс. ₽ | ≈ 260 тыс. ₽ | ≈ 240 тыс. ₽ |
| Мессенджеры и телефония | из коробки | из коробки, сложнее настроить | через модули |
| Срок внедрения | 2–3 недели | 6–8 недель | 4–5 недель |
| Интеграция с 1С | через коннектор | встроенная | через модуль |
| Риск «не приживётся» | низкий | средний | средний |

## План внедрения

1. **Неделя 1.** Выгрузить базу из таблиц, очистить дубли, настроить воронку из 6 этапов.
2. **Недели 2–3.** Подключить WhatsApp, Telegram и телефонию; провести обучение пилотной группы.
3. **Недели 4–6.** Пилот на 4 менеджерах; метрики — скорость первого ответа и конверсия в счёт.
4. **Неделя 7.** Решение о переходе всего отдела по итогам пилота.

> Порог успеха пилота: время первого ответа ≤ 15 минут и конверсия заявки в счёт не ниже текущих 22%.

Тарифы и условия — на странице https://www.amocrm.ru/tariffs/ (проверено на дату заседания)."""

CRM_WHY = """Вариант B выиграл у каждого соперника в прямых сравнениях (метод Шульце: 3 из 3) и получил 3 из 5 первых мест.
Его сильные стороны — реалистичный срок внедрения и понятная метрика пилота; критики отметили, что он единственный
посчитал стоимость владения за год, а не только цену лицензий. Председатель взял из варианта A план очистки базы,
а из варианта C — порог успеха пилота."""


def _proposal(title: str, summary: str, *, reasoning: str = "", pros=(), risks=(), confidence=0.75, **kw) -> dict:
    return {"title": title, "summary": summary, "reasoning": reasoning, "pros": list(pros), "risks": list(risks),
            "confidence": confidence, **kw}


def _reviews(labels: dict[str, str], criteria: list[str], base: dict[str, float]) -> dict[str, dict]:
    """Рецензии каждого участника на чужие варианты; оценки — вокруг «силы» варианта."""
    out: dict[str, dict] = {}
    for i, reviewer in enumerate(labels.values()):
        if reviewer is None:
            continue
        revs = []
        for j, (lab, author) in enumerate(labels.items()):
            if author == reviewer:
                continue
            s = base[lab]
            scores = {c: max(1.0, min(10.0, round(s + ((i + j + k) % 3 - 1) * 0.8))) for k, c in enumerate(criteria)}
            revs.append({"label": lab, "scores": scores,
                         "strengths": [f"Конкретный план для варианта {lab}", "Учтены ограничения бюджета"][: 1 + (i + j) % 2],
                         "weaknesses": [f"У варианта {lab} мало данных о стоимости поддержки", "Не раскрыт запасной план"][: 1 + (i * j) % 2]})
        out[reviewer] = {"reviews": revs}
    return out


def _stats(names: list[str], calls: int, cost: float, failures: dict[str, list[str]] | None = None) -> dict:
    return {n: {"calls": calls, "prompt_tokens": 18000 + 1500 * i, "completion_tokens": 5200 + 400 * i,
                "cost": round(cost * (0.7 + 0.15 * i), 4), "failures": (failures or {}).get(n, []),
                "cached_tokens": 6000 + 300 * i} for i, n in enumerate(names)}


def _tally(rows: list[tuple]) -> list[dict]:
    """(метка, борда, средняя критика, первых мест, бюллетеней, попарно, шульце, tie_break)."""
    return [{"label": l, "borda": b, "avg_score": a, "first_places": f, "ballots": n, "pairwise": pw,
             "tie_break": tb, "schulze": sc} for l, b, a, f, n, pw, sc, tb in rows]


def _png(w: int = 960, h: int = 540) -> bytes:
    """Картинка-вложение: простая диаграмма воронки продаж."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (w, h), "#ffffff")
    d = ImageDraw.Draw(img)
    vals = [100, 64, 41, 22, 15]
    for i, v in enumerate(vals):
        bw = int((w - 160) * v / 100)
        d.rectangle([80, 60 + i * 90, 80 + bw, 120 + i * 90], fill=["#4f46e5", "#6366f1", "#818cf8", "#a5b4fc", "#00c7b5"][i])
        d.text((90 + bw, 80 + i * 90), f"{v}%", fill="#18233d")
    d.text((80, 20), "Воронка продаж, 2026", fill="#18233d")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def full_debate() -> dict[str, Any]:
    """Подробное обсуждение: 5 участников, вложения, требования, проверка фактов, доработка."""
    criteria = ["Стоимость владения", "Скорость внедрения", "Удобство для менеджеров", "Риски"]
    authors = {"A": "chatgpt", "B": "gemini", "C": "kimi", "D": "deepseek"}
    initial = {
        "A": _proposal("Bitrix24: единая система для продаж и склада", "Выбрать **Bitrix24** — CRM, задачи и склад в одном окне."),
        "B": _proposal("amoCRM с пилотом на части отдела", "Выбрать **amoCRM** и проверить на 4 менеджерах."),
        "C": _proposal("RetailCRM для розницы", "Выбрать **RetailCRM** — сильная интеграция с маркетплейсами."),
        "D": _proposal("Остаться на таблицах ещё полгода", "Не внедрять CRM до стабилизации процессов."),
    }
    proposals = {
        "A": _proposal("Bitrix24: единая система для продаж и склада",
                       "Выбрать **Bitrix24**: CRM, задачи, склад и интеграция с 1С в одной системе.\n\n"
                       "- Стоимость: ≈ 260 тыс. ₽ в год на 12 мест.\n- Внедрение: 6–8 недель с интегратором.\n"
                       "- Плюс: не нужно потом переходить, если компания вырастет.",
                       reasoning="Одна система на все процессы снижает число интеграций.",
                       pros=["Встроенная интеграция с 1С", "Задачи и склад в той же системе"],
                       risks=["Долгое обучение менеджеров", "Нужен внешний интегратор"],
                       comparison="B быстрее внедряется, но уступает в учёте склада.", borrowed=["План очистки базы — из C"],
                       not_accepted=["Сократить срок до 3 недель — нереалистично для 1С"], position="kept",
                       changes="Добавлен расчёт стоимости поддержки интегратора."),
        "B": _proposal("amoCRM с пилотом на части отдела", CRM_ANSWER.split("## План внедрения")[0].strip(),
                       reasoning="Сценарий отдела — входящие заявки из мессенджеров; amoCRM решает его быстрее и дешевле.",
                       pros=["Быстрый старт: 2–3 недели", "Менеджеры осваивают за 1–2 дня", "Ниже стоимость владения"],
                       risks=["Интеграция с 1С через сторонний коннектор", "При росте до склада понадобится вторая система"],
                       comparison="A сильнее для склада, C — для маркетплейсов; для текущего сценария B точнее.",
                       borrowed=["Порог успеха пилота — из C", "Очистка базы перед переносом — из A"],
                       not_accepted=["Отложить внедрение (D): потери заявок уже заметны"], position="improved",
                       changes="Добавлены стоимость владения за год и порог успеха пилота.", confidence=0.82),
        "C": _proposal("RetailCRM для розницы и маркетплейсов",
                       "Выбрать **RetailCRM**: сильная интеграция с маркетплейсами и программой лояльности.",
                       reasoning="Если доля маркетплейсов вырастет, RetailCRM окупится быстрее.",
                       pros=["Модули для маркетплейсов"], risks=["Мессенджеры — через платные модули"],
                       comparison="B проще, но слабее для маркетплейсов.", position="improved",
                       changes="Уточнена стоимость модулей мессенджеров.", confidence=0.6),
        "D": _proposal("Остаться на таблицах ещё полгода",
                       "Отложить CRM: сначала описать процесс продаж и роли, иначе система закрепит хаос.",
                       pros=["Нет затрат сейчас"], risks=["Потери заявок продолжатся", "Через полгода переход дороже"],
                       position="switched", comparison="B убедительно показал, что потери уже стоят дороже внедрения.",
                       changes="Подход сменён: поддерживаю пилот, но с описанием процесса до старта.", confidence=0.45),
    }
    base = {"A": 6.8, "B": 8.3, "C": 6.5, "D": 4.6}
    return {
        "question": ("Какую CRM выбрать для отдела продаж из 12 человек?\n\nСейчас заявки ведём в Google-таблицах и "
                     "мессенджерах, теряем 10–15% обращений. Бюджет — до 300 тыс. ₽ в год. Нужна таблица сравнения "
                     "и план внедрения по неделям."),
        "refined_question": "Какая CRM лучше подойдёт отделу продаж из 12 человек с бюджетом до 300 тыс. ₽ в год, "
                            "и как её внедрить без потери заявок?",
        "criteria": criteria, "requirements": ["Таблица сравнения кандидатов", "План внедрения по неделям",
                                               "Бюджет — до 300 тыс. ₽ в год"],
        "mode": "debate", "authors": authors, "initial_proposals": initial, "proposals": proposals,
        "critiques": _reviews(authors, criteria, base),
        "ballots": {"claude": {"ranking": ["B", "A", "C", "D"], "rationale": "B — реалистичный срок и понятная метрика пилота."},
                    "chatgpt": {"ranking": ["B", "C", "D"], "rationale": "B точнее всего отвечает сценарию отдела."},
                    "gemini": {"ranking": ["A", "C", "D"], "rationale": "A надёжнее на горизонте трёх лет."},
                    "kimi": {"ranking": ["B", "A", "D"], "rationale": "B дешевле и быстрее, риски названы честно."},
                    "deepseek": {"ranking": ["A", "B", "C"], "rationale": "A — единая система без интеграций."}},
        "tally": _tally([("B", 0.83, 8.1, 3, 4, 3.0, 3, ""), ("A", 0.70, 7.0, 2, 4, 1.0, 2, ""),
                         ("C", 0.33, 6.6, 0, 4, -1.0, 1, ""), ("D", 0.08, 4.7, 0, 4, -3.0, 0, "")]),
        "verdict": {"winner": "B", "final_answer": CRM_ANSWER, "why": CRM_WHY,
                    "dissent": ["deepseek и gemini считают, что через 2–3 года всё равно понадобится Bitrix24 для склада.",
                                "deepseek предупреждает: без описания процесса продаж CRM закрепит нынешние потери."],
                    "risks": ["Коннектор с 1С — сторонний: проверить на тестовой базе до пилота.",
                              "Если доля маркетплейсов превысит 30%, вернуться к сравнению с RetailCRM.",
                              "Пилот на 4 менеджерах может не показать нагрузку всего отдела."],
                    "deviation": ""},
        "stats": _stats(MEMBERS5, 5, 0.09), "total_cost_usd": 0.4811,
        "started_at": "2026-11-10T14:05:00+00:00", "duration_s": 312.0,
        "attachments": [{"name": "воронка-2026.png", "file": "01-воронка-2026.png", "kind": "image", "size": 48000,
                         "width": 960, "height": 540},
                        {"name": "Регламент отдела продаж.docx", "file": "02-регламент.docx", "kind": "document",
                         "size": 84000, "pages": 6, "chars": 14200}],
        "image_notes": {"воронка-2026.png": "Столбчатая диаграмма воронки: 100% заявок → 64% → 41% → 22% счетов → 15% оплат."},
        "image_notes_by": "gemini", "followup_of": "", "thread": [],
        "request": {"chair": "claude", "compact_proposals": False, "fact_check": True},
        "agreement": {"tau": 0.47, "level": "medium", "pairs": 10, "ballots": 5}, "chair_proposed": False,
        "ballot_flags": {}, "leader_without_flagged": None,
        "fact_check": {"B": {"summary": "Ключевые цифры варианта подтверждаются открытыми источниками.",
                             "claims": [{"claim": "Тариф «Расширенный» amoCRM — около 1 500 ₽ за пользователя в месяц.",
                                         "status": "confirmed", "note": "Цена на сайте amoCRM на ноябрь 2026.",
                                         "sources": ["https://www.amocrm.ru/tariffs/"]},
                                        {"claim": "Интеграция с 1С доступна только через сторонний коннектор.",
                                         "status": "confirmed", "note": "", "sources": []},
                                        {"claim": "Внедрение занимает 2–3 недели.", "status": "unclear",
                                         "note": "Зависит от числа интеграций; открытых данных нет.", "sources": []}]},
                       "A": {"summary": "Одна цифра устарела.",
                             "claims": [{"claim": "Bitrix24 на 12 мест — 180 тыс. ₽ в год.", "status": "refuted",
                                         "note": "С 2026 года тариф «Профессиональный» стоит ≈ 260 тыс. ₽ в год.",
                                         "sources": ["https://www.bitrix24.ru/prices/"]},
                                        {"claim": "Регламент отдела требует согласования скидок руководителем.",
                                         "status": "user_doc", "note": "Раздел 4 приложенного регламента.", "sources": []}]}},
        "fact_checker": "perplexity/sonar-pro", "co_leaders": [], "decisive": {"deepseek": "A"},
        "coverage": {"min": 2, "max": 4, "options": 4, "ballots": 5},
    }


def quick_short() -> dict[str, Any]:
    """Быстрый ответ: три участника, короткое решение, без критики."""
    authors = {"A": "claude", "B": "chatgpt", "C": "gemini"}
    props = {"A": _proposal("«Зерно и книга»", "Название **«Зерно и книга»** — про кофе и учёбу, легко запомнить."),
             "B": _proposal("«Конспект»", "**«Конспект»** — студенческое слово, хорошо для вывески."),
             "C": _proposal("«Перемена»", "**«Перемена»** — про паузу между парами.")}
    return {"question": "Как назвать кофейню у университета?", "refined_question": "", "criteria": ["Запоминаемость", "Связь с местом"],
            "mode": "quick", "authors": authors, "proposals": props, "critiques": {},
            "ballots": {"claude": {"ranking": ["B", "C"], "rationale": "Короче и смешнее."},
                        "chatgpt": {"ranking": ["C", "A"], "rationale": "Тёплое слово."},
                        "gemini": {"ranking": ["B", "A"], "rationale": "Сразу понятно, для кого."}},
            "tally": _tally([("B", 0.67, None, 2, 2, 1.0, 2, ""), ("C", 0.5, None, 1, 2, 0.0, 1, ""), ("A", 0.0, None, 0, 2, -1.0, 0, "")]),
            "verdict": {"winner": "B", "final_answer": "Назвать кофейню **«Конспект»**. Короткое, студенческое, хорошо читается на вывеске.",
                        "why": "Два из трёх бюллетеней поставили его первым.", "dissent": [], "risks": ["Проверить, свободен ли товарный знак."]},
            "stats": _stats(["claude", "chatgpt", "gemini"], 2, 0.004), "total_cost_usd": 0.0184,
            "started_at": "2026-11-09T09:12:00+00:00", "duration_s": 41.0, "attachments": [], "request": {"chair": "claude"},
            "agreement": {"tau": 0.33, "level": "medium", "pairs": 3, "ballots": 3}, "chair_proposed": True,
            "coverage": {"min": 1, "max": 2, "options": 3, "ballots": 3}}


def tie_deviation() -> dict[str, Any]:
    """Ничья за первое место, низкое согласие, спорные бюллетени, сбой модели."""
    d = full_debate()
    d["question"] = "Стоит ли переводить команду разработки на четырёхдневную рабочую неделю?"
    d["refined_question"] = ""
    d["requirements"] = []
    d["attachments"], d["image_notes"], d["fact_check"] = [], {}, {}
    d["proposals"]["A"]["title"] = "Пилот четырёхдневки на одной команде"
    d["proposals"]["B"]["title"] = "Гибкий пятничный день вместо полной четырёхдневки"
    d["proposals"]["C"]["title"] = "Оставить пять дней, сократить совещания"
    d["proposals"]["D"]["title"] = "Перевести всех сразу"
    d["tally"] = _tally([("A", 0.62, 7.2, 2, 4, 1.0, 2, ""), ("B", 0.62, 7.4, 2, 4, 1.0, 2, "label"),
                         ("C", 0.45, 6.1, 1, 4, -1.0, 1, ""), ("D", 0.21, 4.2, 0, 4, -1.0, 0, "")])
    d["co_leaders"] = ["A", "B"]
    d["agreement"] = {"tau": 0.12, "level": "low", "pairs": 10, "ballots": 5}
    d["verdict"]["winner"] = "C"
    d["verdict"]["deviation"] = "risk: у обоих лидеров не решён вопрос дежурств и поддержки клиентов по пятницам."
    d["verdict"]["final_answer"] = ("**Не переводить команду на четырёхдневку сейчас.** Сначала на 2 месяца сократить "
                                    "совещания на 30% и ввести «тихие часы», затем вернуться к вопросу с данными о нагрузке.")
    d["ballot_flags"] = {"deepseek": {"flags": ["against", "leader_last"], "tau": -0.67}}
    d["leader_without_flagged"] = "B"
    d["decisive"] = {"deepseek": "B", "gemini": "A"}
    d["stats"]["kimi"]["failures"] = ["vote: модель вернула пустой ответ (лимит рассуждений)", "revision: таймаут 180 с"]
    del d["ballots"]["kimi"]
    return d


EVAL_ANSWER = r"""**Choose PostgreSQL with the TimescaleDB extension.** It covers 90% of the analytics workload without a second
database and keeps operations simple for a team of three.

## Neden PostgreSQL? (Türkçe özet)

Ekip küçük; tek bir veritabanı işletmek, iki ayrı sistem işletmekten çok daha ucuzdur. Günlük 40 milyon satıra kadar
TimescaleDB yeterli performans gösterir.

## Capacity estimate

Expected load is $N = 4 \times 10^7$ rows per day; with compression ratio $k \approx 8$ storage for a year is

$$S = \frac{N \cdot 365 \cdot 120\ \text{bytes}}{k} \approx 220\ \text{GB}$$

## Benchmark summary

| Engine | Ingest, rows/s | p95 query, ms | Storage/yr | Ops effort | License | Managed in RU | SQL dialect | Joins |
|---|---|---|---|---|---|---|---|---|
| PostgreSQL + TimescaleDB | 180 000 | 420 | 220 GB | low | Apache/TSL | yes | full SQL | full |
| ClickHouse | 1 200 000 | 85 | 90 GB | medium | Apache 2.0 | yes | ClickHouse SQL | limited |
| BigQuery | n/a (batch) | 1 900 | pay per TB | very low | proprietary | no | GoogleSQL | full |

## Migration snippet

```sql
CREATE EXTENSION IF NOT EXISTS timescaledb;
SELECT create_hypertable('events', 'created_at', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE events SET (timescaledb.compress, timescaledb.compress_segmentby = 'tenant_id');
```

Documentation: https://docs.timescale.com/use-timescale/latest/compression/about-compression/?utm_source=council-report&utm_medium=pdf&utm_campaign=very-long-link-check"""


def evaluate_mixed() -> dict[str, Any]:
    """Оценка вариантов пользователя: английский и турецкий текст, формулы, код, широкая таблица, длинная ссылка."""
    criteria = ["Performance", "Operations", "Cost"]
    authors = {"A": None, "B": None, "C": None}
    props = {"A": _proposal("PostgreSQL + TimescaleDB", "PostgreSQL with the TimescaleDB extension.", confidence=None),
             "B": _proposal("ClickHouse", "ClickHouse cluster of three nodes.", confidence=None),
             "C": _proposal("BigQuery", "Google BigQuery with scheduled loads.", confidence=None)}
    base = {"A": 8.0, "B": 7.4, "C": 5.1}
    crit = _reviews({"x1": "claude", "x2": "chatgpt", "x3": "gemini", "x4": "kimi"}, criteria, {"x1": 0, "x2": 0, "x3": 0, "x4": 0})
    crit = {r: {"reviews": [{"label": l, "scores": {c: base[l] + (i % 2) for c in criteria}, "strengths": ["Clear trade-offs"],
                             "weaknesses": ["Needs a load test"]} for l in props]} for i, r in enumerate(crit)}
    return {"question": "Which database should we use for product analytics: PostgreSQL, ClickHouse or BigQuery?",
            "refined_question": "", "criteria": criteria, "mode": "evaluate", "authors": authors, "proposals": props,
            "critiques": crit,
            "ballots": {m: {"ranking": ["A", "B", "C"] if i % 3 else ["B", "A", "C"], "rationale": "Simplest operations for a small team."}
                        for i, m in enumerate(["claude", "chatgpt", "gemini", "kimi"])},
            "tally": _tally([("A", 0.88, 8.5, 3, 4, 2.0, 2, ""), ("B", 0.62, 7.9, 1, 4, 0.0, 1, ""), ("C", 0.0, 5.6, 0, 4, -2.0, 0, "")]),
            "verdict": {"winner": "A", "final_answer": EVAL_ANSWER,
                        "why": "Three of four ballots ranked PostgreSQL first; reviewers valued the lower operational load.",
                        "dissent": ["gemini: ClickHouse will be needed once ingest exceeds 100k rows/s sustained."],
                        "risks": ["Compression settings must be tested on real data before go-live."]},
            "stats": _stats(["claude", "chatgpt", "gemini", "kimi"], 3, 0.02), "total_cost_usd": 0.1032,
            "started_at": "2026-11-08T18:30:00+00:00", "duration_s": 128.0, "attachments": [], "request": {"chair": "claude"},
            "agreement": {"tau": 0.83, "level": "high", "pairs": 6, "ballots": 4}, "chair_proposed": False,
            "coverage": {"min": 4, "max": 4, "options": 3, "ballots": 4}}


def many_members() -> dict[str, Any]:
    """Восемь участников и восемь вариантов: проверка таблиц голосования и матрицы критики."""
    names = list(MODELS)
    labels = "ABCDEFGH"
    authors = dict(zip(labels, names))
    criteria = ["Польза", "Реалистичность", "Риски"]
    themes = ["Подписка на кофе", "Корпоративные заказы", "Ночная смена", "Коворкинг при кофейне",
              "Обжарка на месте", "Доставка через агрегаторы", "Программа лояльности", "Мастер-классы для студентов"]
    props = {l: _proposal(t, f"Идея **{t.lower()}**: запуск за 2 месяца, окупаемость 6–9 месяцев.\n\n"
                             f"- Вложения: {120 + 30 * i} тыс. ₽\n- Ожидаемый рост выручки: {8 + i}%",
                          pros=["Быстрый запуск"], risks=["Нужен отдельный сотрудник"], confidence=0.5 + i * 0.05)
             for i, (l, t) in enumerate(zip(labels, themes))}
    base = {l: 8.2 - i * 0.45 for i, l in enumerate(labels)}
    order = list(labels)
    ballots = {n: {"ranking": [x for x in order[i % 3:] + order[:i % 3] if authors[x] != n], "rationale": "По окупаемости."}
               for i, n in enumerate(names)}
    tally = _tally([(l, round(1 - i / 7, 2), round(base[l], 1), max(0, 3 - i), 7, float(7 - 2 * i), 7 - i, "") for i, l in enumerate(labels)])
    return {"question": "Как увеличить выручку кофейни на 20% за год без расширения площади?", "refined_question": "",
            "criteria": criteria, "mode": "debate", "authors": authors, "proposals": props,
            "critiques": _reviews(authors, criteria, base), "ballots": ballots, "tally": tally,
            "verdict": {"winner": "A", "final_answer": "Запустить **подписку на кофе** и **корпоративные заказы** — вместе они дают "
                                                     "+21–24% выручки за год при вложениях около 270 тыс. ₽.",
                        "why": "Варианты A и B лидируют по окупаемости и дополняют друг друга.", "dissent": [], "risks": []},
            "stats": _stats(names, 4, 0.03), "total_cost_usd": 0.92, "started_at": "2026-11-07T11:00:00+00:00",
            "duration_s": 640.0, "attachments": [], "request": {"chair": "claude"},
            "agreement": {"tau": 0.58, "level": "medium", "pairs": 28, "ballots": 8}, "chair_proposed": True,
            "coverage": {"min": 5, "max": 7, "options": 8, "ballots": 8}}


def followup() -> dict[str, Any]:
    """Уточнение к прошлому заседанию: совет учитывает своё прежнее решение."""
    d = quick_short()
    d["question"] = "А если кофейня будет работать и как коворкинг по вечерам?"
    d["followup_of"] = "20261109-091200-как-назвать-кофейню-у-университета"
    d["thread"] = [{"id": d["followup_of"], "title": "Как назвать кофейню у университета?",
                    "question": "Как назвать кофейню у университета?", "decision": "Назвать кофейню «Конспект»."}]
    d["verdict"]["final_answer"] = ("Оставить **«Конспект»**: название подходит и для вечернего коворкинга. Добавить "
                                    "подзаголовок на вывеске — «кофе и место для учёбы».")
    return d


SAMPLES = {"01-podrobnoe-obsuzhdenie": full_debate, "02-bystryi-otvet": quick_short,
           "03-nichya-i-sboi": tie_deviation, "04-ocenka-variantov-en-tr": evaluate_mixed,
           "05-vosem-uchastnikov": many_members, "06-utochnenie": followup}


def build(out: Path) -> list[Path]:
    """Собирает все отчёты по демо-заседаниям в папку out (PDF, Word, Markdown; полный, итог, решение)."""
    from ..archive import Archive, all_reports
    out.mkdir(parents=True, exist_ok=True)
    arc = Archive(out)
    made = []
    for stem, fn in SAMPLES.items():
        d = fn()
        files = out / f"{stem}.files"
        if d.get("attachments"):
            files.mkdir(exist_ok=True)
            (files / "01-воронка-2026.png").write_bytes(_png())
        (out / f"{stem}.json").write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        built = arc.build(stem, all_reports(), data=d)
        made += [Path(x) for x in built.values() if not str(x).startswith("ошибка")]
        bad = {k: v for k, v in built.items() if str(v).startswith("ошибка")}
        if bad:
            raise RuntimeError(f"{stem}: {bad}")
    return made


if __name__ == "__main__":  # pragma: no cover
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/examples")
    for p in build(target):
        print(p)
