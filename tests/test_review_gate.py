from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / ".github" / "scripts" / "review_gate.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "review-gate.yml"
AUTO_LABEL_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ai-review-autolabel.yml"
PROMPT_PATH = ROOT / ".github" / "review-gate-prompt.md"


def load_review_gate():
    spec = importlib.util.spec_from_file_location("review_gate", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ai_review_label_triggers_review_and_approval() -> None:
    workflow = WORKFLOW_PATH.read_text()

    assert "types: [opened, synchronize, reopened, labeled]" in workflow
    assert "github.event.label.name == 'ai review'" in workflow
    assert "contains(github.event.pull_request.labels.*.name, 'ai review')" in workflow
    assert "Run review gate and submit approval" in workflow
    assert "secrets.REVIEW_GATE_APPROVAL_TOKEN || secrets.GITHUB_TOKEN" in workflow
    assert "npm install -g @openai/codex@0.141.0" in workflow
    assert "OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}" in workflow
    assert "codex login --with-api-key" in workflow
    run_gate_block = workflow.split("Run review gate and submit approval", 1)[1]
    run_gate_block = run_gate_block.split("agent-review-dispatch:", 1)[0]
    assert "OPENAI_API_KEY" not in run_gate_block
    assert "--submit-approval" in workflow
    assert "REVIEW_GATE_BASE_REF" in workflow
    assert "--base-ref" not in workflow
    assert "Checkout trusted review gate" in workflow
    assert "Check trusted review gate availability" in workflow
    assert "steps.trusted-gate.outputs.available == 'true'" in workflow
    assert "Review Gate unavailable: trusted gate missing" in workflow
    assert "-f state=failure" in workflow
    assert "Load review prompt" in workflow
    assert "review-gate-main/.github/review-gate-prompt.md" in workflow
    assert "github.event.repository.default_branch" in workflow
    assert "ref: ${{ github.sha }}" not in workflow
    assert "ref: ${{ github.ref_name }}" not in workflow
    assert "REVIEW_GATE_BACKEND: deterministic" not in workflow
    assert "issue_comment" not in workflow
    assert "/agent-review" not in workflow


def test_ai_review_label_is_applied_to_prs_by_default() -> None:
    workflow = AUTO_LABEL_WORKFLOW_PATH.read_text()

    assert "pull_request_target:" in workflow
    assert "types: [opened, reopened, ready_for_review, synchronize]" in workflow
    assert "actions/checkout" not in workflow
    assert "--add-label \"ai review\"" in workflow
    assert "gh label create \"ai review\"" in workflow


def test_review_prompt_includes_harder_architecture_domain_and_security_lenses() -> None:
    workflow = WORKFLOW_PATH.read_text()
    prompt = PROMPT_PATH.read_text()

    assert "senior software architect" not in workflow
    assert "senior software architect" in prompt
    assert "AI engineer" in prompt
    assert "mechanical engineering reviewer" in prompt
    assert "source-grounded" in prompt
    assert "adapters, registries, profiles, or stable tool IDs" in prompt
    assert "Never run PR-controlled review scripts" in prompt
    assert "on-prem, air-gap, data-residency" in prompt


def test_build_review_comment_includes_prompt_sha_and_run_url() -> None:
    review_gate = load_review_gate()
    result = review_gate.ReviewResult("codex", summary="No blocking findings.")

    comment = review_gate.build_review_comment(
        result,
        sha="abc1234def",
        run_url="https://github.com/example-org/example/actions/runs/1",
        pr_number=7,
        review_prompt="Check workflow safety.",
    )

    assert review_gate.COMMENT_MARKER in comment
    assert "**PR:** #7" in comment
    assert "`abc1234d`" in comment
    assert "https://github.com/example-org/example/actions/runs/1" in comment
    assert "## Review prompt" in comment
    assert "Check workflow safety." in comment
    assert "PASS" in comment


def test_codex_review_invokes_codex_exec_and_parses_findings(monkeypatch, tmp_path: Path) -> None:
    review_gate = load_review_gate()
    calls = []

    def fake_run_command(args, cwd=None, env=None):
        calls.append((args, env))
        if args[:3] == ["git", "fetch", "--force"]:
            assert args[-1] == "main:refs/remotes/review-gate-base/main"
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["codex", "exec"]:
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(
                """
                {
                  "verdict": "request_changes",
                  "summary": "The PR is not safe to merge yet.",
                  "findings": [
                    {
                      "severity": "P1",
                      "title": "Fix the unsafe workflow",
                      "body": "The workflow executes untrusted PR code.",
                      "files": [".github/workflows/review-gate.yml"]
                    }
                  ]
                }
                """
            )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)

    result = review_gate.run_codex_review(
        tmp_path,
        "Review strictly.",
        repo="example-org/example",
        pr_number=7,
        sha="abc123",
        base_ref="main",
    )

    codex_args, codex_env = next(call for call in calls if call[0][:2] == ["codex", "exec"])
    assert codex_args[:2] == ["codex", "exec"]
    assert "--sandbox" in codex_args
    assert "read-only" in codex_args
    assert codex_env is not None
    assert "GH_TOKEN" not in codex_env
    assert "GITHUB_TOKEN" not in codex_env
    assert "OPENAI_API_KEY" not in codex_env
    assert not result.passed
    assert result.backend == "codex"
    assert result.summary == "The PR is not safe to merge yet."
    assert result.blocking[0].title == "Fix the unsafe workflow"


def test_submit_pr_approval_posts_approve_review(monkeypatch) -> None:
    review_gate = load_review_gate()
    calls = []

    def fake_run_command(args, cwd=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)

    assert review_gate.submit_pr_approval("example-org/example", 12, "AI review passed")
    assert calls == [
        [
            "gh",
            "api",
            "repos/example-org/example/pulls/12/reviews",
            "--method",
            "POST",
            "-f",
            "event=APPROVE",
            "-f",
            "body=AI review passed",
        ]
    ]


def test_analyze_workspace_blocks_conflict_markers(tmp_path: Path) -> None:
    review_gate = load_review_gate()
    source = tmp_path / "module.py"
    source.write_text("<<<<<<< HEAD\nleft\n=======\nright\n>>>>>>> branch\n")

    result = review_gate.analyze_workspace(tmp_path)

    assert not result.passed
    assert result.blocking[0].code == "MERGE_CONFLICT_MARKERS"
