# Harness Bootstrap Contract

Repository: `agent-ops-community`

## Clock In

1. Read `AGENTS.md`.
2. Read `ARCHITECTURE.md`.
3. Read `.agentops/harness/PROGRESS.md`.
4. Read `.agentops/harness/DECISIONS.md` before architecture or workflow changes.
5. Search or recall relevant shared-memory entries when prior context could
   affect the work.
6. Run `git status --short --branch`.
7. Determine the target branch and whether repository instructions declare an integration branch, a routine feature pull request, integration-controller work, or stable-branch landing.
8. Run the fastest relevant verification command before broad edits when feasible.
9. Continue from `.agentops/harness/PROGRESS.md` "Next Actions" when that file is owned by the current branch role; otherwise continue from exact pull request and tracker evidence.

## Clock Out

1. If no integration branch is declared, update `.agentops/harness/PROGRESS.md` with current state, verification, blockers, and next actions.
2. If repository instructions declare an integration branch, hand off a routine feature pull request through exact pull request and tracker evidence; do not replace `.agentops/harness/PROGRESS.md`.
3. The integration controller or stable-branch landing updates `.agentops/harness/PROGRESS.md` when repository instructions assign that role.
4. Add durable architecture or workflow decisions to `.agentops/harness/DECISIONS.md`.
5. Write shared-memory entries only for distilled cross-agent memory: important
   decisions, durable discoveries, non-obvious debugging findings, or workflow
   changes. Do not write automatic session summaries.
6. Remove stale debug artifacts and leave the startup path usable.
7. Run the local CI contract or record why it could not be run.

## ACID State Rules

- Atomicity: finish one logical operation at a time and commit only coherent
  verified units.
- Consistency: do not claim completion unless verification and harness checks
  pass or failures are recorded.
- Isolation: use branches, worktrees, or explicit file ownership to avoid
  concurrent-agent collisions.
- Durability: keep cross-session state in git-tracked files, not chat memory.

## Definition Of Done

- Requested behavior is implemented.
- Relevant verification has been run and recorded.
- Durable decisions are recorded locally and promoted to shared memory only when reusable.
- The repository is left in a clean handoff state.
