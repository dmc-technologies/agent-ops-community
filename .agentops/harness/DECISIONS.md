# Decisions

Repository: `agent-ops-community`

Record durable architecture, workflow, and harness decisions here.

### 2026-08-09: Prime Agent uses native handoff and namespaced owned bundles

- Decision: register Prime Agent as `prime-agent`; require every framework command handoff to receive an existing directory through `--cwd`; hand context packs to `prime-agent --print --cwd <repo> -- <prompt>` using that same directory; default installs to `${PRIME_AGENT_CODING_AGENT_DIR:-$HOME/.prime/agent}`; and generate namespaced `agentops-gstack-*` and `agentops-superpowers-*` skills with fingerprint ownership manifests outside the shared skill namespace.
- Rationale: Prime Agent 0.7.1 discovers coding-agent resources through `PRIME_AGENT_CODING_AGENT_DIR`, while the upstream pinned bundles contain provider-specific paths and tool contracts. Explicit Prime generation plus collision preflight preserves frequently used workflows without overwriting a person’s skills or exposing raw Claude instructions. A validated repository directory also prevents the reported adapter working directory from diverging from Prime Agent’s `--cwd`.
- Applies to: framework and registry models, adapters, bootstrap generation, Prime bundle adapters, skill dependency installation, public data registries, documentation, and tests.
- Revisit when: Prime Agent changes its CLI or skill discovery contract, or either upstream project publishes and tests an equivalent Prime host package.

### 2026-08-08: AI review runs once on the final head with risk-routed Sol effort

- Decision: a person applies `ai review` once when a pull request's final head is ready. A low-effort `gpt-5.6-sol` classifier evaluates the complete diff and routes the full review to the same model at low, medium, high, or xhigh effort. Low confidence and critical authority domains route to xhigh. Auto-labeling and fast advisory mode are removed.
- Rationale: historical review data showed repeated full reviews concentrated cost and comments on a few difficult pull requests. Final-head triggering removes repeated full reviews, while risk routing preserves xhigh for credentials, authorization, engineering source authority, persistent evidence, and similarly difficult changes regardless of diff size.
- Applies to: `.github/workflows/review-gate.yml`, `.github/workflows/review-gate-reusable.yml`, `.github/scripts/review_gate.py`, and `tests/test_review_gate.py`.
- Revisit when: production profile summaries provide enough low, medium, high, and xhigh outcomes to compare material-finding recall and run cost by tier.

### 2026-08-08: Review Gate blocks only current material implementation defects

- Decision: a Codex observation blocks only when the pull request introduced an incorrect current behavior, a plausible current path reaches it, the consequence is material, evidence and confidence are high, the issue is not already reported by deterministic checks, and a correction is required before merge. Missing tests, possible future regressions, style, general hardening, and approval prerequisites do not block. Repeated instances are grouped by root cause.
- Rationale: the historical sample contained many requests for coverage, documentation, hardening, and prerequisites without a demonstrated current material consequence. Exact-head shadow evaluation removed one such test-only blocker while retaining a current false-verification defect.
- Applies to: `.github/review-gate-prompt.md`, `.github/scripts/review_gate.py`, `tests/test_review_gate.py`, and `docs/review-gate-evaluation.md`.
- Revisit when: shadow or production review shows a material defect was suppressed, or non-material observations still create review cycles.

### 2026-08-03: Repositories own their CI tier vocabulary

- Decision: newly generated harnesses use `Harness Check` and `CI Contract`
  sections. The checker also accepts a complete legacy `Fast Gate` and `Full
  Gate` pair so released harnesses remain valid after upgrade. Each repository
  defines its named local tiers and hosted routing inside `CI Contract`.
- Rationale: verification cost and deployment boundaries differ by project.
  The shared harness should ensure the contract is visible without imposing
  terminology that conflicts with the repository's executable CI interface.
- Applies to: `src/agent_ops/harness.py`, generated
  `.agentops/harness/VERIFY.md` files, and `tests/test_harness.py`.
- Revisit when: Agent Ops gains a typed, machine-readable CI-tier schema shared
  by multiple repositories.

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
