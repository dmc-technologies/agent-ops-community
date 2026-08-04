# Progress

Repository: `agent-ops-community`

## Current State

- Branch: `fix/harness-project-ci-contract`
- Latest commit: `origin/main` at branch creation.
- Verification: focused harness tests, lint, source-tree harness check, and
  whitespace validation pass.

## Current Work

- Goal: let repositories define their own CI tier vocabulary while preserving
  a visible, checkable harness contract.
- Active task: replace generic fast/full headings with a repository-owned
  `CI Contract` section and prove generated harnesses pass validation.
- Files in play: `src/agent_ops/harness.py`, `tests/test_harness.py`,
  `.agentops/harness/VERIFY.md`, `.agentops/harness/DECISIONS.md`, and
  `.agentops/harness/PROGRESS.md`.
- Blockers: none

## Session Log

- 2026-08-03: Changed the generic verification template and validator to
  require `CI Contract` while leaving tier names to each repository.
- 2026-06-19: Ported Momentum commit `c3a49fc` into a generic label-driven
  Review Gate workflow for public Agent Ops. Adding the `ai review` label runs
  deterministic preflight plus Codex review on the PR diff, posts a status,
  summary comment, per-finding comments, and attempts an approving PR review.
- 2026-06-19: Hardened Review Gate after external review: label-triggered
  review now loads gate code from the default branch, reruns for new commits on
  already-labeled PRs, loads `.github/review-gate-prompt.md`, and passes a tougher public-safe architecture, AI,
  mechanical/domain, product, security, and evidence prompt into Codex.

## Verification Log

- 2026-08-03: `PYTHONPATH=src
  /home/dmc-openclaw/agent-ops/.venv/bin/python -m pytest
  tests/test_harness.py -q` passed (`2 passed`);
  `/home/dmc-openclaw/agent-ops/.venv/bin/ruff check
  src/agent_ops/harness.py tests/test_harness.py` passed;
  `PYTHONPATH=src /home/dmc-openclaw/agent-ops/.venv/bin/agentops harness check
  .` passed; `git diff --check` passed.
- 2026-06-19: `python -m pytest tests/test_review_gate.py -q` passed (`4 passed`).
- 2026-06-19: `ruff check .github/scripts/review_gate.py tests/test_review_gate.py` passed.
- 2026-06-19: Codex review dry-run against the committed deterministic Review
  Gate correctly failed because the prompt was not yet wired into the reviewer.
- 2026-06-19: hardening validation passed: `python -m pytest tests/test_review_gate.py tests/test_public_safety.py -q`,
  `ruff check .github/scripts/review_gate.py tests/test_review_gate.py`,
  `git diff --check`, and `agentops harness check .`.

## Next Actions

1. Commit, push, and open a pull request.
2. Install the merged Agent Ops Community version in downstream environments.
