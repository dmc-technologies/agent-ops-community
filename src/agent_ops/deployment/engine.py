"""Deterministic orchestration for grouped managed deployments."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import replace
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
    SourceSnapshot,
    TargetSpec,
    TargetState,
    TargetStatus,
)
from agent_ops.deployment.providers import (
    load_deployment_providers,
    normalize_deployment_providers,
)
from agent_ops.deployment.registry import (
    DeploymentRegistry,
    RegistryConfig,
    RegistrySnapshot,
)
from agent_ops.deployment.source_store import (
    SourceStore,
    _open_provider_data_closure,
    _validate_provider_data_closure,
)
from agent_ops.deployment.transaction import (
    _locked_provider_plan_targets,
    _preflight_provider_plans_read_only,
    audit_provider_plans,
    install_provider_plans,
    rollback_manifests,
)


class DeploymentEngineError(RuntimeError):
    """Base error for orchestration failures."""


class DeploymentAuditError(DeploymentEngineError):
    """Installed output did not match every accepted provider plan."""


class DeploymentRecoveryError(DeploymentEngineError):
    """A failed operation could not restore every affected authority."""


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

    def status(
        self, target_ids: tuple[str, ...] | None = None
    ) -> tuple[TargetStatus, ...]:
        snapshot = self._registry.load_snapshot()
        targets = self._select_targets(snapshot.config, target_ids, allow_all=True)
        latest: dict[str, TargetStatus] = {}
        for record in self._registry.receipt_records():
            if record.registry_fingerprint != snapshot.fingerprint:
                continue
            for target_status in record.receipt.targets:
                latest[target_status.target_id] = target_status
        statuses: list[TargetStatus] = []
        for target in targets:
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
            audit = None
            failure = None
            resolved = recorded.commit
            if recorded.state is TargetState.FAILED:
                failure = "latest deployment receipt records failure"
            elif recorded.state is TargetState.MODIFIED:
                audit = DeploymentAudit(target.id, matches=False, changed=("recorded",))
            elif recorded.state is TargetState.MISSING_REF:
                resolved = None
            statuses.append(
                self._registry.status(
                    target.id,
                    manifest=None,
                    resolved_commit=resolved,
                    audit=audit,
                    failure=failure,
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
                receipt = self._receipt(
                    "refresh",
                    registry_snapshot,
                    targets,
                    snapshots,
                    manifests=manifests,
                    audits=audits,
                )
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
            receipt = self._receipt(
                "audit",
                registry_snapshot,
                targets,
                snapshots,
                manifests=(),
                audits=audits,
            )
            self._registry.append_receipt(receipt, snapshot=registry_snapshot)
        return receipt

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
        selected_ids = {target.id for target in selected}
        candidate = RegistryConfig(
            original_snapshot.config.schema_version,
            original_snapshot.config.sources,
            original_snapshot.config.channels,
            tuple(
                replace(target, channel=channel)
                if target.id in selected_ids
                else target
                for target in original_snapshot.config.targets
            ),
        )
        candidate_targets = tuple(
            target for target in candidate.targets if target.id in selected_ids
        )
        snapshots = self._fetch_snapshots(candidate, candidate_targets)
        plan = self._build_plan(candidate, candidate_targets, snapshots)
        _preflight_provider_plans_read_only(plan.provider_plans)
        with _locked_provider_plan_targets(plan.provider_plans):
            plan = self._build_plan(candidate, candidate_targets, snapshots)
            _preflight_provider_plans_read_only(plan.provider_plans)
            plan, manifests = self._apply_with_one_stale_retry(
                candidate,
                candidate_targets,
                snapshots,
                plan,
            )
            candidate_snapshot: RegistrySnapshot | None = None
            try:
                audits = self._audit_plans(plan.provider_plans, require_matches=True)
                candidate_snapshot = self._registry.save(
                    candidate,
                    expected_snapshot=original_snapshot,
                )
                receipt = self._receipt(
                    "switch",
                    candidate_snapshot,
                    candidate_targets,
                    snapshots,
                    manifests=manifests,
                    audits=audits,
                )
                self._registry.append_receipt(receipt, snapshot=candidate_snapshot)
            except BaseException as error:
                recovery_errors: list[BaseException] = []
                if candidate_snapshot is not None:
                    try:
                        self._registry.save(
                            original_snapshot.config,
                            expected_snapshot=candidate_snapshot,
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
                        prior_recovery = "; ".join(
                            str(item) for item in recovery_errors
                        )
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
    ) -> tuple[TargetSpec, ...]:
        if target_ids is None:
            if not allow_all:
                raise ValueError("target ids are required")
            return config.targets
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
        return tuple(targets[target_id] for target_id in sorted(target_ids))

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
        return DeploymentPlan(ordered_snapshots, ordered_plans)

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
                if not valid_kind or stat.S_IMODE(item.st_mode) != mode:
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
        if any(type(path) is not Path for path in plan.removals):
            raise ValueError("provider plan removals must be exact Path values")

    def _apply_with_one_stale_retry(
        self,
        config: RegistryConfig,
        targets: tuple[TargetSpec, ...],
        snapshots: dict[tuple[str, str], SourceSnapshot],
        plan: DeploymentPlan,
    ) -> tuple[DeploymentPlan, tuple[DeploymentManifest, ...]]:
        current_plan = plan
        for attempt in range(2):
            try:
                manifests = install_provider_plans(current_plan.provider_plans)
                return current_plan, manifests
            except ValueError as error:
                if attempt or not self._is_stale_apply_error(error):
                    raise
                current_plan = self._build_plan(config, targets, snapshots)
                _preflight_provider_plans_read_only(current_plan.provider_plans)
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
