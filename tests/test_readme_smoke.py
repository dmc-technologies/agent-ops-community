from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agent_ops.cli import app

runner = CliRunner()


def test_readme_quick_start_commands_work(tmp_path: Path) -> None:
    sample_repo = tmp_path / "agentops-example"
    job = Path("examples/local-smoke.yaml")

    commands = [
        [
            "harness",
            "init",
            str(sample_repo),
            "--repo-name",
            "agentops-example",
            "--repo-type",
            "python",
        ],
        ["harness", "check", str(sample_repo)],
        ["validate", str(job)],
        ["verify", str(job), "--json"],
        ["bootstrap", "codex", "--output-dir", str(tmp_path / "bootstrap")],
        [
            "context",
            "build",
            str(job),
            "--framework",
            "codex",
            "--output-dir",
            str(tmp_path / "context"),
        ],
        ["frameworks", "command", str(job), "--framework", "codex", "--json"],
    ]

    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output

    command_result = runner.invoke(
        app,
        ["frameworks", "command", str(job), "--framework", "codex", "--json"],
    )
    assert json.loads(command_result.output)["framework"] == "codex"


def test_public_docs_do_not_advertise_unimplemented_commands() -> None:
    checked = [Path("README.md"), *Path("docs").rglob("*.md")]
    unsupported = ["agentops doctor", "agentops catalog"]
    offenders: list[str] = []

    for path in checked:
        text = path.read_text(encoding="utf-8")
        for command in unsupported:
            if command in text:
                offenders.append(f"{path}: {command}")

    assert offenders == []
