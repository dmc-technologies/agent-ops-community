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
