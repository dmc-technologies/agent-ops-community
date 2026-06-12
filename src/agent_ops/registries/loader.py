from __future__ import annotations

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

from agent_ops.paths import repo_root
from agent_ops.registries.models import Capability, SkillDependency, SkillSpec, ToolSpec

T = TypeVar("T", bound=BaseModel)


def _data_path(filename: str) -> Path:
    root_path = repo_root() / "data" / filename
    if root_path.exists():
        return root_path
    return Path(__file__).resolve().parents[1] / "data" / filename


def _load_list(path: Path, model: type[T]) -> list[T]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a YAML list")
    return [model.model_validate(item) for item in data]


def load_capabilities(path: Path | None = None) -> list[Capability]:
    return _load_list(path or _data_path("capabilities.yaml"), Capability)


def load_skills(path: Path | None = None) -> list[SkillSpec]:
    return _load_list(path or _data_path("skills.yaml"), SkillSpec)


def load_skill_dependencies(path: Path | None = None) -> list[SkillDependency]:
    return _load_list(path or _data_path("skill_dependencies.yaml"), SkillDependency)


def load_tools(path: Path | None = None) -> list[ToolSpec]:
    return _load_list(path or _data_path("tools.yaml"), ToolSpec)


def get_by_id(items: list[T], item_id: str) -> T:
    for item in items:
        if item.id == item_id:
            return item
    raise KeyError(item_id)
