# Agent Ops Community Claude Instructions

Claude Code should start with `AGENTS.md`, then read `ARCHITECTURE.md`.

Use `CLAUDE.md` only for Claude-specific routing that does not belong in the
portable agent entry point.

## Operating Loop

- Hand off through GitHub pull requests and the tracker; this repository keeps no progress file.
- Record durable architecture and workflow decisions in `docs/DECISIONS.md`.
- Use shared memory only for distilled cross-agent conclusions, not automatic session logs.
- Run repository verification before claiming completion.

## Verification

```bash
ruff check .
pytest
```
