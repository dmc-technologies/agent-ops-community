# Progress

Repository: `agent-ops-community`

## Current State

- Branch: `fix/deployment-status-reads-installed-manifest`
- Base: Community `origin/main` at `35defac62486a3c465825836cb489d27609016a1`
- Plane: no Community Plane work item applies
- Merge authority: Dan retains it; this branch is published for review only

## Current Work

`agentops deployment status` could never report a healthy managed target. The managed branch of `DeploymentEngine.status` passed `manifest=None` into `DeploymentRegistry.status`, which classifies a missing manifest as `stale`, so `stable`, `branch`, and `modified` were unreachable for every managed target and status contradicted `refresh` and `audit` on the same machine. Status now reads each managed target's installed ownership manifest through the same cooperative status-evidence reader preview already used, and the receipt bound to the current registry snapshot supplies only the last resolved commit. Preview behavior is unchanged.

## Session Log

- Reproduced the defect against the machine's live Claude Code registry: `status` printed `stale` while `refresh` and `audit` reported `stable` at the same commit.
- Added five failing engine status tests before changing source, covering stable, branch, stale, absent manifest, and modified classification plus target-home immutability.
- Generalized `_read_preview_status_evidence` into one pinned status-evidence reader with preview and managed validation, and added a native Windows shared-lock manifest read so managed status keeps working on Windows.
- Replaced the managed branch of `DeploymentEngine.status` with the manifest read, keeping the receipt lookup only for the last resolved commit and the recorded failure and missing-ref outcomes.
- Documented managed status classification in `README.md` and `ARCHITECTURE.md`.

## Verification Log

- Focused feedback before the fix: `python -m pytest tests/test_deployment_engine.py -k engine_status` reported 5 failed, 1 passed, 83 deselected.
- Focused feedback after the fix: the same node selection reported 6 passed, 83 deselected.
- `python -m pytest tests/test_deployment_preview.py tests/test_deployment_transaction.py tests/test_deployment_models.py tests/test_deployment_engine.py tests/test_deployment_registry.py tests/test_deployment_cli.py` reported 629 passed before the change and 634 passed after it.
- `ruff check .` passed. `python -m pytest -q` reported 1,034 passed with 13 supported skips. `agentops harness check .` reported ok. `git diff --check` reported no whitespace defects.
- Live read-only proof on this machine: `agentops deployment status --registry ~/.claude/.agentops/deployments.yaml --all` and the same command for `~/.agentops/.agentops/deployments.yaml` now print `stable` at commit `3940bcdaa5971c5c4e8ccbd3d684fd090bc4eba5`, matching `refresh` and `audit`, and neither target home was modified.

## Next Actions

1. Obtain exact-head hosted Linux and Windows CI, then one Review Gate acceptance.
2. Merge only with Dan's explicit merge authority on the unchanged accepted head.
