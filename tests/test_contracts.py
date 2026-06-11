from __future__ import annotations

from pathlib import Path

import pytest

from agent_ops.contracts.job import AgentJob, JobMode, VerificationCommand, load_job


def test_load_job_accepts_verify_only_contract(tmp_path: Path) -> None:
    job_file = tmp_path / "job.yaml"
    job_file.write_text(
        """
id: public-smoke
title: Public smoke
runner: local
mode: verify-only
verification:
  - name: ok
    command: "true"
""",
        encoding="utf-8",
    )

    job = load_job(job_file)

    assert job.id == "public-smoke"
    assert job.runner == "local"
    assert job.mode == JobMode.VERIFY_ONLY
    assert job.verification == [VerificationCommand(name="ok", command="true")]


def test_job_id_rejects_shell_metacharacters() -> None:
    with pytest.raises(ValueError, match="id may only contain"):
        AgentJob(id="bad;id", title="Bad")
