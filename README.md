# Agent Ops Community

Agent Ops Community is a public, tool-neutral core for agentic development
workflows. It provides repository harness templates, job contracts, plugin
discovery, and verification helpers that can be used by local agents and
framework-specific runners.

This repository intentionally contains only generic infrastructure. Proprietary
runner implementations, private prompts, private job examples, and internal
operational workflows belong in private plugins.

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

Private and third-party runners integrate through Python entry points under the
`agent_ops.plugins` group. The public core discovers installed plugins only when
running plugin-backed execution paths. Harness checks, contract validation, and
public safety checks do not import arbitrary plugins.

## Development

```bash
ruff check .
pytest
```

