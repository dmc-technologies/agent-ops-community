from __future__ import annotations

from dataclasses import dataclass

from agent_ops.contracts.job import AgentJob
from agent_ops.contracts.result import RunResult, RunStatus
from agent_ops.plugins import AgentOpsPlugin, run_with_plugins


@dataclass(frozen=True)
class FakeRunner:
    name: str = "fake"

    def supports(self, job: AgentJob) -> bool:
        return job.runner == "fake"

    def run(self, job: AgentJob, *, dry_run: bool = False) -> RunResult:
        return RunResult(
            job_id=job.id,
            runner=self.name,
            mode=job.mode,
            status=RunStatus.DRY_RUN if dry_run else RunStatus.PASS,
        )


def test_run_with_plugins_uses_matching_runner() -> None:
    job = AgentJob(id="plugin-smoke", title="Plugin smoke", runner="fake")
    plugin = AgentOpsPlugin(runners=(FakeRunner(),))

    result = run_with_plugins(job, plugins=[plugin], dry_run=True)

    assert result.status == RunStatus.DRY_RUN
    assert result.runner == "fake"


def test_run_with_plugins_fails_without_matching_runner() -> None:
    job = AgentJob(id="missing-plugin", title="Missing plugin", runner="missing")

    result = run_with_plugins(job, plugins=[], dry_run=False)

    assert result.status == RunStatus.FAIL
    assert "No plugin runner" in result.metadata["error"]
