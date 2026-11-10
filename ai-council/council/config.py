"""Загрузка конфигурации совета из YAML и ключа из окружения / .env."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

Mode = Literal["quick", "debate", "evaluate"]
Effort = Literal["none", "low", "medium", "high"]


class MemberConfig(BaseModel):
    name: str
    model: str
    enabled: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: Effort | None = None  # своя глубина рассуждений для участника


class CouncilConfig(BaseModel):
    language: str = "Russian"
    mode: Mode = "debate"
    debate_rounds: int = Field(default=1, ge=0, le=5)
    min_quorum: int = Field(default=2, ge=1)
    max_cost_usd: float = Field(default=2.0, gt=0)
    temperature: float = 0.7
    max_tokens: int = 8000
    max_tokens_cap: int = 32000  # до скольких токенов можно поднять лимит, если ответ обрезан
    reasoning_effort: Effort = "low"  # глубина скрытых рассуждений «думающих» моделей; none — не задавать
    timeout_s: float = 180
    max_retries: int = 3
    json_retries: int = 2
    max_concurrency: int = 8
    chair: str
    devil_advocate: str | None = None
    runs_dir: str = "runs"
    members: list[MemberConfig]

    @model_validator(mode="after")
    def _check(self) -> "CouncilConfig":
        names = [m.name for m in self.members]
        if len(names) != len(set(names)):
            raise ValueError("имена участников должны быть уникальны")
        active = {m.name for m in self.active_members}
        if self.chair not in active:
            raise ValueError(f"председатель '{self.chair}' не найден среди включённых участников")
        if self.devil_advocate and self.devil_advocate not in active:
            raise ValueError(f"адвокат дьявола '{self.devil_advocate}' не найден среди включённых участников")
        if len(active) < self.min_quorum:
            raise ValueError(f"включено {len(active)} участников, а кворум {self.min_quorum}")
        return self

    @property
    def active_members(self) -> list[MemberConfig]:
        return [m for m in self.members if m.enabled]

    def member(self, name: str) -> MemberConfig:
        for m in self.members:
            if m.name == name:
                return m
        raise KeyError(name)


def load_config(path: str | Path) -> CouncilConfig:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return CouncilConfig.model_validate(data)


def load_dotenv(path: str | Path = ".env") -> None:
    """Минимальный парсер .env: KEY=VALUE, без перезаписи уже заданных переменных."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) > 1:
            value = value[1:-1]
        elif " #" in value:  # комментарий после значения: KEY=value  # пояснение
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


class OverrideError(ValueError):
    """Некорректные пользовательские настройки заседания."""


def with_overrides(
    base: CouncilConfig,
    *,
    members: list[str] | None = None,
    chair: str | None = None,
    devil_advocate: str | None = None,  # "" — убрать адвоката дьявола, None — оставить как есть
    budget: float | None = None,
) -> CouncilConfig:
    """Возвращает копию конфига с выбранными участниками/ролями/бюджетом, проверяя результат."""
    from pydantic import ValidationError

    data = base.model_dump()
    if members is not None:
        unknown = set(members) - {m["name"] for m in data["members"]}
        if unknown:
            raise OverrideError(f"неизвестные участники: {', '.join(sorted(unknown))}")
        for m in data["members"]:
            m["enabled"] = m["name"] in members
    enabled = [m["name"] for m in data["members"] if m["enabled"]]
    if chair:
        data["chair"] = chair
    elif data["chair"] not in enabled and enabled:
        data["chair"] = enabled[0]
    if devil_advocate is not None:
        data["devil_advocate"] = devil_advocate or None
    elif data.get("devil_advocate") not in enabled:
        data["devil_advocate"] = None
    if budget:
        data["max_cost_usd"] = budget
    try:
        return CouncilConfig.model_validate(data)
    except ValidationError as e:
        raise OverrideError("; ".join(err["msg"].removeprefix("Value error, ") for err in e.errors())) from e
