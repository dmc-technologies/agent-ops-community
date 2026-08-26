# Verification

Repository: `agent-ops-community`

## Harness Check

- Preferred local command: `agentops harness check .`

## CI Contract

- `ruff check .`
- `python -m pytest -q`
- `agentops harness check .`
- `git diff --check`

The final community deployment-channel head must run these commands once without file changes between them. Focused implementation feedback uses `python -m pytest -q tests/test_deployment_preview.py tests/test_deployment_transaction.py tests/test_deployment_models.py tests/test_deployment_engine.py tests/test_deployment_registry.py tests/test_deployment_cli.py` before the complete suite. Record exact command output summaries in `.agentops/harness/PROGRESS.md`.
