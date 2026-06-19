# Decisions

Repository: `agent-ops-community`

Record durable architecture, workflow, and harness decisions here.

### 2026-06-19: AI review is label-driven and PR-backed

- Decision: PRs can run deterministic AI review by adding the `ai review` label. The workflow posts the `Review Gate` status, writes a durable findings comment, and attempts an approving PR review on pass.
- Rationale: Labels, commit statuses, PR comments, and PR reviews are visible GitHub primitives that stay CLI-verifiable without adding a daemon or separate review service.
- Applies to: `.github/workflows/review-gate.yml`, `.github/scripts/review_gate.py`, `tests/test_review_gate.py`.
- Revisit when: GitHub token policy blocks approval submission or Agent Ops adopts a stronger shared review backend.

## Template

### YYYY-MM-DD: Decision title

- Decision: what changed or what standard was chosen.
- Rationale: why this is the right tradeoff.
- Applies to: files, commands, workflows, or repositories affected.
- Revisit when: condition that should trigger review or removal.
