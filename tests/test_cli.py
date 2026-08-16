from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

try:
    from click import unstyle
except ModuleNotFoundError:  # Typer 0.27 vendors Click instead of exposing its package.
    from typer._click.utils import strip_ansi as unstyle

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


def test_deployment_json_parse_boundary_does_not_change_unrelated_commands() -> None:
    result = runner.invoke(app, ["bootstrap", "--unknown", "--json"], color=True)

    assert result.exit_code == 2
    output = unstyle(result.output)
    assert output.startswith("Usage:")
    assert "No such option: --unknown" in output


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
    assert (tmp_path / "prime-agent/AGENTOPS.md").exists()
    text = (tmp_path / "codex/AGENTOPS.md").read_text(encoding="utf-8")
    assert "Agent Ops Bootstrap: codex" in text
    assert "agent-knowledge" in text


def test_skills_install_dry_run_reports_gstack_destination(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "gstack"
    source.mkdir()
    (source / "SKILL.md").write_bytes(b"gstack\n")
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency",
        lambda _dependency, _cache: source,
    )
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
            "--cache-dir",
            str(tmp_path / "cache"),
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert f"would install: gstack -> {tmp_path / 'home' / 'skills' / 'gstack'}" in result.output


def test_skills_install_fails_on_unknown_dependency(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "skills",
            "install",
            "codex",
            "--dependency",
            "typo",
            "--home",
            str(tmp_path / "home"),
            "--dry-run",
        ],
    )

    assert result.exit_code == 1
    assert "unknown skill dependency id(s): typo" in result.output


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
        [
            "frameworks",
            "command",
            str(job),
            "--framework",
            "codex",
            "--cwd",
            str(tmp_path),
            "--json",
        ],
    )

    assert command_result.exit_code == 0
    command = json.loads(command_result.output)
    assert command["framework"] == "codex"
    assert command["command"][0] == "codex"


def test_prime_agent_cli_handoff_and_skill_install_dry_run(tmp_path: Path, monkeypatch) -> None:
    gstack = tmp_path / "gstack"
    gstack.mkdir()
    (gstack / "SKILL.md").write_bytes(b"gstack\n")
    superpowers = tmp_path / "superpowers"
    (superpowers / "skills").mkdir(parents=True)

    def checkout(dependency, _cache):
        return gstack if dependency.id == "gstack" else superpowers

    def render_gstack(_source, destination, **_kwargs):
        skill = destination / "skills/generated-gstack/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_bytes(b"gstack\n")
        manifest = destination / ".agentops/gstack-prime-manifest.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"files": ["skills/generated-gstack/SKILL.md"]}))

    def render_superpowers(_source, destination):
        skill = destination / "generated-superpowers/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_bytes(b"superpowers\n")
        manifest = destination.parent / ".agentops/skill-dependencies/superpowers.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"skills": ["generated-superpowers"]}))

    monkeypatch.setattr("agent_ops.skill_installer._checkout_dependency", checkout)
    monkeypatch.setattr("agent_ops.skill_installer.install_prime_gstack", render_gstack)
    monkeypatch.setattr("agent_ops.skill_installer.install_prime_superpowers", render_superpowers)
    job = tmp_path / "job.yaml"
    job.write_text(
        """
id: prime-cli
title: Prime CLI
mode: verify-only
verification:
  - name: ok
    command: "true"
""",
        encoding="utf-8",
    )

    command_result = runner.invoke(
        app,
        [
            "frameworks",
            "command",
            str(job),
            "--framework",
            "prime-agent",
            "--cwd",
            str(tmp_path),
            "--json",
        ],
    )
    install_result = runner.invoke(
        app,
        [
            "skills",
            "install",
            "prime-agent",
            "--dependency",
            "gstack",
            "--dependency",
            "superpowers",
            "--home",
            str(tmp_path / "prime-home"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--dry-run",
        ],
    )

    assert command_result.exit_code == 0
    command = json.loads(command_result.output)
    assert command["framework"] == "prime-agent"
    assert command["command"][:4] == ["prime-agent", "--print", "--cwd", str(tmp_path)]
    assert command["cwd"] == str(tmp_path)
    assert install_result.exit_code == 0
    assert "gstack ->" in install_result.output
    assert "superpowers ->" in install_result.output


def test_framework_command_requires_explicit_existing_directory(tmp_path: Path) -> None:
    job = tmp_path / "job.yaml"
    job.write_text(
        "id: explicit-cwd\ntitle: Explicit CWD\nmode: verify-only\n",
        encoding="utf-8",
    )

    missing_result = runner.invoke(
        app,
        ["frameworks", "command", str(job), "--framework", "codex", "--json"],
    )
    file_result = runner.invoke(
        app,
        [
            "frameworks",
            "command",
            str(job),
            "--framework",
            "codex",
            "--cwd",
            str(job),
            "--json",
        ],
    )

    assert missing_result.exit_code != 0
    assert "Missing option" in missing_result.output
    assert file_result.exit_code != 0
    assert "Directory" in file_result.output
