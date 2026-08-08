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


def material_finding(
    *,
    severity: str = "P1",
    title: str = "Fix the unsafe workflow",
    body: str = "The workflow executes untrusted PR code.",
    root_cause: str = "untrusted-workflow-execution",
    files: list[str] | None = None,
) -> dict:
    return {
        "disposition": "block",
        "severity": severity,
        "title": title,
        "body": body,
        "files": files or [".github/workflows/review-gate.yml"],
        "introduced_by_pr": True,
        "current_behavior_defect": "The privileged job executes PR-controlled code.",
        "current_failure_path": "A pull request changes the privileged workflow.",
        "consequence_class": "security",
        "consequence": "Untrusted code can execute with repository credentials.",
        "confidence": "high",
        "root_cause": root_cause,
        "already_caught_by": "none",
        "required_correction": "Keep PR-controlled code out of the privileged job.",
    }


def test_ai_review_label_triggers_review_and_approval() -> None:
    workflow = WORKFLOW_PATH.read_text()
    reusable = REUSABLE_WORKFLOW_PATH.read_text()
    org_name = "d" + "mc-technologies"
    reusable_workflow = (
        f"uses: {org_name}/agent-ops-community/.github/workflows/"
        "review-gate-reusable.yml@main"
    )
    concurrency_group = "review-gate-${{ inputs.repo }}-${{ inputs.pr_number }}"

    assert "types: [labeled]" in workflow
    assert reusable_workflow in workflow
    assert "secrets: inherit" in workflow
    assert "head_repo: ${{ github.event.pull_request.head.repo.full_name }}" in workflow
    assert "head_sha: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "base_ref: ${{ github.event.pull_request.base.ref }}" in workflow
    assert "codex_model: ${{ vars.REVIEW_GATE_CODEX_MODEL || '' }}" in workflow
    assert "Resolve PR" in workflow
    assert "github.event.label.name == 'ai review'" in workflow
    assert "github.event.action != 'labeled'" not in workflow
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
    assert "timeout-minutes: 30" in reusable
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


def test_ai_review_label_is_only_applied_by_a_person_at_final_review() -> None:
    assert not AUTO_LABEL_WORKFLOW_PATH.exists()
    workflow = WORKFLOW_PATH.read_text()
    assert "types: [labeled]" in workflow
    assert "opened" not in workflow
    assert "synchronize" not in workflow


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
                json.dumps(
                    {
                        "verdict": "request_changes",
                        "summary": "The PR is not safe to merge yet.",
                        "findings": [material_finding()],
                    }
                )
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


def test_review_gate_defaults_to_sol_and_explicit_reasoning_effort(monkeypatch) -> None:
    review_gate = load_review_gate()
    monkeypatch.delenv("REVIEW_GATE_CODEX_MODEL", raising=False)

    assert review_gate.resolve_codex_model() == "gpt-5.6-sol"
    assert review_gate.codex_model_args(effort="xhigh") == [
        "--model",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="xhigh"',
    ]


def test_classifier_uses_sol_low_and_raises_uncertain_or_critical_changes_to_xhigh(
    monkeypatch, tmp_path: Path
) -> None:
    review_gate = load_review_gate()
    calls = []

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append((args, input_text))
        if args[:3] == ["git", "fetch", "--force"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["codex", "exec"]:
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(
                json.dumps(
                    {
                        "recommended_effort": "medium",
                        "confidence": "low",
                        "risk_domains": ["authorization"],
                        "reasons": ["The change alters approval evaluation."],
                        "review_questions": ["Can stale approval state authorize a mutation?"],
                    }
                )
            )
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)
    profile = review_gate.run_codex_classification(
        tmp_path,
        repo="example-org/example",
        pr_number=7,
        sha="abc123",
        base_ref="main",
    )

    codex_args, prompt = next(call for call in calls if call[0][:2] == ["codex", "exec"])
    assert codex_args[codex_args.index("--model") + 1] == "gpt-5.6-sol"
    assert 'model_reasoning_effort="low"' in codex_args
    assert "Classify impact and review difficulty" in prompt
    assert profile.effort == "xhigh"
    assert profile.confidence == "low"
    assert profile.risk_domains == ("authorization",)


def test_classifier_routes_incomplete_low_effort_output_to_xhigh() -> None:
    review_gate = load_review_gate()

    profile = review_gate.review_profile_from_payload(
        {"recommended_effort": "low", "confidence": "high"}
    )

    assert profile.effort == "xhigh"
    assert profile.confidence == "low"


def test_routed_review_passes_profile_and_selected_effort_to_full_review(
    monkeypatch, tmp_path: Path
) -> None:
    review_gate = load_review_gate()
    calls = []
    responses = [
        {
            "recommended_effort": "high",
            "confidence": "high",
            "risk_domains": ["concurrency"],
            "reasons": ["Lock ordering changes."],
            "review_questions": ["Can two callers enter the mutation concurrently?"],
        },
        {"verdict": "approve", "summary": "No material findings.", "findings": []},
    ]

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append((args, input_text))
        if args[:3] == ["git", "fetch", "--force"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["codex", "exec"]:
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(json.dumps(responses.pop(0)))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)
    result = review_gate.run_routed_codex_review(
        tmp_path,
        "Review strictly.",
        repo="example-org/example",
        pr_number=7,
        sha="abc123",
        base_ref="main",
    )

    codex_calls = [call for call in calls if call[0][:2] == ["codex", "exec"]]
    assert len(codex_calls) == 2
    full_args, full_prompt = codex_calls[1]
    assert 'model_reasoning_effort="high"' in full_args
    assert "concurrency" in full_prompt
    assert "Can two callers enter the mutation concurrently?" in full_prompt
    assert result.passed
    assert result.profile.effort == "high"
    rendered = review_gate.render_structured_summary(result)
    assert "## Review profile" in rendered
    assert "- Model: gpt-5.6-sol" in rendered
    assert "- Reasoning effort: high" in rendered


def test_materiality_contract_suppresses_noise_and_groups_one_root_cause(
    monkeypatch, tmp_path: Path
) -> None:
    review_gate = load_review_gate()
    first = material_finding()
    duplicate = material_finding(
        title="Fix the second unsafe workflow instance",
        body="A second job executes the same untrusted checkout.",
        files=[".github/workflows/second.yml"],
    )
    p3 = material_finding(severity="P3", title="Document another example")
    already_caught = material_finding(title="Repeat the failing CI assertion")
    already_caught["already_caught_by"] = "CI: workflow-policy"
    suppressed = material_finding(title="Rename the helper")
    suppressed["disposition"] = "suppress"

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        if args[:3] == ["git", "fetch", "--force"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["codex", "exec"]:
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(
                json.dumps(
                    {
                        "verdict": "request_changes",
                        "summary": "One material root cause remains.",
                        "findings": [first, duplicate, p3, already_caught, suppressed],
                    }
                )
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
        profile=review_gate.ReviewProfile(effort="high"),
    )

    assert not result.passed
    assert len(result.blocking) == 1
    assert result.blocking[0].title == "Fix the unsafe workflow"
    assert set(result.blocking[0].files) == {
        ".github/workflows/review-gate.yml",
        ".github/workflows/second.yml",
    }
    assert result.warnings == ()


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

    assert review_gate.submit_pr_approval(
        "example-org/example", 12, "abc123", "AI review passed"
    )
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
            "commit_id=abc123",
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


def test_run_codex_review_rejects_invalid_output_contract(monkeypatch, tmp_path) -> None:
    review_gate = load_review_gate()

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        if args[:3] == ["git", "fetch", "--force"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["codex", "exec"]:
            out = Path(args[args.index("--output-last-message") + 1])
            # Unknown verdict + no findings would otherwise be a silent pass.
            out.write_text('{"verdict":"looks-fine","summary":"ok","findings":[]}')
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)
    result = review_gate.run_codex_review(
        tmp_path, "Review.", repo="o/r", pr_number=7, sha="abc", base_ref="main"
    )
    assert not result.passed
    assert result.blocking[0].code == "CODEX_REVIEW_INVALID"


def test_run_codex_review_rejects_malformed_finding_entries(monkeypatch, tmp_path) -> None:
    """A non-object finding entry must fail the contract, not be silently dropped."""
    review_gate = load_review_gate()

    def make_fake(findings_json):
        def fake(args, cwd=None, env=None, input_text=None):
            if args[:3] == ["git", "fetch", "--force"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            if args[:2] == ["codex", "exec"]:
                out = Path(args[args.index("--output-last-message") + 1])
                out.write_text(
                    '{"verdict":"request_changes","summary":"s","findings":' + findings_json + "}"
                )
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return fake

    for bad in ("[null]", "[123]", '[{"severity":5,"title":"x"}]', '[{"files":"nope"}]'):
        monkeypatch.setattr(review_gate, "run_command", make_fake(bad))
        result = review_gate.run_codex_review(
            tmp_path, "Review.", repo="o/r", pr_number=7, sha="abc", base_ref="main"
        )
        assert not result.passed, bad
        assert result.blocking[0].code == "CODEX_REVIEW_INVALID", bad


def test_fetch_issue_comments_flattens_multiple_pages(monkeypatch) -> None:
    """gh --paginate --slurp yields an array-of-pages; it must be flattened."""
    review_gate = load_review_gate()

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        assert "--slurp" in args and "--paginate" in args
        pages = [
            [{"id": 1, "body": "a"}, {"id": 2, "body": "b"}],
            [{"id": 3, "body": "c"}],
        ]
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(pages), stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)
    comments = review_gate.fetch_issue_comments("example-org/example", 7)
    assert [c["id"] for c in comments] == [1, 2, 3]


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


def test_fast_mode_is_removed_from_the_supported_interface() -> None:
    reusable = REUSABLE_WORKFLOW_PATH.read_text()
    script = SCRIPT_PATH.read_text()

    assert "fast_codex_model:" not in reusable
    assert '--mode "${{ inputs.mode }}"' not in reusable
    assert "REVIEW_GATE_FAST_CODEX_MODEL" not in reusable
    assert "REVIEW_GATE_STATE_KEY" not in reusable
    assert "fast advisory" not in reusable.lower()
    assert "FAST_COMMENT_MARKER" not in script
    assert "run_fast_advisory" not in script
    assert not (ROOT / "docs" / "review-gate-fast-mode.md").exists()
    assert "--submit-approval" in reusable
