# Plan: first-class third-party skills for Codex and Claude Code

Repository: `agent-ops-community`
Branch: `feat/first-class-third-party-skills`
Base: `origin/main` at `77b9a2d`
Date: 2026-09-04

## What will work when this is done

An engineer runs one Agent Ops refresh and both the Codex home and the Claude Code home end up holding the same third-party skill set, at the same upstream pins, provisioned by Agent Ops rather than by hand. A read-only audit command names every declared skill and says whether each home actually holds it, and that audit exits non-zero when a declared skill is missing. Today the audit does not exist, the two homes disagree on every third-party source, and provisioning is a manual command a person has to remember.

## Acceptance check

`python -m agent_ops.skill_parity --all` (read-only) reports every declared skill present in both `~/.claude` and `~/.codex`, exits 0, and the same command exits non-zero naming the skill when one declared skill directory is removed.

## Frozen decisions

- `code-review` is excluded from the allowlist for **both** frameworks. Claude Code ships a built-in `/code-review`; installing a same-named skill shadows it. Excluding it from Codex too preserves the stated parity requirement. Reversing this is one line in each of the two registry copies.
- `setup-matt-pocock-skills` is included in **both**. `wayfinder` redirects to it, and excluding it leaves a redirect pointing at a skill that is not installed.
- Trail of Bits stays as it is. Commit `756c648` removed seven of its skills from `copy-named-skills` because they depend on plugin-level hooks and subagents a directory copy cannot carry. This plan does not reverse that.

## Upstream pins

| Dependency | Repository | Pin | Promoted set | Declared here |
| --- | --- | --- | --- | --- |
| `mattpocock` | `https://github.com/mattpocock/skills.git` | `3cca18b368ae95cdbdebbff572ccafa662551015` | 25 skills in `.claude-plugin/plugin.json` | 24 (all but `code-review`) |
| `humanlayer` | `https://github.com/humanlayer/skills.git` | `3c2629142c5d437428269b1b722b08c0b87f574d` | 5 plugins in `.claude-plugin/marketplace.json` | 5 |

Both are MIT. Upstream copyright travels with the vendored `LICENSE`, matching how the `trailofbits` and `superpowers` entries already carry attribution.

## Checkpoint 1 — Complete, framework-symmetric declarations

Scope: `data/skill_dependencies.yaml` and `src/agent_ops/data/skill_dependencies.yaml`, which are tracked separately and must stay byte-identical because `load_skill_dependencies` reads the packaged copy.

- Re-pin `mattpocock` to `3cca18b`, widen its allowlist from 12 names to the 24 declared names, bump `version`.
- Replace the `humanlayer-show-me` entry with a `humanlayer` entry using `copy-named-skills` over all five promoted skills, re-pinned to `3c26291`. Keep the existing show-me collision guard in `_validate_target_ancestors` and `_validate_prior_shared_files` working, because an installed `show-me` already exists in at least one home.
- Give each entry one shared YAML anchor used by both the `codex` and `claude-code` install blocks, so the two frameworks cannot drift apart by editing one and not the other.
- Add a test asserting the `codex` and `claude-code` allowlists are equal for every dependency that declares both. This is the structural guard that makes parity a property of the file rather than a thing someone has to check.

Done when: `python -m pytest -q tests/test_skill_installer.py tests/test_registries.py` passes, and the new parity-of-declaration test fails when one framework's list is edited alone.

## Checkpoint 2 — Provisioning becomes part of refresh, and the check can fail

Scope: a new read-only audit module, plus wiring dependency reconciliation into the deployment refresh path.

Today `install_skill_dependencies` has exactly one caller, `src/agent_ops/cli.py:281`, behind `agentops skills install <framework>`. `agentops bootstrap` writes an instruction file telling a person to run that command by hand (`src/agent_ops/bootstrap.py:25`). The deployment engine never calls it. That is why a declaration added on 2026-08-23 at 14:30 never reached a Claude Code home whose public-skills manifest was written at 13:37 the same day.

- Add `agent_ops.skill_parity`: reads each home's `skills/.agentops-public-provider-index.json` off disk, compares it against the declared allowlist for that home's framework, prints one line per skill, exits non-zero listing every missing name. Read-only: no writes, no network, no state-changing options.
- Run it red first against the live homes and record the exact output before any provisioning.
- Wire dependency reconciliation into deployment refresh so `agentops deployment refresh` and `scripts/refresh_agentops_homes.py` provision declared dependencies instead of only reporting on them.
- Provision both homes, run the audit green, and record both outputs.

Done when: the red run is recorded with the missing skills named, the green run exits 0, and removing one declared skill directory reproduces a non-zero exit naming that skill.

## Checkpoint 3 — One gstack shape, and the hand copies removed

gstack is installed three ways across two homes and the two homes are thirty versions apart.

| Home | Shape | Version |
| --- | --- | --- |
| `~/.claude/skills/gstack` | Managed copy, 761 files, one nested tree | 1.32.0.0 |
| `~/.codex/skills/gstack` | Symlinks into `~/agent-ops/.agentops/dependencies/gstack` | 1.62.0.0 |
| `~/.codex/skills/` top level | ~100 flattened directories, bare and `gstack-` prefixed | 1.62.0.0 |

The flattened Codex copies collide by declared name: `~/.codex/skills/qa/SKILL.md` and `~/.codex/skills/gstack-qa/SKILL.md` both declare `name: qa` and their contents differ. `browse`, `canary`, `ship`, and `health` collide the same way. Which one a framework resolves is undefined.

- Bring both homes to one gstack install shape at one pin.
- Remove the duplicate flattened Codex directories that make a declared skill name ambiguous.
- Only after managed deployment provides them, remove the 16 hand-copied directories from `~/.claude/skills`. Order matters: removing them first re-breaks `/grill-me`.

Done when: no declared skill name resolves to two directories in either home, both homes report the same gstack version, and `Skill(grilling)` still loads after the hand copies are gone.

## Human verification

| Field | Value |
| --- | --- |
| Human run command | `agentops deployment refresh --registry ~/.claude/.agentops/deployments.yaml --all` then the same for `~/.codex/.agentops/deployments.yaml` |
| Human audit command, read-only | `python -m agent_ops.skill_parity --all` |
| Expected observation | Every declared skill listed as present for both homes; exit status 0 |
| Refusal check | Move one declared skill directory out of `~/.codex/skills`, re-run the audit, confirm non-zero and that the skill is named |
| Implementation evidence, recorded separately | `ruff check .`, `python -m pytest -q`, `agentops harness check .`, `git diff --check` |

Read exit status directly, not through a pipe. A check piped into `head` or `grep` reports the pipe's status and reads as a pass.

## Excluded

- Cursor, OpenClaw, opencode, and Prime Agent homes. Codex and Claude Code were the two named.
- Reversing the Trail of Bits plugin-dependent exclusions from `756c648`.
- No commit to any default branch. Work reaches `main` only through this pull request.

## Merge authority

Dan granted merge authority for this pull request on 2026-09-04: "once the required checks pass you can merge that into main". The bar is unchanged otherwise: repository CI green on the final head, a current-head CodeRabbit review with an `APPROVED` decision, and zero unresolved conversations. Any other CodeRabbit state -- `COMMENTED`, `CHANGES_REQUESTED`, stale, or missing -- stops the merge and is reported instead.

## First unmet prerequisite

None blocking. The two naming decisions are frozen in this file with their reversal cost stated.

## Where future work is recorded

`.agentops/harness/TASKS.md` in this repository. No Plane work item applies to `agent-ops-community` today; if one is wanted it is created before the pull request is opened, not invented afterward.
