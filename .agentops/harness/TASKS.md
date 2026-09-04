# Harness Tasks

Repository: `agent-ops-community`

Use this file for agent-readable task decomposition when work spans sessions or agents.

## Ready

- [ ] Bring gstack to one install shape across Codex and Claude Code.
  - Scope: `~/.codex/skills`, `~/.claude/skills/gstack`, the `gstack` entry in both copies of `skill_dependencies.yaml`, and the private `scripts/gstack setup-codex`.
  - Problem: gstack is installed three ways. Claude Code holds a managed 761-file tree at `skills/gstack`, version 1.32.0.0, owned by `public-skill:gstack`. Codex holds a symlink farm at `skills/gstack` pointing into `~/agent-ops/.agentops/dependencies/gstack` at version 1.62.0.0, plus roughly 100 flattened top-level directories owned by no ownership record at all. Of the 141 top-level skills in the Codex home, 43 declared names are each claimed by two directories -- every one a bare-versus-`gstack-` pair such as `qa` and `gstack-qa`, whose contents differ. Which directory a framework resolves for those names is undefined. The Claude Code home has no such collision.
  - Decision needed before work starts: which version and shape wins. Aligning Codex to the registry pin downgrades it from 1.62.0.0 to 1.32.0.0 and removes roughly 100 skills a person may be using; aligning Claude Code upward means bumping the registry pin and re-rendering. Both are user-visible product changes, so neither is an agent's call.
  - Verification: `agentops skills audit --framework claude-code --framework codex` exits 0, and no declared skill name resolves to two directories in either home.

- [ ] Bump the `humanlayer/skills` pin past `4d8d644ca747517973f58d7953f58d7cd07520cd`.
  - Scope: both copies of `skill_dependencies.yaml` and `src/agent_ops/show_me_adapter.py`.
  - Problem: `show_me_adapter.PINNED_REF` pins the commit in Python and refuses any other, and the adapter replaces the skill's host-specific HTML opener with a portable fallback. Moving the pin means re-verifying that adaptation against new upstream content. Upstream is at `3c2629142c5d437428269b1b722b08c0b87f574d`.
  - Verification: `agentops skills install claude-code --dependency humanlayer-show-me --dry-run` exits 0 at the new pin, and the installed `SKILL.md` still carries the portable opener.

- [ ] Investigate why `humanlayer-show-me` cannot install while a prior copy exists.
  - Problem: with `show-me` already present and its ownership manifest intact at `.agentops/skill-dependencies/humanlayer-show-me.json`, the install fails with `ShowMeCollisionError: user-owned collision at skills/show-me`, raised at `show_me_adapter.py:1677` where `current is None`. The manifest exists in the real home, so the adapter appears to read it relative to the shadow home that `_render_dependency_in_workspace` builds rather than the context home. Reproduces on unmodified `main`, so it predates this work.
  - Verification: a failing test that reproduces the shadow-home read, then `agentops skills install codex --dependency humanlayer-show-me` succeeding over an existing owned copy.

## Acceptance Criteria Format

- Scope: files, modules, or behavior affected.
- Verification: exact command that proves completion.
- Handoff: progress and decision updates required before session end.
