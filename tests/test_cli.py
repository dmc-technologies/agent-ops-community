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
    result = runner.invoke(app, ["bootstrap", "all", "--output-dir", str(tmp_path)])

    assert result.exit_code == 0
    assert (tmp_path / "codex/AGENTOPS.md").exists()
    assert (tmp_path / "claude-code/AGENTOPS.md").exists()
    assert (tmp_path / "cursor/AGENTOPS.md").exists()
    text = (tmp_path / "codex/AGENTOPS.md").read_text(encoding="utf-8")
    assert "Agent Ops Bootstrap: codex" in text
    assert "agent-knowledge" in text


def test_skills_install_dry_run_reports_gstack_destination(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "skills",
            "install",
            "codex",
            "--dependency",
            "gstack",
            "--home",
            str(tmp_path / "home"),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert f"would install: gstack -> {tmp_path / 'home' / 'skills' / 'gstack'}" in result.output


def test_context_build_and_framework_command_are_public(tmp_path: Path) -> None:
    job = tmp_path / "job.yaml"
    job.write_text(
        """
id: framework-job
title: Framework Job
runner: local
mode: verify-only
verification:
  - name: ok
    command: "echo ok"
""",
        encoding="utf-8",
    )

    context_result = runner.invoke(
        app,
        [
            "context",
            "build",
            str(job),
            "--framework",
            "codex",
            "--output-dir",
            str(tmp_path / "context"),
        ],
    )

    assert context_result.exit_code == 0
    assert (tmp_path / "context/framework-job-codex.json").exists()
    markdown = (tmp_path / "context/framework-job-codex.md").read_text(encoding="utf-8")
    assert "agent-knowledge" in markdown

    command_result = runner.invoke(
        app,
        ["frameworks", "command", str(job), "--framework", "codex", "--json"],
    )

    assert command_result.exit_code == 0
    command = json.loads(command_result.output)
    assert command["framework"] == "codex"
    assert command["command"][0] == "codex"
