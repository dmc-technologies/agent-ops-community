"""Strict, atomic machine registry and append-only deployment receipts."""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
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
_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\\\[]")
_RECEIPT_FILE = re.compile(r"(?P<sequence>[0-9]{20})\.json\Z")
_MAX_REGISTRY_BYTES = 1024 * 1024
_MAX_RECEIPT_BYTES = 1024 * 1024
_ROOT_KEYS = frozenset({"schema_version", "sources", "channels", "targets"})
_SOURCE_KEYS = frozenset({"url", "stable_ref"})
_CHANNEL_KEYS = frozenset({"source", "ref"})
_TARGET_KEYS = frozenset({"framework", "home", "channel"})
_RECEIPT_KEYS = frozenset({"operation", "commits", "targets"})
_STATUS_KEYS = frozenset({"target_id", "state", "channel", "commit"})


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

    def load(self) -> RegistryConfig:
        _require_secure_platform()
        parent = _open_directory_chain(self.path.parent)
        try:
            content = _read_regular_file(
                parent,
                self.path.name,
                required_mode=0o600,
                maximum=_MAX_REGISTRY_BYTES,
                label="registry file",
            )
        finally:
            os.close(parent)
        return _parse_registry(content)

    def save(self, config: RegistryConfig) -> None:
        _require_secure_platform()
        _validate_config(config, require_nonempty=True)
        content = _dump_registry(config)
        _parse_registry(content)
        parent = _open_directory_chain(self.path.parent)
        temporary = f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        published = False
        try:
            original = _optional_regular_identity(parent, self.path.name, "registry file")
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
            os.replace(temporary, self.path.name, src_dir_fd=parent, dst_dir_fd=parent)
            published = True
            final_stat = os.stat(self.path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(final_stat.st_mode) or stat.S_IMODE(final_stat.st_mode) != 0o600:
                raise RuntimeError("published registry file is not a private regular file")
            os.fsync(parent)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            if not published:
                _unlink_if_regular(parent, temporary)
            raise
        finally:
            os.close(parent)

    def append_receipt(self, receipt: DeploymentReceipt) -> Path:
        _require_secure_platform(require_locking=True)
        content = _dump_receipt(receipt)
        parent = _open_directory_chain(self.path.parent)
        state: int | None = None
        receipts: int | None = None
        lock: int | None = None
        temporary: str | None = None
        try:
            _read_regular_file(
                parent,
                self.path.name,
                required_mode=0o600,
                maximum=_MAX_REGISTRY_BYTES,
                label="registry file",
            )
            state = _open_private_directory(parent, self.state_path.name, create=True)
            lock = _open_private_lock(state, "receipts.lock")
            fcntl.flock(lock, fcntl.LOCK_EX)
            receipts = _open_private_directory(state, "receipts", create=True)
            sequence = _next_receipt_sequence(receipts)
            destination = f"{sequence:020d}.json"
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
            os.link(
                temporary,
                destination,
                src_dir_fd=receipts,
                dst_dir_fd=receipts,
                follow_symlinks=False,
            )
            os.unlink(temporary, dir_fd=receipts)
            temporary = None
            os.fsync(receipts)
            os.fsync(state)
            return self.receipts_path / destination
        except BaseException:
            if temporary is not None and receipts is not None:
                _unlink_if_regular(receipts, temporary)
            raise
        finally:
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.close(lock)
            if receipts is not None:
                os.close(receipts)
            if state is not None:
                os.close(state)
            os.close(parent)

    def receipts(self) -> tuple[DeploymentReceipt, ...]:
        _require_secure_platform(require_locking=True)
        parent = _open_directory_chain(self.path.parent)
        state: int | None = None
        receipts: int | None = None
        lock: int | None = None
        try:
            _read_regular_file(
                parent,
                self.path.name,
                required_mode=0o600,
                maximum=_MAX_REGISTRY_BYTES,
                label="registry file",
            )
            state = _open_private_directory(
                parent, self.state_path.name, create=False, missing_ok=True
            )
            if state is None:
                return ()
            lock = _open_existing_private_lock(state, "receipts.lock")
            if lock is None:
                if _child_exists(state, "receipts"):
                    raise RuntimeError("receipt lock file is missing")
                return ()
            fcntl.flock(lock, fcntl.LOCK_SH)
            receipts = _open_private_directory(state, "receipts", create=False, missing_ok=True)
            if receipts is None:
                return ()
            names = _receipt_names(receipts)
            return tuple(
                _parse_receipt(
                    _read_regular_file(
                        receipts,
                        name,
                        required_mode=0o600,
                        maximum=_MAX_RECEIPT_BYTES,
                        label="receipt file",
                    )
                )
                for name in names
            )
        finally:
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.close(lock)
            if receipts is not None:
                os.close(receipts)
            if state is not None:
                os.close(state)
            os.close(parent)

    def status(
        self,
        target_id: str,
        *,
        manifest: DeploymentManifest | None,
        resolved_commit: str | None,
        audit: DeploymentAudit | None,
        failure: str | None = None,
    ) -> TargetStatus:
        _validate_id(target_id, "target id")
        config = self.load()
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


def _dump_receipt(receipt: DeploymentReceipt) -> bytes:
    return (
        json.dumps(
            _receipt_to_data(receipt),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("ascii")


def _parse_receipt(content: bytes) -> DeploymentReceipt:
    try:
        data = json.loads(content.decode("utf-8"), object_pairs_hook=_strict_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("receipt must be strict UTF-8 JSON") from error
    root = _mapping(data, "receipt")
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
    return DeploymentReceipt(operation=operation, commits=commits, targets=tuple(targets))


def _require_secure_platform(*, require_locking: bool = False) -> None:
    if (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or not os.supports_dir_fd
    ):
        raise RuntimeError("secure registry operations are unavailable on this platform")
    if require_locking and fcntl is None:
        raise RuntimeError("secure receipt locking is unavailable on this platform")


def _open_directory_chain(path: Path) -> int:
    _validate_absolute_normal_path(path, "registry parent")
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
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
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


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
        return content
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _unlink_if_regular(parent: int, name: str) -> None:
    try:
        item = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISREG(item.st_mode) and not stat.S_ISLNK(item.st_mode):
        os.unlink(name, dir_fd=parent)


def _open_private_directory(
    parent: int, name: str, *, create: bool, missing_ok: bool = False
) -> int | None:
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
            os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
            os.fsync(parent)
        item = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
        raise RuntimeError(f"private directory {name!r} must be a non-symlink directory")
    if stat.S_IMODE(item.st_mode) != 0o700:
        raise PermissionError(f"private directory {name!r} mode must be 0700")
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    opened = os.fstat(descriptor)
    if _identity(opened) != _identity(item):
        os.close(descriptor)
        raise RuntimeError(f"private directory {name!r} changed while it was opened")
    return descriptor


def _open_private_lock(parent: int, name: str) -> int:
    try:
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
    except FileExistsError:
        descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
    else:
        os.fchmod(descriptor, 0o600)
    opened = os.fstat(descriptor)
    if not stat.S_ISREG(opened.st_mode):
        os.close(descriptor)
        raise RuntimeError("receipt lock must be a regular file")
    if stat.S_IMODE(opened.st_mode) != 0o600:
        os.close(descriptor)
        raise PermissionError("receipt lock mode must be 0600")
    return descriptor


def _open_existing_private_lock(parent: int, name: str) -> int | None:
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError("receipt lock must be a non-symlink regular file")
    descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent)
    opened = os.fstat(descriptor)
    if _identity(opened) != _identity(before):
        os.close(descriptor)
        raise RuntimeError("receipt lock changed while it was opened")
    if stat.S_IMODE(opened.st_mode) != 0o600:
        os.close(descriptor)
        raise PermissionError("receipt lock mode must be 0600")
    return descriptor


def _child_exists(parent: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _receipt_names(directory: int) -> tuple[str, ...]:
    names = os.listdir(directory)
    receipt_names: list[str] = []
    for name in names:
        if _RECEIPT_FILE.fullmatch(name) is None:
            raise RuntimeError(f"unexpected entry in receipt directory: {name}")
        receipt_names.append(name)
    return tuple(sorted(receipt_names))


def _next_receipt_sequence(directory: int) -> int:
    names = _receipt_names(directory)
    if not names:
        return 0
    return int(names[-1][:-5]) + 1


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
        or bool(audit.validation_errors)
    )


__all__ = ["ChannelSpec", "DeploymentRegistry", "RegistryConfig"]
