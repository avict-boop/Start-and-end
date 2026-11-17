"""Фейковый клиент: позволяет прогнать заседание без ключа и без затрат (--mock, тесты)."""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from .client import Completion, ProviderError


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest(), 16)


class MockClient:
    def __init__(self, *, fail_models: set[str] | None = None, delay: float = 0.05) -> None:
        self.fail_models = fail_models or set()
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    async def complete(self, model, messages, *, temperature, max_tokens, reasoning_effort=None, meta=None,
                       on_progress=None, web_search=False, **kw) -> Completion:
        meta = meta or {}
        self.calls.append({"model": model, "meta": meta})
        report = on_progress or (lambda ev: None)
        report({"phase": "sent", "attempt": 1, "max_tokens": max_tokens})
        # имитация: модель сначала «думает», потом «пишет» ответ порциями
        think = self.delay * (0.5 + _h(model, str(meta.get("stage"))) % 100 / 100)
        report({"phase": "thinking", "chars": 0})
        demo = [f"(демо) Читаю задание этапа «{meta.get('stage')}»… ", "Сравниваю с критериями и требованиями. ",
                "Взвешиваю, что важнее для ответа, и проверяю, ничего ли не упустил. "]
        for i, t in enumerate(demo):
            await asyncio.sleep(think / len(demo))
            report({"phase": "thinking", "chars": 0, "reasoning_chars": sum(len(x) for x in demo[:i + 1]), "thought": t})
        for i in range(1, 4):
            await asyncio.sleep(self.delay / 3)
            report({"phase": "writing", "chars": 180 * i})
        if model in self.fail_models:
            raise ProviderError(f"{model}: имитация недоступности")
        stage = meta.get("stage")
        who = meta.get("member", model)
        if meta.get("selection"):
            text = str((messages[-1] or {}).get("content") or "") if messages else ""
            out = _selection(str(stage), str(who), meta, text)
            return Completion(text=json.dumps(out, ensure_ascii=False), prompt_tokens=900, completion_tokens=250,
                              cost=0.001, model=model)
        if meta.get("research"):
            out = _research(stage, who, meta)
            text = str((messages[-1] or {}).get("content") or "") if messages else ""
            if stage == "framing":  # демо: объекты задания — строки нумерованной таблицы вопроса
                from .splitter import heuristic_objects
                out["objects"] = heuristic_objects(text)
            if stage == "split":  # демо: председатель делит объекты на части по порядку
                objs = [ln[2:].strip() for ln in text.split("Use exactly these objects")[-1].splitlines() if ln.startswith("- ")]
                per = 10
                out = {"objects": objs, "facts_per_object": 1, "one_session": False, "reason": "демо: много объектов",
                       "groups": [{"title": f"Пакет {i // per + 1}", "objects": objs[i:i + per]} for i in range(0, len(objs), per)]}
            return Completion(text=json.dumps(out, ensure_ascii=False), prompt_tokens=800, completion_tokens=300,
                              cost=0.001, model=model,
                              citations=["https://example.com/stat", "https://example.org/report"] if stage == "arbitration" else [])
        if stage == "framing":
            out = {"refined_question": "Уточнённый вопрос (mock)", "criteria": ["Польза", "Стоимость", "Риски"],
                   "requirements": ["Ответ на русском языке", "Привести таблицу с цифрами"]}
        elif stage in ("proposal", "revision"):
            out = {
                "title": f"Вариант {who}" + (" (доработан)" if stage == "revision" else ""),
                "summary": (f"**Суть:** вариант участника {who} — сбалансированный подход.\n"
                            "### План\n"
                            "1. Исследовать рынок и конкурентов\n"
                            "2. Сформировать гипотезы:\n"
                            "  - быстрый запуск MVP\n"
                            "  - проверка спроса через *лендинг*\n"
                            "3. Посчитать экономику\n\n"
                            "| Этап | Срок | Бюджет |\n|---|---|---|\n| MVP | 2 недели | 150 000 ₽ |\n"
                            "| Запуск | 1 месяц | 400 000 ₽ |\n\n"
                            "Пример конфигурации: `stack = Django + PostgreSQL`."),
                "reasoning": "Потому что это сбалансированный вариант.",
                "pros": ["быстро", "дёшево"],
                "risks": ["зависимость от внешних факторов"],
                "confidence": 0.7,
            }
            if stage == "revision":
                out["changes"] = "Учёл замечания по рискам."
                out["comparison"] = "Сильнее в плане запуска; у других лучше проработан маркетинг (демо)."
                out["borrowed"] = ["лендинг для проверки спроса (из другого варианта)"]
                out["not_accepted"] = ["отказаться от MVP — без MVP не проверить спрос (демо)"]
                out["position"] = ["kept", "improved", "switched"][_h(who) % 3]
        elif stage == "critique":
            crit = meta["criteria"]
            out = {
                "reviews": [
                    {
                        "label": l,
                        "scores": {c: 3 + _h(who, l, c) % 8 for c in crit},
                        "strengths": [f"сильная сторона {l}"],
                        "weaknesses": [f"слабое место {l}"],
                        "keep": ([f"проверить спрос лендингом до разработки (идея из {l})"] if _h(who, l) % 3 == 0 else
                                 [f"договориться о пилоте с первыми клиентами заранее (идея из {l})"] if _h(who, l) % 3 == 1 else []),
                    }
                    for l in meta["labels"]
                ]
            }
        elif stage == "transcribe":
            text = f"Как назвать кофейню рядом с университетом? (демо-расшифровка записи {meta.get('seconds', 0):.0f} с)"
            return Completion(text=text, prompt_tokens=100, completion_tokens=30, cost=0.0002, model=model)
        elif stage == "factcheck":
            label = meta.get("label", "?")
            out = {"claims": [
                {"claim": f"Средний чек кофейни у вуза — 250–350 ₽ (вариант {label})", "status": "confirmed",
                 "note": "Совпадает с обзорами рынка (демо).", "sources": ["https://example.com/market"]},
                {"claim": "Аренда у кампуса дешевле, чем в центре, в 3 раза", "status": "refuted",
                 "note": "По данным объявлений — примерно в 1,5 раза (демо).", "sources": ["https://example.com/rent"]},
                {"claim": "Студенты тратят на кофе больше 5 000 ₽ в месяц", "status": "unclear",
                 "note": "Надёжных данных не найдено (демо).", "sources": []},
            ], "summary": "Фактическая база в целом надёжна, одно утверждение завышено (демо)."}
        elif stage == "describe":
            out = {"images": [{"name": n, "description": f"Описание (демо): на картинке «{n}» таблица с цифрами."}
                              for n in meta.get("images", [])]}
        elif stage == "vote":
            ranking = sorted(meta["labels"], key=lambda l: _h(l) % 100)  # общий «вкус» у всех → консенсус
            out = {"ranking": ranking, "rationale": f"{ranking[0]} лучше всего соответствует критериям."}
        elif stage == "verdict":
            out = {
                "winner": meta["leader"],
                "final_answer": (f"**Итог (демо):** принять вариант {meta['leader']} с доработками.\n\n"
                                 "## Что делать\n- Начать с MVP за 2 недели\n- Параллельно проверить спрос\n"
                                 "  - лендинг + реклама на 30 000 ₽\n- Через месяц — решение о масштабировании\n\n"
                                 "| Метрика | Цель |\n|---|---|\n| Конверсия | ≥ 3% |\n| CAC | ≤ 1 500 ₽ |"),
                "why": "Набрал больше всего голосов и лучшие оценки.",
                "dissent": ["Часть участников предпочла другой вариант."],
                "risks": ["Нужно проверить допущения на реальных данных."],
                "ideas": [{"id": i, "decision": "included" if n % 2 == 0 else "rejected",
                           "reason": "добавлено в раздел «Что делать» (демо)" if n % 2 == 0 else
                           "противоречит выбранному подходу: бюджет уже распределён (демо)"}
                          for n, i in enumerate(meta.get("ideas") or [])],
            }
        else:
            out = {}
        text = json.dumps(out, ensure_ascii=False)
        return Completion(text=text, prompt_tokens=500, completion_tokens=200, cost=0.001, model=model)

    async def aclose(self) -> None:
        pass


def _research(stage: str, who: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Демо-ответы режима «Исследование»."""
    if stage == "semantic":
        return {"items": [{"item_id": i, "status": "supported"} for i in meta.get("items") or []]}
    if stage == "split_audit":  # демо: вторая проверка пропусков не находит
        return {"missing": []}
    if stage == "conflicts":  # демо: противоречий между частями нет
        return {"conflicts": []}
    if stage == "framing":
        return {"refined_question": "Каков рынок кофеен у вузов и стоит ли открывать точку? (демо)",
                "criteria": ["Полнота покрытия", "Качество доказательств", "Калибровка неопределённости", "Практическая ценность"],
                "requirements": ["Ответ на русском языке"], "as_of": "2026-11", "scope": "Россия, города-миллионники, 2024–2026, ₽",
                "subquestions": ["Размер и динамика спроса", "Экономика одной точки", "Риски и что может пойти не так"],
                "lenses": ["рынок и первичные данные", "экономика и расчёты", "скептик: как это может провалиться"]}
    if stage in ("proposal", "revision"):
        n = _h(who) % 3
        out = {
            "title": f"Исследование {who}" + (" (доработано)" if stage == "revision" else ""),
            "summary": ("### Q1. Спрос\nСредний чек у вуза — 250–350 ₽ [MEM]; поток — 400–600 гостей в день [CALC].\n"
                        "### Q2. Экономика\nОкупаемость 14–20 месяцев при аренде 150 тыс. ₽ [CALC]"
                        + (" [E1]" if stage == "revision" else "") + ".\n"
                        "### Q3. Риски\nЛетний спад спроса на 40% [MEM]; конкуренция сетей."),
            "reasoning": "Опора на типичные показатели отрасли (демо).",
            "claims": [
                {"text": "Средний чек кофейни у вуза — 250–350 ₽", "basis": "memory", "scope": "Россия, 2025", "volatility": "changes_fast"},
                {"text": f"Летом спрос падает на {30 + 10 * n}%", "basis": "memory", "scope": "вузы, лето", "volatility": "stable"},
                {"text": "Окупаемость 14–20 месяцев", "basis": "calc", "scope": "одна точка", "volatility": "unknown"},
            ],
            "pros": ["покрыты все подвопросы"], "risks": ["цифры из памяти"],
            "coverage": [{"subquestion_id": f"Q{i}", "status": "covered"} for i in (1, 2, 3)],
            "unknowns": ["нет открытых данных о выручке точек у вузов"],
            "what_would_change": ["если аренда выше 250 тыс. ₽, окупаемость уходит за 2 года"],
            "confidence": 0.6,
        }
        if stage == "revision":
            out.update({"changes": "Исправлена цифра чека по реестру [E1].", "comparison": "у других сильнее риски (демо)",
                        "borrowed": ["летний спад (из другого варианта)"], "not_accepted": [], "position": "improved",
                        "new_after_ledger": []})
        return out
    if stage == "critique":
        claims = meta.get("claims") or []
        return {"reviews": [{"label": l, "scores": {c: 3 + _h(who, l, c) % 8 for c in meta["criteria"]},
                             "strengths": [f"сильная сторона {l}"], "weaknesses": [f"слабое место {l}"],
                             "keep": [f"учитывать летний спад спроса (идея из {l})"] if _h(who, l) % 2 else [],
                             "disputed_claims": [{"claim_id": c, "reason": "цифра из памяти"} for c in claims
                                                 if c.startswith(l) and c.endswith("C1")],
                             "missing_subquestions": []} for l in meta["labels"]]}
    if stage == "arbitration":
        ids = meta.get("claims") or []
        st = ["confirmed", "contested_in_sources", "unclear"]
        return {"checked_claims": [
            {"claim_id": c, "status": st[i % 3] if meta.get("mission") == 1 else ("confirmed" if i % 3 == 0 else "unclear"),
             "note": "Данные отраслевого обзора (демо).",
             "sources": [{"url": "https://example.com/stat", "title": "Обзор рынка кофеен (демо)", "publisher": "Example",
                          "date": "2026-03-01", "source_type": "primary", "material_seen": "page", "relation": "supports"}]
             + ([{"url": "https://example.org/report", "title": "Альтернативная оценка (демо)", "relation": "contradicts"}]
                if i % 3 == 1 else [])}
            for i, c in enumerate(ids)],
            "gaps": [{"question": "выручка конкретных точек", "searched_for": "отчётность", "why_unresolved": "нет открытых данных"}]}
    if stage == "vote":
        labels = sorted(meta["labels"], key=lambda l: _h(l) % 100)
        return {"ranking": labels, "rationale": f"{labels[0]} лучше подкреплён источниками (демо).",
                "keep_from_others": [{"label": labels[-1], "idea": "учесть летний спад спроса (демо)"}],
                "best_by_subquestion": {f"Q{i}": labels[(i - 1) % len(labels)] for i in range(1, (meta.get("subquestions") or 3) + 1)}}
    if stage == "verdict":
        ok = meta.get("evidence_ok") or []
        n = meta.get("subquestions") or 3
        return {"winner": meta["leader"], "section_backbone": {f"Q{i}": meta["leader"] for i in range(1, n + 1)},
                "final_answer": ("## Итог исследования (демо)\n\n### Q1. Спрос\nСредний чек 250–350 ₽ [E1].\n\n"
                                 "### Q2. Экономика\nОкупаемость 14–20 месяцев [CALC].\n\n### Q3. Риски\nЛетний спад [MEM].\n\n"
                                 "**Источники:** [E1] https://example.com/stat"),
                "why": "Синтез по подвопросам: каркас — варианты-лидеры разделов (демо).",
                "decision_card": {"summary_decision": "Открывать стоит, если подтвердятся аренда и поток гостей в часы пик (демо).",
                                  "known_item_ids": ["K1"] if ok else [], "key_unknown_ids": ["U1"],
                                  "next_steps": ["Узнать аренду у трёх ближайших вузов", "Посчитать поток в часы пик"]},
                "known": [{"statement": "Средний чек кофейни у вуза — 250–350 ₽", "evidence_ids": ok[:1], "scope": "Россия, 2025",
                           "claim_ids": [(meta.get("claims_ok") or {}).get(ok[0], "")] if ok else [],
                           "volatility": "changes_fast"}] if ok else [],
                "contested": [{"issue": "Насколько падает спрос летом", "positions": ["на 30%", "на 50%"],
                               "evidence_ids": ok[:1], "why_they_differ": "разные выборки", "what_would_resolve": "данные чеков за год"}],
                "unknown": [{"gap": "выручка конкретных точек у вузов", "reason": "no_public_data", "how_to_check": "запросить у арендодателя"}],
                "coverage_audit": [{"subquestion_id": f"Q{i}", "status": "covered", "note": f"раздел Q{i}"} for i in range(1, n + 1)],
                "what_would_change": ["аренда выше 250 тыс. ₽"], "next_questions": ["Сколько стоит аренда у трёх ближайших вузов?"],
                "dissent": ["один участник считает риск сезонности недооценённым"], "risks": ["цифры спроса не проверены на месте"],
                "deviation": "",
                "ideas": [{"id": i, "decision": "included", "reason": "учтено в разделе Q3 (демо)"} for i in meta.get("ideas") or []]}
    return {}


# ---------------------------------------------------------------------------------------------- режим «Отбор» (демо)

def demo_page(url: str) -> str:
    """Текст демо-страницы завода N (адрес plantN.example.com): на нём лежат цитаты, которые «находят» модели."""
    import re as _re
    if "expo.example.com" in url:  # демо-список участников выставки: заводы 1–10 и один дистрибьютор
        return " ".join(f"stand a{i:02d} demo plant {i} co., ltd. — softgel contract manufacturing (oem/odm), china"
                        for i in range(1, 11)) + " stand b01 demo trading co. — distributor"
    m = _re.search(r"plant(\d+)", url)
    if not m:
        return ""
    i = int(m.group(1))
    forms = "capsules, tablets" + (", powders" if i % 2 else "")
    return (f"demo plant {i} — contract manufacturer (oem/odm) of dietary supplements. annual capacity 年产软胶囊{10 * i}亿粒. "
            f"forms: {forms}. gmp certificate valid until 202{6 + i % 3}-0{i % 9 + 1}-15. "
            f"demo plant {i} annual capacity was expanded in 2024 to {5 * i}亿粒.")


async def mock_pages(urls: list[str], *, texts: bool = False, **kw: Any) -> dict[str, dict[str, Any]]:
    """Скачивание страниц в демо: демо-заводы — свои тексты, остальное «не открылось»."""
    out = {}
    for u in urls:
        t = demo_page(u)
        out[u] = {"status": "ok", "code": 200, "text": t} if t else {"status": "error", "code": None}
    return out


def _named(text: str) -> list[str]:
    """Демо-председатель: объекты из списка «Objects named in the task» (строки «- …» после заголовка)."""
    if "Objects named in the task" not in text:
        return []
    tail = text.split("Objects named in the task", 1)[1].split("\n\n", 1)[0]
    return [ln[2:].strip() for ln in tail.splitlines() if ln.startswith("- ") and ln[2:].strip()]


def _selection(stage: str, who: str, meta: dict[str, Any], text: str) -> dict[str, Any]:
    import re as _re
    if stage == "frame":
        from .splitter import heuristic_objects
        return {"refined_question": "Отбор контрактных производителей (демо)", "object_kind": "контрактный производитель",
                "inclusion": ["сам производит", "работает по контракту (OEM/ODM)"], "exclusion": ["перепродавец"],
                "attributes": [
                    {"name": "Производитель по контракту", "kind": "bool", "weight": 0, "better": "yes", "screen": True,
                     "knockout": "нет", "keywords": ["oem", "odm", "contract manufacturer"]},
                    {"name": "Мощность софтгелей", "kind": "number", "unit": "млн капсул/год", "weight": 5,
                     "better": "high", "keywords": ["产能", "capacity", "亿粒"], "evidence": "отчёт или сайт"},
                    {"name": "Формы выпуска", "kind": "list", "weight": 3, "wanted": ["capsules", "tablets", "powders"],
                     "keywords": ["forms"], "screen": True, "knockout": "нет капсул"},
                    {"name": "Сертификат GMP действует до", "kind": "date", "weight": 2, "better": "newer",
                     "keywords": ["gmp", "valid until"]},
                    {"name": "Примечание", "kind": "text", "weight": 0}],
                "angles": ["демо: участники выставки", "демо: биржевые отчёты", "демо: реестр сертификатов"],
                "objects": _named(text), "shortlist": 0, "top_n": 3}
    if stage == "lists":
        return {"lists": [{"url": "https://expo.example.com/exhibitors", "title": "Демо-выставка: участники",
                           "kind": "exhibitors", "size": "11"}]}
    if stage == "list_read":
        ids = sorted({int(x) for x in _re.findall(r"demo plant (\d+)", text)})
        return {"items": [{"name": f"Demo Plant {i}", "quote": f"stand a{i:02d} demo plant {i} co., ltd.",
                           "location": "Китай", "why": "участник выставки, OEM"} for i in ids]}
    if stage == "angles":
        return {"angles": ["демо: поиск на китайском", "демо: отраслевой рейтинг"]}
    if stage == "discover":
        h = _h(str(meta.get("angle", "")), who)
        ids = sorted({(h >> (k * 3)) % 8 + 1 for k in range(4)})
        return {"items": [{"name": f"Demo Plant {i}", "local_name": f"演示工厂{i}", "website": f"https://plant{i}.example.com",
                           "url": f"https://plant{i}.example.com/about",
                           "quote": f"demo plant {i} — contract manufacturer (oem/odm)", "location": "Китай"} for i in ids]}
    if stage == "sites":  # демо: официальный сайт демо-завода
        names = _re.findall(r"^- (.+?)(?: \(| — |$)", text.split("Companies:")[-1], _re.M)
        return {"items": [{"name": n, "website": f"https://plant{m.group(1)}.example.com"}
                          for n in names if (m := _re.search(r"(\d+)", n))]}
    if stage == "screen_confirm":
        return {"items": [{"n": int(n), "exclude": True, "why": "демо"} for n in _re.findall(r"(?m)^(\d+)\. ", text)]}
    if stage == "screen":
        names = _re.findall(r"^- (.+?)(?: \(| — site|$)", text.split("Objects:")[-1], _re.M)
        rows = []
        for n in names:
            i = int((_re.search(r"(\d+)", n) or [0, 0])[1] or 0) if _re.search(r"(\d+)", n) else 0
            bad = i == 8
            rows.append({"object": n, "checks": [
                {"attr": "A1", "verdict": "fail" if bad else "pass", "value": "перепродавец" if bad else "да",
                 "url": f"https://plant{i}.example.com/about", "quote": f"demo plant {i}"},
                {"attr": "A3", "verdict": "pass", "value": "capsules"}]})
        return {"rows": rows}
    m = _re.search(r"Object: (.+?) \(also", text)
    name = m.group(1) if m else "Demo Plant 1"
    i = int((_re.search(r"(\d+)", name) or _re.search(r"(1)", "1")).group(1))
    site = f"https://plant{i}.example.com"
    if stage == "sources":
        return {"website": site, "sources": [{"url": f"{site}/annual.pdf", "title": "Годовой отчёт", "kind": "document"},
                                             {"url": f"{site}/about", "title": "О компании", "kind": "official"}],
                "cells": [{"attr": "A2", "value": str(500 * i), "unit": "млн капсул/год", "url": f"{site}/about",
                           "quote": f"annual capacity was expanded in 2024 to {5 * i}亿粒"} if i == 3 else  # демо-спор
                          {"attr": "A2", "value": str(1000 * i), "unit": "млн капсул/год", "url": f"{site}/about",
                           "quote": f"年产软胶囊{10 * i}亿粒"}]}
    if stage == "fill":
        forms = "capsules, tablets" + (", powders" if i % 2 else "")
        cells = [{"attr": "A1", "value": "да", "url": f"{site}/about", "quote": f"demo plant {i} — contract manufacturer"},
                 {"attr": "A2", "value": str(1000 * i), "unit": "млн капсул/год", "url": f"{site}/about",
                  "quote": f"年产软胶囊{10 * i}亿粒", "note": "мощность, не выпуск"},
                 {"attr": "A3", "value": forms, "url": f"{site}/about", "quote": f"forms: {forms}"},
                 {"attr": "A4", "value": f"202{6 + i % 3}-0{i % 9 + 1}-15", "url": f"{site}/about",
                  "quote": f"gmp certificate valid until 202{6 + i % 3}-0{i % 9 + 1}-15"}]
        return {"cells": cells}
    if stage == "dispute":
        keys = _re.findall(r"^(O\d+:A\d+) — ", text, _re.M) or _re.findall(r"^(A\d+) «", text, _re.M)
        return {"items": [{"key": k, "choice": "a", "reason": "цитата на странице отчёта (демо)"} for k in keys]}
    if stage == "conclusion":
        return {"summary": "**Демо-вывод.** Первым идёт завод с наибольшей подтверждённой мощностью; у остальных "
                           "часть ячеек не подтверждена.", "next_steps": ["Запросить сертификаты с номерами (демо)"],
                "risks": ["Демо-данные"]}
    return {}
