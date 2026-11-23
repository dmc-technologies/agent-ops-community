# agent-ops-community Agent Instructions

<!-- dmc-review-policy:start -->
## Pull requests and review

This block is the DMC review standard. Every DMC repository carries it at the top of `AGENTS.md`. The source is https://github.com/dmc-technologies/agent-ops/blob/main/configs/global/review-policy.md; change it there.

- Open pull requests under the GitHub login this machine is configured with, and never switch to another person's login. DMC-managed machines use the agent account `dmc-agents`; `.github/agent-accounts` lists the agent accounts. Open the pull request ready, not as a draft, and turn on auto-merge at once.
- Use one T3 thread per Plane item; the item may have many pull requests. Plane does not close an item on merge. The item closes when its last pull request merges and its thread reports done. Do not stack pull requests across threads.
- The description uses the headings What problem this solves, User impact, Why this change was made, Evidence, and Excluded. Evidence holds the exact commands with their results and exit status. Write no `Criticality:` line. Set a body with `gh api -X PATCH repos/<owner>/<repo>/pulls/<n> -F body=@file`.
- Before you open a pull request or push a new head, run `~/agent-ops/scripts/pre-pr-review <base>` (CodeRabbit CLI, then Macroscope CLI; it posts nothing). Fix any Critical it finds in code first. Note Major findings and below in the body; they do not block.
- Five hard gates must hold on the head commit, in this order:
  1. CI green: the `CI` check passes. Heavy CI runs before merge only on pull requests into momentum `main`; elsewhere `CI` is the light tier.
  2. No open Critical finding. The `human-review` check always waits for CodeRabbit's severity gate (`No open Critical finding`) to report, also when CodeRabbit is rate-limited. The gate fails only on an unfixed Critical finding or a committed secret. A committed secret counts as Critical; it is fixed, never overridden. An open Critical finding blocks the merge until code fixes it; it needs no human approval. Fix each Critical finding with a failing test first, reply on its thread, and resolve the thread once fixed. Major, Minor, Trivial, and Info findings never block a merge. List them for the post-merge sweep. When no Critical finding is open, a stale CodeRabbit changes-requested review may be dismissed. CodeRabbit reviews when the pull request opens, and again only when you post `@coderabbitai review`.
  3. Architecture Conformance pass: Macroscope's Architecture Conformance check passes.
  4. Physics: the `mech-eng-review` check reports this gate. Where the check is required, it passes when Macroscope's Mechanical Engineering Correctness check reads `NO PHYSICS`, or when Mech Eng passes the head. In any repository, add the `needs-mech-eng` label when your change states or changes a load, factor, allowable, margin, unit conversion, material property, mass, frequency, or solver setup; `mech-eng-review` then holds the merge. Mech Eng and Product gate a merge only when a Macroscope check or that label pulls them in. Otherwise they review after the merge, and each finding becomes a fix pull request.
  5. Critical Infrastructure: Macroscope's Critical Infrastructure check raises no flag, or a `dmc-developers` member who did not do the work approves the head.
- Each gate allows a fixed number of tries. Then the agent stops and escalates with its evidence. The tries, the escalation targets, and the judgment stop live in https://github.com/dmc-technologies/review-rules/blob/main/rules/bounded-tries.md.
- The human layer is the `dmc-developers` GitHub team. Any member may approve and merge. These pull requests always need the approval of a `dmc-developers` member. Macroscope's Critical Infrastructure check, which reads the critical-path list, flags the first four, `label-hold` holds a judgment stop, and an Architecture Conformance objection makes `human-review` wait for that approval:
  - rulebook text: `AGENTS.md`, `ARCHITECTURE.md`, `DECISIONS.md`, or an ADR;
  - secrets, access, or CI workflows;
  - anything that loosens a gate;
  - spend or deploys;
  - a judgment stop;
  - an architecture conflict.
- A pull request that needs human review (a critical path, or any `dmc-developers` review) carries a proof clip of its result working, linked under Evidence in the description. Only a Macroscope call needs a person. An open CodeRabbit Critical finding or committed secret needs no `dmc-developers` approval: the severity gate holds the merge until code fixes it.
- An approval of the head from a person who did not do the work is final: not the author, not a commit author, and not an agent account. Anyone may add another reviewer.
- Auto-merge when all five gates hold. In the repositories that review-rules syncs, GitHub then merges once the required `human-review` check passes. Macroscope decides whether a person must review. Macroscope's approval counts as the human review on a routine pull request. `human-review` passes on its own when CodeRabbit's severity gate reported and Macroscope approves: its Approvability check approves, or Critical Infrastructure says `NONE` and Architecture Conformance ran without objection. It never passes on its own while Macroscope calls for a person: Critical Infrastructure says a person is needed, Architecture Conformance reports a conflict, or Approvability says Needs Human; then one approval from any `dmc-developers` member clears it. A failed severity gate does not hold `human-review`; the gate itself blocks the merge until code fixes the finding. A review-sync pull request whose files are byte-identical to the review-rules render also passes on its own, without CodeRabbit's report: CodeRabbit never reviews a pull request from a bot user. If Macroscope flags the pull request, `human-review` waits for an approval from a `dmc-developers` member who did not do the work; a Macroscope or agent approval does not count. In every other repository, GitHub merges once the head has one approval from a `dmc-developers` code owner under `.github/CODEOWNERS`. As soon as the pull request is open, ready, and free of merge conflicts, the agent that opened or last pushed it turns on auto-merge as `dmc-agents`, with `gh pr merge <n> --auto --merge --match-head-commit <headSha>`. GitHub then waits for the required checks. Re-check after every push and turn it on again if it is off. Never turn auto-merge on for a draft, a pull request in an archived repository, a pull request with merge conflicts or failing required checks, business-ops#11 or momentum-utilities#24, or a pull request that needs a host deploy step before it can merge, until that step is done. While a pre-merge host step is pending, turn auto-merge off with `gh pr merge <n> --disable-auto` as dmc-agents, and tell CTO. Once that step is done, turn auto-merge on again. Without write access, ask someone who has it to turn auto-merge on. If GitHub reports that auto-merge is not allowed, ask a repository admin to turn on **Allow auto-merge**. A `dmc-developers` member may merge any pull request whose gates hold and that is approved. Agents never merge by hand and never use a plain `gh pr merge` without `--auto`. Agents never use `gh pr merge --admin`. The one exception is a pull request for which Dan gives explicit authorization, one case at a time. A `dmc-developers` member may admin-merge when needed. agent-knowledge itself is the exception: agents commit directly to main as dmc-agents, with no pull request and no auto-merge. Every other repository follows this auto-merge flow.
- Hand a pull request to a person only when GitHub shows a pending review request to the `dmc-developers` team or one of its members, or an approval of the head from a person who did not do the work. Before you say that only an approval remains, read back `gh api repos/<owner>/<repo>/pulls/<n>/requested_reviewers` and the reviews on the head commit. Never say an approval lands a pull request while its auto-merge is off.
- When dmc-agents is GitHub rate-limited, fall back to the `dan-s-mueller` gh account for GitHub calls under `flock /tmp/gh-account.lock`, and switch back after (Dan t2278u). Poll pull request state through REST at most once per 60 seconds, with no `--watch` and one poller per pull request.
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
