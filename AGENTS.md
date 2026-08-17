# agent-ops-community Agent Instructions

## Project

`agent-ops-community` is the public Agent Ops package. It should provide the
same generic Agent Ops workflow across common agent frameworks while excluding
only proprietary runner/verifier implementations and organization-owned
operational workflows.

## Harness

- Read `.agentops/harness/BOOTSTRAP.md` at session start.
- Use `.agentops/harness/PROGRESS.md` for active handoff state.
- Use `.agentops/harness/DECISIONS.md` for durable local decisions.
- Use shared-memory tooling only for distilled cross-agent memory.
- Keep public-facing docs free of proprietary runner names and organization-specific references.

## Verification

- `ruff check .`
- `pytest`
- `agentops harness check .`

## Pull Requests And Reviews

The local `pr-review` command is an independent advisory before hosted review; it is not merge evidence. After CI passes on the final unchanged pull-request head, a person or explicitly authorized agent applies `ai review` once. A normal pull request receives one discovery review. Only a proven critical defect blocks; record verified noncritical findings in one follow-up issue. After a critical correction, review the reported finding class and fix delta without repeating complete discovery over unchanged code.

A pull request may close only when exact-head CI and the hosted Review Gate pass, its plain-English description matches that head, and addressed human review conversations are resolved. Leave ambiguous or unaddressed conversations open. A behavior-changing commit invalidates prior CI and review acceptance.

Dan retains merge authority unless he explicitly grants merge permission for the exact pull request or task. Preparing, pushing, labeling, or reviewing a pull request does not grant merge authority.

## Stack and migration

Do not introduce a new implementation language, runtime, or framework unless an existing supported boundary strictly requires it or Dan has approved a material product or operational benefit. Prefer the repository's current stack, preserve prototypes and history, and require source-backed migration plus rollback proof before replacement.
