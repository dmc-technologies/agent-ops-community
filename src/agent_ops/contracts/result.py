from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class RunStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    DRY_RUN = "dry-run"


class CommandResult(BaseModel):
    command: list[str]
    cwd: str
    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    elapsed_seconds: float = 0.0


class VerificationResult(BaseModel):
    name: str
    command: str
    status: RunStatus
    exit_code: int | None = None
    elapsed_seconds: float = 0.0
    stdout_tail: str = ""
    stderr_tail: str = ""


class RunResult(BaseModel):
    job_id: str
    runner: str
    mode: str
    status: RunStatus
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    command: CommandResult | None = None
    verification: list[VerificationResult] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def write_json(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.model_dump_json(indent=2), encoding="utf-8")
