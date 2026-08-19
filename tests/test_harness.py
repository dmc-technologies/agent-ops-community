from __future__ import annotations

from pathlib import Path

from agent_ops.harness import check_harness, init_harness

ROOT = Path(__file__).resolve().parents[1]


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
    verification = (tmp_path / ".agentops/harness/VERIFY.md").read_text(encoding="utf-8")
    assert "## CI Contract" in verification
    assert "## Fast Gate" not in verification
    assert "## Full Gate" not in verification


def test_rendered_harness_supports_controller_owned_integration_delivery(
    tmp_path: Path,
) -> None:
    """Generated guidance separates feature handoff from controller state."""
    init_harness(tmp_path, repo_name="example", repo_type="python")
    bootstrap = (tmp_path / ".agentops/harness/BOOTSTRAP.md").read_text(
        encoding="utf-8"
    )
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    bootstrap_flat = " ".join(bootstrap.split())
    agents_flat = " ".join(agents.split())

    for guidance in (bootstrap_flat, agents_flat):
        assert "repository instructions declare an integration branch" in guidance
        assert "routine feature pull request" in guidance
        assert "integration controller" in guidance
        assert "stable-branch landing" in guidance
    assert "do not replace `.agentops/harness/PROGRESS.md`" in bootstrap_flat
    assert "exact pull request and tracker evidence" in bootstrap_flat


def test_rendered_harness_preserves_single_branch_progress_ownership(
    tmp_path: Path,
) -> None:
    """Repositories without integration delivery retain the default handoff."""
    init_harness(tmp_path, repo_name="example", repo_type="python")
    bootstrap = (tmp_path / ".agentops/harness/BOOTSTRAP.md").read_text(
        encoding="utf-8"
    )
    bootstrap_flat = " ".join(bootstrap.split())

    assert "If no integration branch is declared" in bootstrap_flat
    assert "update `.agentops/harness/PROGRESS.md`" in bootstrap_flat


def test_repository_harness_exposes_the_same_branch_role_contract() -> None:
    """The package's own entry points demonstrate the generated behavior."""
    bootstrap = " ".join(
        (ROOT / ".agentops/harness/BOOTSTRAP.md")
        .read_text(encoding="utf-8")
        .split()
    )
    agents = " ".join((ROOT / "AGENTS.md").read_text(encoding="utf-8").split())

    for guidance in (bootstrap, agents):
        assert "repository instructions declare an integration branch" in guidance
        assert "routine feature pull request" in guidance
        assert "integration controller" in guidance
        assert "stable-branch landing" in guidance


def test_check_harness_rejects_missing_files(tmp_path: Path) -> None:
    report = check_harness(tmp_path)

    assert report.ok is False
    assert any(finding.path == "AGENTS.md" for finding in report.findings)
    assert any(finding.path == ".agentops/harness/BOOTSTRAP.md" for finding in report.findings)


def test_check_harness_accepts_complete_legacy_verification_contract(tmp_path: Path) -> None:
    init_harness(tmp_path, repo_name="legacy", repo_type="python")
    verification_path = tmp_path / ".agentops/harness/VERIFY.md"
    verification = verification_path.read_text(encoding="utf-8")
    verification_path.write_text(
        verification.replace(
            "## CI Contract",
            "## Fast Gate\n\n- `pytest -q`\n\n## Full Gate",
        ),
        encoding="utf-8",
    )

    report = check_harness(tmp_path)

    assert report.ok is True
    assert report.findings == []


def test_check_harness_rejects_partial_legacy_verification_contract(tmp_path: Path) -> None:
    init_harness(tmp_path, repo_name="partial", repo_type="python")
    verification_path = tmp_path / ".agentops/harness/VERIFY.md"
    verification = verification_path.read_text(encoding="utf-8")
    verification_path.write_text(
        verification.replace("## CI Contract", "## Fast Gate"),
        encoding="utf-8",
    )

    report = check_harness(tmp_path)

    assert report.ok is False
    assert any("missing CI contract" in finding.message for finding in report.findings)


def test_check_harness_rejects_ci_contract_heading_lookalikes(tmp_path: Path) -> None:
    for index, heading in enumerate(("## CI Contract Notes", "## CI Contractual")):
        repo = tmp_path / str(index)
        init_harness(repo, repo_name="lookalike", repo_type="python")
        verification_path = repo / ".agentops/harness/VERIFY.md"
        verification = verification_path.read_text(encoding="utf-8")
        verification_path.write_text(
            verification.replace("## CI Contract", heading),
            encoding="utf-8",
        )

        report = check_harness(repo)

        assert report.ok is False
        assert any("missing CI contract" in finding.message for finding in report.findings)


def test_check_harness_rejects_legacy_heading_lookalikes(tmp_path: Path) -> None:
    init_harness(tmp_path, repo_name="legacy-lookalike", repo_type="python")
    verification_path = tmp_path / ".agentops/harness/VERIFY.md"
    verification = verification_path.read_text(encoding="utf-8")
    verification_path.write_text(
        verification.replace(
            "## CI Contract",
            "## Fast Gateway\n\n- `pytest -q`\n\n## Full Gateway",
        ),
        encoding="utf-8",
    )

    report = check_harness(tmp_path)

    assert report.ok is False
    assert any("missing CI contract" in finding.message for finding in report.findings)


def test_check_harness_ignores_ci_headings_rendered_as_code(tmp_path: Path) -> None:
    replacements = (
        "```markdown\n## CI Contract\n```",
        "    ## CI Contract",
        "~~~markdown\n## Fast Gate\n## Full Gate\n~~~",
        "    ## Fast Gate\n\n    ## Full Gate",
    )
    for index, replacement in enumerate(replacements):
        repo = tmp_path / str(index)
        init_harness(repo, repo_name="code-heading", repo_type="python")
        verification_path = repo / ".agentops/harness/VERIFY.md"
        verification = verification_path.read_text(encoding="utf-8")
        verification_path.write_text(
            verification.replace("## CI Contract", replacement),
            encoding="utf-8",
        )

        report = check_harness(repo)

        assert report.ok is False
        assert any("missing CI contract" in finding.message for finding in report.findings)


def test_check_harness_ignores_required_heading_rendered_as_code(tmp_path: Path) -> None:
    replacements = (
        "```markdown\n## Project\n```",
        "    ## Project",
    )
    for index, replacement in enumerate(replacements):
        repo = tmp_path / str(index)
        init_harness(repo, repo_name="code-heading", repo_type="python")
        agents_path = repo / "AGENTS.md"
        agents = agents_path.read_text(encoding="utf-8")
        agents_path.write_text(
            agents.replace("## Project", replacement),
            encoding="utf-8",
        )

        report = check_harness(repo)

        assert report.ok is False
        assert any(
            finding.path == "AGENTS.md" and "## Project" in finding.message
            for finding in report.findings
        )


def test_check_harness_ignores_ci_headings_hidden_by_html(tmp_path: Path) -> None:
    replacements = (
        "<!--\n## CI Contract\n-->",
        "<section>\n## CI Contract\n</section>",
        "<!--\n## Fast Gate\n## Full Gate\n-->",
        "<section>\n## Fast Gate\n## Full Gate\n</section>",
    )
    for index, replacement in enumerate(replacements):
        repo = tmp_path / str(index)
        init_harness(repo, repo_name="hidden-heading", repo_type="python")
        verification_path = repo / ".agentops/harness/VERIFY.md"
        verification = verification_path.read_text(encoding="utf-8")
        verification_path.write_text(
            verification.replace("## CI Contract", replacement),
            encoding="utf-8",
        )

        report = check_harness(repo)

        assert report.ok is False
        assert any("missing CI contract" in finding.message for finding in report.findings)


def test_check_harness_ignores_required_heading_hidden_by_html(tmp_path: Path) -> None:
    replacements = (
        "<!--\n## Project\n-->",
        "<section>\n## Project\n</section>",
    )
    for index, replacement in enumerate(replacements):
        repo = tmp_path / str(index)
        init_harness(repo, repo_name="hidden-heading", repo_type="python")
        agents_path = repo / "AGENTS.md"
        agents = agents_path.read_text(encoding="utf-8")
        agents_path.write_text(
            agents.replace("## Project", replacement),
            encoding="utf-8",
        )

        report = check_harness(repo)

        assert report.ok is False
        assert any(
            finding.path == "AGENTS.md" and "## Project" in finding.message
            for finding in report.findings
        )
