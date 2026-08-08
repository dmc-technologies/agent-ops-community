# Progress

Repository: `agent-ops-community`

## Current State

- Branch: `agent/review-gate-risk-routing`
- Latest commit: `origin/main` at branch creation.
- Verification: full tests, lint, source-tree harness check, whitespace validation, and historical shadow evaluation pass.

## Current Work

- Goal: preserve material AI review findings while reducing repeated review cost and non-material review cycles.
- Active task: publish the final-label risk router and materiality contract for review.
- Files in play: Review Gate workflows, script, prompt, tests, and evaluation evidence.
- Blockers: none

## Session Log

- 2026-08-08: Changed Review Gate to run only when a person applies `ai review` to the final pull-request head. Removed auto-labeling and all fast advisory code, inputs, state, tests, and documentation.
- 2026-08-08: Added a low-effort `gpt-5.6-sol` classifier that routes the complete review to Sol at low, medium, high, or xhigh effort based on consequence and review difficulty. Low confidence and critical authority domains route to xhigh.
- 2026-08-08: Added a structured materiality contract that suppresses missing-test and future-regression observations without an incorrect current behavior, suppresses findings already reported by deterministic checks, and groups repeated instances by root cause.
- 2026-08-08: Completed the historical shadow evaluation recorded in `docs/review-gate-evaluation.md`: 11 routing cases separated four high-effort changes from seven xhigh changes; one former test-only blocker passed; one current false-verification path still blocked.
- 2026-08-03: Changed the generic verification template and validator to
  require `CI Contract` while leaving tier names to each repository.
- 2026-08-03: Preserved upgrade safety by accepting a complete legacy
  `Fast Gate` and `Full Gate` pair while generating only the new contract.
- 2026-06-19: Ported an engineering-repository Review Gate into a generic
  label-driven workflow for public Agent Ops. Adding the `ai review` label runs
  deterministic preflight plus Codex review on the PR diff, posts a status,
  summary comment, per-finding comments, and attempts an approving PR review.
- 2026-06-19: Hardened Review Gate after external review: label-triggered
  review now loads gate code from the default branch, reruns for new commits on
  already-labeled PRs, loads `.github/review-gate-prompt.md`, and passes a tougher public-safe architecture, AI,
  mechanical/domain, product, security, and evidence prompt into Codex.

## Verification Log

- 2026-08-08: `.venv/bin/python -m pytest -q` passed (`57 passed`); `.venv/bin/ruff check .` passed; `.venv/bin/agentops harness check .` passed; `git diff --check` passed.
- 2026-08-08: Exact-head materiality rerun passed with zero blockers in 564.8 seconds at xhigh for a former test-only finding; exact-head recall rerun returned one material root-cause blocker in 1,031.9 seconds at xhigh for a current false-verification path.
- 2026-08-03: `PYTHONPATH=src python -m pytest tests/test_harness.py
  tests/test_public_safety.py -q` passed (`5 passed`); `ruff check
  src/agent_ops/harness.py tests/test_harness.py tests/test_public_safety.py`
  passed; `PYTHONPATH=src agentops harness check .` passed; `git diff --check`
  passed.
- 2026-06-19: `python -m pytest tests/test_review_gate.py -q` passed (`4 passed`).
- 2026-06-19: `ruff check .github/scripts/review_gate.py tests/test_review_gate.py` passed.
- 2026-06-19: Codex review dry-run against the committed deterministic Review
  Gate correctly failed because the prompt was not yet wired into the reviewer.
- 2026-06-19: hardening validation passed: `python -m pytest tests/test_review_gate.py tests/test_public_safety.py -q`,
  `ruff check .github/scripts/review_gate.py tests/test_review_gate.py`,
  `git diff --check`, and `agentops harness check .`.

## Next Actions

1. Commit, push, open the pull request, and wait for exact-head hosted CI.
2. After approval and merge, update downstream callers to the final-label-only workflow.
