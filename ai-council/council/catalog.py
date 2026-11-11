"""Каталог моделей OpenRouter: свежие модели и подсказки, чем обновить участников совета."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .config import CouncilConfig

# не участники совета: эмбеддинги, генерация картинок, озвучка, модерация
SKIP = re.compile(r"embed|image-gen|tts|whisper|moderation|guard|rerank|-audio-preview", re.I)


def created(m: dict[str, Any]) -> int:
    try:
        return int(m.get("created") or 0)
    except (TypeError, ValueError):
        return 0


def date(m: dict[str, Any]) -> str:
    ts = created(m)
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "—"


# варианты, которые не подходят совету: :batch — пакетная обработка с ответом позже, :free — с жёсткими лимитами
UNSUITABLE_SUFFIX = (":batch", ":free")


def is_text_model(m: dict[str, Any]) -> bool:
    arch = m.get("architecture") or {}
    out = arch.get("output_modalities") or ["text"]
    mid = m.get("id", "")
    return "text" in out and not SKIP.search(mid) and not mid.endswith(UNSUITABLE_SUFFIX)


def modalities(m: dict[str, Any]) -> set[str]:
    return set((m.get("architecture") or {}).get("input_modalities") or [])


LIGHT_WORDS = {"flash", "mini", "nano", "lite", "haiku", "small", "air", "tiny"}


def is_light(model_id: str) -> bool:
    """Облегчённая версия по слову в названии: «gemini-4-flash», «gpt-6.1-mini». По целым словам —
    иначе «gemini» содержит «mini» и любая Gemini считалась бы облегчённой."""
    name = model_id.split("/", 1)[-1].split(":", 1)[0].lower()
    return any(w in LIGHT_WORDS or w.rstrip("0123456789") in LIGHT_WORDS for w in re.split(r"[-_.\s]+", name))


def provider(model_id: str) -> str:
    return model_id.split("/", 1)[0]


def family(model_id: str) -> str:
    """Семейство без версий и дат: anthropic/claude-sonnet-4.5 -> anthropic/claude-sonnet,
    google/gemini-2.5-pro -> google/gemini-pro, openai/gpt-5 -> openai/gpt."""
    prov, _, name = model_id.partition("/")
    name = name.split(":", 1)[0]
    parts = [p for p in re.split(r"[-_.]", name) if p and not re.fullmatch(r"v?\d+[a-z]?|\d{4,}|preview|latest|exp", p)]
    parts = [re.sub(r"\d+$", "", p) for p in parts]
    return f"{prov}/{'-'.join(p for p in parts if p)}"


def sort_newest(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(models, key=lambda m: (-created(m), m.get("id", "")))


def upgrades(cfg: CouncilConfig, models: list[dict[str, Any]], per_provider: int = 3) -> list[dict[str, Any]]:
    """Для каждого участника: его модель, более новые модели того же семейства и самые свежие у того же разработчика."""
    by_id = {m["id"]: m for m in models if m.get("id")}
    text = [m for m in models if m.get("id") and is_text_model(m)]
    out = []
    for member in cfg.members:
        cur = by_id.get(member.model)
        cur_ts = created(cur) if cur else 0
        fam = family(member.model)
        same_family = [m for m in sort_newest(text)
                       if family(m["id"]) == fam and m["id"] != member.model and created(m) > cur_ts]
        newest = [m for m in sort_newest(text) if provider(m["id"]) == provider(member.model)
                  and m["id"] != member.model and created(m) > cur_ts and m not in same_family][:per_provider]
        out.append({"name": member.name, "enabled": member.enabled, "model": member.model,
                    "found": cur is not None, "current": cur, "same_family": same_family[:per_provider],
                    "newest": newest})
    return out
