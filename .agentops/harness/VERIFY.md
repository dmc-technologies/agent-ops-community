# Verification

Repository: `agent-ops-community`

## Harness Check

- Preferred local command: `agentops harness check .`

## Fast Gate

- `ruff check .`
- `pytest`

## Full Gate

Use the repo's complete CI-equivalent command when the fast gate is not enough.
Record exact command output summaries in `.agentops/harness/PROGRESS.md`.
