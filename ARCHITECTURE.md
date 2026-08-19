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

On native Windows, the same registry, source-store, grouped-refresh, audit, rollback, and recovery contracts use Windows filesystem authority rather than POSIX descriptors. The engine retains a handle chain for each managed ancestor with delete sharing denied, opens reparse points themselves and rejects them, and holds a native byte-range target lock while it validates and mutates the target. A competing process therefore cannot replace a validated directory with a junction during a managed operation. Transaction records and exact prior bytes remain recoverable after interruption, and an identical refresh exercises the same engine path as first installation.

Prime gstack has one narrow migration from its retired owner-only manifest into shared deployment ownership. The installed public provider declares the exact Prime target, public channel, canonical public gstack repository, pinned ref, and complete planned destination set. Under the retained target lock, the transaction accepts the legacy manifest only when its strict schema, owner, ref, canonical gstack-only paths, current file fingerprints, link counts, and planned modes all match. Before the first replacement, every accepted file and the retired manifest become bound to retained no-follow descriptors. Each backup move first journals its exact operation, path, fingerprint, mode, and filesystem identity. If a non-cooperating process replaces or deletes that path after classification, recovery uses the pre-move authority to distinguish an unstarted move, exact prior backup, mismatched concurrent backup, restored live entry, or deletion. A mismatched safe backup becomes exact concurrent evidence before no-replace live restoration; another live entry is never overwritten. Earlier replacements roll back, no shared manifest is published, and recovery cannot report completion until the concurrent state is restored. The legacy manifest and every owned file, including a file already equal to its planned bytes, become ordinary rollback-backed prior state in the same transaction; unrelated files remain untouched, and rollback or crash recovery restores the exact legacy files and manifest.

A planned removal of an exact prior-manifest Python source lets the locked transaction discover only its finite CPython 3.11–3.14 PEP 3147 cache paths for default, `opt-1`, and `opt-2` modes. Each accepted cache becomes a separate provenance-bound transaction operation with backup, rollback, and crash-recovery evidence. The running cache tag retains code-object equality validation; another listed tag is confined by exact derived name and parent, known magic, timestamp and source-length header, safe file identity and mode, and bounded size. Invalid or unrelated entries are preserved and remain visible to audit. This is cache-retirement support, not an expanded Python runtime contract.

The audit unexpected-file scan uses the same finite matrix for a plan-owned runtime Python source that remains installed. It resolves the candidate's source from the PEP 3147 name for any listed CPython 3.11–3.14 tag, requires that source to be declared in the plan's runtime Python sources, and accepts the cache only through the shared provenance check: running-tag caches keep code-object equality validation, and another listed tag is confined by exact derived name and parent, known magic, timestamp and source-length header bound to the live source, safe single-link `0644` identity, and bounded size. Resolving only the running interpreter's tag previously classified a cache left by an earlier installed CPython minor as an unexpected unmanaged file, which reported the target as `modified` and made every refresh fail its post-install audit and roll back. Unrelated and invalid entries remain unexpected.

Stable and arbitrary branch channels use managed source-store snapshots. Preview is separate: it requires an explicit checkout, one or more explicit skills, and an existing preview-reserved target. Installed providers bind each requested name to exactly one canonical skill identity and an exclusive path closure; missing identities, aliases that resolve to the same canonical skill, duplicate identities, ignored selection, and path collisions fail before capture, while a provider that owns no requested identity is omitted from planning. Preview permits only an internal allowlist of read-only Git metadata commands under executable-configuration-disabled environment and command settings, including linked-worktree Git paths. It retains descriptor authority over every selected file and parent plus the Git index and HEAD, fingerprints paths, kinds, modes, and bytes, plans from those captured bytes, and revalidates exact filesystem, index, and commit state immediately before using the shared target transaction and again after install and audit before terminal success. Target locks precede a retained shared authority for the exact original registry snapshot; planning is repeated under target locks and the target channel, home, framework, registry bytes, and registry identity are revalidated before apply and success. A terminal source or registry mismatch is a primary deployment failure inside the same reverse recovery boundary; incomplete recovery retains both the primary and recovery failures plus transaction evidence. Preview status takes the established target lock cooperatively, opens the ownership manifest exactly once, retains no-follow descriptors for the manifest and every owned file and directory, and terminally revalidates canonical identity, mode, and content. The strict manifest binds target ID, framework, exact channel alias, `unreviewed-local` state, and 64-hex fingerprint; status uses no managed fetch or receipt. Its source revision cannot be promoted, launched, switched, refreshed, or deployed as a managed channel.

Managed status uses the same installed ownership manifest that audit validates. It takes the established target lock cooperatively, opens the manifest once, retains no-follow descriptors for the manifest and every owned file and directory, and revalidates canonical identity, mode, and content before returning. The receipt for the current registry snapshot supplies only the last resolved commit, so classification stays deterministic without a fetch: a recorded revision equal to that commit reports the channel's stable or branch state, owned-state differences report `modified`, an absent or different recorded revision reports `stale`, and unreadable or contradictory evidence reports `failed`. A managed manifest must carry no preview review state and a full 40-hex source revision.

The shared grouped transaction carries one explicit channel transition per target. Its default is current channel to current channel, so an edited prior manifest cannot silently change authority during refresh, preview, or direct provider installation. Switch and deploy derive the only authorized prior-to-candidate pair from the original registry snapshot and the candidate plan. Prepared records persist both values; apply, rollback, and recovery reject any manifest or record outside that pair and restore the exact prior manifest bytes.

Framework launch is allowed only after a fresh exact-target audit while registry, target-home, manifest, and lock authorities remain retained. The adapter injects the isolated framework home through its native environment variable and checks only native readiness facts without reading credential contents. Authentication is a prerequisite performed separately inside every target home.

## Public Deployment Provider Interface

Providers are installed Python objects discovered from `agent_ops.deployment_providers`. `provider_id` is unique and stable. `supports` returns an exact boolean. For managed committed sources, `source_closure` returns a tuple of normalized repository-relative `Path` values. For preview, it receives the explicit selection and returns an exact `ProviderSourceClosure` containing canonical `SkillSourceClosure` identities, aliases, and owned paths; legacy tuple results fail closed only on preview. `plan` receives only a restricted `SourceSnapshot` and returns one exact immutable `ProviderPlan` bound to its provider, target, and source revision. The engine rejects provider type, identity, target, revision, path, mode, topology, or ownership conflicts before publication.

## Verification Architecture

The public repository must pass:

```bash
ruff check .
pytest
agentops harness check .
```

The test suite includes a public-safety scan that rejects private terms and
local absolute paths.
