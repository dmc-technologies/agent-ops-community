# Agent Ops Community Claude Instructions

Claude Code should start with `AGENTS.md`, then read `ARCHITECTURE.md` and the
shared harness files under `.agentops/harness/`.

Use `CLAUDE.md` only for Claude-specific routing that does not belong in the
portable agent entry point.

## Operating Loop

- Read `.agentops/harness/BOOTSTRAP.md` before nontrivial work.
- Keep active handoff state in `.agentops/harness/PROGRESS.md`.
- Record durable architecture and workflow decisions in `.agentops/harness/DECISIONS.md`.
- Use shared memory only for distilled cross-agent conclusions, not automatic session logs.
- Run repository verification before claiming completion.

## Verification

```bash
ruff check .
pytest
agentops harness check .
```
