"""Чтение и сохранение настроек совета из веб-интерфейса.

config.yaml редактируется «на месте»: комментарии, порядок и форматирование файла сохраняются
(ruamel.yaml), меняются только значения. Перед записью настройки проверяются той же схемой,
что и при запуске заседания, а прежняя версия файла сохраняется в резервную копию.
"""
from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from .config import AdvisorConfig, BrandingConfig, CouncilConfig, FactCheckConfig, MemberConfig, ResearchConfig

TOP_KEYS = ("language", "mode", "debate_rounds", "min_quorum", "max_cost_usd", "temperature", "max_tokens",
            "reasoning_effort", "chair", "chair_proposes", "chair_follows_vote", "devil_advocate", "transcribe_model",
            "compact_proposals", "compact_words", "max_concurrency", "judge_temperature", "verdict_max_tokens",
            "max_requirements", "quick_keep", "evaluate_allow_none", "budget_reserve", "reserve_output_share", "answer_size", "quorum_large", "reports_memory_mb",
            "pilot_solo_model")
SECTIONS = {"branding": BrandingConfig, "fact_check": FactCheckConfig, "research": ResearchConfig,
            "advisor": AdvisorConfig}
MEMBER_KEYS = ("name", "model", "enabled", "reasoning_effort", "temperature", "max_tokens", "web_search")
MAX_BACKUPS = 20


class SettingsError(ValueError):
    pass


def _yaml() -> YAML:
    y = YAML()  # round-trip: сохраняет комментарии
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def editable(cfg: CouncilConfig) -> dict[str, Any]:
    """Настройки для формы в браузере."""
    d = cfg.model_dump()
    return {**{k: d[k] for k in TOP_KEYS}, **{k: d[k] for k in SECTIONS},
            "members": [{k: m.get(k) for k in MEMBER_KEYS} for m in d["members"]]}


def validate(data: dict[str, Any], base: CouncilConfig) -> CouncilConfig:
    """Проверка новых настроек: берём текущий конфиг и накладываем изменения."""
    merged = base.model_dump()
    for k in TOP_KEYS:
        if k in data:
            merged[k] = data[k]
    for k in SECTIONS:
        if isinstance(data.get(k), dict):
            merged[k] = {**merged[k], **data[k]}
    if "members" in data:
        members = []
        for m in data["members"]:
            m = {k: v for k, v in m.items() if k in MEMBER_KEYS}
            m["name"] = str(m.get("name") or "").strip()
            m["model"] = str(m.get("model") or "").strip()
            if not m["name"] or not m["model"]:
                raise SettingsError("у каждого участника должны быть имя и модель")
            if any(ch.isspace() for ch in m["name"]):
                raise SettingsError(f"имя участника «{m['name']}» не должно содержать пробелов")
            for opt in ("reasoning_effort", "temperature", "max_tokens"):
                if m.get(opt) in ("", None):
                    m[opt] = None
            members.append(m)
        merged["members"] = members
    if merged.get("devil_advocate") == "":
        merged["devil_advocate"] = None
    try:
        return CouncilConfig.model_validate(merged)
    except ValidationError as e:
        msgs = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            msgs.append((f"{loc}: " if loc else "") + err["msg"].removeprefix("Value error, "))
        raise SettingsError("; ".join(msgs)) from e


def _set(node: CommentedMap, key: str, value: Any, default: Any) -> None:
    """Меняет значение, сохраняя комментарий в строке. Ключ, которого не было, добавляется только
    если значение отличается от значения по умолчанию (чтобы не засорять файл)."""
    if key in node:
        if node[key] != value:
            node[key] = value
    elif value != default:
        node[key] = value


def apply(doc: CommentedMap, cfg: CouncilConfig) -> None:
    """Переносит проверенные настройки в документ YAML, не трогая остальное."""
    new = cfg.model_dump()
    defaults = CouncilConfig.model_construct().__class__.model_fields
    for k in TOP_KEYS:
        default = defaults[k].default if defaults[k].default is not None or k == "devil_advocate" else None
        _set(doc, k, new[k], default)
    for k, model in SECTIONS.items():
        section = doc.get(k)
        changed = {f: v for f, v in new[k].items() if v != model.model_fields[f].default}
        if section is None:
            if changed:
                doc[k] = CommentedMap(changed)
            continue
        for f, v in new[k].items():
            _set(section, f, v, model.model_fields[f].default)

    old = {m["name"]: m for m in (doc.get("members") or []) if isinstance(m, dict)}
    seq = doc.get("members")
    items = []
    for m in new["members"]:
        node = old.get(m["name"]) or CommentedMap()
        for f in MEMBER_KEYS:
            v = m.get(f)
            default = MemberConfig.model_fields[f].default if f not in ("name", "model") else None
            if f in ("name", "model"):
                node[f] = v
            elif v is None or v == default:
                if f in node and f != "enabled":
                    del node[f]
                elif f == "enabled" and f in node:
                    node[f] = v
            else:
                node[f] = v
        items.append(node)
    if seq is None:
        doc["members"] = items
    else:
        seq[:] = items


def save(path: Path, data: dict[str, Any], *, backups: Path | None = None) -> CouncilConfig:
    """Проверяет и записывает настройки. Возвращает новый конфиг."""
    path = Path(path)
    yaml = _yaml()
    text = path.read_text(encoding="utf-8")
    doc = yaml.load(text) or CommentedMap()
    base = CouncilConfig.model_validate(_plain(doc))
    cfg = validate(data, base)
    apply(doc, cfg)
    out = io.StringIO()
    yaml.dump(doc, out)
    new_text = out.getvalue()
    CouncilConfig.model_validate(_plain(yaml.load(new_text)))  # итоговый файл должен читаться
    if backups is not None:
        backups.mkdir(parents=True, exist_ok=True)
        (backups / f"config-{time.strftime('%Y%m%d-%H%M%S')}.yaml").write_text(text, encoding="utf-8")
        for old in sorted(backups.glob("config-*.yaml"))[:-MAX_BACKUPS]:
            old.unlink(missing_ok=True)
    try:
        # запись поверх (не через переименование): файл подключён в контейнер отдельным томом
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
    except PermissionError as e:
        raise SettingsError(
            "нет прав на запись config.yaml. На сервере один раз выполните: "
            "sudo bash /opt/ai-council/ai-council/deploy/web-control.sh install") from e
    except OSError as e:
        raise SettingsError(f"не удалось записать config.yaml: {e}") from e
    return cfg


def _plain(x: Any) -> Any:
    """CommentedMap/CommentedSeq -> обычные dict/list для pydantic."""
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_plain(v) for v in x]
    return x
