---
title: Critical Infrastructure
model: gpt-6-1-sol
reasoning: medium
input: full_diff
tools:
- browse_code
include:
- '**/*'
maxBudgetPerRun: 3
maxBudgetPerPR: 15
conclusion: failure
showToolCalls: true
exclude:
- .review-selftest/marked/**
---
<!-- GENERATED from dmc-technologies/review-rules. Do not edit here: change review-rules (dmc-developers) and let the sync open a pull request. -->

# Check: Critical infrastructure

Purpose: good human-level review flags on infrastructure. A change that can alter who may merge,
what CI proves, where secrets go, what deploys or what money is spent is **Needs Human**:
Dan (`dan-s-mueller`) or Nick (`nicdavidson-dmc`) approves the head. Macroscope never approves it.

## 1. What counts
- Any file matching the critical path list (`rules/critical-paths.txt` in
  dmc-technologies/review-rules; the generated list is in this repository's `.review/paths.json`).
- Anywhere: a new or changed secret name, token, key or credential reference; a widened
  `permissions:` block in a workflow; a new third-party action or an action pinned by tag instead
  of SHA; `pull_request_target`; a change to who can bypass, approve or merge; a change to
  deploy targets, instance sizes, paid API calls or budgets.
- Rulebook text: AGENTS.md review rules, DECISIONS.md, ADRs, ARCHITECTURE.md.

## 2. What to report
For each finding, name the file and line, what capability it changes and for whom, and the
smallest safe alternative. Do not report style. Do not edit the pull request.
End every run with one line: `Critical infrastructure: NONE` or
`Critical infrastructure: NEEDS HUMAN <file>:<line>` (one per finding).

## 3. Always Needs Human
- Any changed file on the critical path list.
- Any finding from section 1.
