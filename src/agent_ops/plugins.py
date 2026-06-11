from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import entry_points
from typing import Protocol

from agent_ops.contracts.job import AgentJob
from agent_ops.contracts.result import RunResult, RunStatus


class RunnerPlugin(Protocol):
    name: str

    def supports(self, job: AgentJob) -> bool:
        """Return True when this plugin can execute the job."""

    def run(self, job: AgentJob, *, dry_run: bool = False) -> RunResult:
        """Run or preview the job and return a normalized result."""


@dataclass(frozen=True)
class AgentOpsPlugin:
    runners: tuple[RunnerPlugin, ...] = ()


def load_plugins() -> list[AgentOpsPlugin]:
    plugins: list[AgentOpsPlugin] = []
    for entry_point in entry_points(group="agent_ops.plugins"):
        plugin = entry_point.load()()
        plugins.append(plugin)
    return plugins


def run_with_plugins(
    job: AgentJob,
    *,
    plugins: list[AgentOpsPlugin] | None = None,
    dry_run: bool = False,
) -> RunResult:
    for plugin in plugins if plugins is not None else load_plugins():
        for runner in plugin.runners:
            if runner.supports(job):
                return runner.run(job, dry_run=dry_run)
    return RunResult(
        job_id=job.id,
        runner=job.runner,
        mode=job.mode.value,
        status=RunStatus.FAIL,
        finished_at=datetime.now(UTC),
        metadata={"error": f"No plugin runner is installed for runner {job.runner!r}"},
    )
