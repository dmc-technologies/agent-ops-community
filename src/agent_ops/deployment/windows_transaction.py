"""Native Windows backend for the shared deployment transaction contract."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from agent_ops.deployment.models import (
    DeploymentAudit,
    DeploymentManifest,
    ManifestFile,
    ProviderPlan,
    TargetChannelTransition,
    TargetSpec,
)
from agent_ops.deployment.windows_fs import WindowsTargetLock

_METADATA = Path(".agentops/deployment")
_PATHS: dict[str, Path] = {}
_LOCKS: ContextVar[dict[Path, WindowsTargetLock] | None] = ContextVar(
    "windows_deployment_locks",
    default=None,
)


def _tx() -> Any:
    from agent_ops.deployment import transaction

    return transaction


def _fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _absolute(path: Path) -> Path:
    return _tx()._absolute_home(path)


def _manifest_path(target: Any) -> Path:
    return _tx()._manifest_path(target)


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _manifest_bytes(manifest: DeploymentManifest) -> bytes:
    return _tx()._manifest_bytes(manifest)


def _manifest_data(manifest: DeploymentManifest) -> dict[str, Any]:
    return _tx()._manifest_to_dict(manifest)


def _ensure_directory(lock: WindowsTargetLock, relative: Path) -> bool:
    if lock.exists(relative):
        lock.identity(relative, directory=True)
        return False
    marker = relative / ".agentops-create-marker"
    with lock.pin_parent(marker, create=True):
        pass
    lock.identity(relative, directory=True)
    return True


def _file_matches(lock: WindowsTargetLock, path: Path, content: bytes) -> bool:
    try:
        return lock.read_file(path) == content
    except (FileNotFoundError, OSError, ValueError):
        return False


def _decode_prior(
    content: bytes,
    *,
    target: Any,
    expected_channel: str,
) -> dict[str, Any]:
    return _tx()._validated_prior_manifest_data(
        content,
        target=target,
        expected_channel=expected_channel,
    )


def _verify_prior(lock: WindowsTargetLock, prior: dict[str, Any]) -> None:
    for item in prior["files"]:
        path = Path(item["path"])
        try:
            content = lock.read_file(path)
        except (FileNotFoundError, OSError, ValueError) as error:
            raise ValueError(f"prior managed file is missing or unsafe: {path}") from error
        if _fingerprint(content) != item["fingerprint"]:
            raise ValueError(f"prior managed file changed: {path}")
    for item in prior["directories"]:
        path = Path(item["path"])
        try:
            lock.identity(path, directory=True)
        except (FileNotFoundError, OSError, ValueError) as error:
            raise ValueError(f"prior managed directory is missing or unsafe: {path}") from error


def _record_manifest(record: dict[str, Any]) -> DeploymentManifest:
    data = record["manifest"]
    from agent_ops.registries.models import Framework

    return DeploymentManifest(
        schema_version=data["schema_version"],
        target_id=data["target_id"],
        framework=Framework(data["framework"]),
        channel=data["channel"],
        source_revision=data["source_revision"],
        provider_ids=tuple(data["provider_ids"]),
        files=tuple(
            ManifestFile(Path(item["path"]), item["fingerprint"], item["mode"])
            for item in data["files"]
        ),
        directories=tuple(
            _tx().ManifestDirectory(Path(item["path"]), item["mode"])
            for item in data["directories"]
        ),
        transaction_id=data["transaction_id"],
        review_state=data.get("review_state"),
    )


def _validate_record(record: object, *, transaction_id: str | None = None) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != {
        "schema_version",
        "state",
        "home",
        "manifest",
        "manifest_path",
        "manifest_content",
        "prior_manifest_content",
        "created_directories",
        "operations",
    }:
        raise ValueError("invalid Windows deployment transaction record")
    if record["schema_version"] != 1 or record["state"] not in {
        "prepared",
        "applying",
        "committed",
        "rolling-back",
        "rolled-back",
    }:
        raise ValueError("invalid Windows deployment transaction state")
    manifest = _record_manifest(record)
    if transaction_id is not None and manifest.transaction_id != transaction_id:
        raise ValueError("Windows deployment transaction identity mismatch")
    if (
        record["manifest_path"]
        != _manifest_path(type("Target", (), {"id": manifest.target_id})()).as_posix()
    ):
        raise ValueError("invalid Windows deployment manifest path")
    try:
        expected = base64.b64decode(record["manifest_content"], validate=True)
        prior = (
            None
            if record["prior_manifest_content"] is None
            else base64.b64decode(record["prior_manifest_content"], validate=True)
        )
    except (TypeError, ValueError) as error:
        raise ValueError("invalid Windows deployment manifest evidence") from error
    if expected != _manifest_bytes(manifest):
        raise ValueError("Windows deployment manifest evidence changed")
    if not isinstance(record["operations"], list) or not isinstance(
        record["created_directories"], list
    ):
        raise ValueError("invalid Windows deployment transaction operations")
    for index, operation in enumerate(record["operations"]):
        if (
            not isinstance(operation, dict)
            or operation.get("index") != index
            or operation.get("kind") not in {"write", "remove", "adopt"}
            or operation.get("phase") not in {"ready", "applying", "applied"}
        ):
            raise ValueError("invalid Windows deployment transaction operation")
        for key in ("destination", "staged", "backup"):
            value = operation.get(key)
            if value is not None:
                _tx()._canonical_relative_text(value, label="Windows transaction")
    record["_expected_manifest"] = expected
    record["_prior_manifest"] = prior
    return record


def _write_record(lock: WindowsTargetLock, path: Path, record: dict[str, Any]) -> None:
    authored = {key: value for key, value in record.items() if not key.startswith("_")}
    lock.write_atomic(path, _json_bytes(authored))


@contextmanager
def locked_provider_plan_targets(plans: tuple[ProviderPlan, ...]) -> Iterator[None]:
    groups = _tx()._validate_and_group(plans)
    homes = tuple(sorted({_absolute(group.target.home) for group in groups}, key=str))
    active = _LOCKS.get()
    if active is not None:
        if any(home not in active for home in homes):
            raise RuntimeError("active Windows deployment lock set is incomplete")
        yield
        return
    with ExitStack() as stack:
        locks = {home: stack.enter_context(WindowsTargetLock(home)) for home in homes}
        token = _LOCKS.set(locks)
        try:
            yield
        finally:
            _LOCKS.reset(token)


def verify_locked_provider_plan_targets(plans: tuple[ProviderPlan, ...]) -> None:
    active = _LOCKS.get()
    if active is None:
        raise RuntimeError("Windows deployment target lock set is not active")
    for group in _tx()._validate_and_group(plans):
        active[_absolute(group.target.home)].verify()


def _target_lock(home: Path) -> WindowsTargetLock:
    active = _LOCKS.get()
    if active is None:
        raise RuntimeError("Windows deployment target lock set is not active")
    return active[_absolute(home)]


def install_provider_plans(
    plans: tuple[ProviderPlan, ...],
    *,
    channel_transitions: tuple[TargetChannelTransition, ...] | None = None,
) -> tuple[DeploymentManifest, ...]:
    groups = _tx()._validate_and_group(plans)
    transitions = _tx()._channel_transitions(groups, channel_transitions)
    manifests: list[DeploymentManifest] = []
    with locked_provider_plan_targets(plans):
        try:
            for group in groups:
                manifests.append(_install_group(group, transitions[group.target.id]))
        except BaseException as install_error:
            try:
                rollback_manifests(tuple(manifests))
            except BaseException as rollback_error:
                raise _tx().IncompleteRollbackError(
                    f"installation failed: {install_error}; Windows grouped rollback "
                    "incomplete; transaction evidence retained"
                ) from rollback_error
            raise
    return tuple(manifests)


def _install_group(group: Any, transition: TargetChannelTransition) -> DeploymentManifest:
    lock = _target_lock(group.target.home)
    lock.verify()
    if group.legacy_link_transition is not None and lock.exists(
        group.legacy_link_transition.destination
    ):
        raise ValueError("Windows deployment does not adopt symbolic-link policy state")
    if group.prime_gstack_legacy_adoption is not None:
        raise ValueError("Prime gstack legacy adoption is not a Windows deployment path")
    transaction_id = uuid.uuid4().hex
    files = group.files
    manifest = DeploymentManifest(
        schema_version=1,
        target_id=group.target.id,
        framework=group.target.framework,
        channel=group.target.channel,
        source_revision=group.source_revision,
        provider_ids=group.provider_ids,
        files=tuple(ManifestFile(item.path, item.fingerprint, item.mode) for item in files),
        directories=_tx()._directories(files),
        transaction_id=transaction_id,
        review_state=(
            "unreviewed-local"
            if group.target.channel == "preview"
            or group.target.channel.startswith("preview-")
            or group.target.channel.startswith("unreviewed-local")
            else None
        ),
    )
    manifest_path = _manifest_path(group.target)
    prior_content = lock.read_optional(manifest_path)
    prior = None
    if prior_content is not None:
        prior = _decode_prior(
            prior_content,
            target=group.target,
            expected_channel=transition.expected_prior_channel,
        )
        _verify_prior(lock, prior)
    managed = (
        {Path(item["path"]): (item["fingerprint"], item["mode"]) for item in prior["files"]}
        if prior is not None
        else {}
    )
    planned = {item.path: item for item in files}
    removals = tuple(sorted(set(group.removals), key=lambda item: item.as_posix()))
    operations: list[dict[str, Any]] = []
    for item in files:
        current = lock.read_optional(item.path)
        prior_item = managed.get(item.path)
        if current is not None and prior_item is None:
            if current == item.content:
                operations.append(
                    {
                        "index": len(operations),
                        "kind": "adopt",
                        "phase": "ready",
                        "destination": item.path.as_posix(),
                        "staged": None,
                        "backup": None,
                        "expected": item.fingerprint,
                        "prior": item.fingerprint,
                    }
                )
                continue
            raise ValueError(f"unmanaged destination conflicts with plan: {item.path}")
        if current is not None and prior_item is not None:
            if _fingerprint(current) != prior_item[0]:
                raise ValueError(f"managed destination changed: {item.path}")
            if current == item.content:
                operations.append(
                    {
                        "index": len(operations),
                        "kind": "adopt",
                        "phase": "ready",
                        "destination": item.path.as_posix(),
                        "staged": None,
                        "backup": None,
                        "expected": item.fingerprint,
                        "prior": prior_item[0],
                    }
                )
                continue
        index = len(operations)
        operations.append(
            {
                "index": index,
                "kind": "write",
                "phase": "ready",
                "destination": item.path.as_posix(),
                "staged": (
                    _METADATA / "transactions" / transaction_id / "rendered" / f"{index:04d}"
                ).as_posix(),
                "backup": (
                    (
                        _METADATA / "transactions" / transaction_id / "backups" / f"{index:04d}"
                    ).as_posix()
                    if current is not None
                    else None
                ),
                "expected": item.fingerprint,
                "prior": _fingerprint(current) if current is not None else None,
            }
        )
    for path in removals:
        if path in planned:
            raise ValueError(f"planned file is also removed: {path}")
        prior_item = managed.get(path)
        current = lock.read_optional(path)
        if prior_item is None:
            if current is not None:
                raise ValueError(f"refusing to remove unmanaged destination: {path}")
            continue
        if current is not None and _fingerprint(current) != prior_item[0]:
            raise ValueError(f"managed destination changed: {path}")
        index = len(operations)
        operations.append(
            {
                "index": index,
                "kind": "remove",
                "phase": "ready",
                "destination": path.as_posix(),
                "staged": None,
                "backup": (
                    (
                        _METADATA / "transactions" / transaction_id / "backups" / f"{index:04d}"
                    ).as_posix()
                    if current is not None
                    else None
                ),
                "expected": None,
                "prior": prior_item[0] if current is not None else None,
            }
        )
    transaction = _METADATA / "transactions" / transaction_id
    record_path = transaction / "record.json"
    created_directories = []
    for directory in (*manifest.directories,):
        if _ensure_directory(lock, directory.path):
            created_directories.append(directory.path.as_posix())
    for relative in (
        _METADATA,
        _METADATA / "transactions",
        transaction,
        transaction / "rendered",
        transaction / "backups",
    ):
        _ensure_directory(lock, relative)
    record = {
        "schema_version": 1,
        "state": "prepared",
        "home": str(_absolute(group.target.home)),
        "manifest": _manifest_data(manifest),
        "manifest_path": manifest_path.as_posix(),
        "manifest_content": base64.b64encode(_manifest_bytes(manifest)).decode(),
        "prior_manifest_content": (
            None if prior_content is None else base64.b64encode(prior_content).decode()
        ),
        "created_directories": created_directories,
        "operations": operations,
    }
    for operation in operations:
        if operation["kind"] == "write":
            item = planned[Path(operation["destination"])]
            lock.write_new(Path(operation["staged"]), item.content)
    _write_record(lock, record_path, record)
    try:
        record["state"] = "applying"
        _write_record(lock, record_path, record)
        for operation in operations:
            if operation["kind"] == "adopt":
                operation["phase"] = "applied"
                _write_record(lock, record_path, record)
                continue
            operation["phase"] = "applying"
            _write_record(lock, record_path, record)
            destination = Path(operation["destination"])
            backup = operation["backup"]
            if backup is not None and lock.exists(destination):
                lock.replace(destination, Path(backup), replace=False)
                if _fingerprint(lock.read_file(Path(backup))) != operation["prior"]:
                    raise ValueError(f"managed destination changed during backup: {destination}")
            if operation["kind"] == "write":
                lock.replace(Path(operation["staged"]), destination, replace=False)
                if _fingerprint(lock.read_file(destination)) != operation["expected"]:
                    raise OSError(f"installed destination changed: {destination}")
            operation["phase"] = "applied"
            _write_record(lock, record_path, record)
        manifest_temp = transaction / "manifest.tmp"
        lock.write_new(manifest_temp, _manifest_bytes(manifest))
        lock.replace(manifest_temp, manifest_path, replace=prior_content is not None)
        if lock.read_file(manifest_path) != _manifest_bytes(manifest):
            raise OSError("Windows ownership manifest changed during publication")
        record["state"] = "committed"
        _write_record(lock, record_path, record)
        lock.verify()
    except BaseException:
        _rollback_record(lock, record_path, record, retain=True)
        raise
    _PATHS[transaction_id] = _absolute(group.target.home) / record_path
    return manifest


def _rollback_record(
    lock: WindowsTargetLock,
    record_path: Path,
    record: dict[str, Any],
    *,
    retain: bool,
) -> None:
    record["state"] = "rolling-back"
    _write_record(lock, record_path, record)
    expected_manifest = base64.b64decode(record["manifest_content"], validate=True)
    manifest_path = Path(record["manifest_path"])
    current_manifest = lock.read_optional(manifest_path)
    if current_manifest == expected_manifest:
        prior = record["prior_manifest_content"]
        if prior is None:
            lock.unlink(manifest_path)
        else:
            lock.write_atomic(manifest_path, base64.b64decode(prior, validate=True))
    elif current_manifest not in {
        None,
        (
            None
            if record["prior_manifest_content"] is None
            else base64.b64decode(record["prior_manifest_content"], validate=True)
        ),
    }:
        raise _tx().IncompleteRollbackError(
            "Windows ownership manifest changed; transaction evidence retained"
        )
    for operation in reversed(record["operations"]):
        if operation["kind"] == "adopt":
            continue
        destination = Path(operation["destination"])
        backup = None if operation["backup"] is None else Path(operation["backup"])
        if operation["kind"] == "write" and lock.exists(destination):
            if _fingerprint(lock.read_file(destination)) != operation["expected"]:
                raise _tx().IncompleteRollbackError(
                    f"Windows rollback destination changed: {destination}"
                )
            lock.unlink(destination)
        if backup is not None and lock.exists(backup):
            if lock.exists(destination):
                raise _tx().IncompleteRollbackError(
                    f"Windows rollback destination is occupied: {destination}"
                )
            if _fingerprint(lock.read_file(backup)) != operation["prior"]:
                raise _tx().IncompleteRollbackError(f"Windows rollback backup changed: {backup}")
            lock.replace(backup, destination, replace=False)
    for authored in sorted(
        record["created_directories"],
        key=lambda item: len(Path(item).parts),
        reverse=True,
    ):
        lock.remove_empty_dir(Path(authored))
    record["state"] = "rolled-back"
    _write_record(lock, record_path, record)
    if not retain:
        lock.unlink(record_path)
    lock.verify()


def rollback_manifests(manifests: tuple[DeploymentManifest, ...]) -> None:
    for manifest in reversed(manifests):
        path = _PATHS.get(manifest.transaction_id)
        if path is None:
            raise ValueError(f"transaction recovery path is unavailable: {manifest.transaction_id}")
        home = path.parents[4]
        relative = path.relative_to(home)
        with locked_provider_plan_targets(
            (
                ProviderPlan(
                    "windows-rollback",
                    manifest.source_revision,
                    TargetSpec(
                        manifest.target_id,
                        manifest.framework,
                        home,
                        manifest.channel,
                    ),
                    (),
                ),
            )
        ):
            lock = _target_lock(home)
            record = _validate_record(
                json.loads(lock.read_file(relative).decode("utf-8")),
                transaction_id=manifest.transaction_id,
            )
            if _record_manifest(record) != manifest:
                raise ValueError("Windows transaction manifest does not match rollback request")
            _rollback_record(lock, relative, record, retain=True)


def recover_transaction(path: Path) -> DeploymentManifest:
    path = _absolute(Path(path))
    if path.name != "record.json" or path.parents[1].name != "transactions":
        raise ValueError("invalid Windows transaction recovery path")
    home = path.parents[4]
    relative = path.relative_to(home)
    with WindowsTargetLock(home) as lock:
        record = _validate_record(json.loads(lock.read_file(relative).decode("utf-8")))
        manifest = _record_manifest(record)
        if record["state"] == "rolled-back":
            return manifest
        expected = record["_expected_manifest"]
        current = lock.read_optional(Path(record["manifest_path"]))
        if current == expected:
            for item in manifest.files:
                content = lock.read_optional(item.path)
                if content is None or _fingerprint(content) != item.fingerprint:
                    raise _tx().PublicationIndeterminateError(
                        f"Windows transaction output changed: {item.path}"
                    )
            record["state"] = "committed"
            _write_record(lock, relative, record)
            _PATHS[manifest.transaction_id] = path
            return manifest
        _rollback_record(lock, relative, record, retain=True)
        _PATHS[manifest.transaction_id] = path
        return manifest


def audit_provider_plans(plans: tuple[ProviderPlan, ...]) -> DeploymentAudit:
    groups = _tx()._validate_and_group(plans)
    if not groups:
        return DeploymentAudit(target_id="none", matches=True)
    if len(groups) != 1:
        raise ValueError("audit requires plans for exactly one target")
    group = groups[0]
    active = _LOCKS.get()
    if active is None:
        try:
            with WindowsTargetLock(group.target.home, shared=True, create=False) as lock:
                return _audit_group(lock, group)
        except FileNotFoundError:
            return DeploymentAudit(
                target_id=group.target.id,
                matches=False,
                missing=tuple(item.path.as_posix() for item in group.files),
            )
    return _audit_group(active[_absolute(group.target.home)], group)


def _audit_group(lock: WindowsTargetLock, group: Any) -> DeploymentAudit:
    missing: list[str] = []
    changed: list[str] = []
    unexpected: set[str] = set()
    validation_errors: list[str] = []
    manifest_content = lock.read_optional(_manifest_path(group.target))
    if manifest_content is None:
        validation_errors.append("deployment ownership manifest is missing")
    else:
        try:
            manifest = _tx()._validated_manifest_data(manifest_content, target=group.target)
            expected_files = {
                item.path.as_posix(): (item.fingerprint, item.mode) for item in group.files
            }
            recorded_files = {
                item["path"]: (item["fingerprint"], item["mode"]) for item in manifest["files"]
            }
            if recorded_files != expected_files:
                validation_errors.append("deployment manifest files do not match plan")
            if manifest["source_revision"] != group.source_revision:
                validation_errors.append("deployment manifest source revision does not match plan")
            if manifest["provider_ids"] != list(group.provider_ids):
                validation_errors.append("deployment manifest providers do not match plan")
        except ValueError as error:
            validation_errors.append(str(error))
    names: dict[str, list[str]] = {}
    planned_paths = {item.path for item in group.files}
    for item in group.files:
        content = lock.read_optional(item.path)
        if content is None:
            missing.append(item.path.as_posix())
            continue
        if content != item.content:
            changed.append(item.path.as_posix())
        if item.path.name == "SKILL.md":
            name = _tx()._frontmatter_name(content)
            if name is not None:
                names.setdefault(name, []).append(item.path.as_posix())
    for root in group.audit_roots:
        if not lock.exists(root):
            continue
        try:
            installed = lock.scan(root)
        except (OSError, ValueError) as error:
            validation_errors.append(str(error))
            continue
        for relative, kind in installed.items():
            if kind == "directory":
                continue
            candidate = root / relative
            if candidate not in planned_paths:
                unexpected.add(candidate.as_posix())
    duplicates = tuple(
        f"{name}: {', '.join(sorted(paths))}"
        for name, paths in sorted(names.items())
        if len(paths) > 1
    )
    try:
        lock.verify()
    except (OSError, ValueError) as error:
        validation_errors.append(str(error))
    return DeploymentAudit(
        target_id=group.target.id,
        matches=not any((missing, changed, unexpected, duplicates, validation_errors)),
        missing=tuple(sorted(missing)),
        changed=tuple(sorted(changed)),
        unexpected=tuple(sorted(unexpected)),
        duplicates=duplicates,
        validation_errors=tuple(validation_errors),
    )


class _Evidence:
    def __init__(self, plans: tuple[ProviderPlan, ...], require_matches: bool) -> None:
        self.plans = plans
        self.require_matches = require_matches

    def verify(self) -> None:
        verify_locked_provider_plan_targets(self.plans)
        groups = _tx()._validate_and_group(self.plans)
        for group in groups:
            audit = _audit_group(_target_lock(group.target.home), group)
            if self.require_matches and not audit.matches:
                raise ValueError(f"retained Windows deployment evidence changed: {audit}")


@contextmanager
def retain_provider_plan_evidence(
    plans: tuple[ProviderPlan, ...],
    *,
    require_matches: bool = True,
    expected_audits: dict[str, DeploymentAudit] | None = None,
) -> Iterator[_Evidence]:
    del expected_audits
    evidence = _Evidence(plans, require_matches)
    evidence.verify()
    yield evidence
    evidence.verify()
