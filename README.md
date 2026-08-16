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
agentops skills install prime-agent
```

By default, supported frameworks install all configured skill dependency bundles. Codex, Claude Code, and OpenCode retain the existing upstream layouts for gstack and Superpowers, while all six supported agent hosts receive HumanLayer's pinned `show-me` skill through a collision-safe adapter with a portable HTML artifact opener. Prime Agent receives generated Prime-native variants of the two larger bundles: gstack skills use `agentops-gstack-*` names with their built runtime under `.agentops/runtime/gstack`, and Superpowers skills use `agentops-superpowers-*` names. Fingerprint manifests prevent every adapted bundle from overwriting an unowned or locally modified skill.

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
agentops bootstrap prime-agent
```

Build framework context packs and handoff commands:

```bash
agentops context build examples/local-smoke.yaml --framework codex
agentops frameworks command examples/local-smoke.yaml --framework codex --cwd /tmp/agentops-example --json
agentops frameworks command examples/local-smoke.yaml --framework prime-agent --cwd /tmp/agentops-example --json
```

Prime Agent handoff uses its non-interactive `--print` mode and passes the job context with an explicit `--cwd`. Prime Agent skill bundles install namespaced skills under `${PRIME_AGENT_CODING_AGENT_DIR:-$HOME/.prime/agent}/skills` and keep ownership records outside the shared skill namespace. See [Prime Agent support](docs/prime-agent.md) for the complete setup, collision contract, and command contract.

## Pull-Based Deployment Channels

Agent Ops can keep multiple isolated framework homes on stable `main` or an explicitly selected branch. Each machine owns its registry, fetched snapshots, locks, receipts, and target homes; machines that follow the same branch do not share mutable deployment state. Stable remains the reviewed path, while a branch target can refresh immediately and continue to receive ordinary pull-request review in parallel.

Supported daily commands are:

```bash
agentops deployment status --all
agentops deployment plan --all
agentops deployment refresh --all
agentops deployment audit --all
agentops channel deploy skill-routing-v2 --ref refs/heads/feat/skill-routing-v2 --targets codex-skill-routing-v2,claude-skill-routing-v2
agentops channel refresh skill-routing-v2
agentops channel switch stable --targets codex-skill-routing-v2,claude-skill-routing-v2
agentops channel launch skill-routing-v2 --framework codex
```

Branch snapshots are data sources only. The stable installed package discovers providers, validates each provider's declared source closure, and plans against a restricted copy containing only that closure. Agent Ops does not import or execute Python from the branch, invoke branch build files or hooks, or copy unrelated catalog or configuration content. A grouped refresh plans every selected target before mutation, locks homes in deterministic order, audits the result, and restores every changed target if installation or audit fails. Incomplete recovery stops with retained transaction evidence under the affected target's `.agentops/deployment/transactions` directory.

Every target needs its own home. Authenticate the native framework inside that target's environment before launch, such as `CODEX_HOME=/path/to/home codex login`; the launch command reports the framework-specific prerequisite when readiness cannot be verified. Credentials remain in the isolated home and are not copied from a repository or another target.

For an explicitly unreviewed local skill edit, configure an existing preview-reserved target and run:

```bash
agentops deployment preview --source-checkout /path/to/agent-ops-checkout --skill my-skill --target codex-preview
```

Preview accepts only selected Git-tracked data from that checkout, requires every requested name to resolve exactly once to an installed provider's canonical skill identity and exclusively owned paths, includes selected working-tree changes in a SHA-256 fingerprint, and reports `review_state=unreviewed-local`. It retains descriptors for the checkout, selected paths and parents, Git index, and Git HEAD through planning, then revalidates their identities, bytes, modes, tracked state, and selected commit immediately before installation. It rejects stable and branch targets, links, untracked referenced resources, writable or Git-inconsistent modes, overlapping source and target paths, and managed refresh, switch, deploy, or launch treatment. It never fetches or publishes a ref or creates a managed source-store snapshot.

Installed extension packages expose deployment providers through the `agent_ops.deployment_providers` entry-point group. A provider supplies a stable `provider_id`, a boolean `supports(snapshot, target)` decision, an exact repository-relative `source_closure(snapshot, target, selection)`, and an immutable `plan(snapshot, target)`. Managed branch planning retains the tuple-of-paths closure contract. Preview requires `source_closure` to return `ProviderSourceClosure`, which binds the provider to canonical `SkillSourceClosure` identities, aliases, and exclusively owned paths. The shared engine confines and validates the closure before calling `plan`.

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
- If `agentops` is not found, rerun `python -m pip install -e ".[dev]"` from the repository root.
- Prime gstack generation requires Bun. Agent Ops uses the pinned upstream lockfile and stops without changing the Prime profile if generation or the required runtime build fails.
- The Prime gstack runtime contains compiled browser, design, and PDF executables and currently uses about 600 MB on Linux.

## Agent Harness

This repository carries the standard Agent Ops harness:

- `AGENTS.md`: portable agent entry point.
- `CLAUDE.md`: Claude Code-specific routing.
- `ARCHITECTURE.md`: package and workflow architecture.
- `.agentops/harness/BOOTSTRAP.md`: clock-in and clock-out contract.
- `.agentops/harness/PROGRESS.md`: active handoff state.
- `.agentops/harness/DECISIONS.md`: durable local decisions.
- `.agentops/harness/VERIFY.md`: verification gates.
