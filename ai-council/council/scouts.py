"""«Разведчики» для поиска кандидатов: поисковики разных компаний через их дешёвые модели.

«Слепые зоны» у поисковиков разные (Google, Bing у OpenAI, поиск Anthropic, xAI, Perplexity), а дорого обходится не
поиск, а чтение найденного большой моделью. Поэтому каждое направление прогоняется через все поисковики, но читают
найденное младшие модели тех же компаний (Gemini Flash, GPT mini, Claude Haiku, Grok mini) — в 5–20 раз дешевле, а
Perplexity (Sonar, Sonar Pro) ищет встроенно: найденное вообще не идёт во вход. Большие модели совета остаются для
рамки, споров и вывода.
"""
from __future__ import annotations

import re
from typing import Any

# компания → (имя на экране, слова в id младших моделей)
PROVIDERS: dict[str, tuple[str, tuple[str, ...]]] = {
    "google": ("Google", ("flash",)),
    "openai": ("OpenAI", ("mini",)),
    "anthropic": ("Anthropic", ("haiku",)),
    "x-ai": ("xAI", ("mini", "fast")),
}
# не берём: картинки и звук, «облегчённые» (lite, nano — слабее в поиске), особые поисковые и кодовые модели, бесплатные
SKIP = re.compile(r"image|audio|tts|realtime|embed|transcri|vision-only|nano|lite|search|:free|:beta|deep-research|codex|code")
MAX_PROMPT_PRICE = 1.5e-6  # дороже $1,5 за 1M входа — уже не «младшая» модель


def _text_model(m: dict[str, Any]) -> bool:
    arch = m.get("architecture") or {}
    outs = arch.get("output_modalities") or ["text"]
    ins = arch.get("input_modalities") or ["text"]
    return "text" in outs and "text" in ins and outs == ["text"]


def pick_scouts(models: list[dict[str, Any]] | None, providers: tuple[str, ...] = tuple(PROVIDERS)) -> dict[str, str]:
    """Компания → id самой новой младшей модели этой компании в каталоге OpenRouter (дешёвой, текстовой, с длинным
    контекстом). Нет каталога или подходящей модели — компании нет в ответе."""
    out: dict[str, str] = {}
    for p in providers:
        hints = PROVIDERS.get(p, ("", ()))[1]
        best: tuple[float, float, str] | None = None
        for m in models or []:
            mid = str(m.get("id") or "")
            low = mid.lower()
            if not low.startswith(f"{p}/") or SKIP.search(low) or not any(h in low for h in hints):
                continue
            if not _text_model(m) or int(m.get("context_length") or 128_000) < 100_000:
                continue
            params = m.get("supported_parameters")
            if params and "tools" not in params:
                continue
            pr = m.get("pricing") or {}
            try:
                price = float(pr.get("prompt") or 0)
            except (TypeError, ValueError):
                continue
            if not 0 < price <= MAX_PROMPT_PRICE:
                continue
            key = (float(m.get("created") or 0), -price, mid)  # самая новая; при равенстве — дешевле
            if best is None or key > best:
                best = key
        if best:
            out[p] = best[2]
    return out


def perplexity_pair(search_model: str, models: list[dict[str, Any]] | None) -> list[str]:
    """Обе модели Perplexity для поиска: модель поиска «Отбора» и вторая (Sonar ↔ Sonar Pro), если она есть."""
    ids = {str(m.get("id")) for m in models or []}
    other = "perplexity/sonar" if search_model != "perplexity/sonar" else "perplexity/sonar-pro"
    return [other] if search_model.startswith("perplexity/") and (not ids or other in ids) else []


def model_title(model: str) -> str:
    """«google/gemini-3-flash» → «Gemini 3 Flash»."""
    tail = (model or "").split("/")[-1].split(":")[0]
    return " ".join(w if any(ch.isdigit() for ch in w) and len(w) > 3 else w.capitalize()
                    for w in tail.replace("_", "-").split("-") if w) or model


def scout_name(model: str) -> str:
    """Имя участника-разведчика (ключ в статистике и журнале): «поиск Google», «поиск Perplexity»."""
    p = model.split("/")[0]
    return f"поиск {PROVIDERS.get(p, (p.capitalize(),))[0] if p != 'perplexity' else 'Perplexity'}"
