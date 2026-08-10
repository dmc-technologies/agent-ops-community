# Progress

Repository: `agent-ops-community`

## Current State

- Branch: `feat/prime-agent-first-class`
- Current head before this handoff update: `2dfc55f`.
- Current handoff: all four public pull request 11 Review Gate findings are corrected locally. Prime now uses its native profile variable and an explicit repository path, while gstack and Superpowers install through namespaced Prime adapters with fingerprint ownership and safe legacy migration.
- Verification: Ruff, 85 tests, the source-tree harness check, whitespace validation, real pinned Superpowers adaptation, and a real pinned gstack generation/runtime build pass. The generated gstack browser executable loads and prints its help; starting Chromium reaches the host sandbox restriction rather than a missing runtime dependency.

## Current Work

- Goal: make Prime Agent a first-class supported framework without overwriting user-authored skills.
- Active task: update pull request 11, rerun hosted CI and Review Gate, then merge the public dependency before the private Prime support pull request.
- Files in play: Prime framework handoff, bootstrap, skill registries, gstack and Superpowers adapters, documentation, tests, and this repository handoff.
- Blockers: hosted CI and Review Gate must pass on the final public head before merge.

## Next Actions

- Commit and push the final public corrections, then require CI and Review Gate to pass with no unresolved material finding.
- Merge pull request 11, update the coordinated private Agent Ops branch against public main, rerun its full verification, and merge it.
- Reinstall from both canonical main branches, restart Prime sessions, and complete the approved Agent Knowledge write with immediate readback during clock-out.

## Session Log

- 2026-08-09: Replaced raw Prime bundle copies with namespaced adapters. gstack now uses the pinned upstream generator, a Prime host/tool contract, compiled runtime assets, `agentops-gstack-*` names, and fingerprint rollback; Superpowers uses all 14 pinned skills under `agentops-superpowers-*` names with a Prime startup/tool contract and the same collision protections. Exact unmodified legacy copies migrate; changed or logically colliding skills are preserved and refused.
- 2026-08-09: Replaced the obsolete Prime home variable with Prime 0.7.1’s `PRIME_AGENT_CODING_AGENT_DIR`, made default skill installation honor it, and required a validated explicit `--cwd` for framework command handoffs without changing gstack or Superpowers install mappings.
- 2026-08-09: Clock-out recorded pull request 11 as pending external finalization after the public Prime implementation and real dependency installation passed locally. The dependent private pull request must follow this public merge.
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

- 2026-08-09: `uv run --extra dev ruff check .`, `uv run --extra dev pytest -q` (`85 passed`), `uv run --extra dev agentops harness check .`, and `git diff --check` passed after the Prime adapter changes. Real pinned Superpowers adaptation produced 14 unique namespaced skills with no raw provider path or unavailable-tool requirement. Real pinned gstack generation produced 42 unique namespaced skills and 726 managed files; required browser, design, and PDF executables were built, and `browse --help` ran from the staged runtime.
- 2026-08-09: `uv run --extra dev pytest tests/test_frameworks.py tests/test_skill_installer.py tests/test_cli.py tests/test_readme_smoke.py -q` passed (`25 passed`); final `uv run --extra dev pytest -q` passed (`62 passed`); final `uv run --extra dev ruff check .` and `uv run --extra dev agentops harness check .` passed; `git diff --check` and the obsolete-variable scan passed.
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
