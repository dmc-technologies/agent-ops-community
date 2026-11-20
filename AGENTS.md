# agent-ops-community Agent Instructions

<!-- dmc-review-policy:start -->
## Pull requests and review

This block is the DMC review standard. Every DMC repository carries it at the top of `AGENTS.md`. The source is https://github.com/dmc-technologies/agent-ops/blob/main/configs/global/review-policy.md; change it there.

- Open pull requests under the GitHub login this machine is configured with, and never switch to another person's login. DMC-managed machines use the agent account `dmc-agents`; `.github/agent-accounts` lists the agent accounts. Open the pull request as a draft; mark it ready when CI is green.
- The description uses the headings What problem this solves, User impact, Why this change was made, Evidence, and Excluded. Evidence holds the exact commands with their results and exit status. Write no `Criticality:` line. Set a body with `gh api -X PATCH repos/<owner>/<repo>/pulls/<n> -F body=@file`.
- CodeRabbit applies one tier label: `routine`, `moderate`, or `critical`. Never add or remove a tier label. A person may add `critical`. `critical` is a change to rulebook text (`DECISIONS.md`, `ARCHITECTURE.md`, ADRs, or `AGENTS.md`), security or access (`CODEOWNERS`, branch protection, tokens or secrets, or CI permissions or secrets, including a runner change that reads a new secret), deploys or spend, or customer-facing deliverable content; `moderate` is engineering behavior, a CodeRabbit config change, a desktop or UI change that does not change customer-facing deliverable content, or a CI change that only selects runners or caches and reads no new secret; `routine` is tests only, tooling, dependency bumps, refactors that keep behavior, or docs that are not rulebook text. A change under `.github/workflows/` or of more than 40 files is a warning to split the pull request, not a tier, unless the change also hits a critical trigger.
- Every pull request follows five steps:
  1. CodeRabbit reviews when the pull request leaves draft, and again only when you post `@coderabbitai review`. Fix only Critical and Major findings, each with a failing test first; Minor and Trivial findings are optional. Batch the fixes, reply on each thread, and never request a review after every push. Review rounds follow the tier label: `routine` gets one review and one batch of fixes; `moderate` and `critical` get one review, one batch of fixes, and one confirming review. Findings on code that fixed an earlier finding count toward the same rounds. After the last round, change no code except for a security or correctness defect that a failing test or a concrete reproduction demonstrates, and list anything left in one pull request comment whose first line starts with `What's left`. The source is the Current standard in https://github.com/dmc-technologies/coderabbit/blob/main/README.md. CodeRabbit reviews are comments, not an approval or a merge gate. The author resolves a CodeRabbit thread once it has fixed the finding or answered it with evidence. If the author disagrees with a Critical or Major finding that stays unresolved, it goes to critical-path team review.
  2. CI is green on the head commit.
  3. Critical paths need dmc-developers (Dan or Nick) review. Give each point in the `What's left` comment a reason. A pull request that needs human review (a critical path, or any dmc-developers review) carries a proof clip of its result working, linked under Evidence in the description. An open CodeRabbit Security or critical finding is a human stop that a dmc-developers member resolves before merge.
  4. An approval of the head from a person who did not do the work is final: not the author, not a commit author, and not an agent account. Anyone may add another reviewer. Merge gates are native GitHub only: required approvals (Macroscope may supply the approval on low-risk PRs), conversation resolution, the DMC Mech Eng App check, and critical-path team review. CodeRabbit's own severity gate blocks merge on open Critical, Major or security findings, as part of one tool per function (MODEV-1232).
  5. Once the readiness readback for the current head passes — the pull request is not a draft, it has no merge conflicts, and required checks are green — the agent that opened or last pushed the pull request turns on auto-merge as `dmc-agents` with `gh pr merge <n> --auto --merge --match-head-commit <headSha>`. Re-check after every push and turn it on again if it is off. A missing approval from Dan (`dan-s-mueller`) does not keep auto-merge off; GitHub's required review is the final gate. Never turn auto-merge on for a draft, a pull request in an archived repository, a pull request with merge conflicts or failing required checks, business-ops#11 or momentum-utilities#24, or a pull request that needs a host deploy step before it can merge, until that step is done. While a pre-merge host step is pending, turn auto-merge off with `gh pr merge <n> --disable-auto` as dmc-agents, and tell CTO. Once that step is done, turn auto-merge on again through the readiness readback. Without write access, ask someone who has it to turn auto-merge on. GitHub merges it once the head passes the required checks and step 4's native merge gates. If GitHub reports that auto-merge is not allowed, ask a repository admin to turn on **Allow auto-merge**. A person may merge any pull request that is green and approved. Agents never merge by hand and never use a plain `gh pr merge` without `--auto`. The one exception is an admin merge by SWE or CTO with `gh pr merge --admin` under Dan's standing rules in the 2026-11-14 entry of https://github.com/dmc-technologies/agent-ops/blob/main/docs/DECISIONS.md: only after CTO signs off on the pull request and its head, squash only, no force-push, as `dan-s-mueller`, then back to the agent account. Nobody uses `gh pr merge --admin` otherwise. Never dismiss a CodeRabbit review, and resolve a CodeRabbit thread only as step 1 allows. agent-knowledge itself is the exception: agents commit directly to main as dmc-agents, with no pull request and no auto-merge. Every other repository follows this auto-merge flow.
- Hand a pull request to a person only when GitHub shows a pending review request to an eligible person or team, or an approval of the head from a person who did not do the work. Before you say that only an approval remains, read back `gh api repos/<owner>/<repo>/pulls/<n>/requested_reviewers` and the reviews on the head commit. The pull request author owns the request: request a person who did not do the work and confirm the request is listed. Never say an approval lands a pull request while its auto-merge is off.
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
