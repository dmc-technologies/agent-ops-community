"""Strict, atomic machine registry and append-only deployment receipts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from yaml.events import AliasEvent

from agent_ops.deployment.models import (
    DeploymentAudit,
    DeploymentManifest,
    DeploymentReceipt,
    SourceSpec,
    TargetSpec,
    TargetState,
    TargetStatus,
)
from agent_ops.registries.models import Framework

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows imports, then fails closed before mutation
    fcntl = None  # type: ignore[assignment]


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\\\[]")
_RECEIPT_FILE = re.compile(r"(?P<sequence>[0-9]{20})\.json\Z")
_RECEIPT_TEMP = re.compile(r"\.(?P<receipt>[0-9]{20}\.json)\.[A-Za-z0-9]+\.tmp\Z")
_MAX_REGISTRY_BYTES = 1024 * 1024
_MAX_RECEIPT_BYTES = 1024 * 1024
_ROOT_KEYS = frozenset({"schema_version", "sources", "channels", "targets"})
_SOURCE_KEYS = frozenset({"url", "stable_ref"})
_CHANNEL_KEYS = frozenset({"source", "ref"})
_TARGET_KEYS = frozenset({"framework", "home", "channel"})
_RECEIPT_KEYS = frozenset({"operation", "commits", "targets"})
_STATUS_KEYS = frozenset({"target_id", "state", "channel", "commit"})
_RECEIPT_WRAPPER_KEYS = frozenset({"schema_version", "registry_fingerprint", "receipt"})
_RECEIPT_COUNTER = "receipt-count"
_MAX_RECEIPTS = 1_000_000


class _StrictLoader(yaml.SafeLoader):
    """Safe YAML loader that additionally rejects aliases and duplicate keys."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise yaml.constructor.ConstructorError(
                None, None, "YAML aliases are not allowed", self.peek_event().start_mark
            )
        return super().compose_node(parent, index)

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as error:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "mapping keys must be scalar",
                    key_node.start_mark,
                ) from error
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate mapping key: {key!r}",
                    key_node.start_mark,
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


@dataclass(frozen=True)
class ChannelSpec:
    id: str
    source: str
    ref: str

    def __post_init__(self) -> None:
        _validate_id(self.id, "channel id")
        _validate_id(self.source, "channel source")
        _validate_ref(self.ref)


@dataclass(frozen=True)
class RegistryConfig:
    schema_version: int
    sources: tuple[SourceSpec, ...]
    channels: tuple[ChannelSpec, ...]
    targets: tuple[TargetSpec, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))
        object.__setattr__(self, "channels", tuple(self.channels))
        object.__setattr__(self, "targets", tuple(self.targets))
        _validate_config_fields(self, require_nonempty=False)
        object.__setattr__(self, "sources", tuple(sorted(self.sources, key=lambda item: item.id)))
        object.__setattr__(self, "channels", tuple(sorted(self.channels, key=lambda item: item.id)))
        object.__setattr__(self, "targets", tuple(sorted(self.targets, key=lambda item: item.id)))


@dataclass(frozen=True)
class RegistrySnapshot:
    config: RegistryConfig
    fingerprint: str
    registry_identity: tuple[int, int]

    def __post_init__(self) -> None:
        if type(self.config) is not RegistryConfig:
            raise ValueError("snapshot config must be an exact RegistryConfig")
        _validate_fingerprint(self.fingerprint)
        if (
            type(self.registry_identity) is not tuple
            or len(self.registry_identity) != 2
            or any(type(value) is not int or value < 0 for value in self.registry_identity)
        ):
            raise ValueError("snapshot registry identity must be a device/inode pair")


@dataclass(frozen=True)
class ReceiptRecord:
    sequence: int
    registry_fingerprint: str
    receipt: DeploymentReceipt

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or not 0 <= self.sequence < _MAX_RECEIPTS:
            raise ValueError("receipt record sequence exceeds supported bound")
        _validate_fingerprint(self.registry_fingerprint)
        _receipt_to_data(self.receipt)


class DeploymentRegistry:
    """A host-local deployment registry with private append-only receipts."""

    def __init__(self, path: Path):
        self.path = Path(path)
        _validate_absolute_normal_path(self.path, "registry path")
        if self.path.name in {"", ".", ".."}:
            raise ValueError("registry path must name a file")

    @property
    def state_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.state")

    @property
    def receipts_path(self) -> Path:
        return self.state_path / "receipts"

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.lock")

    def load(self) -> RegistryConfig:
        return self.load_snapshot().config

    def load_snapshot(self) -> RegistrySnapshot:
        _require_secure_platform(require_locking=True)
        parent, identities, lock = _open_locked_parent(
            self.path.parent, self.lock_path.name, exclusive=False, create_lock=False
        )
        try:
            snapshot = _snapshot_from_parent(parent, self.path.name)
            _verify_canonical_parent(self.path.parent, parent, identities)
            return snapshot
        finally:
            _close_registry_lock(lock)
            os.close(parent)

    def save(self, config: RegistryConfig) -> None:
        _require_secure_platform(require_locking=True)
        _validate_config(config, require_nonempty=True)
        content = _dump_registry(config)
        _parse_registry(content)
        parent, identities, lock = _open_locked_parent(
            self.path.parent, self.lock_path.name, exclusive=True, create_lock=True
        )
        temporary = f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        backup: str | None = None
        descriptor: int | None = None
        published = False
        original: tuple[int, int] | None = None
        try:
            original = _optional_regular_identity(parent, self.path.name, "registry file")
            if original is not None:
                old_snapshot = _snapshot_from_parent(parent, self.path.name)
                if old_snapshot.registry_identity != original:
                    raise RuntimeError("registry file changed before receipt recovery")
                _verify_canonical_parent(self.path.parent, parent, identities)
                _recover_history_before_save(
                    parent,
                    self.state_path.name,
                    old_snapshot.fingerprint,
                    before_mutation=lambda: _verify_registry_context(
                        parent,
                        self.path.name,
                        old_snapshot,
                        self.path.parent,
                        identities,
                    ),
                )
                _verify_canonical_parent(self.path.parent, parent, identities)
            elif _entry_exists(parent, self.state_path.name):
                raise RuntimeError("receipt state exists without an initialized registry")
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            temporary_stat = os.fstat(descriptor)
            if not stat.S_ISREG(temporary_stat.st_mode):
                raise RuntimeError("registry temporary file must be regular")
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            if _optional_regular_identity(parent, self.path.name, "registry file") != original:
                raise RuntimeError("registry file changed during save")
            _verify_canonical_parent(self.path.parent, parent, identities)
            if original is not None:
                backup = f".{self.path.name}.{uuid.uuid4().hex}.backup"
                os.link(
                    self.path.name,
                    backup,
                    src_dir_fd=parent,
                    dst_dir_fd=parent,
                    follow_symlinks=False,
                )
                os.fsync(parent)
            _replace_at(parent, temporary, self.path.name)
            published = True
            final_stat = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(final_stat.st_mode) or stat.S_IMODE(final_stat.st_mode) != 0o600:
                raise RuntimeError("published registry file is not a private regular file")
            os.fsync(parent)
            _verify_canonical_parent(self.path.parent, parent, identities)
            if backup is not None:
                os.unlink(backup, dir_fd=parent)
                backup = None
                os.fsync(parent)
            _verify_canonical_parent(self.path.parent, parent, identities)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            if published:
                if backup is not None:
                    with suppress(BaseException):
                        _replace_at(parent, backup, self.path.name)
                        os.fsync(parent)
                    backup = None
                elif original is None:
                    with suppress(BaseException):
                        _unlink_if_regular(parent, self.path.name)
                        os.fsync(parent)
            else:
                _unlink_if_regular(parent, temporary)
                if backup is not None:
                    _unlink_if_regular(parent, backup)
            raise
        finally:
            _close_registry_lock(lock)
            os.close(parent)

    def append_receipt(
        self,
        receipt: DeploymentReceipt,
        *,
        snapshot: RegistrySnapshot,
    ) -> Path:
        _require_secure_platform(require_locking=True)
        receipt_data = _receipt_to_data(receipt)
        if type(snapshot) is not RegistrySnapshot:
            raise TypeError("snapshot must be an exact RegistrySnapshot")
        parent, identities, lock = _open_locked_parent(
            self.path.parent, self.lock_path.name, exclusive=True, create_lock=False
        )
        state: int | None = None
        receipts: int | None = None
        temporary: str | None = None
        destination: str | None = None
        sequence: int | None = None
        published = False
        try:
            _require_current_snapshot(parent, self.path.name, snapshot)
            state = _open_private_directory(parent, self.state_path.name, create=True)
            receipts = _open_private_directory(state, "receipts", create=True)
            _verify_canonical_parent(self.path.parent, parent, identities)
            sequence = _load_or_initialize_counter(state, receipts)
            if sequence >= _MAX_RECEIPTS:
                raise ValueError("receipt counter is at the supported bound")
            destination = f"{sequence:020d}.json"
            content = _dump_receipt_wrapper(receipt_data, snapshot.fingerprint)
            _verify_canonical_parent(self.path.parent, parent, identities)
            names = _receipt_names_for_append(
                receipts,
                destination=destination,
                sequence=sequence,
                registry_fingerprint=snapshot.fingerprint,
                receipt=receipt,
                before_publication=lambda: _require_current_snapshot(
                    parent, self.path.name, snapshot
                ),
            )
            if names == sequence + 1:
                orphan_fingerprint, orphan = _read_receipt_wrapper(receipts, destination)
                if orphan_fingerprint != snapshot.fingerprint or orphan != receipt:
                    raise RuntimeError("orphan receipt does not match append retry")
                _require_current_snapshot(parent, self.path.name, snapshot)
                _verify_canonical_parent(self.path.parent, parent, identities)
                _write_counter(state, sequence + 1)
                try:
                    _require_current_snapshot(parent, self.path.name, snapshot)
                    _verify_canonical_parent(self.path.parent, parent, identities)
                except BaseException:
                    with suppress(BaseException):
                        _write_counter(state, sequence)
                    raise
                return self.receipts_path / destination
            if names != sequence:
                raise RuntimeError("receipt history is not contiguous with its counter")
            temporary = f".{destination}.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=receipts,
            )
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise RuntimeError("receipt temporary file must be regular")
                os.fchmod(descriptor, 0o600)
                _write_all(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _require_current_snapshot(parent, self.path.name, snapshot)
            _verify_canonical_parent(self.path.parent, parent, identities)
            os.link(
                temporary,
                destination,
                src_dir_fd=receipts,
                dst_dir_fd=receipts,
                follow_symlinks=False,
            )
            published = True
            os.unlink(temporary, dir_fd=receipts)
            temporary = None
            os.fsync(receipts)
            _require_current_snapshot(parent, self.path.name, snapshot)
            _verify_canonical_parent(self.path.parent, parent, identities)
            _write_counter(state, sequence + 1)
            _require_current_snapshot(parent, self.path.name, snapshot)
            _verify_canonical_parent(self.path.parent, parent, identities)
            return self.receipts_path / destination
        except BaseException:
            if temporary is not None and receipts is not None:
                _unlink_if_regular(receipts, temporary)
            if published and receipts is not None and state is not None and sequence is not None:
                with suppress(BaseException):
                    _write_counter(state, sequence)
                if destination is not None:
                    with suppress(BaseException):
                        _unlink_if_regular(receipts, destination)
                        os.fsync(receipts)
            raise
        finally:
            if receipts is not None:
                os.close(receipts)
            if state is not None:
                os.close(state)
            _close_registry_lock(lock)
            os.close(parent)

    def receipts(self) -> tuple[DeploymentReceipt, ...]:
        return tuple(record.receipt for record in self.receipt_records())

    def receipt_records(self) -> tuple[ReceiptRecord, ...]:
        _require_secure_platform(require_locking=True)
        parent, identities, lock = _open_locked_parent(
            self.path.parent, self.lock_path.name, exclusive=False, create_lock=False
        )
        state: int | None = None
        receipts: int | None = None
        try:
            snapshot = _snapshot_from_parent(parent, self.path.name)
            state = _open_private_directory(
                parent, self.state_path.name, create=False, missing_ok=True
            )
            if state is None:
                _require_current_snapshot(parent, self.path.name, snapshot)
                _verify_canonical_parent(self.path.parent, parent, identities)
                return ()
            receipts = _open_private_directory(state, "receipts", create=False, missing_ok=True)
            if receipts is None:
                raise RuntimeError("receipt history directory is missing")
            try:
                counter = _read_counter(state)
            except FileNotFoundError:
                raise RuntimeError("receipt counter is missing") from None
            names = _receipt_names(receipts)
            result = _read_contiguous_records(receipts, names, counter)
            _require_current_snapshot(parent, self.path.name, snapshot)
            _verify_canonical_parent(self.path.parent, parent, identities)
            return result
        finally:
            if receipts is not None:
                os.close(receipts)
            if state is not None:
                os.close(state)
            _close_registry_lock(lock)
            os.close(parent)

    def status(
        self,
        target_id: str,
        *,
        manifest: DeploymentManifest | None,
        resolved_commit: str | None,
        audit: DeploymentAudit | None,
        failure: str | None = None,
        snapshot: RegistrySnapshot | None = None,
    ) -> TargetStatus:
        _validate_id(target_id, "target id")
        if snapshot is not None and type(snapshot) is not RegistrySnapshot:
            raise ValueError("status snapshot must be an exact RegistrySnapshot")
        config = snapshot.config if snapshot is not None else self.load()
        targets = {target.id: target for target in config.targets}
        try:
            target = targets[target_id]
        except KeyError as error:
            raise ValueError(f"unknown target: {target_id}") from error
        if manifest is not None:
            if manifest.target_id != target_id:
                raise ValueError("manifest belongs to a different target")
            if manifest.framework is not target.framework:
                raise ValueError("manifest framework does not match target")
            _validate_commit(manifest.source_revision, "manifest source revision")
        if audit is not None and audit.target_id != target_id:
            raise ValueError("audit belongs to a different target")
        if resolved_commit is not None:
            _validate_commit(resolved_commit, "resolved commit")
        commit = manifest.source_revision if manifest is not None else resolved_commit
        if failure is not None:
            _require_string(failure, "failure")
            state = TargetState.FAILED
        elif resolved_commit is None:
            state = TargetState.MISSING_REF
        elif audit is not None and _audit_is_invalid(audit):
            state = TargetState.MODIFIED
        elif manifest is None or manifest.source_revision != resolved_commit:
            state = TargetState.STALE
        elif _is_preview_channel(target.channel):
            state = TargetState.PREVIEW
        else:
            channels = {channel.id: channel for channel in config.channels}
            sources = {source.id: source for source in config.sources}
            channel = channels[target.channel]
            source = sources[channel.source]
            state = (
                TargetState.STABLE
                if channel.ref == source.stable_ref
                else TargetState.BRANCH
            )
        return TargetStatus(target_id=target_id, state=state, channel=target.channel, commit=commit)


def _validate_unique(items: tuple[Any, ...], label: str) -> None:
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            raise ValueError(f"duplicate {label} id: {item.id}")
        seen.add(item.id)


def _require_string(value: Any, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _validate_id(value: Any, label: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError(f"{label} must be a nonempty safe identifier")
    return value


def _validate_ref(value: Any) -> str:
    invalid = (
        type(value) is not str
        or not value.startswith("refs/")
        or value.endswith(("/", ".", ".lock"))
        or "//" in value
        or ".." in value
        or "@{" in value
        or _REF_FORBIDDEN.search(value) is not None
    )
    if not invalid:
        invalid = any(
            not part or part.startswith(".") or part.endswith(".lock")
            for part in value.split("/")
        )
    if invalid:
        raise ValueError("Git ref must be a fully qualified refs/... string")
    return value


def _validate_commit(value: Any, label: str = "commit") -> str:
    if type(value) is not str or _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{label} must be a full 40-hex commit")
    return value


def _validate_absolute_normal_path(path: Path, label: str) -> None:
    if not isinstance(path, Path):
        raise ValueError(f"{label} must be a Path")
    raw = os.fspath(path)
    if not path.is_absolute() or os.path.normpath(raw) != raw:
        raise ValueError(f"{label} must be an absolute lexically normalized host path")


def _validate_home(path: Path, label: str) -> tuple[tuple[int, int] | None, str]:
    _validate_absolute_normal_path(path, label)
    normalized = os.path.abspath(os.path.normpath(os.fspath(path)))
    if os.sep == "/" and normalized.startswith("//"):
        normalized = f"/{normalized.lstrip('/')}"
    normalized = os.path.normcase(normalized)
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            item = current.lstat()
        except FileNotFoundError:
            return None, normalized
        if stat.S_ISLNK(item.st_mode):
            raise ValueError(f"{label} contains a symlink ancestor")
        if current != path and not stat.S_ISDIR(item.st_mode):
            raise ValueError(f"{label} has a non-directory ancestor")
        if current == path and not stat.S_ISDIR(item.st_mode):
            raise ValueError(f"{label} must be a directory when it exists")
    descriptor = _open_directory_chain(path)
    try:
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if _identity(opened) != _identity(item):
        raise RuntimeError(f"{label} changed while it was opened")
    return _identity(opened), normalized


def _validate_config_fields(config: RegistryConfig, *, require_nonempty: bool) -> None:
    if type(config.schema_version) is not int or config.schema_version != 1:
        raise ValueError("registry schema version must be integer 1")
    collections = (
        (config.sources, SourceSpec, "source"),
        (config.channels, ChannelSpec, "channel"),
        (config.targets, TargetSpec, "target"),
    )
    for values, expected, label in collections:
        if type(values) is not tuple or any(type(value) is not expected for value in values):
            raise ValueError(f"registry {label}s must contain exact {label} values")
        if require_nonempty and not values:
            raise ValueError("registry sources, channels, and targets must be nonempty mappings")
        _validate_unique(values, label)
    source_ids = {source.id for source in config.sources}
    channel_ids = {channel.id for channel in config.channels}
    for source in config.sources:
        _validate_id(source.id, "source id")
        _require_string(source.url, "source URL")
        _validate_ref(source.stable_ref)
    for channel in config.channels:
        _validate_id(channel.id, "channel id")
        _validate_id(channel.source, "channel source")
        _validate_ref(channel.ref)
        if channel.source not in source_ids:
            raise ValueError(f"channel {channel.id!r} refers to an unknown source")
    for target in config.targets:
        _validate_id(target.id, "target id")
        if type(target.framework) is not Framework:
            raise ValueError(f"target {target.id!r} has an invalid framework")
        if not isinstance(target.home, Path):
            raise ValueError(f"target {target.id!r} home must be a Path")
        _validate_id(target.channel, "target channel")
        if target.channel not in channel_ids:
            raise ValueError(f"target {target.id!r} refers to an unknown channel")


def _validate_config(config: RegistryConfig, *, require_nonempty: bool) -> None:
    if type(config) is not RegistryConfig:
        raise ValueError("config must be an exact RegistryConfig")
    _validate_config_fields(config, require_nonempty=require_nonempty)
    homes: set[str] = set()
    identities: set[tuple[int, int]] = set()
    for target in config.targets:
        identity, key = _validate_home(target.home, f"target {target.id!r} home")
        if key in homes or (identity is not None and identity in identities):
            raise ValueError("two targets may not use the same home")
        homes.add(key)
        if identity is not None:
            identities.add(identity)


def _mapping(value: Any, label: str, *, nonempty: bool = False) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be a mapping")
    if nonempty and not value:
        raise ValueError(f"{label} must be a nonempty mapping")
    if any(type(key) is not str for key in value):
        raise ValueError(f"{label} keys must be strings")
    return value


def _exact_keys(mapping: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = set(mapping) - allowed
    missing = allowed - set(mapping)
    if unknown:
        raise ValueError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")
    if missing:
        raise ValueError(f"{label} is missing keys: {', '.join(sorted(missing))}")


def _load_yaml(content: bytes) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("registry must be UTF-8 YAML") from error
    try:
        value = yaml.load(text, Loader=_StrictLoader)
    except yaml.YAMLError as error:
        detail = str(error)
        if "tag" in detail:
            raise ValueError(f"registry YAML tag is not allowed: {detail}") from error
        raise ValueError(f"invalid registry YAML: {detail}") from error
    return _mapping(value, "registry document")


def _parse_registry(content: bytes) -> RegistryConfig:
    root = _load_yaml(content)
    _exact_keys(root, _ROOT_KEYS, "registry")
    if type(root["schema_version"]) is not int:
        raise ValueError("registry schema version must be an integer")
    if root["schema_version"] != 1:
        raise ValueError("registry schema version must be 1")
    source_data = _mapping(root["sources"], "sources", nonempty=True)
    channel_data = _mapping(root["channels"], "channels", nonempty=True)
    target_data = _mapping(root["targets"], "targets", nonempty=True)
    sources: list[SourceSpec] = []
    channels: list[ChannelSpec] = []
    targets: list[TargetSpec] = []
    for source_id in sorted(source_data):
        _validate_id(source_id, "source id")
        item = _mapping(source_data[source_id], f"source {source_id!r}")
        _exact_keys(item, _SOURCE_KEYS, f"source {source_id!r}")
        sources.append(
            SourceSpec(
                id=source_id,
                url=_require_string(item["url"], "source URL"),
                stable_ref=_validate_ref(item["stable_ref"]),
            )
        )
    for channel_id in sorted(channel_data):
        _validate_id(channel_id, "channel id")
        item = _mapping(channel_data[channel_id], f"channel {channel_id!r}")
        _exact_keys(item, _CHANNEL_KEYS, f"channel {channel_id!r}")
        channels.append(
            ChannelSpec(
                id=channel_id,
                source=_validate_id(item["source"], "channel source"),
                ref=_validate_ref(item["ref"]),
            )
        )
    for target_id in sorted(target_data):
        _validate_id(target_id, "target id")
        item = _mapping(target_data[target_id], f"target {target_id!r}")
        _exact_keys(item, _TARGET_KEYS, f"target {target_id!r}")
        framework_value = _require_string(item["framework"], "target framework")
        try:
            framework = Framework(framework_value)
        except ValueError as error:
            raise ValueError(f"target {target_id!r} has an invalid framework") from error
        home = Path(_require_string(item["home"], "target home"))
        targets.append(
            TargetSpec(
                id=target_id,
                framework=framework,
                home=home,
                channel=_validate_id(item["channel"], "target channel"),
            )
        )
    config = RegistryConfig(1, tuple(sources), tuple(channels), tuple(targets))
    _validate_config(config, require_nonempty=True)
    return config


def _dump_registry(config: RegistryConfig) -> bytes:
    sources = sorted(config.sources, key=lambda source: source.id)
    channels = sorted(config.channels, key=lambda channel: channel.id)
    targets = sorted(config.targets, key=lambda target: target.id)
    document = {
        "schema_version": 1,
        "sources": {
            source.id: {"url": source.url, "stable_ref": source.stable_ref}
            for source in sources
        },
        "channels": {
            channel.id: {"source": channel.source, "ref": channel.ref}
            for channel in channels
        },
        "targets": {
            target.id: {
                "framework": target.framework.value,
                "home": os.fspath(target.home),
                "channel": target.channel,
            }
            for target in targets
        },
    }
    return yaml.safe_dump(
        document,
        allow_unicode=False,
        default_flow_style=False,
        sort_keys=False,
    ).encode("utf-8")


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _receipt_to_data(receipt: DeploymentReceipt) -> dict[str, Any]:
    if not isinstance(receipt, DeploymentReceipt):
        raise TypeError("receipt must be a DeploymentReceipt")
    operation = _require_string(receipt.operation, "receipt operation")
    commits = [_validate_commit(commit) for commit in receipt.commits]
    targets: list[dict[str, Any]] = []
    for target in receipt.targets:
        if not isinstance(target, TargetStatus):
            raise ValueError("receipt targets must be TargetStatus values")
        _validate_id(target.target_id, "receipt target id")
        _validate_id(target.channel, "receipt target channel")
        if not isinstance(target.state, TargetState):
            raise ValueError("receipt target state is invalid")
        if target.commit is not None:
            _validate_commit(target.commit, "receipt target commit")
        targets.append(
            {
                "target_id": target.target_id,
                "state": target.state.value,
                "channel": target.channel,
                "commit": target.commit,
            }
        )
    return {"operation": operation, "commits": commits, "targets": targets}


def _dump_receipt_wrapper(
    receipt: dict[str, Any], registry_fingerprint: str
) -> bytes:
    _validate_fingerprint(registry_fingerprint)
    return (
        json.dumps(
            {
                "schema_version": 1,
                "registry_fingerprint": registry_fingerprint,
                "receipt": receipt,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _parse_receipt_wrapper(content: bytes) -> tuple[str, DeploymentReceipt]:
    try:
        data = json.loads(content.decode("utf-8"), object_pairs_hook=_strict_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("receipt must be strict UTF-8 JSON") from error
    wrapper = _mapping(data, "receipt wrapper")
    _exact_keys(wrapper, _RECEIPT_WRAPPER_KEYS, "receipt wrapper")
    if type(wrapper["schema_version"]) is not int or wrapper["schema_version"] != 1:
        raise ValueError("receipt wrapper schema version must be integer 1")
    fingerprint = _validate_fingerprint(wrapper["registry_fingerprint"])
    root = _mapping(wrapper["receipt"], "receipt")
    _exact_keys(root, _RECEIPT_KEYS, "receipt")
    operation = _require_string(root["operation"], "receipt operation")
    if type(root["commits"]) is not list:
        raise ValueError("receipt commits must be a list")
    if type(root["targets"]) is not list:
        raise ValueError("receipt targets must be a list")
    commits = tuple(_validate_commit(commit) for commit in root["commits"])
    targets: list[TargetStatus] = []
    for index, value in enumerate(root["targets"]):
        item = _mapping(value, f"receipt target {index}")
        _exact_keys(item, _STATUS_KEYS, f"receipt target {index}")
        try:
            state = TargetState(_require_string(item["state"], "receipt target state"))
        except ValueError as error:
            raise ValueError("receipt target state is invalid") from error
        commit = item["commit"]
        if commit is not None:
            _validate_commit(commit, "receipt target commit")
        targets.append(
            TargetStatus(
                target_id=_validate_id(item["target_id"], "receipt target id"),
                state=state,
                channel=_validate_id(item["channel"], "receipt target channel"),
                commit=commit,
            )
        )
    return fingerprint, DeploymentReceipt(
        operation=operation, commits=commits, targets=tuple(targets)
    )


def _read_receipt_wrapper(
    receipts: int, name: str
) -> tuple[str, DeploymentReceipt]:
    return _parse_receipt_wrapper(
        _read_regular_file(
            receipts,
            name,
            required_mode=0o600,
            maximum=_MAX_RECEIPT_BYTES,
            label="receipt file",
        )
    )


def _read_receipt_fingerprint(receipts: int, name: str) -> str:
    fingerprint, _receipt = _read_receipt_wrapper(receipts, name)
    return fingerprint


def _validate_fingerprint(value: Any) -> str:
    if type(value) is not str or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError("registry fingerprint must be 64 lowercase hex characters")
    return value


def _require_secure_platform(*, require_locking: bool = False) -> None:
    required_flags = ("O_NOFOLLOW", "O_DIRECTORY", "O_CREAT", "O_EXCL")
    required_dir_fd = (os.open, os.stat, os.mkdir, os.unlink, os.link, os.rename)
    missing = [
        function.__name__
        for function in required_dir_fd
        if function not in os.supports_dir_fd
    ]
    unsupported = (
        os.name != "posix"
        or any(not hasattr(os, flag) for flag in required_flags)
        or os.stat not in os.supports_follow_symlinks
        or os.link not in os.supports_follow_symlinks
        or os.listdir not in os.supports_fd
        or bool(missing)
    )
    if unsupported:
        detail = f": missing descriptor support for {', '.join(missing)}" if missing else ""
        raise RuntimeError(
            f"secure registry descriptor operations are unavailable on this platform{detail}"
        )
    if require_locking and fcntl is None:
        raise RuntimeError("secure registry locking is unavailable on this platform")


def _open_directory_chain_with_identity(
    path: Path,
) -> tuple[int, tuple[tuple[int, int], ...]]:
    _validate_absolute_normal_path(path, "registry parent")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    identities = [_identity(os.fstat(descriptor))]
    try:
        for part in path.parts[1:]:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            item = os.fstat(child)
            if not stat.S_ISDIR(item.st_mode):
                os.close(child)
                raise RuntimeError("registry parent contains a non-directory component")
            os.close(descriptor)
            descriptor = child
            identities.append(_identity(item))
        return descriptor, tuple(identities)
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_chain(path: Path) -> int:
    descriptor, _ = _open_directory_chain_with_identity(path)
    return descriptor


def _verify_canonical_parent(
    path: Path,
    descriptor: int,
    identities: tuple[tuple[int, int], ...],
) -> None:
    if _identity(os.fstat(descriptor)) != identities[-1]:
        raise RuntimeError("retained registry parent descriptor changed identity")
    reopened, observed = _open_directory_chain_with_identity(path)
    try:
        if observed != identities:
            raise RuntimeError("canonical registry parent ancestor chain changed")
    finally:
        os.close(reopened)


def _open_locked_parent(
    path: Path, lock_name: str, *, exclusive: bool, create_lock: bool
) -> tuple[int, tuple[tuple[int, int], ...], int]:
    parent, identities = _open_directory_chain_with_identity(path)
    lock: int | None = None
    try:
        _verify_canonical_parent(path, parent, identities)
        lock = _open_private_lock(
            parent,
            lock_name,
            create=create_lock,
            writable=exclusive,
        )
        fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        observed_lock = os.stat(lock_name, dir_fd=parent, follow_symlinks=False)
        if _identity(observed_lock) != _identity(os.fstat(lock)):
            raise RuntimeError("registry lock changed after acquisition")
        _verify_canonical_parent(path, parent, identities)
        return parent, identities, lock
    except BaseException:
        if lock is not None:
            os.close(lock)
        os.close(parent)
        raise


def _close_registry_lock(lock: int) -> None:
    try:
        fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        os.close(lock)


def _snapshot_from_parent(parent: int, name: str) -> RegistrySnapshot:
    content, registry_identity = _read_regular_file_with_identity(
        parent,
        name,
        required_mode=0o600,
        maximum=_MAX_REGISTRY_BYTES,
        label="registry file",
    )
    config = _parse_registry(content)
    return RegistrySnapshot(
        config=config,
        fingerprint=hashlib.sha256(content).hexdigest(),
        registry_identity=registry_identity,
    )


def _require_current_snapshot(
    parent: int, name: str, expected: RegistrySnapshot
) -> None:
    current = _snapshot_from_parent(parent, name)
    if current != expected:
        raise ValueError("registry no longer matches the required snapshot")


def _verify_registry_context(
    parent: int,
    name: str,
    snapshot: RegistrySnapshot,
    parent_path: Path,
    parent_identities: tuple[tuple[int, int], ...],
) -> None:
    _require_current_snapshot(parent, name, snapshot)
    _verify_canonical_parent(parent_path, parent, parent_identities)


def _identity(item: os.stat_result) -> tuple[int, int]:
    return item.st_dev, item.st_ino


def _file_snapshot(item: os.stat_result) -> tuple[int, int, int, int, int]:
    return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns


def _optional_regular_identity(parent: int, name: str, label: str) -> tuple[int, int] | None:
    try:
        item = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode):
        raise RuntimeError(f"{label} must be a non-symlink regular file")
    return _identity(item)


def _read_regular_file(
    parent: int,
    name: str,
    *,
    required_mode: int,
    maximum: int,
    label: str,
) -> bytes:
    return _read_regular_file_with_identity(
        parent,
        name,
        required_mode=required_mode,
        maximum=maximum,
        label=label,
    )[0]


def _read_regular_file_with_identity(
    parent: int,
    name: str,
    *,
    required_mode: int,
    maximum: int,
    label: str,
) -> tuple[bytes, tuple[int, int]]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"{label} must be a non-symlink regular file")
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
        dir_fd=parent,
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise RuntimeError(f"{label} changed while it was opened")
        if stat.S_IMODE(opened.st_mode) != required_mode:
            raise PermissionError(f"{label} mode must be {required_mode:04o}")
        if opened.st_size > maximum:
            raise ValueError(f"{label} is too large")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) > maximum:
            raise ValueError(f"{label} is too large")
        after_fd = os.fstat(descriptor)
        after_name = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _file_snapshot(after_fd) != _file_snapshot(opened) or _identity(after_name) != _identity(
            opened
        ):
            raise RuntimeError(f"{label} changed while it was read")
        return content, _identity(opened)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _replace_at(parent: int, source: str, destination: str) -> None:
    os.rename(source, destination, src_dir_fd=parent, dst_dir_fd=parent)


def _unlink_if_regular(parent: int, name: str) -> None:
    try:
        item = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISREG(item.st_mode) and not stat.S_ISLNK(item.st_mode):
        os.unlink(name, dir_fd=parent)


def _entry_exists(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _open_private_directory(
    parent: int, name: str, *, create: bool, missing_ok: bool = False
) -> int | None:
    created = False
    try:
        item = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            if missing_ok:
                return None
            raise RuntimeError(f"private directory {name!r} is missing") from None
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        else:
            created = True
            os.fsync(parent)
        item = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
        raise RuntimeError(f"private directory {name!r} must be a non-symlink directory")
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    if created:
        os.fchmod(descriptor, 0o700)
    opened = os.fstat(descriptor)
    if _identity(opened) != _identity(item):
        os.close(descriptor)
        raise RuntimeError(f"private directory {name!r} changed while it was opened")
    if stat.S_IMODE(opened.st_mode) != 0o700:
        os.close(descriptor)
        raise PermissionError(f"private directory {name!r} mode must be 0700")
    return descriptor


def _open_private_lock(
    parent: int, name: str, *, create: bool, writable: bool
) -> int:
    access = os.O_RDWR if writable else os.O_RDONLY
    try:
        descriptor = os.open(name, access | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        if not create:
            raise RuntimeError(
                "registry lock is missing; initialize the registry with save()"
            ) from None
        try:
            descriptor = os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
        except FileExistsError:
            descriptor = os.open(name, access | os.O_NOFOLLOW, dir_fd=parent)
        else:
            os.fchmod(descriptor, 0o600)
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        raise RuntimeError("registry lock must be a regular file")
    if stat.S_IMODE(opened.st_mode) != 0o600:
        os.close(descriptor)
        raise PermissionError("registry lock mode must be 0600")
    observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if _identity(observed) != _identity(opened):
        os.close(descriptor)
        raise RuntimeError("registry lock changed while it was opened")
    return descriptor


def _receipt_names(directory: int) -> tuple[str, ...]:
    names = os.listdir(directory)
    receipt_names: list[str] = []
    for name in names:
        if _RECEIPT_FILE.fullmatch(name) is None:
            raise RuntimeError(f"unexpected entry in receipt directory: {name}")
        receipt_names.append(name)
    return tuple(sorted(receipt_names))


def _receipt_names_for_append(
    directory: int,
    *,
    destination: str,
    sequence: int,
    registry_fingerprint: str,
    receipt: DeploymentReceipt,
    before_publication: Callable[[], None],
) -> int:
    receipt_names: list[str] = []
    temporary_names: list[str] = []
    for name in os.listdir(directory):
        if _RECEIPT_FILE.fullmatch(name) is not None:
            receipt_names.append(name)
        elif (
            (match := _RECEIPT_TEMP.fullmatch(name)) is not None
            and match.group("receipt") == destination
        ):
            temporary_names.append(name)
        else:
            raise RuntimeError(f"unexpected entry in receipt directory: {name}")
    ordered_names = tuple(sorted(receipt_names))
    count = _validate_contiguous_names(ordered_names, sequence, allow_orphan=True)
    if not temporary_names:
        return count
    if len(temporary_names) != 1:
        raise RuntimeError("receipt history contains multiple interrupted stages")
    temporary = temporary_names[0]
    staged_fingerprint, staged_receipt = _read_receipt_wrapper(directory, temporary)
    if staged_fingerprint != registry_fingerprint or staged_receipt != receipt:
        raise RuntimeError("interrupted receipt stage does not match append retry")
    if destination in receipt_names:
        destination_identity = _optional_regular_identity(
            directory, destination, "orphan receipt"
        )
        temporary_identity = _optional_regular_identity(
            directory, temporary, "receipt temporary file"
        )
        if destination_identity != temporary_identity:
            raise RuntimeError("interrupted receipt stage is not linked to orphan receipt")
    else:
        before_publication()
        os.link(
            temporary,
            destination,
            src_dir_fd=directory,
            dst_dir_fd=directory,
            follow_symlinks=False,
        )
        receipt_names.append(destination)
    os.unlink(temporary, dir_fd=directory)
    os.fsync(directory)
    return len(receipt_names)


def _validate_contiguous_names(
    names: list[str] | tuple[str, ...], counter: int, *, allow_orphan: bool
) -> int:
    maximum_count = counter + (1 if allow_orphan else 0)
    if len(names) > maximum_count:
        raise RuntimeError("receipt history is not contiguous with its counter")
    for index, name in enumerate(names):
        if name != f"{index:020d}.json":
            raise RuntimeError("receipt history is not contiguous with its counter")
    if len(names) < counter:
        raise RuntimeError("receipt history is missing entries or exceeds its counter")
    return len(names)


def _read_contiguous_records(
    receipts: int, names: tuple[str, ...], counter: int
) -> tuple[ReceiptRecord, ...]:
    _validate_contiguous_names(names, counter, allow_orphan=False)
    records: list[ReceiptRecord] = []
    for sequence, name in enumerate(names):
        fingerprint, receipt = _read_receipt_wrapper(receipts, name)
        records.append(ReceiptRecord(sequence, fingerprint, receipt))
    return tuple(records)


def _recover_history_before_save(
    parent: int,
    state_name: str,
    current_fingerprint: str,
    *,
    before_mutation: Callable[[], None],
) -> None:
    state = _open_private_directory(parent, state_name, create=False, missing_ok=True)
    if state is None:
        return
    receipts: int | None = None
    try:
        receipts = _open_private_directory(state, "receipts", create=False)
        counter = _read_counter(state)
        destination = f"{counter:020d}.json"
        receipt_names: list[str] = []
        temporary_names: list[str] = []
        for name in os.listdir(receipts):
            if _RECEIPT_FILE.fullmatch(name) is not None:
                receipt_names.append(name)
            elif (
                (match := _RECEIPT_TEMP.fullmatch(name)) is not None
                and match.group("receipt") == destination
            ):
                temporary_names.append(name)
            else:
                raise RuntimeError(f"unexpected entry in receipt directory: {name}")
        receipt_names.sort()
        count = _validate_contiguous_names(receipt_names, counter, allow_orphan=True)
        orphan_fingerprint: str | None = None
        for sequence, name in enumerate(receipt_names):
            fingerprint = _read_receipt_fingerprint(receipts, name)
            if sequence == counter:
                orphan_fingerprint = fingerprint
        if len(temporary_names) > 1:
            raise RuntimeError("receipt history contains multiple interrupted stages")
        temporary = temporary_names[0] if temporary_names else None
        if temporary is not None:
            staged_fingerprint = _read_receipt_fingerprint(receipts, temporary)
            if staged_fingerprint != current_fingerprint:
                raise RuntimeError("orphan receipt fingerprint does not match old registry")
            if count == counter + 1:
                if orphan_fingerprint != current_fingerprint:
                    raise RuntimeError("orphan receipt fingerprint does not match old registry")
                if _optional_regular_identity(
                    receipts, destination, "orphan receipt"
                ) != _optional_regular_identity(
                    receipts, temporary, "receipt temporary file"
                ):
                    raise RuntimeError("interrupted receipt stage is not linked to orphan")
            else:
                before_mutation()
                os.link(
                    temporary,
                    destination,
                    src_dir_fd=receipts,
                    dst_dir_fd=receipts,
                    follow_symlinks=False,
                )
                count += 1
                orphan_fingerprint = staged_fingerprint
            before_mutation()
            os.unlink(temporary, dir_fd=receipts)
            os.fsync(receipts)
        if count == counter + 1:
            if orphan_fingerprint != current_fingerprint:
                raise RuntimeError("orphan receipt fingerprint does not match old registry")
            if counter >= _MAX_RECEIPTS:
                raise ValueError("receipt counter is at the supported bound")
            before_mutation()
            _write_counter(state, counter + 1)
    finally:
        if receipts is not None:
            os.close(receipts)
        os.close(state)


def _read_counter(state: int) -> int:
    content = _read_regular_file(
        state,
        _RECEIPT_COUNTER,
        required_mode=0o600,
        maximum=32,
        label="receipt counter",
    )
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise ValueError("receipt counter must be canonical ASCII") from error
    if re.fullmatch(r"(?:0|[1-9][0-9]{0,19})\n", text) is None:
        raise ValueError("receipt counter must be a canonical bounded integer")
    counter = int(text)
    if counter > _MAX_RECEIPTS:
        raise ValueError("receipt counter exceeds supported bound")
    return counter


def _load_or_initialize_counter(state: int, receipts: int) -> int:
    try:
        return _read_counter(state)
    except FileNotFoundError:
        if _receipt_names(receipts):
            raise RuntimeError("receipt counter is missing for existing history") from None
        _write_counter(state, 0)
        return 0


def _write_counter(state: int, counter: int) -> None:
    if type(counter) is not int or not 0 <= counter <= _MAX_RECEIPTS:
        raise ValueError("receipt counter exceeds supported bound")
    _write_private_atomic(
        state,
        _RECEIPT_COUNTER,
        f"{counter}\n".encode("ascii"),
        label="receipt counter",
    )


def _write_private_atomic(parent: int, name: str, content: bytes, *, label: str) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    published = False
    try:
        original = _optional_regular_identity(parent, name, label)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise RuntimeError(f"{label} temporary file must be regular")
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, content)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if _optional_regular_identity(parent, name, label) != original:
            raise RuntimeError(f"{label} changed during atomic write")
        _replace_at(parent, temporary, name)
        published = True
        os.fsync(parent)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if not published:
            _unlink_if_regular(parent, temporary)
        raise


def _is_preview_channel(channel: str) -> bool:
    return channel == "preview" or channel.startswith("preview-") or channel.startswith(
        "unreviewed-local"
    )


def _audit_is_invalid(audit: DeploymentAudit) -> bool:
    diagnostics = (
        audit.missing,
        audit.changed,
        audit.unexpected,
        audit.duplicates,
        audit.validation_errors,
    )
    return (
        type(audit.matches) is not bool
        or any(type(values) is not tuple for values in diagnostics)
        or any(type(value) is not str for values in diagnostics for value in values)
        or not audit.matches
        or any(bool(values) for values in diagnostics)
    )


__all__ = [
    "ChannelSpec",
    "DeploymentRegistry",
    "ReceiptRecord",
    "RegistryConfig",
    "RegistrySnapshot",
]
