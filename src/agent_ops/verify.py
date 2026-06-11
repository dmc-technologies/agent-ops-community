from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agent_ops.contracts.job import AgentJob
from agent_ops.contracts.result import RunResult, RunStatus, VerificationResult
from agent_ops.process import run_command


def run_verification(job: AgentJob, base_dir: str | Path) -> RunResult:
    started = datetime.now(UTC)
    root = Path(base_dir).resolve()
    results: list[VerificationResult] = []

    for check in job.verification:
        cwd = Path(check.cwd) if check.cwd else root
        if not cwd.is_absolute():
            cwd = (root / cwd).resolve()
        command_result = run_command(
            ["/bin/sh", "-lc", check.command],
            cwd=cwd,
            timeout_seconds=check.timeout_seconds,
        )
        ok = command_result.exit_code == check.expected_exit
        results.append(
            VerificationResult(
                name=check.name,
                command=check.command,
                status=RunStatus.PASS if ok else RunStatus.FAIL,
                exit_code=command_result.exit_code,
                elapsed_seconds=command_result.elapsed_seconds,
                stdout_tail=command_result.stdout_tail,
                stderr_tail=command_result.stderr_tail,
            )
        )

    status = (
        RunStatus.PASS
        if all(result.status == RunStatus.PASS for result in results)
        else RunStatus.FAIL
    )
    return RunResult(
        job_id=job.id,
        runner=job.runner,
        mode="verify",
        status=status,
        started_at=started,
        finished_at=datetime.now(UTC),
        verification=results,
    )
