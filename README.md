# Agent Ops Community

Agent Ops Community is a public, tool-neutral operations layer for agentic
development workflows. It provides repository harness templates, job contracts,
context handoff, framework bootstrap guidance, plugin discovery, and verification
helpers that can be used by local agents and framework-specific runners.

This repository is intended to contain the full Agent Ops experience for common
agent frameworks. Only proprietary runner/verifier implementations and
organization-owned operational workflows belong in separately installed
extension packages.

## Install

```bash
git clone https://github.com/<your-org>/agent-ops-community.git
cd agent-ops-community
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Install common skill bundles for your agent framework:

```bash
agentops skills install codex
agentops skills install opencode
```

By default, supported frameworks install all configured skill dependency
bundles. The pinned gstack repository is installed as a complete bundle under
`skills/gstack`; Superpowers installs every skill from its pinned `skills/`
tree.

## Quick Start

Initialize a repo-local harness:

```bash
mkdir -p /tmp/agentops-example
agentops harness init /tmp/agentops-example --repo-name agentops-example --repo-type python
agentops harness check /tmp/agentops-example
```

Validate and run verification from a job contract:

```bash
agentops validate examples/local-smoke.yaml
agentops verify examples/local-smoke.yaml --json
```

Generate bootstrap instructions:

```bash
agentops bootstrap all
agentops bootstrap codex
```

Build framework context packs and handoff commands:

```bash
agentops context build examples/local-smoke.yaml --framework codex
agentops frameworks command examples/local-smoke.yaml --framework codex --json
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
- verification gates
- built-in support for common agent-framework handoff and execution paths
- plugin interfaces for proprietary or specialized execution paths

See [docs/roadmap.md](docs/roadmap.md) for the remaining generic scope planned
for the community package.

## Development

```bash
ruff check .
pytest
agentops harness check .
```

## Troubleshooting

- Use Python 3.11 or newer.
- Run commands from an activated virtual environment, or prefix them with
  `.venv/bin/`.
- If `agentops` is not found, rerun `python -m pip install -e ".[dev]"` from
  the repository root.

## Agent Harness

This repository carries the standard Agent Ops harness:

- `AGENTS.md`: portable agent entry point.
- `CLAUDE.md`: Claude Code-specific routing.
- `ARCHITECTURE.md`: package and workflow architecture.
- `.agentops/harness/BOOTSTRAP.md`: clock-in and clock-out contract.
- `.agentops/harness/PROGRESS.md`: active handoff state.
- `.agentops/harness/DECISIONS.md`: durable local decisions.
- `.agentops/harness/VERIFY.md`: verification gates.
