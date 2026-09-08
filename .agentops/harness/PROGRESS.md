# Shared machine skills and policy implementation

An engineer can activate one exact selection of skill sources and optional global policy through the existing deployment transaction. Status verifies installed content and source mapping; rollback uses retained verified snapshots without a network fetch. Harness discovery registration and live machine migration are not implemented by this public core checkpoint.

- Approved plan: private Agent Ops `docs/superpowers/plans/2026-09-07-shared-skill-distribution.md`; later policy and repository enrollment additions are recorded in its integration addendum.
- Branch: `feat/shared-skill-distribution`, based on accepted `c8e0ac95ff20f0bf92c8d9027e6f9be85841e980`. No Plane work item applies. No merge authority was granted.
- Source choices are exact original Git commits; the collection fingerprint is never represented as an upstream commit. Named-reference source fetching retains its prior contract.
- The fixed selector uses the existing journal and ownership manifest. Atomic replacement does not promise consistent multiple file opens across an update; a reader needing that property must pin the resolved snapshot directory once.
- Shared activation currently supports macOS and Linux. Windows refuses before writing; existing Windows deployment remains supported separately.
- `agentops machine install|sync|status|rollback|source|updates` operate on an initialized machine registry. Installation here activates shared content; it does not enroll a harness or migrate an existing home.
- Scope evidence: source-store affected suite passed 111 tests after correcting test process startup timing and using actual process observation on macOS. Policy/selection/composition affected tests passed 52; shared engine passed 10; machine CLI passed 9. New tests exercised refusal by deliberate broken implementations before restoration. These are component evidence, not final full acceptance.
- CI integration readback: organization ruleset 21335575 requires `CI`; accepted workflows emitted other check names. Final job accounting now reports that required name. Deliberate failed input returned `CI refused: test finished as failure`; complete public results returned `CI passed: examined 2 required job results`.
- CodeRabbit remains the only AI PR reviewer. The additive repository overlay inherits central organization configuration; actual configuration-source readback and current-head review await publication.
- Parent owns full repository verification. Run `PATH="$PWD/.venv/bin:$PATH" TMPDIR=/private/tmp .venv/bin/python -m pytest -o addopts= -q`, `.venv/bin/python -m ruff check .`, `.venv/bin/agentops harness check .`, and `.venv/bin/agentops verify examples/local-smoke.yaml --json` against the frozen candidate. Record exact-head results in the PR without rewriting this record solely for bookkeeping.
- Remaining: full local/hosted CI and CodeRabbit review, private entry-point integration, enrollment, recoverable machine migration, and Dan's merge review. Repository enrollment will propose diffs and preserve project state; it is not completed by this checkpoint.

## Integration verification before the corrected candidate

- First full public run: 1185 passed, 13 skipped, 6 failed. The failures exposed host-sensitive fixtures and one public-record wording violation: two tests saw the operator's installed show-me skill; a requested set-group bit was stripped by macOS; a case-insensitive directory fixture failed before its identity assertion; a Unix socket fixture assumed Linux `/proc`; and this progress record contained a prohibited organization term.
- Corrections preserve the exercised safety behavior: isolate the two test home environments, use a retained set-user permission bit, assert alias rejection on case-insensitive filesystems, create the socket relative to its opened evidence directory, and keep public records generic. The 10 targeted cases passed. Full acceptance is rerun on the resulting frozen commit.
- A real CLI walkthrough installed and updated skill resources plus global policy, inspected original source provenance, removed source availability, rolled back both resources, and reported intentionally modified installed bytes with exit 1. This is content-engine evidence; it does not establish harness enrollment or live-home migration.

## Prior source history

# Progress

Repository: `agent-ops-community`

## Current State

- Branch: `feat/first-class-third-party-skills`
- Base: Community `origin/main` at `77b9a2d`
- Pull request: 48 in this repository
- Plane: no Community Plane work item applies
- Merge authority: Dan granted it for this pull request on 2026-09-04, conditional on repository checks passing. The standing bar still holds: a current-head CodeRabbit review with an `APPROVED` decision and zero unresolved conversations.

## Current Work

Codex and Claude Code, the two primary agent harnesses, declared and held different third-party skills, and nothing compared a declaration against a machine. The Matt Pocock allowlist named 12 skills where upstream promotes 25, and one of HumanLayer's five promoted plugins was declared at all. The Matt Pocock declaration widened to 12 on 2026-08-23 at 14:30; the Claude Code home's ownership manifest was written at 13:35 the same day recording 8, and that install never ran again, so four declared skills including `grilling` were absent for months while the registry said otherwise.

The registry now declares 24 Matt Pocock skills and 4 additional HumanLayer skills identically for both harnesses, and `agent_ops.skill_parity` reports whether a machine actually holds them. `agentops skills sync` installs whatever the registry currently declares rather than a hand-picked subset, and bootstrap names it.

## Session Log

- Established that `skill_dependencies.yaml` is owned by this repository, tracked in two copies, and that `_data_path` reads `<repo>/data` in a checkout and the packaged copy when installed. A test now requires the two to be byte-identical, because nothing else catches a one-sided edit.
- Found the shared virtualenv resolves `agent_ops` to the sibling clone rather than this worktree. Every command in this session pinned `PYTHONPATH=src` and the resolution was printed and checked before the results were trusted.
- Discarded one false pass: `python -m agent_ops.cli` has no `__main__` guard, so it imported the module, printed nothing, created no checkout, and exited 0. Re-run through the Typer app, the same dry run surfaced a real defect.
- Reverted an attempted HumanLayer pin bump. `show_me_adapter.PINNED_REF` pins the commit in Python and refuses any other, and the adapter adapts the skill body, so both HumanLayer entries now track `4d8d644c` and the bump is recorded as follow-up.
- Corrected the audit twice against evidence. It first read only the shared provider index and so called every skill in a home provisioned by the older per-dependency installers a hand copy; it then trusted an ownership record over the filesystem, which the live refusal check caught.

## Verification Log

- Focused feedback: each new check was made to fail on purpose before being trusted. Removing `grilling` from the allowlist gave `assert 23 == 24`. Editing the Codex list alone gave `AssertionError: mattpocock`. Editing one registry copy alone gave `At index 4153 diff: b'a' != b'c'`. Counting a hand copy as managed gave `assert [('alpha', 'managed')] == [('alpha', 'unmanaged')]`. Short-circuiting a home with no ownership index gave `SystemExit: 0`. Ignoring the older ownership record gave `('show-me', 'unmanaged') != ('show-me', 'managed')`.
- CI contract on head `4584884`, each command run unpiped and its own exit status read: `ruff check .` exit 0, all checks passed. `python -m pytest` exit 0, 1,037 passed and 13 skipped. `git diff --check` exit 0, no whitespace defects.
- Hosted checks on head `4584884`: `test`, `test-windows`, `Analyze (actions)`, `Analyze (python)`, and `CodeQL` all report SUCCESS.
- Live machine, before: `claude-code: 10/30 managed`, `codex: 18/37 managed`, exit 1. After installing the declared bundles into both homes: `all 67 declared skills are managed by Agent Ops`, exit 0.
- Refusal check on the live machine: moving `~/.codex/skills/grilling` aside makes the audit exit 1 and print `codex:mattpocock/grilling: missing`; restoring it returns exit 0 at 67/67. The first attempt at this check reported exit 0 with the directory gone, which is the defect that head `4584884` corrects.
- Cross-skill redirects verified after provisioning: `grill-me`, `grill-with-docs`, `improve-codebase-architecture`, and `wayfinder` all resolve their targets in `~/.claude/skills`.
- Confirmed the installs did not displace prior providers: the Claude Code ownership index still records `public-skill:gstack` with 761 paths and `public-skill:superpowers` with 46.

## Next Actions

1. Obtain a current-head CodeRabbit review with an `APPROVED` decision, then merge pull request 48.
2. Decide which gstack version and install shape wins across the two harnesses. `.agentops/harness/TASKS.md` records the evidence and why this is not an agent's call.

## Bound snapshot evidence descriptors, 2026-09-07

- A long-lived shared installation exposed descriptor exhaustion while expanding its retained snapshot history. Added subprocess regressions that inspect 161 owned files under a 64-descriptor soft limit through both `read_shared_target_evidence` and `retain_provider_plan_evidence`. Both failed before correction with `[Errno 24] Too many open files: 'snapshots'`.
- Status evidence now retains file identity, mode, and exact bytes while opening one evidence path at a time for capture and revalidation. Target/lock authority remains retained. Revalidation compares canonical path identity, open-file identity before and after the read, and exact content. Identity includes change time, so restoring bytes and modification time does not conceal an intervening mutation. Windows behavior remains on its existing backend.
- The first correction classified an owned-file replacement as modified rather than failed. The existing `test_engine_status_rejects_owned_path_replacement_races[file]` caught it; preserved the prior terminal failure classification and reran both replacement cases successfully. New adversarial cases reject same-byte replacement and modification followed by restoration of both original bytes and mtime.
- Affected suite: `TMPDIR=/private/tmp .venv/bin/pytest tests/test_shared_activation.py tests/test_deployment_preview.py tests/test_deployment_transaction.py -q` passed 403 cases. The two adversarial cases passed again after a formatting-only correction. `ruff check .` and `git diff --check` passed. Full exact-head suite and Linux acceptance remain pending for the replacement candidate before publication.
- Direct behavioral run and independent audit are the two public evidence APIs exercised by the subprocess regression against an actual owned installation; each reports verification of 161 files. The human-facing follow-up is a normal-limit machine sync/status against an installation retaining multiple snapshots. Do not claim that live acceptance until it is observed, and do not raise permanent descriptor limits or discard rollback snapshots as a substitute.


## Bound destination conflict checks, 2026-09-07

- A routine no-change update of an established 58-skill installation took 120.26 seconds while read-only status took 1.18 seconds. Profiling the same selection in a disposable home found 29.16 of 38.19 profiled seconds in nine calls to `_validate_and_group`, predominantly its all-pairs file-path comparisons. The profile examined about 102 million Python calls; this was local validation work rather than remote fetch latency.
- Replaced pairwise path comparisons with ancestor membership checks against the already deduplicated destination sets. File/file ancestry, removal/removal ancestry, equal file/removal destinations, and both directions of file/removal ancestry still cause refusal before mutation. No persistent cache, alternate updater, relaxed ownership check, snapshot deletion, or changed transaction boundary was introduced.
- Added bounded parent-traversal regressions for file-only, removal-only, and mixed plans. All three failed against the original code: `topology validation repeatedly scans every pair of paths`, including `1921 <= 1920`. They pass after correction. Expanded the existing pre-mutation refusal test to include equal file/removal paths and a file that is an ancestor of a removal. The combined focused run passed 11 cases; the transaction/preview/shared activation suite passed 408 cases. Ruff and diff whitespace checks passed. Full unchanged-head Mac/Linux checks and before/after command timing are next.
- Public PR 48 is confirmed merged. Current delivery remains pull request 49 in this repository; CodeRabbit is the only reviewer and approval/merge are not yet established for this correction. No Plane item applies. Human run is the existing `agentops machine sync` with the enrolled installation's explicit config and state; independent audit is its `machine status` command with the same locations. Record counts, unchanged registrations, and elapsed time before claiming operational acceptance.

- The first Linux full run passed 1199 tests, skipped 13, and rejected the organization name in this progress note under `test_public_tree_has_no_private_terms_or_local_paths`. Removed that repository URL from this public note; the exact URL remains in the private delivery record. Stopped the obsolete Mac run after the known failure (exit 143, not acceptance evidence). The public content sync command passed against the disposable selected installation in 2.10 seconds. Re-run full acceptance on the corrected documentation head.
