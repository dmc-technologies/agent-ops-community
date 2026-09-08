"""Deterministic orchestration for grouped managed deployments."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from pathlib import Path

from agent_ops.deployment.models import (
    DeploymentAudit,
    DeploymentManifest,
    DeploymentPlan,
    DeploymentProvider,
    DeploymentReceipt,
    PlannedFile,
    ProviderPlan,
    RewriteAcceptance,
    SharedSelectionActivation,
    SharedTargetSource,
    SharedTargetSpec,
    SharedTargetStatus,
    SourceSnapshot,
    TargetChannelTransition,
    TargetSource,
    TargetSpec,
    TargetState,
    TargetStatus,
)
from agent_ops.deployment.providers import (
    load_deployment_providers,
    normalize_deployment_providers,
)
from agent_ops.deployment.registry import (
    ChannelSpec,
    DeploymentRegistry,
    RegistryConfig,
    RegistrySnapshot,
    _is_preview_channel,
    _RegistrySnapshotAuthority,
)
from agent_ops.deployment.shared_content import build_shared_content
from agent_ops.deployment.shared_selection import SharedSelection, SharedSourceBinding
from agent_ops.deployment.source_store import (
    SourceStore,
    _open_provider_data_closure,
    _validate_provider_data_closure,
)
from agent_ops.deployment.transaction import (
    _locked_provider_plan_targets,
    _preflight_provider_plans_read_only,
    _read_managed_status_evidence,
    _read_preview_status_evidence,
    _strict_json_loads,
    audit_provider_plans,
    install_provider_plans,
    read_shared_target_evidence,
    retain_provider_plan_evidence,
    rollback_manifests,
)


def _shared_provider_plan(
    target: SharedTargetSpec,
    selection: SharedSelection,
    content: tuple[PlannedFile, ...],
    retained: tuple[PlannedFile, ...],
) -> ProviderPlan:
    files = {item.path: item for item in retained}
    for item in content:
        if item.path in files and files[item.path] != item:
            raise ValueError("shared immutable snapshot content changed")
        files[item.path] = item
    return ProviderPlan(
        "shared-skills",
        selection.fingerprint,
        target,
        tuple(files[path] for path in sorted(files, key=str)),
        audit_roots=(Path("snapshots"),),
        selection_activation=SharedSelectionActivation(Path("snapshots") / selection.fingerprint),
    )


def _selection_from_shared_files(
    files: tuple[PlannedFile, ...], fingerprint: str
) -> SharedSelection:
    path = Path("snapshots") / fingerprint / "source-map.json"
    matches = [item for item in files if item.path == path]
    if len(matches) != 1:
        raise ValueError("shared snapshot source map is missing")
    data = _strict_json_loads(matches[0].content, label="shared source map")
    if (
        type(data) is not dict
        or set(data)
        not in (
            {"schema_version", "selection_fingerprint", "skills"},
            {"schema_version", "selection_fingerprint", "skills", "policy"},
        )
        or type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or data["selection_fingerprint"] != fingerprint
        or type(data["skills"]) is not list
        or not data["skills"]
    ):
        raise ValueError("invalid shared source map")
    grouped, names = {}, set()
    for item in data["skills"]:
        if (
            type(item) is not dict
            or set(item) != {"name", "source_id", "commit", "path"}
            or any(type(value) is not str or not value for value in item.values())
        ):
            raise ValueError("invalid shared skill source mapping")
        if item["name"] in names:
            raise ValueError("duplicate shared skill identity")
        names.add(item["name"])
        binding = grouped.setdefault(item["source_id"], (item["commit"], []))
        if binding[0] != item["commit"] or item["path"] in binding[1]:
            raise ValueError("conflicting shared skill source mapping")
        binding[1].append(item["path"])
    policy = None
    if "policy" in data:
        from agent_ops.deployment.shared_selection import SharedPolicyBinding

        item = data["policy"]
        if (
            type(item) is not dict
            or set(item) != {"source_id", "commit", "path"}
            or any(type(value) is not str or not value for value in item.values())
            or item["source_id"] not in grouped
            or grouped[item["source_id"]][0] != item["commit"]
        ):
            raise ValueError("invalid shared policy source mapping")
        policy = SharedPolicyBinding(source_id=item["source_id"], path=item["path"])
        policy_path = Path("snapshots") / fingerprint / "policy" / "AGENTS.md"
        if sum(file.path == policy_path for file in files) != 1:
            raise ValueError("shared source map requires one owned policy file")
    selection = SharedSelection(
        policy=policy,
        sources=tuple(
            SharedSourceBinding(source_id=key, commit=value[0], skill_paths=tuple(value[1]))
            for key, value in grouped.items()
        ),
    )
    if selection.fingerprint != fingerprint:
        raise ValueError("shared source map fingerprint does not match its constituents")
    return selection


class DeploymentEngineError(RuntimeError):
    """Base error for orchestration failures."""


class DeploymentAuditError(DeploymentEngineError):
    """Installed output did not match every accepted provider plan."""


class DeploymentRecoveryError(DeploymentEngineError):
    """A failed operation could not restore every affected authority."""


@dataclass(frozen=True)
class LaunchAuthorization:
    """Exact audited launch evidence held under deployment authorities."""

    target: TargetSpec
    status: TargetStatus
    receipt: DeploymentReceipt
    plan: DeploymentPlan
    registry_snapshot: RegistrySnapshot
    _registry_authority: _RegistrySnapshotAuthority
    _deployment_authority: object

    def verify(self) -> None:
        self._deployment_authority.verify()
        self._registry_authority.verify()


class DeploymentEngine:
    """Coordinate immutable sources, trusted providers, and atomic targets."""

    def __init__(
        self,
        registry: DeploymentRegistry,
        source_store: SourceStore,
        providers: tuple[DeploymentProvider, ...] | None = None,
    ) -> None:
        if not isinstance(registry, DeploymentRegistry):
            raise TypeError("registry must be a DeploymentRegistry")
        if not isinstance(source_store, SourceStore):
            raise TypeError("source_store must be a SourceStore")
        if providers is not None and type(providers) is not tuple:
            raise TypeError("providers must be a tuple when explicitly supplied")
        self._registry = registry
        self._source_store = source_store
        discovered = load_deployment_providers() if providers is None else providers
        self._providers = normalize_deployment_providers(discovered)

    def shared_status(self) -> SharedTargetStatus:
        """Inspect the installed shared selection without fetching any source."""
        snapshot = self._registry.load_snapshot()
        shared = snapshot.config.shared_selection
        if shared is None:
            raise ValueError("registry has no shared selection")
        try:
            manifest, audit, files = read_shared_target_evidence(shared.target)
        except Exception as error:
            return self._registry.shared_status(
                manifest=None,
                audit=None,
                source_mapping_fingerprint=None,
                failure=str(error),
                snapshot=snapshot,
            )
        mapping_fingerprint = None
        if manifest is not None:
            with suppress(ValueError, KeyError, TypeError):
                mapping_fingerprint = _selection_from_shared_files(
                    files, manifest.source_revision
                ).fingerprint
        return self._registry.shared_status(
            manifest=manifest,
            audit=audit,
            source_mapping_fingerprint=mapping_fingerprint,
            snapshot=snapshot,
        )

    def shared_sync(self, selection: SharedSelection | None = None) -> DeploymentReceipt:
        """Publish a complete pinned source selection through the shared target."""
        return self._shared_operation(selection, rollback=False)

    def shared_rollback(self) -> DeploymentReceipt:
        """Republish the verified retained previous selection without network access."""
        return self._shared_operation(None, rollback=True)

    def _shared_operation(
        self, selection: SharedSelection | None, *, rollback: bool
    ) -> DeploymentReceipt:
        original = self._registry.load_snapshot()
        shared = original.config.shared_selection
        if shared is None:
            raise ValueError("registry has no shared selection")
        desired = (
            shared.previous
            if rollback
            else (selection if selection is not None else shared.selected)
        )
        if type(desired) is not SharedSelection:
            raise ValueError(
                "shared rollback requires a retained previous selection"
                if rollback
                else "invalid shared selection"
            )
        # Validate source registration before fetching or touching the shared home.
        replace(original.config, shared_selection=replace(shared, selected=desired))
        manifest, audit, retained = read_shared_target_evidence(shared.target)
        if manifest is not None and (audit is None or not audit.matches):
            raise ValueError("shared target changed; refusing to replace modified snapshots")
        if rollback:
            if manifest is None:
                raise ValueError("shared rollback requires an installed selection")
            observed = _selection_from_shared_files(retained, desired.fingerprint)
            if observed != desired:
                raise ValueError("retained previous snapshot source mapping does not match")
            prefix = Path("snapshots") / desired.fingerprint
            content = tuple(item for item in retained if prefix in item.path.parents)
            snapshots = ()
        else:
            sources = {source.id: source for source in original.config.sources}
            snapshots = tuple(
                self._source_store.fetch_pinned(sources[binding.source_id], binding.commit)
                for binding in desired.sources
            )
            content = build_shared_content(desired, snapshots)
        initial = _shared_provider_plan(shared.target, desired, content, retained)
        if not rollback:
            DeploymentPlan(
                snapshots, (initial,), (SharedTargetSource(shared.id, shared.channel, desired),)
            )
        _preflight_provider_plans_read_only((initial,))
        with _locked_provider_plan_targets((initial,)):
            manifest, audit, retained = read_shared_target_evidence(shared.target)
            if manifest is not None and (audit is None or not audit.matches):
                raise ValueError("shared target changed while acquiring its lock")
            installed = (
                _selection_from_shared_files(retained, manifest.source_revision)
                if manifest is not None
                else None
            )
            if rollback and _selection_from_shared_files(retained, desired.fingerprint) != desired:
                raise ValueError("retained previous snapshot changed")
            previous = shared.previous if installed == desired else installed
            if (
                previous is not None
                and _selection_from_shared_files(retained, previous.fingerprint) != previous
            ):
                raise ValueError("previous source map does not match the retained selection")
            candidate = replace(
                original.config,
                shared_selection=replace(shared, selected=desired, previous=previous),
            )
            provider_plan = _shared_provider_plan(shared.target, desired, content, retained)
            _preflight_provider_plans_read_only((provider_plan,))
            # Repeated synchronization verifies existing bytes without publishing a
            # replacement ownership manifest or discarding the previous selection.
            manifests = () if installed == desired else install_provider_plans((provider_plan,))
            operation = "shared-rollback" if rollback else "shared-sync"

            def receipt_factory(snapshot, audits):
                return DeploymentReceipt(
                    operation,
                    tuple(sorted({binding.commit for binding in desired.sources})),
                    (
                        SharedTargetStatus(
                            shared.id, TargetState.STABLE, shared.channel, desired.fingerprint
                        ),
                    ),
                )

            return self._commit_candidate(
                original, candidate, (provider_plan,), manifests, receipt_factory
            )

    def status(
        self, target_ids: tuple[str, ...] | None = None
    ) -> tuple[TargetStatus, ...]:
        snapshot = self._registry.load_snapshot()
        targets = self._select_targets(
            snapshot.config, target_ids, allow_all=True, allow_preview=True
        )
        latest: dict[str, TargetStatus] = {}
        if any(not _is_preview_channel(target.channel) for target in targets):
            for record in self._registry.receipt_records():
                if record.registry_fingerprint != snapshot.fingerprint:
                    continue
                for target_status in record.receipt.targets:
                    latest[target_status.target_id] = target_status
        statuses: list[TargetStatus] = []
        for target in targets:
            if _is_preview_channel(target.channel):
                try:
                    manifest, audit = _read_preview_status_evidence(target)
                except Exception as error:
                    statuses.append(
                        self._registry.status(
                            target.id,
                            manifest=None,
                            resolved_commit=None,
                            audit=None,
                            failure=str(error),
                            snapshot=snapshot,
                        )
                    )
                    continue
                statuses.append(
                    self._registry.status(
                        target.id,
                        manifest=manifest,
                        resolved_commit=(
                            manifest.source_revision if manifest is not None else None
                        ),
                        audit=audit,
                        snapshot=snapshot,
                    )
                )
                continue
            recorded = latest.get(target.id)
            if recorded is None or recorded.channel != target.channel:
                statuses.append(
                    self._registry.status(
                        target.id,
                        manifest=None,
                        resolved_commit=None,
                        audit=None,
                        snapshot=snapshot,
                    )
                )
                continue
            if recorded.state is TargetState.FAILED:
                statuses.append(
                    self._registry.status(
                        target.id,
                        manifest=None,
                        resolved_commit=recorded.commit,
                        audit=None,
                        failure="latest deployment receipt records failure",
                        snapshot=snapshot,
                    )
                )
                continue
            if recorded.state is TargetState.MISSING_REF:
                statuses.append(
                    self._registry.status(
                        target.id,
                        manifest=None,
                        resolved_commit=None,
                        audit=None,
                        snapshot=snapshot,
                    )
                )
                continue
            try:
                manifest, audit = _read_managed_status_evidence(target)
            except Exception as error:
                statuses.append(
                    self._registry.status(
                        target.id,
                        manifest=None,
                        resolved_commit=None,
                        audit=None,
                        failure=f"installed deployment manifest is unreadable: {error}",
                        snapshot=snapshot,
                    )
                )
                continue
            if recorded.state is TargetState.MODIFIED:
                audit = DeploymentAudit(target.id, matches=False, changed=("recorded",))
            statuses.append(
                self._registry.status(
                    target.id,
                    manifest=manifest,
                    resolved_commit=recorded.commit,
                    audit=audit,
                    snapshot=snapshot,
                )
            )
        return tuple(statuses)

    def plan(self, target_ids: tuple[str, ...]) -> DeploymentPlan:
        registry_snapshot = self._registry.load_snapshot()
        targets = self._select_targets(registry_snapshot.config, target_ids)
        snapshots = self._fetch_snapshots(registry_snapshot.config, targets)
        plan = self._build_plan(registry_snapshot.config, targets, snapshots)
        _preflight_provider_plans_read_only(plan.provider_plans)
        return plan

    def refresh(
        self,
        target_ids: tuple[str, ...],
        *,
        rewrite: RewriteAcceptance | None = None,
    ) -> DeploymentReceipt:
        registry_snapshot = self._registry.load_snapshot()
        targets = self._select_targets(registry_snapshot.config, target_ids)
        snapshots = self._fetch_snapshots(
            registry_snapshot.config,
            targets,
            rewrite=rewrite,
        )
        plan = self._build_plan(registry_snapshot.config, targets, snapshots)
        _preflight_provider_plans_read_only(plan.provider_plans)
        with _locked_provider_plan_targets(plan.provider_plans):
            plan = self._build_plan(registry_snapshot.config, targets, snapshots)
            _preflight_provider_plans_read_only(plan.provider_plans)
            plan, manifests = self._apply_with_one_stale_retry(
                registry_snapshot.config,
                targets,
                snapshots,
                plan,
            )
            try:
                audits = self._audit_plans(plan.provider_plans, require_matches=True)
                with retain_provider_plan_evidence(plan.provider_plans) as authority:
                    receipt = self._receipt(
                        "refresh",
                        registry_snapshot,
                        targets,
                        snapshots,
                        manifests=manifests,
                        audits=audits,
                    )
                    authority.verify()
                    self._registry.append_receipt(receipt, snapshot=registry_snapshot)
            except BaseException as error:
                self._rollback_after_failure(manifests, error)
                raise
        return receipt

    def audit(self, target_ids: tuple[str, ...]) -> DeploymentReceipt:
        registry_snapshot = self._registry.load_snapshot()
        targets = self._select_targets(registry_snapshot.config, target_ids)
        snapshots = self._fetch_snapshots(registry_snapshot.config, targets)
        plan = self._build_plan(registry_snapshot.config, targets, snapshots)
        with _locked_provider_plan_targets(plan.provider_plans):
            plan = self._build_plan(registry_snapshot.config, targets, snapshots)
            audits = self._audit_plans(plan.provider_plans, require_matches=False)
            with retain_provider_plan_evidence(
                plan.provider_plans,
                require_matches=False,
                expected_audits=audits,
            ) as authority:
                receipt = self._receipt(
                    "audit",
                    registry_snapshot,
                    targets,
                    snapshots,
                    manifests=(),
                    audits=audits,
                )
                authority.verify()
                self._registry.append_receipt(receipt, snapshot=registry_snapshot)
        return receipt

    @contextmanager
    def launch_authorization(
        self, target_id: str
    ) -> Iterator[LaunchAuthorization]:
        registry_snapshot = self._registry.load_snapshot()
        targets = self._select_targets(registry_snapshot.config, (target_id,))
        snapshots = self._fetch_snapshots(registry_snapshot.config, targets)
        plan = self._build_plan(registry_snapshot.config, targets, snapshots)
        with _locked_provider_plan_targets(plan.provider_plans):
            plan = self._build_plan(registry_snapshot.config, targets, snapshots)
            audits = self._audit_plans(plan.provider_plans, require_matches=False)
            receipt = self._receipt(
                "audit",
                registry_snapshot,
                targets,
                snapshots,
                manifests=(),
                audits=audits,
            )
            with retain_provider_plan_evidence(plan.provider_plans) as authority:
                authority.verify()
                self._registry.append_receipt(receipt, snapshot=registry_snapshot)
                with self._registry.retain_snapshot(registry_snapshot) as registry_authority:
                    authorization = LaunchAuthorization(
                        targets[0],
                        receipt.targets[0],
                        receipt,
                        plan,
                        registry_snapshot,
                        registry_authority,
                        authority,
                    )
                    authorization.verify()
                    yield authorization

    def switch(
        self, channel: str, target_ids: tuple[str, ...]
    ) -> DeploymentReceipt:
        original_snapshot = self._registry.load_snapshot()
        if type(channel) is not str or not channel:
            raise ValueError("channel must be a nonempty string")
        channels = {item.id for item in original_snapshot.config.channels}
        if channel not in channels:
            raise ValueError(f"unknown channel: {channel}")
        selected = self._select_targets(original_snapshot.config, target_ids)
        candidate, candidate_targets = self._candidate_config(
            original_snapshot.config,
            selected,
            channel,
            original_snapshot.config.channels,
        )
        return self._deploy_candidate(
            "switch",
            original_snapshot,
            candidate,
            candidate_targets,
        )

    def deploy(
        self,
        channel: str,
        ref: str,
        target_ids: tuple[str, ...],
        *,
        rewrite: RewriteAcceptance | None = None,
    ) -> DeploymentReceipt:
        original_snapshot = self._registry.load_snapshot()
        selected = self._select_targets(original_snapshot.config, target_ids)
        configured_channels = {
            item.id: item for item in original_snapshot.config.channels
        }
        source_ids = {
            configured_channels[target.channel].source for target in selected
        }
        if len(source_ids) != 1:
            raise ValueError(
                "deploy targets must resolve to exactly one configured source"
            )
        source_id = next(iter(source_ids))
        sources = {item.id: item for item in original_snapshot.config.sources}
        source = sources[source_id]
        if type(ref) is not str or not ref.startswith("refs/heads/"):
            raise ValueError("deploy ref must begin exactly with refs/heads/")
        requested = ChannelSpec(channel, source_id, ref)
        if _is_preview_channel(requested.id):
            raise ValueError("deploy channel alias must not use a preview-reserved name")
        if requested.ref == source.stable_ref:
            raise ValueError("deploy ref must not be the configured stable ref")
        existing = configured_channels.get(channel)
        if existing is not None and existing != requested:
            raise ValueError("existing channel alias source or ref differs")
        channels = (
            original_snapshot.config.channels
            if existing is not None
            else original_snapshot.config.channels + (requested,)
        )
        candidate, candidate_targets = self._candidate_config(
            original_snapshot.config,
            selected,
            channel,
            channels,
        )
        return self._deploy_candidate(
            "deploy",
            original_snapshot,
            candidate,
            candidate_targets,
            rewrite=rewrite,
        )

    @staticmethod
    def _candidate_config(
        config: RegistryConfig,
        selected: tuple[TargetSpec, ...],
        channel: str,
        channels: tuple[ChannelSpec, ...],
    ) -> tuple[RegistryConfig, tuple[TargetSpec, ...]]:
        selected_ids = {target.id for target in selected}
        candidate = RegistryConfig(
            config.schema_version,
            config.sources,
            channels,
            tuple(
                replace(target, channel=channel)
                if target.id in selected_ids
                else target
                for target in config.targets
            ),
        )
        candidate_targets = tuple(
            target for target in candidate.targets if target.id in selected_ids
        )
        return candidate, candidate_targets

    def _deploy_candidate(
        self,
        operation: str,
        original_snapshot: RegistrySnapshot,
        candidate: RegistryConfig,
        candidate_targets: tuple[TargetSpec, ...],
        *,
        rewrite: RewriteAcceptance | None = None,
    ) -> DeploymentReceipt:
        original_targets = {target.id: target for target in original_snapshot.config.targets}
        channel_transitions = tuple(
            TargetChannelTransition(
                target.id,
                original_targets[target.id].channel,
                target.channel,
            )
            for target in candidate_targets
        )
        snapshots = self._fetch_snapshots(
            candidate,
            candidate_targets,
            rewrite=rewrite,
        )
        plan = self._build_plan(candidate, candidate_targets, snapshots)
        _preflight_provider_plans_read_only(
            plan.provider_plans,
            channel_transitions=channel_transitions,
        )
        with _locked_provider_plan_targets(plan.provider_plans):
            plan = self._build_plan(candidate, candidate_targets, snapshots)
            _preflight_provider_plans_read_only(
                plan.provider_plans,
                channel_transitions=channel_transitions,
            )
            plan, manifests = self._apply_with_one_stale_retry(
                candidate,
                candidate_targets,
                snapshots,
                plan,
                channel_transitions=channel_transitions,
            )
            return self._commit_candidate(
                original_snapshot,
                candidate,
                plan.provider_plans,
                manifests,
                lambda candidate_snapshot, audits: self._receipt(
                    operation,
                    candidate_snapshot,
                    candidate_targets,
                    snapshots,
                    manifests=manifests,
                    audits=audits,
                ),
            )

    def _commit_candidate(
        self,
        original_snapshot,
        candidate,
        provider_plans,
        manifests,
        receipt_factory,
    ) -> DeploymentReceipt:
        """Commit audited targets and registry selection through one recovery path."""
        candidate_snapshot: RegistrySnapshot | None = None
        try:
            audits = self._audit_plans(provider_plans, require_matches=True)
            with retain_provider_plan_evidence(provider_plans) as authority:
                candidate_snapshot = (
                    original_snapshot
                    if candidate == original_snapshot.config
                    else self._registry.save(candidate, expected_snapshot=original_snapshot)
                )
                receipt = receipt_factory(candidate_snapshot, audits)
                authority.verify()
                self._registry.append_receipt(receipt, snapshot=candidate_snapshot)
        except BaseException as error:
            recovery_errors: list[BaseException] = []
            if candidate_snapshot is not None and candidate_snapshot != original_snapshot:
                try:
                    self._registry.save(
                        original_snapshot.config, expected_snapshot=candidate_snapshot
                    )
                except BaseException as recovery_error:
                    if not isinstance(recovery_error, Exception):
                        recovery_error.add_note(
                            "deployment recovery incomplete while restoring the "
                            f"registry; original failure: {error}; target rollback "
                            "was not attempted"
                        )
                        raise
                    recovery_errors.append(recovery_error)
            try:
                rollback_manifests(manifests)
            except BaseException as recovery_error:
                if not isinstance(recovery_error, Exception):
                    prior_recovery = "; ".join(str(item) for item in recovery_errors)
                    recovery_error.add_note(
                        "deployment recovery incomplete while restoring targets; "
                        f"original failure: {error}; prior recovery failures: "
                        f"{prior_recovery or 'none'}; transaction evidence retained"
                    )
                    raise
                recovery_errors.append(recovery_error)
            if recovery_errors:
                self._raise_incomplete_recovery(error, recovery_errors)
            raise
        return receipt

    @staticmethod
    def _select_targets(
        config: RegistryConfig,
        target_ids: tuple[str, ...] | None,
        *,
        allow_all: bool = False,
        allow_preview: bool = False,
    ) -> tuple[TargetSpec, ...]:
        if target_ids is None:
            if not allow_all:
                raise ValueError("target ids are required")
            selected = config.targets
            if not allow_preview and any(
                _is_preview_channel(target.channel) for target in selected
            ):
                raise ValueError("managed deployment operations reject preview targets")
            return selected
        if type(target_ids) is not tuple or not target_ids:
            raise ValueError("target ids must be a nonempty tuple")
        if any(type(target_id) is not str or not target_id for target_id in target_ids):
            raise ValueError("target ids must be nonempty strings")
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("duplicate target ids are not allowed")
        targets = {target.id: target for target in config.targets}
        unknown = sorted(set(target_ids) - targets.keys())
        if unknown:
            raise ValueError(f"unknown target: {unknown[0]}")
        selected = tuple(targets[target_id] for target_id in sorted(target_ids))
        if not allow_preview and any(
            _is_preview_channel(target.channel) for target in selected
        ):
            raise ValueError("managed deployment operations reject preview targets")
        return selected

    def _fetch_snapshots(
        self,
        config: RegistryConfig,
        targets: tuple[TargetSpec, ...],
        *,
        rewrite: RewriteAcceptance | None = None,
    ) -> dict[tuple[str, str], SourceSnapshot]:
        sources = {source.id: source for source in config.sources}
        channels = {channel.id: channel for channel in config.channels}
        keys = {
            (channels[target.channel].source, channels[target.channel].ref)
            for target in targets
        }
        if rewrite is not None and len(keys) != 1:
            raise ValueError("rewrite acceptance requires exactly one selected source ref")
        snapshots: dict[tuple[str, str], SourceSnapshot] = {}
        for key in sorted(keys):
            source_id, ref = key
            snapshots[key] = self._source_store.fetch(
                sources[source_id],
                ref,
                rewrite=rewrite,
            )
        return snapshots

    def _build_plan(
        self,
        config: RegistryConfig,
        targets: tuple[TargetSpec, ...],
        snapshots: dict[tuple[str, str], SourceSnapshot],
    ) -> DeploymentPlan:
        channels = {channel.id: channel for channel in config.channels}
        plans: list[ProviderPlan] = []
        for target in targets:
            channel = channels[target.channel]
            snapshot = snapshots[(channel.source, channel.ref)]
            supported: list[DeploymentProvider] = []
            for provider in self._providers:
                decision = provider.supports(snapshot, target)
                if type(decision) is not bool:
                    raise ValueError("provider supports decision must be boolean")
                if decision:
                    supported.append(provider)
            if not supported:
                raise ValueError(f"no deployment provider supports target {target.id!r}")
            for provider in supported:
                plans.append(self._plan_provider(provider, snapshot, target))
        ordered_snapshots = tuple(snapshots[key] for key in sorted(snapshots))
        ordered_plans = tuple(
            sorted(plans, key=lambda plan: (plan.target.id, plan.provider_id))
        )
        target_sources: list[TargetSource] = []
        for target in sorted(targets, key=lambda item: item.id):
            channel = channels[target.channel]
            snapshot = snapshots[(channel.source, channel.ref)]
            target_sources.append(
                TargetSource(
                    target.id,
                    target.channel,
                    channel.source,
                    snapshot.ref,
                    snapshot.commit,
                )
            )
        return DeploymentPlan(ordered_snapshots, ordered_plans, tuple(target_sources))

    def _plan_provider(
        self,
        provider: DeploymentProvider,
        snapshot: SourceSnapshot,
        target: TargetSpec,
    ) -> ProviderPlan:
        declared = provider.source_closure(snapshot, target, None)
        if type(declared) is not tuple:
            raise ValueError("provider source closure must be a tuple")
        _validate_provider_data_closure(snapshot, declared)
        expanded = self._expand_declared_closure(snapshot, declared)
        with (
            _open_provider_data_closure(snapshot, expanded) as closure,
            tempfile.TemporaryDirectory(
                prefix="agentops-deployment-snapshot-"
            ) as raw,
        ):
            root = Path(raw)
            expected = self._materialize_closure(root, closure.entries)
            restricted = SourceSnapshot(
                snapshot.source_id,
                snapshot.ref,
                snapshot.commit,
                root,
            )
            plan = provider.plan(restricted, target)
            self._verify_materialized(root, expected)
        self._validate_provider_plan(plan, provider, target, snapshot.commit)
        return plan

    @staticmethod
    def _expand_declared_closure(
        snapshot: SourceSnapshot, declared: tuple[Path, ...]
    ) -> tuple[Path, ...]:
        expanded: set[Path] = set(declared)
        for relative in declared:
            if not isinstance(relative, Path):
                raise ValueError("provider data closure entries must be Path values")
            expanded.update(parent for parent in relative.parents if parent != Path("."))
            candidate = snapshot.root / relative
            try:
                item = candidate.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(item.st_mode):
                continue
            for current, directories, files in os.walk(candidate, followlinks=False):
                current_path = Path(current)
                current_relative = current_path.relative_to(snapshot.root)
                expanded.add(current_relative)
                for name in directories:
                    expanded.add(current_relative / name)
                for name in files:
                    expanded.add(current_relative / name)
        return tuple(sorted(expanded, key=lambda path: (len(path.parts), path.as_posix())))

    @staticmethod
    def _materialize_closure(
        root: Path, entries: Iterable[object]
    ) -> dict[Path, tuple[str, bytes | None, int]]:
        expected: dict[Path, tuple[str, bytes | None, int]] = {}
        for entry in entries:
            relative = entry.relative_path
            destination = root / relative
            if entry.kind == "directory":
                destination.mkdir(parents=True, exist_ok=True)
                destination.chmod(entry.mode)
                expected[relative] = ("directory", None, entry.mode)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = entry.read_bytes()
            destination.write_bytes(content)
            destination.chmod(entry.mode)
            expected[relative] = ("file", content, entry.mode)
        return expected

    @staticmethod
    def _verify_materialized(
        root: Path, expected: dict[Path, tuple[str, bytes | None, int]]
    ) -> None:
        observed: set[Path] = set()
        for current, directories, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            relative_root = current_path.relative_to(root)
            for name in (*directories, *files):
                path = current_path / name
                relative = relative_root / name
                observed.add(relative)
                item = path.lstat()
                expected_item = expected.get(relative)
                if expected_item is None:
                    raise RuntimeError("provider changed the restricted source snapshot")
                kind, content, mode = expected_item
                valid_kind = (
                    stat.S_ISDIR(item.st_mode)
                    if kind == "directory"
                    else stat.S_ISREG(item.st_mode)
                )
                mode_changed = os.name != "nt" and stat.S_IMODE(item.st_mode) != mode
                if not valid_kind or mode_changed:
                    raise RuntimeError("provider changed the restricted source snapshot")
                if kind == "file" and path.read_bytes() != content:
                    raise RuntimeError("provider changed the restricted source snapshot")
        if observed != set(expected):
            raise RuntimeError("provider changed the restricted source snapshot")

    @staticmethod
    def _validate_provider_plan(
        plan: ProviderPlan,
        provider: DeploymentProvider,
        target: TargetSpec,
        commit: str,
    ) -> None:
        if type(plan) is not ProviderPlan:
            raise ValueError("provider plan must be an exact ProviderPlan")
        if plan.provider_id != provider.provider_id:
            raise ValueError("provider plan id does not match provider")
        if plan.target != target:
            raise ValueError("provider plan target does not match selected target")
        if plan.source_revision != commit:
            raise ValueError("provider plan source revision does not match snapshot")
        if not plan.files and not plan.removals:
            raise ValueError("provider plan must contain files or removals")
        if any(type(item) is not PlannedFile for item in plan.files):
            raise ValueError("provider plan files must be exact PlannedFile values")
        if any(type(path) is not type(Path()) for path in plan.removals):
            raise ValueError("provider plan removals must be exact Path values")

    def _apply_with_one_stale_retry(
        self,
        config: RegistryConfig,
        targets: tuple[TargetSpec, ...],
        snapshots: dict[tuple[str, str], SourceSnapshot],
        plan: DeploymentPlan,
        *,
        channel_transitions: tuple[TargetChannelTransition, ...] | None = None,
    ) -> tuple[DeploymentPlan, tuple[DeploymentManifest, ...]]:
        current_plan = plan
        for attempt in range(2):
            try:
                manifests = install_provider_plans(
                    current_plan.provider_plans,
                    channel_transitions=channel_transitions,
                )
                return current_plan, manifests
            except ValueError as error:
                if attempt or not self._is_stale_apply_error(error):
                    raise
                current_plan = self._build_plan(config, targets, snapshots)
                _preflight_provider_plans_read_only(
                    current_plan.provider_plans,
                    channel_transitions=channel_transitions,
                )
        raise AssertionError("bounded deployment retry exhausted")

    @staticmethod
    def _is_stale_apply_error(error: ValueError) -> bool:
        message = str(error)
        return any(
            marker in message
            for marker in (
                "managed destination changed",
                "unmanaged destination conflicts",
                "new unmanaged destination appeared",
                "deployment manifest changed before publication",
            )
        )

    @staticmethod
    def _audit_plans(
        plans: tuple[ProviderPlan, ...], *, require_matches: bool
    ) -> dict[str, DeploymentAudit]:
        target_ids = sorted({plan.target.id for plan in plans})
        audits: dict[str, DeploymentAudit] = {}
        for target_id in target_ids:
            target_plans = tuple(
                plan for plan in plans if plan.target.id == target_id
            )
            audit = audit_provider_plans(target_plans)
            audits[target_id] = audit
            if require_matches and not audit.matches:
                raise DeploymentAuditError(
                    f"deployment audit did not match target {target_id!r}"
                )
        return audits

    def _receipt(
        self,
        operation: str,
        registry_snapshot: RegistrySnapshot,
        targets: tuple[TargetSpec, ...],
        snapshots: dict[tuple[str, str], SourceSnapshot],
        *,
        manifests: tuple[DeploymentManifest, ...],
        audits: dict[str, DeploymentAudit],
    ) -> DeploymentReceipt:
        manifest_by_target = {manifest.target_id: manifest for manifest in manifests}
        channels = {
            channel.id: channel for channel in registry_snapshot.config.channels
        }
        statuses: list[TargetStatus] = []
        for target in targets:
            channel = channels[target.channel]
            resolved = snapshots[(channel.source, channel.ref)].commit
            status_manifest = manifest_by_target.get(target.id)
            if status_manifest is None and audits[target.id].matches:
                status_manifest = DeploymentManifest(
                    schema_version=1,
                    target_id=target.id,
                    framework=target.framework,
                    channel=target.channel,
                    source_revision=resolved,
                    provider_ids=(),
                    files=(),
                    directories=(),
                    transaction_id="verified-audit",
                )
            statuses.append(
                self._registry.status(
                    target.id,
                    manifest=status_manifest,
                    resolved_commit=resolved,
                    audit=audits[target.id],
                    snapshot=registry_snapshot,
                )
            )
        commits = tuple(sorted({snapshot.commit for snapshot in snapshots.values()}))
        return DeploymentReceipt(operation, commits, tuple(statuses))

    @staticmethod
    def _rollback_after_failure(
        manifests: tuple[DeploymentManifest, ...], error: BaseException
    ) -> None:
        try:
            rollback_manifests(manifests)
        except BaseException as rollback_error:
            if not isinstance(rollback_error, Exception):
                rollback_error.add_note(
                    "deployment recovery incomplete while restoring targets; "
                    f"original failure: {error}; transaction evidence retained"
                )
                raise
            DeploymentEngine._raise_incomplete_recovery(error, [rollback_error])

    @staticmethod
    def _raise_incomplete_recovery(
        error: BaseException, recovery_errors: list[BaseException]
    ) -> None:
        if not isinstance(error, Exception):
            error.add_note("deployment recovery was incomplete; transaction evidence retained")
            raise error from recovery_errors[0]
        details = "; ".join(str(item) for item in recovery_errors)
        raise DeploymentRecoveryError(
            f"deployment failed: {error}; recovery incomplete: {details}"
        ) from recovery_errors[0]


__all__ = [
    "DeploymentAuditError",
    "DeploymentEngine",
    "DeploymentEngineError",
    "DeploymentRecoveryError",
]
