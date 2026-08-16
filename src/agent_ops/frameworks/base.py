from __future__ import annotations

import shlex
import shutil
import stat
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field

from agent_ops.context.models import ContextPack
from agent_ops.contracts.job import AgentJob
from agent_ops.deployment.models import TargetReadiness
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
    home_environment_variable: str

    def available(self) -> bool:
        return bool(self.executable and shutil.which(self.executable))

    def target_environment(self, home: Path) -> dict[str, str]:
        return {self.home_environment_variable: str(home)}

    def target_readiness(self, home: Path) -> TargetReadiness:
        return TargetReadiness(ready=False, prerequisite=self.target_setup_command(home))

    def target_setup_command(self, home: Path) -> str:
        if self.executable is None:
            return f"framework {self.framework.value} has no executable"
        return shlex.join(
            [
                "env",
                f"{self.home_environment_variable}={home}",
                *self.target_setup_arguments(),
            ]
        )

    def target_setup_arguments(self) -> tuple[str, ...]:
        if self.executable is None:
            return ()
        return (self.executable,)

    def native_home_readiness(self, home: Path) -> TargetReadiness:
        prerequisite = self.target_setup_command(home)
        if not self.available():
            return TargetReadiness(ready=False, prerequisite=prerequisite)
        try:
            item = home.lstat()
        except OSError:
            return TargetReadiness(ready=False, prerequisite=prerequisite)
        if not stat.S_ISDIR(item.st_mode):
            return TargetReadiness(ready=False, prerequisite=prerequisite)
        return TargetReadiness(ready=True, prerequisite=None)

    def native_file_readiness(
        self, home: Path, path: Path, prerequisite: str
    ) -> TargetReadiness:
        if not self.native_home_readiness(home).ready:
            return TargetReadiness(ready=False, prerequisite=prerequisite)
        try:
            item = path.lstat()
        except OSError:
            return TargetReadiness(ready=False, prerequisite=prerequisite)
        if not stat.S_ISREG(item.st_mode):
            return TargetReadiness(ready=False, prerequisite=prerequisite)
        return TargetReadiness(ready=True, prerequisite=None)

    @abstractmethod
    def build_command(self, job: AgentJob, context_pack: ContextPack, cwd: Path) -> AdapterCommand:
        """Build the command used to hand a job to this framework."""
