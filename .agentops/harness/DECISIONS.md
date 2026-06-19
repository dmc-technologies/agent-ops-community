# Decisions

Repository: `agent-ops-community`

Record durable architecture, workflow, and harness decisions here.

### 2026-06-19: AI review is label-driven, Codex-backed, and PR-backed

- Decision: PRs can run Codex-backed AI review by adding the `ai review` label. The workflow runs deterministic preflight checks first, invokes Codex with `.github/review-gate-prompt.md` against the PR diff, posts the `Review Gate` status, writes a durable summary comment plus per-finding comments, and attempts an approving PR review on pass.
- Rationale: Labels, commit statuses, PR comments, and PR reviews are visible GitHub primitives that stay CLI-verifiable without adding a daemon or separate review service.
- Applies to: `.github/workflows/review-gate.yml`, `.github/scripts/review_gate.py`, `tests/test_review_gate.py`.
- Revisit when: GitHub token policy blocks approval submission or Agent Ops adopts a stronger shared review backend.

### 2026-06-19: AI review uses trusted gate code and proposal-aligned principles

- Decision: Label-driven AI review loads the gate script from the default branch, reruns when an already-labeled PR receives new commits, and passes a harder public-safe prompt into Codex covering architecture, AI, mechanical/domain, product, security, evidence, and deployment posture.
- Rationale: Review automation should not execute PR-controlled gate code with write permissions, approvals must not go stale after new commits, and the review lens should enforce source-grounded engineering evidence without naming private proposal details.
- Applies to: `.github/workflows/review-gate.yml`, `.github/review-gate-prompt.md`, `tests/test_review_gate.py`.
- Revisit when the review backend can enforce these principles with richer inline review APIs or repository-specific policy packs.

## Template

### YYYY-MM-DD: Decision title

- Decision: what changed or what standard was chosen.
- Rationale: why this is the right tradeoff.
- Applies to: files, commands, workflows, or repositories affected.
- Revisit when: condition that should trigger review or removal.
