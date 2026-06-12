# Architecture

## Public Core Boundary

Agent Ops Community owns the general-purpose Agent Ops experience: harnesses,
job contracts, context handoff, framework bootstrap, common framework command
handoff, verification helpers, and plugin interfaces. It does not own
proprietary runner/verifier implementations or organization-specific operational
workflows.

## Runtime And Tooling

- Python 3.11 or newer.
- Typer for the CLI.
- Pydantic for typed contracts and result manifests.
- pytest and ruff for verification.

## Package Boundaries

- `agent_ops.contracts`: stable job and result models.
- `agent_ops.harness`: file-based repository harness templates and checks.
- `agent_ops.plugins`: public extension interfaces and plugin discovery.
- `agent_ops.verify`: deterministic local verification execution.
- `agent_ops.cli`: thin Typer command surface.
- `AGENTS.md`, `CLAUDE.md`, and `.agentops/harness/`: repo-local agent
  operating contract and handoff state.

## Verification Architecture

The public repository must pass:

```bash
ruff check .
pytest
agentops harness check .
```

The test suite includes a public-safety scan that rejects private terms and
local absolute paths.
