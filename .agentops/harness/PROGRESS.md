# Progress

Repository: `agent-ops-community`

## Current State

- Branch: `codex/ai-review-prompt-hardening`
- Latest commit: `origin/main` at branch creation.
- Verification: focused Review Gate tests, lint, and Codex invocation tests pass locally.

## Current Work

- Goal: harden label-driven AI review for public Agent Ops PRs.
- Active task: open a follow-up PR for trusted review-gate checkout, automatic re-review on new commits, and stronger markdown-managed proposal-aligned review principles.
- Files in play: `.github/workflows/review-gate.yml`, `.github/scripts/review_gate.py`,
  `tests/test_review_gate.py`, `.agentops/harness/DECISIONS.md`, `.agentops/harness/PROGRESS.md`.
- Blockers: none

## Session Log

- 2026-06-19: Ported Momentum commit `c3a49fc` into a generic label-driven
  Review Gate workflow for public Agent Ops. Adding the `ai review` label runs
  deterministic preflight plus Codex review on the PR diff, posts a status,
  summary comment, per-finding comments, and attempts an approving PR review.
- 2026-06-19: Hardened Review Gate after external review: label-triggered
  review now loads gate code from the default branch, reruns for new commits on
  already-labeled PRs, loads `.github/review-gate-prompt.md`, and passes a tougher public-safe architecture, AI,
  mechanical/domain, product, security, and evidence prompt into Codex.

## Verification Log

- 2026-06-19: `python -m pytest tests/test_review_gate.py -q` passed (`4 passed`).
- 2026-06-19: `ruff check .github/scripts/review_gate.py tests/test_review_gate.py` passed.
- 2026-06-19: Codex review dry-run against the committed deterministic Review
  Gate correctly failed because the prompt was not yet wired into the reviewer.
- 2026-06-19: hardening validation passed: `python -m pytest tests/test_review_gate.py tests/test_public_safety.py -q`,
  `ruff check .github/scripts/review_gate.py tests/test_review_gate.py`,
  `git diff --check`, and `agentops harness check .`.

## Next Actions

1. Push `codex/ai-review-prompt-hardening` and open a PR.
2. Add the `ai review` label to prove the hardened GitHub label-triggered workflow path.
