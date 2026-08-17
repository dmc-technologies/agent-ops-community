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
POLICY_PATH = ROOT / "docs" / "review-gate-policy.md"


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
    assert "codex_model:" not in workflow
    assert "REVIEW_GATE_CODEX_MODEL" not in workflow
    assert "Resolve PR" in workflow
    assert "github.event.label.name == 'ai review'" in workflow
    assert "github.event.label.name == 'critical'" in workflow
    assert "contains(github.event.pull_request.labels.*.name, 'ai review')" in workflow
    assert "github.event.action != 'labeled'" not in workflow
    assert "name: Review Gate" in workflow
    assert "npm install -g @openai/codex@0.144.1" not in workflow
    assert "python review-gate-main/.github/scripts/review_gate.py" not in workflow

    assert "workflow_call:" in reusable
    assert "codex_model:" not in reusable
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
    assert "REVIEW_GATE_CODEX_MODEL" not in reusable
    assert "--base-ref" not in reusable
    assert "ref: ${{ github.sha }}" not in workflow
    assert "ref: ${{ github.ref_name }}" not in workflow
    assert "REVIEW_GATE_BACKEND: deterministic" not in workflow
    assert "issue_comment" not in workflow
    assert "/agent-review" not in workflow


def test_ai_review_can_be_requested_by_a_person_or_authorized_agent() -> None:
    assert not AUTO_LABEL_WORKFLOW_PATH.exists()
    workflow = WORKFLOW_PATH.read_text()
    prompt = PROMPT_PATH.read_text()
    assert "types: [labeled]" in workflow
    assert "opened" not in workflow
    assert "synchronize" not in workflow
    assert "person or authorized agent" in prompt
    assert "a person applies the label again" not in prompt


def test_bounded_review_policy_is_documented_for_callers() -> None:
    policy = POLICY_PATH.read_text()
    readme = (ROOT / "README.md").read_text()

    assert "Review once. Block only proven critical defects." in policy
    assert "File verified noncritical defects in one follow-up issue." in policy
    assert "Recheck only the critical fixes." in policy
    assert (
        "security, safety, data loss, broken core behavior, or false acceptance evidence"
        in policy
    )
    assert "person or authorized agent" in policy
    assert "`critical`" in policy
    assert "docs/review-gate-policy.md" in readme


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


def test_codex_review_ignores_model_override_environment(
    monkeypatch, tmp_path: Path
) -> None:
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
    assert codex_args[codex_args.index("--model") + 1] == "gpt-5.6-sol"
    assert codex_env is not None
    assert "REVIEW_GATE_CODEX_MODEL" not in codex_env


def test_review_gate_uses_sol_and_explicit_reasoning_effort() -> None:
    review_gate = load_review_gate()

    assert review_gate.codex_model_args(effort="xhigh") == [
        "--model",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="xhigh"',
    ]


def test_scope_classifier_uses_changed_paths_without_a_model_call() -> None:
    review_gate = load_review_gate()

    lite = review_gate.classify_review_profile(["docs/widget.md", "README.md"])
    critical = review_gate.classify_review_profile(
        [
            ".github/workflows/review-gate.yml",
            "configs/global/AGENTS.md",
            "src/auth/permissions.py",
        ]
    )
    unknown = review_gate.classify_review_profile(None)
    critical_files = review_gate.classify_review_profile(
        ["src/auth.py", "Dockerfile", "pyproject.toml", ".github/actions/deploy/action.yml"]
    )
    unmatched_behavior = review_gate.classify_review_profile(
        [
            "src/agent_ops/verify.py",
            "src/agent_ops/process.py",
            "src/agent_ops/plugins.py",
            "docs/build.py",
        ]
    )
    docs_behavior = review_gate.classify_review_profile(["docs/build.py"])

    assert lite.scope == "lite"
    assert lite.effort == "medium"
    assert critical.scope == "critical"
    assert critical.effort == "xhigh"
    assert "privileged_workflow" in critical.risk_domains
    assert "ci_policy" in critical.risk_domains
    assert "authorization" in critical.risk_domains
    assert unknown.scope == "critical"
    assert unknown.effort == "xhigh"
    assert critical_files.scope == "critical"
    assert unmatched_behavior.scope == "critical"
    assert docs_behavior.scope == "critical"

    forced_lite = review_gate.apply_scope_policy(critical_files, requested_scope="lite")
    raised_resolution = review_gate.apply_scope_policy(lite, prior_scope="critical")

    assert forced_lite.scope == "critical"
    assert raised_resolution.scope == "critical"


def test_routed_review_uses_one_model_call_with_deterministic_scope(
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
                    {"verdict": "approve", "summary": "No critical findings.", "findings": []}
                )
            )
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
        changed_files=[".github/workflows/review-gate.yml"],
    )

    codex_calls = [call for call in calls if call[0][:2] == ["codex", "exec"]]
    assert len(codex_calls) == 1
    full_args, full_prompt = codex_calls[0]
    assert 'model_reasoning_effort="xhigh"' in full_args
    assert "critical" in full_prompt
    assert "privileged_workflow" in full_prompt
    assert result.passed
    assert result.profile.scope == "critical"
    assert result.profile.effort == "xhigh"
    rendered = review_gate.render_structured_summary(result)
    assert "## Review profile" in rendered
    assert "- Model: gpt-5.6-sol" in rendered
    assert "- Scope: critical" in rendered
    assert "- Reasoning effort: xhigh" in rendered


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
    suppressed["disposition"] = "ignore"

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


def test_only_proven_critical_findings_block_and_other_defects_become_follow_ups(
    monkeypatch, tmp_path: Path
) -> None:
    review_gate = load_review_gate()
    critical = material_finding(title="Prevent credential exposure")
    noncritical = material_finding(
        severity="P2",
        title="Preserve the optional display value",
        root_cause="optional-display-value",
        files=["src/view.py"],
    )
    noncritical["disposition"] = "follow_up"
    noncritical["consequence_class"] = "correctness"
    noncritical["consequence"] = "An optional field is omitted from one secondary view."
    duplicate = dict(noncritical)
    duplicate["title"] = "Preserve the second optional display value"
    duplicate["files"] = ["src/other_view.py"]
    ignored = dict(noncritical)
    ignored["disposition"] = "ignore"
    ignored["root_cause"] = "style-only"

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        if args[:3] == ["git", "fetch", "--force"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[:2] == ["codex", "exec"]:
            output_path = Path(args[args.index("--output-last-message") + 1])
            output_path.write_text(
                json.dumps(
                    {
                        "verdict": "request_changes",
                        "summary": "One critical defect and one follow-up remain.",
                        "findings": [critical, noncritical, duplicate, ignored],
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
        profile=review_gate.ReviewProfile(scope="critical", effort="xhigh"),
    )

    assert [finding.title for finding in result.blocking] == ["Prevent credential exposure"]
    assert len(result.warnings) == 1
    assert result.warnings[0].title == "Preserve the optional display value"
    assert set(result.warnings[0].files) == {"src/view.py", "src/other_view.py"}


def test_critical_consequence_blocks_regardless_of_severity() -> None:
    review_gate = load_review_gate()
    payload = material_finding(severity="P3", title="Prevent credential exposure")

    disposition, finding = review_gate.finding_from_payload(payload, 0)

    assert disposition == "block"
    assert finding.severity == "P3"


def test_resolution_prompt_checks_prior_blockers_and_fix_delta_without_rediscovery() -> None:
    review_gate = load_review_gate()
    blocker = review_gate.Finding(
        "P1",
        "CODEX_P1_1",
        "Prevent credential exposure",
        "Behavior: a token is printed\nConsequence: credential exposure",
        ("src/auth.py",),
        "credential-exposure",
    )

    prompt = review_gate.build_codex_prompt(
        "Review strictly.",
        repo="example-org/example",
        pr_number=7,
        sha="def456",
        base_ref="main",
        base_diff_ref="refs/remotes/review-gate-base/main",
        profile=review_gate.ReviewProfile(scope="critical", effort="xhigh"),
        stage="resolution",
        delta_from_sha="abc123",
        carried_findings=(blocker,),
    )

    assert "git diff abc123...HEAD" in prompt
    assert "Prevent credential exposure" in prompt
    assert "credential-exposure" in prompt
    assert "Do not repeat the discovery review" in prompt
    assert "new critical defect caused by the fix" in prompt


def test_resolution_state_is_signed_and_rejects_tampering(monkeypatch) -> None:
    review_gate = load_review_gate()
    monkeypatch.setenv("REVIEW_GATE_STATE_KEY", "gate-key")
    state = review_gate.ResolutionState(
        repo="example-org/example",
        pr_number=7,
        base_ref="main",
        merge_base_sha="base123",
        sha="abc123",
        scope="critical",
        findings=(
            review_gate.Finding(
                "P1", "CODEX_P1_1", "Fix auth", "Specific failure", ("auth.py",), "auth"
            ),
        ),
    )

    signed_state = review_gate.sign_resolution_state(state)

    assert signed_state
    assert review_gate.verify_resolution_state(signed_state) == state
    assert review_gate.verify_resolution_state(signed_state + "tampered") is None


def test_resolution_stage_uses_only_a_descendant_fix_delta(monkeypatch, tmp_path: Path) -> None:
    review_gate = load_review_gate()
    state = review_gate.ResolutionState(
        repo="example-org/example",
        pr_number=7,
        base_ref="main",
        merge_base_sha="base123",
        sha="abc123",
        scope="lite",
        findings=(
            review_gate.Finding(
                "P1", "CODEX_P1_1", "Fix auth", "Specific failure", ("auth.py",), "auth"
            ),
        ),
    )

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        assert args == ["git", "merge-base", "--is-ancestor", "abc123", "HEAD"]
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)

    context = {
        "repo": "example-org/example",
        "pr_number": 7,
        "base_ref": "main",
        "merge_base_sha": "base123",
    }
    resolution = review_gate.select_review_stage(tmp_path, "def456", state, **context)
    unchanged = review_gate.select_review_stage(tmp_path, "abc123", state, **context)
    copied = review_gate.select_review_stage(
        tmp_path,
        "def456",
        state,
        repo="example-org/example",
        pr_number=8,
        base_ref="main",
        merge_base_sha="base123",
    )

    assert resolution.name == "resolution"
    assert resolution.scope == "critical"
    assert resolution.delta_from_sha == "abc123"
    assert resolution.carried_findings == state.findings
    assert unchanged.name == "unchanged"
    assert copied.name == "discovery"


def test_resolution_state_without_a_key_requires_the_exact_gate_identity(monkeypatch) -> None:
    review_gate = load_review_gate()
    monkeypatch.delenv("REVIEW_GATE_STATE_KEY", raising=False)
    state = review_gate.ResolutionState(
        repo="example-org/example",
        pr_number=7,
        base_ref="main",
        merge_base_sha="base123",
        sha="abc123",
        scope="lite",
        findings=(
            review_gate.Finding(
                "P1", "CODEX_P1_1", "Fix auth", "Specific failure", ("auth.py",), "auth"
            ),
        ),
    )
    body = review_gate.build_review_comment(
        review_gate.ReviewResult("codex", blocking=state.findings),
        resolution_state=state,
    )
    comments = [
        {"id": 1, "body": body, "user": {"login": "attacker"}},
        {"id": 2, "body": body, "user": {"login": "github-actions[bot]"}},
    ]

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        if args[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="unavailable")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(comments), stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)

    assert review_gate.read_resolution_state("example-org/example", 7) == state


def test_resolution_state_rejects_model_injected_and_keyed_unsigned_markers(monkeypatch) -> None:
    review_gate = load_review_gate()
    state = review_gate.ResolutionState(
        repo="example-org/example",
        pr_number=7,
        base_ref="main",
        merge_base_sha="base123",
        sha="abc123",
        scope="critical",
        findings=(
            review_gate.Finding(
                "P1", "CODEX_P1_1", "Fix auth", "Specific failure", ("auth.py",), "auth"
            ),
        ),
    )
    monkeypatch.delenv("REVIEW_GATE_STATE_KEY", raising=False)
    unsigned = review_gate.sign_resolution_state(state)
    monkeypatch.setenv("REVIEW_GATE_STATE_KEY", "gate-key")
    injected = review_gate.build_review_comment(
        review_gate.ReviewResult(
            "codex",
            summary=f"Model repeated {review_gate.RESOLUTION_STATE_MARKER_PREFIX}{unsigned} -->",
        )
    )
    keyed_unsigned_header = "\n".join(
        (
            review_gate.COMMENT_MARKER,
            "# Review Gate agent review",
            f"{review_gate.RESOLUTION_STATE_MARKER_PREFIX}{unsigned} -->",
        )
    )
    comments = [
        {"id": 1, "body": injected, "user": {"login": "gate-bot"}},
        {"id": 2, "body": keyed_unsigned_header, "user": {"login": "gate-bot"}},
    ]

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        if args[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(args, 0, stdout="gate-bot\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(comments), stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)

    assert review_gate.read_resolution_state("example-org/example", 7) is None


def test_changed_files_are_derived_from_the_exact_checked_out_head(monkeypatch, tmp_path) -> None:
    review_gate = load_review_gate()
    calls = []

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append(args)
        if args == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="a" * 40 + "\n", stderr="")
        if args == ["git", "diff", "--name-only", "b" * 40 + "..." + "a" * 40]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="src/agent_ops/verify.py\ntests/test_verify.py\n",
                stderr="",
            )
        raise AssertionError(args)

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)

    changed = review_gate.changed_files_for_exact_head(tmp_path, "b" * 40, "a" * 40)

    assert changed == ["src/agent_ops/verify.py", "tests/test_verify.py"]
    assert calls[-1][-1] == "b" * 40 + "..." + "a" * 40


def test_noncritical_findings_are_grouped_into_one_follow_up_issue(monkeypatch) -> None:
    review_gate = load_review_gate()
    calls = []
    findings = (
        review_gate.Finding(
            "P2", "CODEX_P2_1", "Preserve display value", "Secondary view omits value.",
            ("src/view.py",), "display-value"
        ),
        review_gate.Finding(
            "P3", "CODEX_P3_2", "Clarify retry message", "Retry text is misleading.",
            ("src/retry.py",), "retry-message"
        ),
    )

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append(args)
        if args[:3] == ["gh", "api", "repos/example-org/example/issues"] and "--paginate" in args:
            return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")
        if args[:3] == ["gh", "api", "repos/example-org/example/issues"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout='{"html_url":"https://github.com/example-org/example/issues/12"}',
                stderr="",
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)
    issue_url = review_gate.post_or_update_follow_up_issue(
        "example-org/example", 7, "abc123", findings
    )

    assert issue_url == "https://github.com/example-org/example/issues/12"
    listing = next(call for call in calls if "--paginate" in call)
    assert listing[listing.index("--method") + 1] == "GET"
    assert "--slurp" in listing
    assert "per_page=100" in listing
    create = next(call for call in calls if "--method" in call and "POST" in call)
    body = next(arg.removeprefix("body=") for arg in create if arg.startswith("body="))
    assert "Follow-up findings from PR #7" in body
    assert "Preserve display value" in body
    assert "Clarify retry message" in body


def test_follow_up_update_ignores_forged_pr_and_preserves_prior_findings(monkeypatch) -> None:
    review_gate = load_review_gate()
    calls = []
    prior = review_gate.Finding(
        "P2",
        "CODEX_P2_1",
        "Preserve display value",
        "Secondary view omits value.",
        ("src/view.py",),
        "display-value",
    )
    current = review_gate.Finding(
        "P3",
        "CODEX_P3_2",
        "Clarify retry message",
        "Retry text is misleading.",
        ("src/retry.py",),
        "retry-message",
    )
    marker = f"{review_gate.FOLLOW_UP_ISSUE_MARKER_PREFIX}example-org/example#7 -->"
    issues = [
        {
            "number": 99,
            "body": marker,
            "user": {"login": "attacker"},
            "pull_request": {"url": "https://api.example.test/pulls/99"},
        },
        {
            "number": 12,
            "body": review_gate.build_follow_up_issue_body(
                "example-org/example", 7, "abc123", (prior,)
            ),
            "user": {"login": "gate-bot"},
            "html_url": "https://github.com/example-org/example/issues/12",
        },
    ]

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append(args)
        if args[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(args, 0, stdout="gate-bot\n", stderr="")
        if "--paginate" in args:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([issues]), stderr="")
        if "PATCH" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout='{"html_url":"https://github.com/example-org/example/issues/12"}',
                stderr="",
            )
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)
    review_gate.post_or_update_follow_up_issue(
        "example-org/example", 7, "def456", (current,)
    )

    update = next(call for call in calls if "PATCH" in call)
    assert "repos/example-org/example/issues/12" in update
    body = next(arg.removeprefix("body=") for arg in update if arg.startswith("body="))
    assert "Preserve display value" in body
    assert "Clarify retry message" in body


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
            "user": {"login": "gate-bot"},
        },
        {
            "id": 102,
            "body": "Unrelated human comment",
        },
    ]

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append(args)
        if args[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(args, 0, stdout="gate-bot\n", stderr="")
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


def test_analyze_workspace_distinguishes_context_token_from_literal_credential(
    tmp_path: Path,
) -> None:
    review_gate = load_review_gate()
    ordinary = tmp_path / "ordinary.py"
    ordinary.write_text(
        "token = very_long_context_variable_name.set(value)\n"
    )
    credential = tmp_path / "credential.py"
    credential.write_text(
        "token = " + repr("abcdefghijklmnopqrstuvwx") + "\n"
    )

    ordinary_result = review_gate.analyze_workspace(tmp_path, [ordinary.name])
    credential_result = review_gate.analyze_workspace(tmp_path, [credential.name])

    assert ordinary_result.passed
    assert not credential_result.passed
    assert credential_result.blocking[0].code == "POSSIBLE_SECRET"


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


def test_pr_comment_updates_only_the_exact_gate_identity(monkeypatch) -> None:
    review_gate = load_review_gate()
    calls = []
    comments = [
        {
            "id": 1,
            "body": review_gate.COMMENT_MARKER,
            "user": {"login": "attacker"},
        },
        {
            "id": 2,
            "body": f"Model repeated {review_gate.COMMENT_MARKER}",
            "user": {"login": "gate-bot"},
        },
        {
            "id": 3,
            "body": "\n".join(
                (review_gate.COMMENT_MARKER, "# Review Gate agent review")
            ),
            "user": {"login": "gate-bot"},
        },
    ]

    def fake_run_command(args, cwd=None, env=None, input_text=None):
        calls.append(args)
        if args[:3] == ["gh", "api", "user"]:
            return subprocess.CompletedProcess(args, 0, stdout="gate-bot\n", stderr="")
        if args[:3] == ["gh", "api", "repos/example-org/example/issues/7/comments"]:
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(comments), stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(review_gate, "run_command", fake_run_command)
    review_gate.post_or_update_pr_comment("example-org/example", 7, "updated")

    patch_call = next(call for call in calls if "PATCH" in call)
    assert "repos/example-org/example/issues/comments/3" in patch_call
    assert all("issues/comments/1" not in arg for arg in patch_call)
    assert all("issues/comments/2" not in arg for arg in patch_call)


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


def test_targeted_resolution_replaces_fast_mode_in_the_supported_interface() -> None:
    reusable = REUSABLE_WORKFLOW_PATH.read_text()
    script = SCRIPT_PATH.read_text()

    assert "fast_codex_model:" not in reusable
    assert '--mode "${{ inputs.mode }}"' not in reusable
    assert "REVIEW_GATE_FAST_CODEX_MODEL" not in reusable
    assert "REVIEW_GATE_STATE_KEY:" in reusable
    assert "REVIEW_GATE_STATE_KEY: ${{ secrets.REVIEW_GATE_STATE_KEY }}" in reusable
    assert "scope:" in reusable
    assert '--scope "${{ inputs.scope }}"' in reusable
    assert "fast advisory" not in reusable.lower()
    assert "FAST_COMMENT_MARKER" not in script
    assert "run_fast_advisory" not in script
    assert not (ROOT / "docs" / "review-gate-fast-mode.md").exists()
    assert "--submit-approval" in reusable
