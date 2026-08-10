# Progress

Repository: `agent-ops-community`

## Current State

- Branch: `fix/prime-runtime-contracts`
- Main baseline: `cfde2439cf7879e8470c1e2c017669566ab19eee`.
- Current handoff: pull request 11 and dependent private pull request 92 merged under Dan's explicit instruction while final Review Gate runs were pending. The completed public gate then identified incorrect external-provider labels, incomplete generated workflows, a Superpowers symlink escape, and unsafe custom-profile shell paths. This narrow correction excludes the unsupported workflows and fails closed on both path classes.
- Verification: Ruff, 97 tests, the source-tree harness check, and whitespace validation pass. A real pinned update removed the unsupported skills, retained 31 gstack and 14 Superpowers skills, rejected both reproduced symlink escapes and an unsafe profile path before writes, and preserved sandboxed headless browser navigation and screenshot capture.

## Current Work

- Goal: make Prime Agent a first-class supported framework without overwriting user-authored skills.
- Active task: complete and merge one narrow post-merge correction, then resume the paused canonical reinstall.
- Files in play: only the gstack adapter, Superpowers destination confinement, focused tests, Prime documentation, and this handoff.
- Blockers: full local verification and hosted review of the corrective pull request.

## Next Actions

- Run the full public verification set and exact four-finding acceptance reproductions.
- Open and merge the narrow corrective pull request after hosted CI and Review Gate pass.
- Reinstall from both canonical main branches, restart Prime sessions, and complete the approved Agent Knowledge write with immediate readback during clock-out.

## Session Log

- 2026-08-10: The narrow corrective implementation passed `uv run --extra dev ruff check .`, `uv run --extra dev pytest -q` (`97 passed`), `uv run --extra dev agentops harness check .`, and `git diff --check`. Exact pinned installation retained 31 gstack and 14 Superpowers skills; unsupported provider/runtime workflows were removed on update, every required retained reference resolved (the absent `VERSION` and `.git` paths are guarded optional update probes), unsafe profile paths and both symlink escapes failed before writes, and the retained headless browser navigated to `https://example.com/` and captured `/tmp/prime-gstack-hotfix.png` at 1280×720 with sandboxing preserved.
- 2026-08-10: Public pull request 11 merged as `cfde2439cf7879e8470c1e2c017669566ab19eee` and private pull request 92 merged as `9bd562ce3cb895bbd60586be53d41043b6c92211`. The late public Review Gate failed with four reproducible runtime and confinement defects, so canonical reinstall was paused. Dan approved one narrow correction.
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

- 2026-08-10: The latest Review Gate corrections bind every generated path to the selected Prime profile, exclude four hook-enforced gstack workflows that Prime cannot safely preserve, adapt investigate to a truthful manual scope boundary, package the required review, plan-review, design, and extension assets, and reject dirty Superpowers source checkouts. A real pinned install produced 38 gstack and 14 Superpowers skills in a custom disposable profile with no unresolved provider/profile markers; required runtime assets and review paths resolved. `uv run --extra dev ruff check .`, `uv run --extra dev pytest -q` (`91 passed`), `uv run --extra dev agentops harness check .`, and `git diff --check` passed.
- 2026-08-10: After the final Review Gate run, the Prime Superpowers adapter now proves closure for namespaced skill transitions and managed file references, states the child-to-parent reply contract correctly, and treats an empty native Prime profile override as unset. `uv run --extra dev ruff check .`, `uv run --extra dev pytest -q` (`88 passed`), `uv run --extra dev agentops harness check .`, and `git diff --check` passed; adapting the exact pinned Superpowers checkout produced all 14 skills with reference validation enabled.
- 2026-08-10: `uv run --extra dev ruff check .`, `uv run --extra dev pytest -q` (`85 passed`), `uv run --extra dev agentops harness check .`, and `git diff --check` passed for the final public branch. The staged gstack `browse` command navigated to `https://example.com`, returned HTTP 200, reported the expected URL, and captured a 1280×720 screenshot with Chromium sandboxing enabled.
- 2026-08-09: `uv run --extra dev ruff check .`, `uv run --extra dev pytest -q` (`85 passed`), `uv run --extra dev agentops harness check .`, and `git diff --check` passed after the Prime adapter changes. Real pinned Superpowers adaptation produced 14 unique namespaced skills with no raw provider path or unavailable-tool requirement. Real pinned gstack generation produced 42 unique namespaced skills and 726 managed files; required browser, design, and PDF executables were built, and `browse --help` ran from the staged runtime.
- 2026-08-09: `uv run --extra dev agentops skills install prime-agent --home <disposable-profile> --cache-dir <pinned-cache>` migrated the actual prior raw gstack and Superpowers installation in a disposable profile. It removed only the exact legacy bundle paths, preserved an unrelated skill byte-for-byte, produced 42 gstack and 14 Superpowers namespaced skills, wrote both ownership manifests, and ran the migrated `browse --help` executable.
- 2026-08-09: Two clean `prime-agent --print --no-session --offline` runs against the disposable migrated profile discovered and loaded `agentops-superpowers-verification-before-completion` and `agentops-gstack-careful`. The upstream Superpowers acceptance prompt `Let’s make a react todo list` automatically entered the adapted brainstorming workflow and asked its first visual-companion question without writing a file. The temporary profile referenced the existing auth file by symlink; its inode, size, and modification time remained unchanged.
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
