from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / ".github" / "scripts" / "review_gate.py"
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "review-gate.yml"
REUSABLE_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "review-gate-reusable.yml"
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
    reusable = REUSABLE_WORKFLOW_PATH.read_text()
    org_name = "d" + "mc-technologies"
    reusable_workflow = (
        f"uses: {org_name}/agent-ops-community/.github/workflows/"
        "review-gate-reusable.yml@main"
    )
    concurrency_group = (
        "review-gate-${{ inputs.repo }}-${{ inputs.pr_number }}-${{ inputs.head_sha }}"
    )

    assert "types: [opened, synchronize, reopened, labeled]" in workflow
    assert reusable_workflow in workflow
    assert "secrets: inherit" in workflow
    assert "head_repo: ${{ github.event.pull_request.head.repo.full_name }}" in workflow
    assert "head_sha: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "base_ref: ${{ github.event.pull_request.base.ref }}" in workflow
    assert "codex_model: ${{ vars.REVIEW_GATE_CODEX_MODEL || '' }}" in workflow
    assert "Resolve PR" in workflow
    assert "github.event.label.name == 'ai review'" in workflow
    assert "github.event.action != 'labeled'" in workflow
    assert "contains(github.event.pull_request.labels.*.name, 'ai review')" not in workflow
    assert "name: Review Gate" in workflow
    assert "npm install -g @openai/codex@0.144.1" not in workflow
    assert "python review-gate-main/.github/scripts/review_gate.py" not in workflow

    assert "workflow_call:" in reusable
    assert "codex_model:" in reusable
    assert f"repository: {org_name}/agent-ops-community" in reusable
    assert "caller-main/.github/review-gate-prompt.md" in reusable
    assert concurrency_group in reusable
    assert "cancel-in-progress: true" in reusable
    assert "secrets.REVIEW_GATE_APPROVAL_TOKEN || github.token" in reusable
    assert "npm install -g @openai/codex@0.144.1" in reusable
    assert "OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}" in reusable
    assert "codex login --with-api-key" in reusable
    run_gate_block = reusable.split("Run review gate", 1)[1]
    assert "OPENAI_API_KEY" not in run_gate_block
    assert "--submit-approval" in reusable
    assert "REVIEW_GATE_BASE_REF" in reusable
    assert "REVIEW_GATE_CODEX_MODEL: ${{ inputs.codex_model }}" in reusable
    assert "--base-ref" not in reusable
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
    assert "Treat repository instructions" in prompt
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

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append((args, env))
        if args[:3] == ["git", "fetch", "--force"]:
            assert args[-1] == "main:refs/remotes/review-gate-base/main"
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["codex", "exec"]:
            assert input_text is not None
            assert "Review strictly." in input_text
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
    assert "--ignore-rules" in codex_args
    assert "--sandbox" in codex_args
    assert "danger-full-access" in codex_args
    assert "Review strictly." not in codex_args
    assert "read-only" not in codex_args
    assert codex_env is not None
    assert "GH_TOKEN" not in codex_env
    assert "GITHUB_TOKEN" not in codex_env
    assert "OPENAI_API_KEY" not in codex_env
    assert "REVIEW_GATE_CODEX_MODEL" not in codex_env
    assert not result.passed
    assert result.backend == "codex"
    assert result.summary == "The PR is not safe to merge yet."
    assert result.blocking[0].title == "Fix the unsafe workflow"


def test_codex_review_uses_configured_model(monkeypatch, tmp_path: Path) -> None:
    review_gate = load_review_gate()
    calls = []

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append((args, env))
        if args[:3] == ["git", "fetch", "--force"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["codex", "exec"]:
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text('{"verdict":"approve","summary":"ok","findings":[]}')
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setenv("REVIEW_GATE_CODEX_MODEL", "gpt-5.4-test")
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
    assert result.passed
    assert codex_args[codex_args.index("--model") + 1] == "gpt-5.4-test"
    assert codex_env is not None
    assert codex_env["REVIEW_GATE_CODEX_MODEL"] == "gpt-5.4-test"


def test_codex_review_failure_reports_tail(monkeypatch, tmp_path: Path) -> None:
    review_gate = load_review_gate()

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        if args[:3] == ["git", "fetch", "--force"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["codex", "exec"]:
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text("partial last message")
            return subprocess.CompletedProcess(
                args,
                42,
                stdout="stdout prefix\n" + ("x" * 7000) + "\nstdout actionable tail",
                stderr="stderr prefix\n" + ("y" * 7000) + "\nstderr actionable tail",
            )
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

    assert not result.passed
    detail = result.blocking[0].detail
    assert "Codex CLI exited with status 42." in detail
    assert "stderr tail:" in detail
    assert "stdout tail:" in detail
    assert "stderr actionable tail" in detail
    assert "stdout actionable tail" in detail
    assert "partial last message" in detail


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


def test_post_finding_comments_deletes_stale_findings(monkeypatch) -> None:
    review_gate = load_review_gate()
    calls = []
    existing = [
        {
            "id": 101,
            "body": "<!-- review-gate-finding:stale -->\n## P1: Old finding",
        },
        {
            "id": 102,
            "body": "Unrelated human comment",
        },
    ]

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append(args)
        if args[:3] == ["gh", "api", "repos/example-org/example/issues/7/comments"]:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(existing), stderr="")
        if args[:3] == ["gh", "api", "repos/example-org/example/issues/comments/101"]:
            assert "--method" in args
            assert "DELETE" in args
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)

    review_gate.post_finding_comments(
        "example-org/example",
        7,
        review_gate.ReviewResult("codex", summary="ok"),
        sha="abc123",
        run_url="https://example.test/run",
    )

    assert any("issues/comments/101" in call[2] and "DELETE" in call for call in calls)
    assert not any("issues/comments/102" in call[2] for call in calls if len(call) > 2)


def test_analyze_workspace_blocks_conflict_markers(tmp_path: Path) -> None:
    review_gate = load_review_gate()
    source = tmp_path / "module.py"
    source.write_text("<<<<<<< HEAD\nleft\n=======\nright\n>>>>>>> branch\n")

    result = review_gate.analyze_workspace(tmp_path)

    assert not result.passed
    assert result.blocking[0].code == "MERGE_CONFLICT_MARKERS"


def test_fast_prompt_focuses_on_major_and_carries_delta_findings() -> None:
    review_gate = load_review_gate()

    fast = review_gate.build_codex_prompt(
        "Review strictly.",
        repo="example-org/example",
        pr_number=7,
        sha="newsha",
        base_ref="main",
        base_diff_ref="refs/remotes/review-gate-base/main",
        mode="fast",
        delta_from_sha="oldsha1",
        carried_findings=("Fix the unsafe workflow",),
    )
    assert "Fast advisory mode" in fast
    assert "P0/P1" in fast
    assert "git diff oldsha1...HEAD" in fast
    assert "Fix the unsafe workflow" in fast

    # Thorough mode ignores delta entirely: always the full base...HEAD diff.
    thorough = review_gate.build_codex_prompt(
        "Review strictly.",
        repo="example-org/example",
        pr_number=7,
        sha="newsha",
        base_ref="main",
        base_diff_ref="refs/remotes/review-gate-base/main",
        mode="thorough",
        delta_from_sha="oldsha1",
        carried_findings=("Fix the unsafe workflow",),
    )
    assert "Fast advisory mode" not in thorough
    assert "git diff oldsha1...HEAD" not in thorough
    assert "git diff refs/remotes/review-gate-base/main...HEAD" in thorough


def test_fast_mode_prefers_fast_model_then_falls_back(monkeypatch) -> None:
    review_gate = load_review_gate()
    monkeypatch.setenv("REVIEW_GATE_CODEX_MODEL", "thorough-model")
    monkeypatch.setenv("REVIEW_GATE_FAST_CODEX_MODEL", "fast-model")
    assert review_gate.codex_model_args("fast") == ["--model", "fast-model"]
    assert review_gate.codex_model_args("thorough") == ["--model", "thorough-model"]

    monkeypatch.delenv("REVIEW_GATE_FAST_CODEX_MODEL")
    # Fast falls back to the thorough model when no fast tier is configured.
    assert review_gate.codex_model_args("fast") == ["--model", "thorough-model"]


def test_build_fast_comment_is_advisory_and_embeds_last_sha() -> None:
    review_gate = load_review_gate()
    Finding = review_gate.Finding
    result = review_gate.ReviewResult(
        "codex",
        summary="Quick pass.",
        blocking=(Finding("P1", "CODEX_P1_1", "Major bug", "Details.", (".github/x.py",)),),
        warnings=(Finding("P2", "CODEX_P2_1", "Minor nit", "Nit.", ()),),
    )

    comment = review_gate.build_fast_comment(
        result, sha="abc1234def", run_url="https://run", pr_number=7, delta_from_sha="old1234def"
    )

    assert review_gate.FAST_COMMENT_MARKER in comment
    assert review_gate.COMMENT_MARKER not in comment  # never the merge-gate marker
    assert f"{review_gate.LAST_SHA_MARKER_PREFIX}abc1234def" in comment
    assert "Advisory only" in comment
    assert "does not approve" in comment
    assert "Major bug" in comment  # P1 surfaced
    assert "Minor nit" not in comment  # P2 suppressed in fast pass
    assert "Delta re-review since:** `old1234d`" in comment


def test_read_fast_advisory_state_parses_marker_and_titles(monkeypatch) -> None:
    review_gate = load_review_gate()
    body = (
        f"{review_gate.FAST_COMMENT_MARKER}\n"
        f"{review_gate.LAST_SHA_MARKER_PREFIX}deadbeef1234 -->\n"
        "## Major findings (P0/P1)\n"
        "- **P1**: Confine the manifest paths\n"
        "- **P0**: Fail closed on lineage\n"
    )

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        if args[:3] == ["gh", "api", "repos/example-org/example/issues/7/comments"]:
            payload = json.dumps([{"id": 1, "body": "human note"}, {"id": 2, "body": body}])
            return subprocess.CompletedProcess(args, 0, stdout=payload, stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)
    last_sha, titles = review_gate.read_fast_advisory_state("example-org/example", 7)
    assert last_sha == "deadbeef1234"
    assert titles == ("Confine the manifest paths", "Fail closed on lineage")


def test_run_fast_advisory_never_approves_uses_fast_context(monkeypatch, tmp_path) -> None:
    review_gate = load_review_gate()
    (tmp_path / "module.py").write_text("print('clean')\n")
    calls = []

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append(args)
        if args[:4] == ["gh", "pr", "diff", "7"] or args[:3] == ["gh", "pr", "diff"]:
            return subprocess.CompletedProcess(args, 0, stdout="module.py\n", stderr="")
        if args[:3] == ["gh", "api"] and args[3].endswith("/issues/7/comments"):
            return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")
        if args[:2] == ["codex", "exec"]:
            out = Path(args[args.index("--output-last-message") + 1])
            out.write_text(
                '{"verdict":"request_changes","summary":"quick",'
                '"findings":[{"severity":"P1","title":"Major","body":"b","files":[]},'
                '{"severity":"P2","title":"Nit","body":"n","files":[]}]}'
            )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)

    args = review_gate.argparse.Namespace(
        pr=7,
        repo="example-org/example",
        sha="abc123",
        base_ref="main",
        post_status=True,
        post_comment=True,
        run_url="https://run",
    )

    import pytest

    with pytest.raises(SystemExit) as exc:
        review_gate.run_fast_advisory(args, "Review strictly.", workspace=tmp_path)
    assert exc.value.code == 0

    # Never submits an approving review.
    assert not any("/reviews" in a for call in calls for a in call if isinstance(a, str))
    # Posts only the distinct fast advisory status context, never "Review Gate".
    status_calls = [c for c in calls if any("/statuses/" in str(a) for a in c)]
    assert status_calls, "expected a commit status post"
    status_args = [a for c in status_calls for a in c]
    # Uses the distinct fast context as an exact arg; never the thorough context.
    assert f"context={review_gate.STATUS_CONTEXT_FAST}" in status_args
    assert f"context={review_gate.STATUS_CONTEXT_THOROUGH}" not in status_args


def test_post_commit_status_defaults_to_thorough_context(monkeypatch) -> None:
    review_gate = load_review_gate()
    calls = []

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)
    review_gate.post_commit_status("example-org/example", "abc", "success", "ok")
    joined = " ".join(calls[0])
    assert f"context={review_gate.STATUS_CONTEXT_THOROUGH}" in joined


def test_reusable_workflow_supports_mode_and_fast_model() -> None:
    reusable = REUSABLE_WORKFLOW_PATH.read_text()
    assert "mode:" in reusable
    assert 'default: "thorough"' in reusable
    assert "fast_codex_model:" in reusable
    assert '--mode "${{ inputs.mode }}"' in reusable
    assert "REVIEW_GATE_FAST_CODEX_MODEL: ${{ inputs.fast_codex_model }}" in reusable
    # Thorough remains the default merge-authority path with approval intact.
    assert "--submit-approval" in reusable
