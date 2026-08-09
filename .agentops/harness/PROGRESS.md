# Progress

Repository: `agent-ops-community`

## Current State

- Branch: `feat/prime-agent-first-class`
- Latest commit before current work: `origin/main` (`bcada9b`).
- Verification: full tests, lint, source-tree harness check, and whitespace validation pass.

## Current Work

- Goal: make Prime Agent a first-class supported framework.
- Active task: complete; implementation, documentation, and tests are recorded on this branch.
- Files in play: framework registry and adapter, bootstrap, skill registries and installer, documentation, and tests.
- Blockers: none

## Session Log

- 2026-08-09: Added Prime Agent framework registration, `prime-agent --print --cwd` context handoff, generated bootstrap support, the native `~/.prime/agent` home, pinned gstack and Superpowers install mappings, registry metadata, documentation, and tests.

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

- 2026-08-09: `uv run --extra dev pytest tests/test_frameworks.py tests/test_skill_installer.py tests/test_cli.py -q` passed (`21 passed`); `uv run --extra dev agentops frameworks command examples/local-smoke.yaml --framework prime-agent --json` produced the expected print-mode handoff; `.venv/bin/ruff check .` passed; `.venv/bin/pytest -q` passed (`60 passed`); `.venv/bin/agentops harness check .` passed; `git diff --check` passed.

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

1. Open a pull request when requested; do not push from this task.
