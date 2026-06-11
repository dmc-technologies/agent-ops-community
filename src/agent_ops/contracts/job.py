from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator


class JobMode(StrEnum):
    BATCH = "batch"
    DISPATCH = "dispatch"
    VERIFY_ONLY = "verify-only"


class EnvironmentNeed(BaseModel):
    commands: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)


class VerificationCommand(BaseModel):
    name: str
    command: str
    cwd: str | None = None
    timeout_seconds: int = 300
    expected_exit: int = 0

    @field_validator("timeout_seconds")
    @classmethod
    def timeout_must_be_positive(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("timeout_seconds must be positive")
        return value


class AgentJob(BaseModel):
    id: str
    title: str
    profile: str = "local"
    runner: str = "local"
    mode: JobMode = JobMode.VERIFY_ONLY
    target_repo: str | None = None
    branch_suffix: str | None = None
    base_branch: str | None = None
    job_files: list[str] = Field(default_factory=list)
    environment: EnvironmentNeed = Field(default_factory=EnvironmentNeed)
    verification: list[VerificationCommand] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def id_is_cli_safe(cls, value: str) -> str:
        if not value:
            raise ValueError("id is required")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_./")
        if any(ch not in allowed for ch in value):
            raise ValueError(
                "id may only contain letters, numbers, dash, underscore, dot, and slash"
            )
        return value


def load_job(path: str | Path) -> AgentJob:
    job_path = Path(path)
    data = yaml.safe_load(job_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("job file must contain a mapping")
    return AgentJob.model_validate(data)
