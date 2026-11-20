# agent-ops-community Agent Instructions

<!-- dmc-review-policy:start -->
## Pull requests and review

This block is the DMC review standard. Every DMC repository carries it at the top of `AGENTS.md`. The source is https://github.com/dmc-technologies/agent-ops/blob/main/configs/global/review-policy.md; change it there.

- Open pull requests under the GitHub login this machine is configured with, and never switch to another person's login. DMC-managed machines use the agent account `dmc-agents`; `.github/agent-accounts` lists the agent accounts. Open the pull request as a draft; mark it ready when CI is green.
- The description uses the headings What problem this solves, User impact, Why this change was made, Evidence, and Excluded. Evidence holds the exact commands with their results and exit status. Write no `Criticality:` line. Set a body with `gh api -X PATCH repos/<owner>/<repo>/pulls/<n> -F body=@file`.
- Five hard gates must hold on the head commit, in this order:
  1. CI green: the `CI` check passes.
  2. CodeRabbit clean: no Critical or Major finding stays open. CodeRabbit reviews when the pull request leaves draft, and again only when you post `@coderabbitai review`. Fix each Critical or Major finding with a failing test first; Minor and Trivial findings are optional. Batch the fixes and reply on each thread. Resolve a thread once you fixed the finding or answered it with evidence. Never dismiss a CodeRabbit review. CodeRabbit's own severity gate blocks merge on open Critical, Major or security findings, as part of one tool per function (MODEV-1232).
  3. Architecture Conformance pass: Macroscope's Architecture Conformance check passes.
  4. Physics: Macroscope's Mechanical Engineering Correctness check reads `NO PHYSICS`, or Mech Eng passes the head. The `mech-eng-review` check reports this gate.
  5. No Critical Infrastructure flag: Macroscope's Critical Infrastructure check raises no flag.
- Each gate allows a fixed number of tries. Then the agent stops and escalates with its evidence. The tries, the escalation targets, and the judgment stop live in https://github.com/dmc-technologies/review-rules/blob/main/rules/bounded-tries.md.
- The human layer is the `dmc-developers` GitHub team. Any member may approve and merge. These pull requests always need the approval of a `dmc-developers` member:
  - rulebook text: `AGENTS.md`, `ARCHITECTURE.md`, `DECISIONS.md`, or an ADR;
  - secrets, access, or CI workflows;
  - anything that loosens a gate;
  - spend or deploys;
  - a judgment stop;
  - an architecture conflict.
- A pull request that needs human review (a critical path, or any `dmc-developers` review) carries a proof clip of its result working, linked under Evidence in the description. An open CodeRabbit Security or Critical finding is a human stop that a `dmc-developers` member resolves before merge.
- An approval of the head from a person who did not do the work is final: not the author, not a commit author, and not an agent account. Anyone may add another reviewer.
- Auto-merge when all five gates hold. GitHub then merges once the head has its required approval (the human approval comes from a `dmc-developers` code owner under `.github/CODEOWNERS`; a Macroscope or agent approval does not count as it). Once the readiness readback for the current head passes — the pull request is not a draft, it has no merge conflicts, and required checks are green — the agent that opened or last pushed the pull request turns on auto-merge as `dmc-agents` with `gh pr merge <n> --auto --merge --match-head-commit <headSha>`. Re-check after every push and turn it on again if it is off. Never turn auto-merge on for a draft, a pull request in an archived repository, a pull request with merge conflicts or failing required checks, business-ops#11 or momentum-utilities#24, or a pull request that needs a host deploy step before it can merge, until that step is done. While a pre-merge host step is pending, turn auto-merge off with `gh pr merge <n> --disable-auto` as dmc-agents, and tell CTO. Once that step is done, turn auto-merge on again through the readiness readback. Without write access, ask someone who has it to turn auto-merge on. If GitHub reports that auto-merge is not allowed, ask a repository admin to turn on **Allow auto-merge**. A `dmc-developers` member may merge any pull request whose gates hold and that is approved. Agents never merge by hand and never use a plain `gh pr merge` without `--auto`. The one exception is an admin merge by SWE or CTO with `gh pr merge --admin` under Dan's standing rules in the 2026-11-14 entry of https://github.com/dmc-technologies/agent-ops/blob/main/docs/DECISIONS.md: only after CTO signs off on the pull request and its head, squash only, no force-push, as `dan-s-mueller`, then back to the agent account. Nobody uses `gh pr merge --admin` otherwise. agent-knowledge itself is the exception: agents commit directly to main as dmc-agents, with no pull request and no auto-merge. Every other repository follows this auto-merge flow.
- Hand a pull request to a person only when GitHub shows a pending review request to the `dmc-developers` team or one of its members, or an approval of the head from a person who did not do the work. Before you say that only an approval remains, read back `gh api repos/<owner>/<repo>/pulls/<n>/requested_reviewers` and the reviews on the head commit. Never say an approval lands a pull request while its auto-merge is off.
- Full guide: https://github.com/dmc-technologies/coderabbit/blob/main/docs/coderabbit-for-dummies.md.
<!-- dmc-review-policy:end -->

## Project

`agent-ops-community` is the public Agent Ops package. It should provide the
same generic Agent Ops workflow across common agent frameworks while excluding
only proprietary runner/verifier implementations and organization-owned
operational workflows.

## Handoff

- GitHub pull requests and the tracker are the handoff. This repository has no Agent Ops harness, so there is no repository progress file and no clock-in or clock-out step.
- Record durable architecture and workflow decisions in `docs/DECISIONS.md`.
- Use shared-memory tooling only for distilled cross-agent memory.
- Keep public-facing docs free of proprietary runner names and organization-specific references.

## Verification

- `ruff check .`
- `pytest`

## Stack and migration

Do not introduce a new implementation language, runtime, or framework unless an existing supported boundary strictly requires it or Dan has approved a material product or operational benefit. Prefer the repository's current stack, preserve prototypes and history, and require source-backed migration plus rollback proof before replacement.
