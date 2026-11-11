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
                       on_progress=None) -> Completion:
        meta = meta or {}
        self.calls.append({"model": model, "meta": meta})
        report = on_progress or (lambda ev: None)
        report({"phase": "sent", "attempt": 1, "max_tokens": max_tokens})
        # имитация: модель сначала «думает», потом «пишет» ответ порциями
        think = self.delay * (0.5 + _h(model, str(meta.get("stage"))) % 100 / 100)
        report({"phase": "thinking", "chars": 0})
        await asyncio.sleep(think)
        for i in range(1, 4):
            await asyncio.sleep(self.delay / 3)
            report({"phase": "writing", "chars": 180 * i})
        if model in self.fail_models:
            raise ProviderError(f"{model}: имитация недоступности")
        stage = meta.get("stage")
        who = meta.get("member", model)
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
