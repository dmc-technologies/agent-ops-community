# Agent Ops Community

Agent Ops Community is a public, tool-neutral operations layer for agentic
development workflows. It provides repository harness templates, job contracts,
context handoff, framework bootstrap guidance, plugin discovery, and verification
helpers that can be used by local agents and framework-specific runners.

This repository is intended to contain the full generic Agent Ops experience.
Runner-specific internals, proprietary verifier prompts, and organization-owned
operational workflows belong in separately installed extension packages.

## Install

```bash
python -m pip install -e ".[dev]"
```

## Quick Start

Initialize a repo-local harness:

```bash
agentops harness init . --repo-type python
agentops harness check .
```

Validate and run verification from a job contract:

```bash
agentops validate examples/local-smoke.yaml
agentops verify examples/local-smoke.yaml --json
```

Generate bootstrap instructions:

```bash
agentops bootstrap
```

## Extension Model

Third-party and organization-specific runners integrate through Python entry
points under the `agent_ops.plugins` group. The public core discovers installed
plugins only when running plugin-backed execution paths. Harness checks,
contract validation, and public safety checks do not import arbitrary plugins.

## Community Scope

Agent Ops Community should support the same generic workflow shape across agent
frameworks:

- repository harnesses and clock-in/clock-out conventions
- job contracts and result manifests
- context packs and framework handoff commands
- bootstrap instructions for common agent frameworks
- capability, skill, and tool registries
- environment checks and verification gates
- plugin interfaces for runner-specific execution

See [docs/roadmap.md](docs/roadmap.md) for the remaining generic scope planned
for the community package.

## Development

```bash
ruff check .
pytest
```
