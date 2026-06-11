from __future__ import annotations

from pathlib import Path


def bootstrap_text() -> str:
    return """# Agent Ops Community Bootstrap

Use this repository as the public, portable agent operations core.

## Operating Loop

1. Read `AGENTS.md` and `.agentops/harness/BOOTSTRAP.md` when present.
2. Use shared-memory tools before nontrivial work when prior context may matter.
3. Run repository verification before reporting completion.
4. Write shared-memory entries only for reusable decisions, discoveries, or workflows.

## Verification

```bash
ruff check .
pytest
agentops harness check .
```
"""


def write_bootstrap(output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / "AGENTOPS.md"
    target.write_text(bootstrap_text(), encoding="utf-8")
    return target
