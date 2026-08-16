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
- `agent_ops.deployment.models`: public immutable source, target, provider, plan, manifest, audit, status, and receipt contracts.
- `agent_ops.deployment.providers`: stable-package provider discovery through `agent_ops.deployment_providers` entry points.
- `agent_ops.deployment.source_store`: fetch-only Git mirrors, exact-ref history, and immutable commit snapshots owned by one machine state root.
- `agent_ops.deployment.registry`: strict machine-local channel configuration, atomic compare-and-swap updates, and append-only snapshot-bound receipts.
- `agent_ops.deployment.transaction`: the shared descriptor-bound install, audit, reverse rollback, and recovery implementation for all providers and targets.
- `agent_ops.deployment.engine`: complete-plan orchestration across sources, providers, sorted target locks, registry publication, and grouped recovery.
- `agent_ops.deployment.preview`: selected Git-tracked working-tree data capture for an isolated preview target without a managed fetch or source-store write.
- `agent_ops.cli`: thin Typer command surface.
- `AGENTS.md`, `CLAUDE.md`, and `.agentops/harness/`: repo-local agent
  operating contract and handoff state.

## Deployment Authority And Isolation

The reviewed installed package is executable authority. Stable and branch repositories supply only provider-declared data. The engine fetches each distinct source and ref once, resolves an immutable commit, opens the declared data closure without following links, materializes only that closure into a restricted temporary root, and asks installed providers for plans. It does not import repository code, run Git hooks, invoke build configuration, or expose unrelated repository content to a provider.

Each configured target has a unique canonical home. Each machine has an independent registry, source store, lock set, receipt sequence, and target homes. A branch name is a selection mechanism, not shared deployment state. A refresh completes all planning before mutation, acquires target locks in sorted order, replans under those locks, applies the shared transaction, audits every target, and appends one registry-snapshot-bound receipt only after success. Failure restores changed targets in reverse order. When recovery cannot complete, the operation fails and retains transaction evidence rather than reporting success.

Stable and arbitrary branch channels use managed source-store snapshots. Preview is separate: it requires an explicit checkout, one or more explicit skills, and an existing preview-reserved target. It reads exactly the selected Git-tracked closure with descriptor-backed containment, fingerprints paths, kinds, modes, and bytes, plans with installed providers, and uses the same target transaction and audit. Its 64-hex source revision and `unreviewed-local` review state cannot be promoted, launched, switched, refreshed, or deployed as a managed channel.

Framework launch is allowed only after a fresh exact-target audit while registry, target-home, manifest, and lock authorities remain retained. The adapter injects the isolated framework home through its native environment variable and checks only native readiness facts without reading credential contents. Authentication is a prerequisite performed separately inside every target home.

## Public Deployment Provider Interface

Providers are installed Python objects discovered from `agent_ops.deployment_providers`. `provider_id` is unique and stable. `supports` returns an exact boolean. `source_closure` returns a tuple of normalized repository-relative `Path` values and receives an optional explicit selection for preview. `plan` receives only a restricted `SourceSnapshot` and returns one exact immutable `ProviderPlan` bound to its provider, target, and source revision. The engine rejects provider type, identity, target, revision, path, mode, topology, or ownership conflicts before publication.

## Verification Architecture

The public repository must pass:

```bash
ruff check .
pytest
agentops harness check .
```

The test suite includes a public-safety scan that rejects private terms and
local absolute paths.
