# Progress

Repository: `agent-ops-community`

## Current State

- Branch: `fix/audit-tolerates-foreign-interpreter-bytecode-cache`
- Base: Community `origin/main` at `fb1e6b812558ae451b8d73c441130fbbd7b7fac7`
- Plane: no Community Plane work item applies
- Merge authority: Dan retains it; this branch is published for review only

## Current Work

A managed target could be blocked from ever reaching its channel commit by one leftover Python bytecode cache. The audit unexpected-file scan resolved a `__pycache__` candidate's source only through the running interpreter's cache tag, so a cache written by a previously installed CPython minor was classified as an unexpected unmanaged file inside an owned audit root. `audit` then reported `modified` and `refresh` installed correctly, failed its post-install audit, and rolled back on every attempt. The scan now resolves the candidate's source for any supported CPython 3.11–3.14 tag with default, `opt-1`, or `opt-2` naming, requires that source to be declared in the plan's runtime Python sources, and accepts the file only through the shared runtime-cache provenance check that retirement already uses. Running-tag caches keep exact compiled code-object equality. Unrelated files, invalid caches, caches for undeclared sources, and any managed file whose bytes or mode differ still fail audit.

## Session Log

- Reproduced the machine's live failure on a faithful copy of the affected Prime home: `refresh` printed `deployment audit did not match target 'prime-agent'` and `audit` printed `modified`, while every managed file matched its manifest.
- Traced the mechanism in code and confirmed it against the copy: the single unexpected entry was a `__pycache__/__init__.cpython-311.pyc` file beside a managed Python source in a provider-owned skill root, read under a Python 3.14 interpreter. Removing that one file made `refresh` succeed with no source change, which isolated the cause.
- Established that the co-resident public-skills ownership manifest in the same home is not the cause. Manifest selection is keyed by exact target ID, and the provider's audit roots cover only its own skill directories, which are disjoint from the public-skills directories.
- Added five failing tests before changing source: four in `tests/test_deployment_transaction.py` covering tolerance beside a co-resident public-skills manifest plus changed-file, unbound-cache, and undeclared-source rejection, and one in `tests/test_deployment_engine.py` covering a full `refresh`.
- Added `_supported_runtime_python_source_for_cache` and `_is_supported_runtime_python_cache` and used the latter in the audit scan, reusing the existing `_runtime_cache_provenance` acceptance rule.
- Documented the audit tolerance in `README.md` and `ARCHITECTURE.md` and recorded the decision in `.agentops/harness/DECISIONS.md`.

## Verification Log

- Focused feedback before the fix: the five new node IDs reported 2 failed, 3 passed. The failures were `assert audit.unexpected == ()` naming `skills/private/src/package/__pycache__/__init__.cpython-311.pyc` and `DeploymentAuditError: deployment audit did not match target 'codex'`.
- Focused feedback after the fix: the same five node IDs reported 5 passed.
- `python -m pytest tests/test_deployment_preview.py tests/test_deployment_transaction.py tests/test_deployment_models.py tests/test_deployment_engine.py tests/test_deployment_registry.py tests/test_deployment_cli.py tests/test_skill_installer.py` reported 2 failed, 752 passed, 3 skipped before the change and 754 passed, 3 skipped after it.
- `ruff check .` reported all checks passed. `python -m pytest -q` reported 1,039 passed with 13 supported skips. `agentops harness check .` reported ok. `git diff --check` reported no whitespace defects.
- Copy-based proof of the live defect and repair, with no write to any real framework home: on a faithful copy of the affected Prime home, `refresh` now reports `stable` at commit `cff170b920e0f5e6f47ea8d1117b5ee787cbd0c6` and a following `audit` reports `stable` at the same commit, with the Python 3.11 cache still present and both ownership manifests intact. Appending bytes to one managed `SKILL.md` on that same copy still reports `changed`, and an added unmanaged file in an owned root is still reported as unexpected.

## Next Actions

1. Obtain exact-head hosted Linux and Windows CI, then complete the repository's ordinary human review.
2. Merge only with Dan's explicit merge authority on the unchanged accepted head.
