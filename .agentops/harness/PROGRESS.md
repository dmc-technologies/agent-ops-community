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
