from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "review-gate.yml"
AUTO_LABEL_WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ai-review-autolabel.yml"
PROMPT_PATH = ROOT / ".github" / "review-gate-prompt.md"


def test_review_gate_delegates_to_agent_ops_reusable_workflow() -> None:
    workflow = WORKFLOW_PATH.read_text()

    assert "types: [opened, synchronize, reopened, labeled]" in workflow
    assert "uses: dmc-technologies/agent-ops/.github/workflows/review-gate-reusable.yml@main" in workflow
    assert "secrets: inherit" in workflow
    assert "head_repo: ${{ github.event.pull_request.head.repo.full_name }}" in workflow
    assert "head_sha: ${{ github.event.pull_request.head.sha }}" in workflow
    assert "base_ref: ${{ github.event.pull_request.base.ref }}" in workflow
    assert "codex_model: ${{ vars.REVIEW_GATE_CODEX_MODEL || '' }}" in workflow
    assert "github.event.label.name == 'ai review'" in workflow
    assert "github.event.action != 'labeled'" in workflow
    assert "contains(github.event.pull_request.labels.*.name, 'ai review')" not in workflow
    assert "Resolve PR" in workflow
    assert "npm install -g @openai/codex" not in workflow
    assert "python review-gate-main/.github/scripts/review_gate.py" not in workflow
    assert "review_gate.py" not in workflow


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
