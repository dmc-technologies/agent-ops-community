from __future__ import annotations

from pathlib import Path

from agent_ops.harness import check_harness, init_harness


def test_init_harness_creates_files_that_pass_check(tmp_path: Path) -> None:
    writes = init_harness(
        tmp_path,
        repo_name="example",
        repo_type="python",
        verification_commands=("pytest", "ruff check ."),
    )

    assert {write.path.name for write in writes} == {
        "AGENTS.md",
        "ARCHITECTURE.md",
        "BOOTSTRAP.md",
        "DECISIONS.md",
        "PROGRESS.md",
        "TASKS.md",
        "VERIFY.md",
    }

    report = check_harness(tmp_path)

    assert report.ok is True
    assert report.findings == []
    bootstrap = (tmp_path / ".agentops/harness/BOOTSTRAP.md").read_text(encoding="utf-8")
    assert "## Clock In" in bootstrap
    assert "Search or recall relevant shared-memory entries" in bootstrap
    assert "Do not write automatic session summaries" in bootstrap


def test_check_harness_rejects_missing_files(tmp_path: Path) -> None:
    report = check_harness(tmp_path)

    assert report.ok is False
    assert any(finding.path == "AGENTS.md" for finding in report.findings)
    assert any(finding.path == ".agentops/harness/BOOTSTRAP.md" for finding in report.findings)
