from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field

from agent_ops.context.models import ContextPack
from agent_ops.contracts.job import AgentJob
from agent_ops.registries.models import Framework


class AdapterCommand(BaseModel):
    framework: Framework
    command: list[str]
    cwd: str
    env: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class FrameworkAdapter(ABC):
    framework: Framework
    executable: str | None = None

    def available(self) -> bool:
        return bool(self.executable and shutil.which(self.executable))

    @abstractmethod
    def build_command(self, job: AgentJob, context_pack: ContextPack, cwd: Path) -> AdapterCommand:
        """Build the command used to hand a job to this framework."""
