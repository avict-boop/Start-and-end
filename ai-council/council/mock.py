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

    async def complete(self, model, messages, *, temperature, max_tokens, meta=None) -> Completion:
        meta = meta or {}
        self.calls.append({"model": model, "meta": meta})
        await asyncio.sleep(self.delay)
        if model in self.fail_models:
            raise ProviderError(f"{model}: имитация недоступности")
        stage = meta.get("stage")
        who = meta.get("member", model)
        if stage == "framing":
            out = {"refined_question": "Уточнённый вопрос (mock)", "criteria": ["Польза", "Стоимость", "Риски"]}
        elif stage in ("proposal", "revision"):
            out = {
                "title": f"Вариант {who}" + (" (доработан)" if stage == "revision" else ""),
                "summary": f"Подробное предложение участника {who}.",
                "reasoning": "Потому что это сбалансированный вариант.",
                "pros": ["быстро", "дёшево"],
                "risks": ["зависимость от внешних факторов"],
                "confidence": 0.7,
            }
            if stage == "revision":
                out["changes"] = "Учёл замечания по рискам."
        elif stage == "critique":
            crit = meta["criteria"]
            out = {
                "reviews": [
                    {
                        "label": l,
                        "scores": {c: 3 + _h(who, l, c) % 8 for c in crit},
                        "strengths": [f"сильная сторона {l}"],
                        "weaknesses": [f"слабое место {l}"],
                    }
                    for l in meta["labels"]
                ]
            }
        elif stage == "vote":
            ranking = sorted(meta["labels"], key=lambda l: _h(l) % 100)  # общий «вкус» у всех → консенсус
            out = {"ranking": ranking, "rationale": f"{ranking[0]} лучше всего соответствует критериям."}
        elif stage == "verdict":
            out = {
                "winner": meta["leader"],
                "final_answer": f"**Итог (mock):** принять вариант {meta['leader']} с доработками.",
                "why": "Набрал больше всего голосов и лучшие оценки.",
                "dissent": ["Часть участников предпочла другой вариант."],
                "risks": ["Нужно проверить допущения на реальных данных."],
            }
        else:
            out = {}
        text = json.dumps(out, ensure_ascii=False)
        return Completion(text=text, prompt_tokens=500, completion_tokens=200, cost=0.001, model=model)

    async def aclose(self) -> None:
        pass
