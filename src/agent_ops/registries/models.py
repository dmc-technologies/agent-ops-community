from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class Framework(StrEnum):
    CLAUDE_CODE = "claude-code"
    CODEX = "codex"
    CURSOR = "cursor"
    OPENCLAW = "openclaw"
    OPENCODE = "opencode"
    LOCAL = "local"


class Capability(BaseModel):
    id: str
    name: str
    description: str
    frameworks: list[Framework]
    requires: list[str] = Field(default_factory=list)
    notes: str = ""


class SkillMapping(BaseModel):
    framework: Framework
    path: str | None = None
    invocation: str | None = None
    install_hint: str | None = None


class SkillSpec(BaseModel):
    id: str
    name: str
    description: str
    category: str = "workflow"
    mappings: list[SkillMapping] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SkillDependencyInstall(BaseModel):
    strategy: str
    source: str | None = None
    destination: str


class SkillDependency(BaseModel):
    id: str
    name: str
    repo: str
    ref: str
    version: str | None = None
    license: str | None = None
    install: dict[str, SkillDependencyInstall] = Field(default_factory=dict)


class ToolSpec(BaseModel):
    id: str
    name: str
    description: str
    kind: str
    frameworks: list[Framework] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)
    config_paths: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
