from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agent_ops.cli import app

runner = CliRunner()


def test_validate_accepts_public_job(tmp_path: Path) -> None:
    job = tmp_path / "job.yaml"
    job.write_text(
        """
id: cli-job
title: CLI Job
runner: local
mode: verify-only
verification:
  - name: ok
    command: "true"
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["validate", str(job)])

    assert result.exit_code == 0
    assert "valid: cli-job" in result.output


def test_verify_command_returns_nonzero_on_failure(tmp_path: Path) -> None:
    job = tmp_path / "job.yaml"
    job.write_text(
        """
id: cli-verify
title: CLI Verify
mode: verify-only
verification:
  - name: missing
    command: test -f missing.txt
""",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["verify", str(job), "--json"])

    assert result.exit_code == 1
    data = json.loads(result.output)
    assert data["status"] == "fail"


def test_harness_init_and_check_cli(tmp_path: Path) -> None:
    init_result = runner.invoke(
        app,
        [
            "harness",
            "init",
            str(tmp_path),
            "--repo-name",
            "example",
            "--repo-type",
            "python",
            "--json",
        ],
    )

    assert init_result.exit_code == 0

    check_result = runner.invoke(app, ["harness", "check", str(tmp_path), "--json"])

    assert check_result.exit_code == 0
    assert json.loads(check_result.output)["ok"] is True


def test_bootstrap_writes_public_agentops_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["bootstrap", "--output-dir", str(tmp_path)])

    assert result.exit_code == 0
    text = (tmp_path / "AGENTOPS.md").read_text(encoding="utf-8")
    assert "Agent Ops Community Bootstrap" in text
    assert "shared-memory" in text
