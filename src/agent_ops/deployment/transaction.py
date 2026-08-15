from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from agent_ops.deployment.models import (
    DeploymentAudit,
    DeploymentManifest,
    ManifestDirectory,
    ManifestFile,
    PlannedFile,
    ProviderPlan,
    TargetSpec,
)
from agent_ops.registries.models import Framework

__all__ = (
    "install_provider_plans",
    "rollback_manifests",
    "audit_provider_plans",
    "recover_transaction",
)

_SCHEMA_VERSION = 1
_TRANSACTION_SCHEMA_VERSION = 2
_DIRECTORY_EVIDENCE_SCHEMA_VERSION = 1
_METADATA = Path(".agentops/deployment")
_TRANSACTION_PATHS: dict[str, Path] = {}


class PublicationIndeterminateError(OSError):
    """Manifest publication could not be classified safely."""


class IncompleteRollbackError(OSError):
    """Rollback stopped to preserve changed content or required evidence."""


def _before_manifest_replace(
    _home_fs: _HomeFS,
    _record_path: Path,
    _manifest_temp: Path,
    _manifest_path: Path,
) -> None:
    """Internal fault-injection boundary immediately before publication."""


def _fingerprint(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_relative(path: Path) -> Path:
    windows = PureWindowsPath(path)
    if (
        path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or not path.parts
        or path == Path(".")
        or any(part in ("", ".", "..") for part in path.parts)
        or any(part == ".." for part in windows.parts)
    ):
        raise ValueError(f"unsafe managed path: {path}")
    return path


def _absolute_home(path: Path) -> Path:
    raw = os.fspath(path.expanduser())
    if raw in ("", "."):
        raise ValueError(f"unsafe target home: {path}")
    home = Path(os.path.abspath(raw))
    if home == Path("/"):
        raise ValueError(f"unsafe target home: {path}")
    return home


def _open_absolute_directory(path: Path, *, create: bool, mode: int = 0o700) -> int:
    if not path.is_absolute():
        raise ValueError(f"directory path must be absolute: {path}")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
                os.fchmod(child, mode)
                os.fsync(descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory_at(parent: int, name: str, *, create: bool, mode: int) -> int:
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, 0o700, dir_fd=parent)
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        os.fchmod(descriptor, mode)
        os.fsync(parent)
        return descriptor


class _HomeFS:
    def __init__(self, home: Path, descriptor: int) -> None:
        self.home = home
        self.descriptor = descriptor

    def __enter__(self) -> _HomeFS:
        return self

    def __exit__(self, *_args: object) -> None:
        os.close(self.descriptor)

    @staticmethod
    def _parts(relative: Path) -> tuple[str, ...]:
        return tuple(_safe_relative(relative).parts)

    def open_dir(
        self,
        relative: Path,
        *,
        create: bool = False,
        mode: int = 0o755,
    ) -> int:
        descriptor = os.dup(self.descriptor)
        try:
            for part in self._parts(relative):
                child = _open_directory_at(
                    descriptor,
                    part,
                    create=create,
                    mode=mode,
                )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @contextmanager
    def parent(
        self,
        relative: Path,
        *,
        create: bool = False,
        mode: int = 0o755,
    ) -> Iterator[tuple[int, str]]:
        parts = self._parts(relative)
        descriptor = os.dup(self.descriptor)
        try:
            for part in parts[:-1]:
                child = _open_directory_at(
                    descriptor,
                    part,
                    create=create,
                    mode=mode,
                )
                os.close(descriptor)
                descriptor = child
            yield descriptor, parts[-1]
        finally:
            os.close(descriptor)

    def ensure_dir(self, relative: Path, mode: int) -> None:
        descriptor = self.open_dir(relative, create=True, mode=mode)
        try:
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def exists(self, relative: Path) -> bool:
        try:
            with self.parent(relative) as (parent, leaf):
                os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def stat(self, relative: Path) -> os.stat_result:
        with self.parent(relative) as (parent, leaf):
            return os.stat(leaf, dir_fd=parent, follow_symlinks=False)

    def read_file(self, relative: Path) -> bytes:
        with self.parent(relative) as (parent, leaf):
            descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                item = os.fstat(descriptor)
                if not stat.S_ISREG(item.st_mode):
                    raise OSError(f"not a regular file: {relative}")
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)

    def read_optional(self, relative: Path) -> bytes | None:
        try:
            return self.read_file(relative)
        except FileNotFoundError:
            return None

    def scan_entries(self, relative: Path) -> dict[Path, str]:
        root = self.open_dir(relative)
        found: dict[Path, str] = {}

        def visit(descriptor: int, prefix: Path) -> None:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    item = entry.stat(follow_symlinks=False)
                    path = prefix / entry.name
                    if stat.S_ISDIR(item.st_mode):
                        found[path] = "directory"
                        child = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor,
                        )
                        try:
                            visit(child, path)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(item.st_mode):
                        found[path] = "regular"
                    elif stat.S_ISLNK(item.st_mode):
                        found[path] = "symlink"
                    elif stat.S_ISSOCK(item.st_mode):
                        found[path] = "socket"
                    else:
                        found[path] = "other"

        try:
            visit(root, Path())
        finally:
            os.close(root)
        return found

    def scan_tree(self, relative: Path) -> set[Path]:
        return {
            path
            for path, kind in self.scan_entries(relative).items()
            if kind != "directory"
        }

    def write_file(self, relative: Path, content: bytes, mode: int) -> None:
        if not _valid_file_mode(mode):
            raise ValueError(f"invalid file mode: {mode!r}")
        with self.parent(relative, create=True) as (parent, leaf):
            descriptor = os.open(
                leaf,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            try:
                view = memoryview(content)
                while view:
                    view = view[os.write(descriptor, view) :]
                os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                if b"".join(chunks) != content:
                    raise OSError(f"staged file content mismatch: {relative}")
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent)

    def write_atomic(self, relative: Path, content: bytes, mode: int) -> None:
        temporary = relative.with_name(f".{relative.name}.{uuid.uuid4().hex}.tmp")
        self.write_file(temporary, content, mode)
        self.replace(temporary, relative)

    def unlink(self, relative: Path) -> None:
        with self.parent(relative) as (parent, leaf):
            item = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(item.st_mode):
                raise OSError(f"refusing to unlink non-regular file: {relative}")
            os.unlink(leaf, dir_fd=parent)
            os.fsync(parent)

    def remove_empty_dir(self, relative: Path) -> bool:
        with self.parent(relative) as (parent, leaf):
            try:
                os.rmdir(leaf, dir_fd=parent)
            except (FileNotFoundError, OSError) as exc:
                if isinstance(exc, FileNotFoundError) or exc.errno in (39, 66):
                    return False
                raise
            os.fsync(parent)
            return True

    def publish_new(self, source: Path, destination: Path) -> None:
        with (
            self.parent(source) as (source_parent, source_leaf),
            self.parent(destination, create=True) as (destination_parent, destination_leaf),
        ):
            os.link(
                source_leaf,
                destination_leaf,
                src_dir_fd=source_parent,
                dst_dir_fd=destination_parent,
                follow_symlinks=False,
            )
            os.unlink(source_leaf, dir_fd=source_parent)
            os.fsync(source_parent)
            os.fsync(destination_parent)

    def replace(self, source: Path, destination: Path) -> None:
        with (
            self.parent(source) as (source_parent, source_leaf),
            self.parent(destination, create=True) as (destination_parent, destination_leaf),
        ):
            _replace_at(
                source_leaf,
                destination_leaf,
                source_dir_fd=source_parent,
                destination_dir_fd=destination_parent,
            )
            os.fsync(source_parent)
            if destination_parent != source_parent:
                os.fsync(destination_parent)


def _replace_at(
    source: str,
    destination: str,
    *,
    source_dir_fd: int,
    destination_dir_fd: int,
) -> None:
    os.replace(
        source,
        destination,
        src_dir_fd=source_dir_fd,
        dst_dir_fd=destination_dir_fd,
    )


@contextmanager
def _target_lock(home: Path) -> Iterator[_HomeFS]:
    parent = _open_absolute_directory(home.parent, create=True)
    try:
        lock_name = f".{home.name}.agentops-deployment.lock"
        lock = os.open(
            lock_name,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
            dir_fd=parent,
        )
        try:
            lock_stat = os.fstat(lock)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
                raise ValueError("deployment lock is not a regular file")
            os.fchmod(lock, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            home_descriptor = _open_directory_at(
                parent,
                home.name,
                create=True,
                mode=0o700,
            )
            with _HomeFS(home, home_descriptor) as home_fs:
                yield home_fs
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.close(lock)
    finally:
        os.close(parent)


def _manifest_path(target: TargetSpec) -> Path:
    key = hashlib.sha256(target.id.encode()).hexdigest()
    return _METADATA / "manifests" / f"{key}.json"


def _directories(files: tuple[PlannedFile, ...]) -> tuple[ManifestDirectory, ...]:
    paths: set[Path] = set()
    for item in files:
        current = item.path.parent
        while current != Path("."):
            paths.add(current)
            current = current.parent
    return tuple(ManifestDirectory(path, 0o755) for path in sorted(paths, key=str))


def _manifest_to_dict(manifest: DeploymentManifest) -> dict[str, Any]:
    return {
        "schema_version": manifest.schema_version,
        "target_id": manifest.target_id,
        "framework": manifest.framework.value,
        "source_revision": manifest.source_revision,
        "provider_ids": list(manifest.provider_ids),
        "transaction_id": manifest.transaction_id,
        "directories": [
            {"path": item.path.as_posix(), "mode": item.mode}
            for item in manifest.directories
        ],
        "files": [
            {
                "path": item.path.as_posix(),
                "fingerprint": item.fingerprint,
                "mode": item.mode,
            }
            for item in manifest.files
        ],
    }


def _manifest_bytes(manifest: DeploymentManifest) -> bytes:
    return (json.dumps(_manifest_to_dict(manifest), indent=2, sort_keys=True) + "\n").encode()


def _validated_manifest_data(content: bytes, *, target: TargetSpec) -> dict[str, Any]:
    try:
        data = json.loads(content.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid deployment manifest JSON: {exc}") from exc
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != _SCHEMA_VERSION
    ):
        raise ValueError("invalid deployment manifest schema")
    if data.get("target_id") != target.id or data.get("framework") != target.framework.value:
        raise ValueError("deployment manifest target does not match plan")
    if not isinstance(data.get("source_revision"), str) or not data["source_revision"]:
        raise ValueError("invalid deployment manifest source revision")
    if (
        not isinstance(data.get("provider_ids"), list)
        or not data["provider_ids"]
        or any(not isinstance(item, str) or not item for item in data["provider_ids"])
        or data["provider_ids"] != sorted(set(data["provider_ids"]))
    ):
        raise ValueError("invalid deployment manifest providers")
    transaction_id = data.get("transaction_id")
    if (
        not isinstance(transaction_id, str)
        or len(transaction_id) != 32
        or any(character not in "0123456789abcdef" for character in transaction_id)
    ):
        raise ValueError("invalid deployment manifest transaction")
    for key in ("files", "directories"):
        if not isinstance(data.get(key), list):
            raise ValueError(f"invalid deployment manifest {key}")
    file_paths: set[Path] = set()
    for item in data["files"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("fingerprint"), str)
            or len(item["fingerprint"]) != 64
            or any(character not in "0123456789abcdef" for character in item["fingerprint"])
            or not _valid_file_mode(item.get("mode"))
        ):
            raise ValueError("invalid deployment manifest file")
        file_path = _validated_manifest_path(item["path"], kind="file")
        if file_path in file_paths:
            raise ValueError("invalid deployment manifest duplicate file path")
        file_paths.add(file_path)
    directory_paths: set[Path] = set()
    for item in data["directories"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not _valid_directory_mode(item.get("mode"))
        ):
            raise ValueError("invalid deployment manifest directory")
        directory_path = _validated_manifest_path(item["path"], kind="directory")
        if directory_path in directory_paths:
            raise ValueError("invalid deployment manifest duplicate directory path")
        directory_paths.add(directory_path)
    expected_directories: set[Path] = set()
    for file_path in file_paths:
        current = file_path.parent
        while current != Path("."):
            expected_directories.add(current)
            current = current.parent
    if directory_paths != expected_directories:
        raise ValueError("invalid deployment manifest directory closure")
    return data


def _valid_permission_mode(value: object) -> bool:
    return type(value) is int and 0 <= value <= 0o777


def _valid_file_mode(value: object) -> bool:
    return _valid_permission_mode(value) and bool(value & 0o400)


def _valid_directory_mode(value: object) -> bool:
    return _valid_permission_mode(value) and value & 0o300 == 0o300


def _validated_manifest_path(value: str, *, kind: str) -> Path:
    path = _safe_relative(Path(value))
    if path.as_posix() != value:
        raise ValueError(f"invalid deployment manifest non-normalized {kind} path")
    return path


def _frontmatter_name(content: bytes) -> str | None:
    try:
        lines = content.decode().splitlines()
    except UnicodeDecodeError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        data = yaml.safe_load("\n".join(lines[1:end]))
    except (StopIteration, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    return data["name"] or None


@dataclass(frozen=True)
class _PlanGroup:
    target: TargetSpec
    source_revision: str
    provider_ids: tuple[str, ...]
    files: tuple[PlannedFile, ...]
    removals: tuple[Path, ...]


def _validate_and_group(
    plans: tuple[ProviderPlan, ...],
) -> tuple[_PlanGroup, ...]:
    groups: dict[tuple[str, Framework, Path, str, str], dict[Path, PlannedFile]] = {}
    providers: dict[tuple[str, Framework, Path, str, str], set[str]] = {}
    removals: dict[tuple[str, Framework, Path, str, str], set[Path]] = {}
    target_keys: dict[str, tuple[str, Framework, Path, str, str]] = {}
    for plan in plans:
        home = _absolute_home(plan.target.home)
        if home != plan.target.home:
            raise ValueError(f"target home must be absolute and normalized: {plan.target.home}")
        key = (
            plan.target.id,
            plan.target.framework,
            home,
            plan.target.channel,
            plan.source_revision,
        )
        prior_key = target_keys.setdefault(plan.target.id, key)
        if prior_key != key:
            raise ValueError(f"incompatible plans for target {plan.target.id!r}")
        files = groups.setdefault(key, {})
        providers.setdefault(key, set()).add(plan.provider_id)
        planned_removals = removals.setdefault(key, set())
        for item in plan.files:
            path = _safe_relative(item.path)
            if not isinstance(item.content, bytes) or not _valid_file_mode(item.mode):
                raise ValueError(f"invalid planned file: {path}")
            prior = files.get(path)
            if prior is not None and prior != item:
                raise ValueError(f"conflicting planned file: {path}")
            files[path] = item
        for removal in plan.removals:
            planned_removals.add(_safe_relative(removal))
    for key, files in groups.items():
        overlap = set(files).intersection(removals[key])
        if overlap:
            raise ValueError(f"planned file is also removed: {min(overlap, key=str)}")
    results = []
    for key in sorted(groups, key=lambda item: (str(item[2]), item[0])):
        target_id, framework, home, channel, source_revision = key
        target = TargetSpec(target_id, framework, home, channel)
        results.append(
            _PlanGroup(
                target=target,
                source_revision=source_revision,
                provider_ids=tuple(sorted(providers[key])),
                files=tuple(groups[key][path] for path in sorted(groups[key], key=str)),
                removals=tuple(sorted(removals[key], key=str)),
            )
        )
    return tuple(results)


def _ensure_directory(home_fs: _HomeFS, path: Path, mode: int) -> bool:
    if home_fs.exists(path):
        item = home_fs.stat(path)
        if not stat.S_ISDIR(item.st_mode):
            raise ValueError(f"directory path is not a directory: {path}")
        if stat.S_IMODE(item.st_mode) != mode:
            raise ValueError(f"directory mode conflicts: {path}")
        return False
    home_fs.ensure_dir(path, mode)
    return True


def _ensure_staged_parents(home_fs: _HomeFS, staged: Path, transaction: Path) -> None:
    current = staged.parent
    paths: list[Path] = []
    while current != transaction and current != Path("."):
        paths.append(current)
        current = current.parent
    if current != transaction:
        raise ValueError(f"staged path escapes transaction: {staged}")
    for path in reversed(paths):
        _ensure_directory(home_fs, path, 0o700)


def _operation(
    item: PlannedFile,
    transaction_id: str,
    index: int,
    *,
    kind: str,
) -> dict[str, Any]:
    transaction = _METADATA / "transactions" / transaction_id
    return {
        "kind": kind,
        "destination": item.path.as_posix(),
        "staged": (
            (transaction / "rendered" / item.path).as_posix()
            if kind == "installed"
            else None
        ),
        "backup": None,
        "expected_fingerprint": item.fingerprint,
        "expected_mode": item.mode,
        "prior_fingerprint": None,
        "prior_mode": None,
        "prior_exists": False,
        "index": index,
    }


def _record_bytes(record: dict[str, Any]) -> bytes:
    return (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()


def _directory_evidence_path(transaction: Path, directory: Path) -> Path:
    key = hashlib.sha256(directory.as_posix().encode()).hexdigest()
    return transaction / "prestate" / "directories" / f"{key}.json"


def _directory_evidence_bytes(directory: Path, mode: int) -> bytes:
    evidence = {
        "schema_version": _DIRECTORY_EVIDENCE_SCHEMA_VERSION,
        "path": directory.as_posix(),
        "mode": mode,
    }
    return (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode()


def _file_matches(
    home_fs: _HomeFS,
    path: Path,
    fingerprint: str,
    mode: int,
) -> bool:
    try:
        item = home_fs.stat(path)
        content = home_fs.read_file(path)
    except (FileNotFoundError, OSError):
        return False
    return (
        stat.S_ISREG(item.st_mode)
        and stat.S_IMODE(item.st_mode) == mode
        and _fingerprint(content) == fingerprint
    )


def _publication_outcome(
    home_fs: _HomeFS,
    manifest_temp: Path,
    manifest_path: Path,
    expected_content: bytes,
) -> str:
    try:
        if home_fs.exists(manifest_temp):
            return "not-published"
        published = home_fs.read_optional(manifest_path)
    except OSError:
        return "indeterminate"
    if published == expected_content:
        return "committed"
    return "indeterminate"


def install_provider_plans(
    plans: tuple[ProviderPlan, ...],
) -> tuple[DeploymentManifest, ...]:
    manifests: list[DeploymentManifest] = []
    for group in _validate_and_group(plans):
        target = group.target
        files = group.files
        transaction_id = uuid.uuid4().hex
        manifest = DeploymentManifest(
            schema_version=_SCHEMA_VERSION,
            target_id=target.id,
            framework=target.framework,
            source_revision=group.source_revision,
            provider_ids=group.provider_ids,
            files=tuple(
                ManifestFile(item.path, item.fingerprint, item.mode) for item in files
            ),
            directories=_directories(files),
            transaction_id=transaction_id,
        )
        with _target_lock(target.home) as home_fs:
            manifest_path = _manifest_path(target)
            prior_manifest_content = home_fs.read_optional(manifest_path)
            prior_data = None
            prior_manifest_mode = None
            if prior_manifest_content is not None:
                prior_data = _validated_manifest_data(prior_manifest_content, target=target)
                prior_manifest_stat = home_fs.stat(manifest_path)
                if not stat.S_ISREG(prior_manifest_stat.st_mode):
                    raise ValueError("deployment manifest is not a regular file")
                prior_manifest_mode = stat.S_IMODE(prior_manifest_stat.st_mode)
            managed = {
                Path(item["path"]): (item["fingerprint"], item["mode"])
                for item in prior_data["files"]
            } if prior_data is not None else {}
            operations: list[dict[str, Any]] = []
            for index, item in enumerate(files):
                destination_exists = home_fs.exists(item.path)
                prior = managed.get(item.path)
                if destination_exists:
                    installed_stat = home_fs.stat(item.path)
                    if stat.S_ISLNK(installed_stat.st_mode):
                        raise ValueError(f"destination is a symbolic link: {item.path}")
                    if not stat.S_ISREG(installed_stat.st_mode):
                        raise ValueError(f"destination is not a regular file: {item.path}")
                    installed = home_fs.read_file(item.path)
                    installed_mode = stat.S_IMODE(installed_stat.st_mode)
                    if prior is None:
                        if installed == item.content and installed_mode == item.mode:
                            operations.append(
                                _operation(item, transaction_id, index, kind="adopted")
                            )
                            continue
                        raise ValueError(
                            f"unmanaged destination conflicts with plan: {item.path}"
                        )
                    if _fingerprint(installed) != prior[0] or installed_mode != prior[1]:
                        raise ValueError(f"managed destination changed: {item.path}")
                operation = _operation(item, transaction_id, index, kind="installed")
                if prior is not None and destination_exists:
                    operation["backup"] = (
                        _METADATA / "transactions" / transaction_id / "backups" / f"{index:04d}"
                    ).as_posix()
                    operation["prior_fingerprint"] = prior[0]
                    operation["prior_mode"] = prior[1]
                    operation["prior_exists"] = True
                operations.append(operation)
            for index, removal in enumerate(group.removals, start=len(operations)):
                prior = managed.get(removal)
                if prior is None:
                    if home_fs.exists(removal):
                        raise ValueError(f"refusing to remove unmanaged destination: {removal}")
                    continue
                operation = {
                    "kind": "removal",
                    "destination": removal.as_posix(),
                    "staged": None,
                    "backup": None,
                    "expected_fingerprint": None,
                    "expected_mode": None,
                    "prior_fingerprint": prior[0],
                    "prior_mode": prior[1],
                    "prior_exists": False,
                }
                if home_fs.exists(removal):
                    removal_stat = home_fs.stat(removal)
                    removal_content = home_fs.read_file(removal)
                    if (
                        not stat.S_ISREG(removal_stat.st_mode)
                        or _fingerprint(removal_content) != prior[0]
                        or stat.S_IMODE(removal_stat.st_mode) != prior[1]
                    ):
                        raise ValueError(f"managed destination changed: {removal}")
                    operation["backup"] = (
                        _METADATA / "transactions" / transaction_id / "backups" / f"{index:04d}"
                    ).as_posix()
                    operation["prior_exists"] = True
                else:
                    operation["prior_fingerprint"] = None
                    operation["prior_mode"] = None
                operations.append(operation)
            transaction = _METADATA / "transactions" / transaction_id
            rendered = transaction / "rendered"
            backups = transaction / "backups"
            prestate = transaction / "prestate"
            directory_prestate = prestate / "directories"
            manifest_temp = transaction / "manifest.tmp"
            record_path = transaction / "record.json"
            metadata_directories = (
                _METADATA,
                _METADATA / "transactions",
                transaction,
                rendered,
                backups,
                prestate,
                directory_prestate,
            )
            for metadata_dir in metadata_directories:
                _ensure_directory(home_fs, metadata_dir, 0o700)
            directory_records: list[dict[str, Any]] = []
            actual_directories: list[ManifestDirectory] = []
            for directory in manifest.directories:
                exists = home_fs.exists(directory.path)
                directory_mode = directory.mode
                if exists:
                    directory_stat = home_fs.stat(directory.path)
                    if not stat.S_ISDIR(directory_stat.st_mode):
                        raise ValueError(f"managed directory is not a directory: {directory.path}")
                    directory_mode = stat.S_IMODE(directory_stat.st_mode)
                if not _valid_directory_mode(directory_mode):
                    raise ValueError(f"invalid managed directory mode: {directory.path}")
                actual_directories.append(ManifestDirectory(directory.path, directory_mode))
                evidence_path = (
                    _directory_evidence_path(transaction, directory.path)
                    if exists
                    else None
                )
                directory_records.append(
                    {
                        "path": directory.path.as_posix(),
                        "created": not exists,
                        "mode": directory_mode,
                        "prestate_evidence": (
                            evidence_path.as_posix()
                            if evidence_path is not None
                            else None
                        ),
                    }
                )
                if evidence_path is not None:
                    home_fs.write_file(
                        evidence_path,
                        _directory_evidence_bytes(directory.path, directory_mode),
                        0o600,
                    )
            manifest = DeploymentManifest(
                schema_version=manifest.schema_version,
                target_id=manifest.target_id,
                framework=manifest.framework,
                source_revision=manifest.source_revision,
                provider_ids=manifest.provider_ids,
                files=manifest.files,
                directories=tuple(actual_directories),
                transaction_id=manifest.transaction_id,
            )
            record = {
                "schema_version": _TRANSACTION_SCHEMA_VERSION,
                "state": "prepared",
                "manifest": _manifest_to_dict(manifest),
                "manifest_path": manifest_path.as_posix(),
                "manifest_content": base64.b64encode(_manifest_bytes(manifest)).decode(),
                "prior_manifest_content": (
                    base64.b64encode(prior_manifest_content).decode()
                    if prior_manifest_content is not None
                    else None
                ),
                "prior_manifest_mode": prior_manifest_mode,
                "directories": directory_records,
                "operations": operations,
            }
            home_fs.write_file(record_path, _record_bytes(record), 0o600)
            try:
                for directory in manifest.directories:
                    if not home_fs.exists(directory.path):
                        _ensure_directory(home_fs, directory.path, directory.mode)
                for operation in operations:
                    staged_text = operation["staged"]
                    if operation["kind"] != "installed" or staged_text is None:
                        continue
                    staged = Path(staged_text)
                    _ensure_staged_parents(home_fs, staged, transaction)
                    item = next(
                        item
                        for item in files
                        if item.path.as_posix() == operation["destination"]
                    )
                    home_fs.write_file(staged, item.content, item.mode)
                    if not _file_matches(
                        home_fs,
                        staged,
                        item.fingerprint,
                        item.mode,
                    ):
                        raise OSError(f"staged file fingerprint mismatch: {item.path}")
                for operation in operations:
                    destination = Path(operation["destination"])
                    backup = Path(operation["backup"]) if operation["backup"] else None
                    if backup is not None:
                        home_fs.replace(destination, backup)
                    if operation["kind"] == "installed":
                        staged = Path(operation["staged"])
                        try:
                            home_fs.publish_new(staged, destination)
                        except FileExistsError as exc:
                            raise ValueError(
                                f"new unmanaged destination appeared: {destination}"
                            ) from exc
                manifest_content = _manifest_bytes(manifest)
                home_fs.write_file(manifest_temp, manifest_content, 0o600)
                try:
                    _before_manifest_replace(
                        home_fs,
                        record_path,
                        manifest_temp,
                        manifest_path,
                    )
                    if prior_manifest_content is None:
                        home_fs.publish_new(manifest_temp, manifest_path)
                    else:
                        current = home_fs.read_optional(manifest_path)
                        if current != prior_manifest_content:
                            raise ValueError("deployment manifest changed before publication")
                        home_fs.replace(manifest_temp, manifest_path)
                except BaseException as publication_error:
                    outcome = _publication_outcome(
                        home_fs,
                        manifest_temp,
                        manifest_path,
                        manifest_content,
                    )
                    if outcome == "not-published":
                        raise
                    if outcome == "indeterminate":
                        record["state"] = "indeterminate"
                        home_fs.write_atomic(record_path, _record_bytes(record), 0o600)
                        _TRANSACTION_PATHS[transaction_id] = target.home / record_path
                        if not isinstance(publication_error, Exception):
                            publication_error.add_note(
                                "publication indeterminate; evidence retained at "
                                f"{target.home / record_path}"
                            )
                            raise
                        raise PublicationIndeterminateError(
                            "manifest publication is indeterminate; evidence retained at "
                            f"{target.home / record_path}"
                        ) from publication_error
                    if not isinstance(publication_error, Exception):
                        raise
                record["state"] = "committed"
                home_fs.write_atomic(record_path, _record_bytes(record), 0o600)
                _TRANSACTION_PATHS[transaction_id] = target.home / record_path
            except BaseException as install_error:
                if record.get("state") == "indeterminate":
                    raise
                try:
                    _rollback_record(home_fs, record, record_path)
                except BaseException as rollback_error:
                    _TRANSACTION_PATHS[transaction_id] = target.home / record_path
                    if not isinstance(install_error, Exception):
                        install_error.add_note(
                            "rollback failed; recovery evidence retained at "
                            f"{target.home / record_path}"
                        )
                        raise install_error from rollback_error
                    raise IncompleteRollbackError(
                        f"installation failed: {install_error}; rollback incomplete; "
                        f"evidence retained at {target.home / record_path}"
                    ) from rollback_error
                raise
        manifests.append(manifest)
    return tuple(manifests)


def _manifest_from_data(
    data: dict[str, Any],
    *,
    home: Path,
) -> tuple[TargetSpec, DeploymentManifest]:
    try:
        framework = Framework(data["framework"])
        target_id = data["target_id"]
        if not isinstance(target_id, str) or not target_id:
            raise ValueError("invalid target id")
        target = TargetSpec(target_id, framework, home, "recovery")
        validated = _validated_manifest_data(
            (json.dumps(data, sort_keys=True) + "\n").encode(),
            target=target,
        )
        manifest = DeploymentManifest(
            schema_version=validated["schema_version"],
            target_id=target_id,
            framework=framework,
            source_revision=validated["source_revision"],
            provider_ids=tuple(validated["provider_ids"]),
            files=tuple(
                ManifestFile(Path(item["path"]), item["fingerprint"], item["mode"])
                for item in validated["files"]
            ),
            directories=tuple(
                ManifestDirectory(Path(item["path"]), item["mode"])
                for item in validated["directories"]
            ),
            transaction_id=validated["transaction_id"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid transaction record manifest: {exc}") from exc
    return target, manifest


def _record_location(path: Path) -> tuple[Path, Path, str]:
    if not path.is_absolute() or path != Path(os.path.abspath(os.fspath(path))):
        raise ValueError(f"unsafe transaction record path: {path}")
    if (
        path.name != "record.json"
        or path.parent.parent.name != "transactions"
        or path.parent.parent.parent.name != "deployment"
        or path.parent.parent.parent.parent.name != ".agentops"
    ):
        raise ValueError(f"unsafe transaction record path: {path}")
    transaction_id = path.parent.name
    if len(transaction_id) != 32 or any(
        character not in "0123456789abcdef" for character in transaction_id
    ):
        raise ValueError(f"unsafe transaction id: {transaction_id}")
    home = path.parents[4]
    return home, path.relative_to(home), transaction_id


def _decode_record(
    content: bytes,
    *,
    home: Path,
    transaction_id: str,
) -> tuple[dict[str, Any], DeploymentManifest]:
    try:
        record = json.loads(content.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid transaction record JSON: {exc}") from exc
    if (
        not isinstance(record, dict)
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != _TRANSACTION_SCHEMA_VERSION
        or record.get("state") not in {"prepared", "committed", "indeterminate"}
        or not isinstance(record.get("manifest"), dict)
    ):
        raise ValueError("invalid transaction record schema")
    target, manifest = _manifest_from_data(record["manifest"], home=home)
    if manifest.transaction_id != transaction_id:
        raise ValueError("invalid transaction record identifier")
    transaction = _METADATA / "transactions" / transaction_id
    if record.get("manifest_path") != _manifest_path(target).as_posix():
        raise ValueError("unsafe transaction record manifest path")
    try:
        manifest_content = base64.b64decode(record["manifest_content"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid transaction record manifest content") from exc
    if manifest_content != _manifest_bytes(manifest):
        raise ValueError("transaction record manifest content does not match manifest")
    prior_encoded = record.get("prior_manifest_content")
    prior_data: dict[str, Any] | None = None
    if prior_encoded is not None:
        if not isinstance(prior_encoded, str):
            raise ValueError("invalid transaction record prior manifest")
        try:
            prior_content = base64.b64decode(prior_encoded, validate=True)
        except ValueError as exc:
            raise ValueError("invalid transaction record prior manifest") from exc
        prior_data = _validated_manifest_data(prior_content, target=target)
        if record.get("prior_manifest_mode") not in (0o600,):
            raise ValueError("invalid transaction record prior manifest mode")
    elif record.get("prior_manifest_mode") is not None:
        raise ValueError("invalid transaction record prior manifest mode")
    operations = record.get("operations")
    directories = record.get("directories")
    if not isinstance(operations, list) or not isinstance(directories, list):
        raise ValueError("invalid transaction record operations")
    destinations: set[Path] = set()
    backup_paths: set[Path] = set()
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("kind") not in {
            "installed",
            "adopted",
            "removal",
        }:
            raise ValueError("invalid transaction record operation")
        if not isinstance(operation.get("destination"), str):
            raise ValueError("invalid transaction record destination")
        destination = _safe_relative(Path(operation["destination"]))
        if destination in destinations:
            raise ValueError("invalid duplicate transaction destination")
        destinations.add(destination)
        expected_fingerprint = operation.get("expected_fingerprint")
        expected_mode = operation.get("expected_mode")
        if operation["kind"] == "removal":
            if expected_fingerprint is not None or expected_mode is not None:
                raise ValueError("invalid removal expectation")
        elif (
            not isinstance(expected_fingerprint, str)
            or len(expected_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in expected_fingerprint)
            or not _valid_file_mode(expected_mode)
        ):
            raise ValueError("invalid transaction expected file")
        staged = operation.get("staged")
        expected_staged = (
            (transaction / "rendered" / destination).as_posix()
            if operation["kind"] == "installed"
            else None
        )
        if staged != expected_staged:
            raise ValueError("unsafe transaction staged path")
        backup = operation.get("backup")
        if backup is not None:
            if not isinstance(backup, str):
                raise ValueError("unsafe transaction backup path")
            backup_path = _safe_relative(Path(backup))
            if backup_path.parent != transaction / "backups":
                raise ValueError("unsafe transaction backup path")
            if backup_path in backup_paths:
                raise ValueError("invalid duplicate transaction backup")
            backup_paths.add(backup_path)
        prior_fingerprint = operation.get("prior_fingerprint")
        prior_mode = operation.get("prior_mode")
        prior_exists = operation.get("prior_exists")
        if not isinstance(prior_exists, bool):
            raise ValueError("invalid transaction prior existence")
        if prior_fingerprint is not None and (
            not isinstance(prior_fingerprint, str)
            or len(prior_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in prior_fingerprint)
            or not _valid_file_mode(prior_mode)
        ):
            raise ValueError("invalid transaction prior file")
        if prior_fingerprint is None and prior_mode is not None:
            raise ValueError("invalid transaction prior mode")
    manifest_files = {
        Path(item["path"]): (item["fingerprint"], item["mode"])
        for item in record["manifest"]["files"]
    }
    prior_files = {
        Path(item["path"]): (item["fingerprint"], item["mode"])
        for item in prior_data["files"]
    } if prior_data is not None else {}
    installed_destinations: set[Path] = set()
    for operation in operations:
        destination = Path(operation["destination"])
        kind = operation["kind"]
        if kind in {"installed", "adopted"}:
            installed_destinations.add(destination)
            if (
                manifest_files.get(destination)
                != (operation["expected_fingerprint"], operation["expected_mode"])
                or kind == "adopted"
                and (
                    operation["prior_exists"]
                    or operation["backup"] is not None
                    or operation["prior_fingerprint"] is not None
                    or operation["prior_mode"] is not None
                )
            ):
                raise ValueError("transaction operations do not match manifest")
        if operation["prior_exists"]:
            if (
                kind == "adopted"
                or operation["backup"] is None
                or prior_files.get(destination)
                != (operation["prior_fingerprint"], operation["prior_mode"])
            ):
                raise ValueError(
                    "transaction operations prior existence does not match prior manifest"
                )
        elif (
            operation["backup"] is not None
            or operation["prior_fingerprint"] is not None
            or operation["prior_mode"] is not None
        ):
            raise ValueError(
                "transaction operations prior existence does not match prior manifest"
            )
        if kind == "removal" and destination not in prior_files:
            raise ValueError("transaction operations do not match manifests")
    if installed_destinations != set(manifest_files):
        raise ValueError("transaction operations do not match manifest")
    directory_paths: set[Path] = set()
    for directory in directories:
        if (
            not isinstance(directory, dict)
            or not isinstance(directory.get("path"), str)
            or not isinstance(directory.get("created"), bool)
            or not _valid_directory_mode(directory.get("mode"))
        ):
            raise ValueError("invalid transaction record directory")
        directory_path = _safe_relative(Path(directory["path"]))
        if directory_path in directory_paths:
            raise ValueError("transaction directories do not match manifest")
        directory_paths.add(directory_path)
        evidence = directory.get("prestate_evidence")
        expected_evidence = _directory_evidence_path(transaction, directory_path).as_posix()
        if (
            directory["created"]
            and evidence is not None
            or not directory["created"]
            and evidence != expected_evidence
        ):
            raise ValueError("transaction directory pre-state evidence is incoherent")
    manifest_directories = {
        Path(item["path"]): item["mode"] for item in record["manifest"]["directories"]
    }
    recorded_directories = {
        Path(item["path"]): item["mode"] for item in directories
    }
    if recorded_directories != manifest_directories:
        raise ValueError("transaction directories do not match manifest")
    return record, manifest


def _read_validated_record(path: Path) -> tuple[Path, Path, dict[str, Any], DeploymentManifest]:
    home, relative, transaction_id = _record_location(path)
    with _HomeFS(home, _open_absolute_directory(home, create=False)) as home_fs:
        content = home_fs.read_file(relative)
    record, manifest = _decode_record(content, home=home, transaction_id=transaction_id)
    return home, relative, record, manifest


def _prior_manifest_content(record: dict[str, Any]) -> bytes | None:
    encoded = record["prior_manifest_content"]
    return base64.b64decode(encoded, validate=True) if encoded is not None else None


def _evidence_entries(home_fs: _HomeFS, root: Path) -> dict[Path, str]:
    try:
        return {root / path: kind for path, kind in home_fs.scan_entries(root).items()}
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ValueError(f"invalid transaction evidence directory: {root}") from exc


def _validate_transaction_evidence(
    home_fs: _HomeFS,
    record: dict[str, Any],
    record_path: Path,
    *,
    missing_backup_error: type[Exception] = ValueError,
) -> None:
    transaction = record_path.parent
    backup_root = transaction / "backups"
    expected_backups = {
        Path(operation["backup"])
        for operation in record["operations"]
        if operation["backup"] is not None
    }
    backup_entries = _evidence_entries(home_fs, backup_root)
    invalid_backups = {
        path: kind for path, kind in backup_entries.items() if kind != "regular"
    }
    if invalid_backups:
        raise ValueError("transaction backup evidence has invalid entries")
    actual_backups = set(backup_entries)
    missing_backups = expected_backups - actual_backups
    if missing_backups:
        missing = min(missing_backups, key=str)
        raise missing_backup_error(
            f"rollback incomplete: backup evidence is missing: {missing}"
        )
    if actual_backups - expected_backups:
        raise ValueError("transaction backup evidence has orphan entries")

    directory_root = transaction / "prestate" / "directories"
    expected_directories = {
        Path(directory["prestate_evidence"])
        for directory in record["directories"]
        if directory["prestate_evidence"] is not None
    }
    directory_entries = _evidence_entries(home_fs, directory_root)
    invalid_directories = {
        path: kind for path, kind in directory_entries.items() if kind != "regular"
    }
    if invalid_directories:
        raise ValueError("transaction directory pre-state evidence has invalid entries")
    actual_directories = set(directory_entries)
    if actual_directories != expected_directories:
        raise ValueError("transaction directory pre-state evidence is not one-to-one")
    for directory in record["directories"]:
        evidence_text = directory["prestate_evidence"]
        if evidence_text is None:
            continue
        evidence_path = Path(evidence_text)
        expected_content = _directory_evidence_bytes(
            Path(directory["path"]),
            directory["mode"],
        )
        evidence_stat = home_fs.stat(evidence_path)
        if (
            not stat.S_ISREG(evidence_stat.st_mode)
            or stat.S_IMODE(evidence_stat.st_mode) != 0o600
            or home_fs.read_file(evidence_path) != expected_content
        ):
            raise ValueError("transaction directory pre-state evidence is invalid")


def _rollback_record(home_fs: _HomeFS, record: dict[str, Any], record_path: Path) -> None:
    _validate_transaction_evidence(
        home_fs,
        record,
        record_path,
        missing_backup_error=IncompleteRollbackError,
    )
    expected_manifest = base64.b64decode(record["manifest_content"], validate=True)
    prior_manifest = _prior_manifest_content(record)
    manifest_path = Path(record["manifest_path"])
    current_manifest = home_fs.read_optional(manifest_path)
    if current_manifest not in {expected_manifest, prior_manifest, None}:
        raise IncompleteRollbackError("rollback incomplete: deployment manifest changed")
    for operation in record["operations"]:
        destination = Path(operation["destination"])
        backup = Path(operation["backup"]) if operation["backup"] else None
        kind = operation["kind"]
        if kind == "installed" and home_fs.exists(destination) and not _file_matches(
            home_fs,
            destination,
            operation["expected_fingerprint"],
            operation["expected_mode"],
        ):
            raise IncompleteRollbackError(
                f"rollback incomplete: installed destination changed: {destination}"
            )
        if (
            kind == "removal"
            and home_fs.exists(destination)
            and (
                backup is not None
                or not _file_matches(
                    home_fs,
                    destination,
                    operation["prior_fingerprint"],
                    operation["prior_mode"],
                )
            )
        ):
            raise IncompleteRollbackError(
                f"rollback incomplete: removed destination changed: {destination}"
            )
        if backup is not None:
            if not home_fs.exists(backup):
                raise IncompleteRollbackError(
                    f"rollback incomplete: backup is missing: {backup}"
                )
            if not _file_matches(
                home_fs,
                backup,
                operation["prior_fingerprint"],
                operation["prior_mode"],
            ):
                raise IncompleteRollbackError(
                    f"rollback incomplete: backup changed: {backup}"
                )
    try:
        for operation in reversed(record["operations"]):
            destination = Path(operation["destination"])
            backup = Path(operation["backup"]) if operation["backup"] else None
            if operation["kind"] == "installed" and home_fs.exists(destination):
                home_fs.unlink(destination)
            if backup is not None and home_fs.exists(backup):
                home_fs.publish_new(backup, destination)
        current_manifest = home_fs.read_optional(manifest_path)
        if prior_manifest is None:
            if current_manifest == expected_manifest:
                home_fs.unlink(manifest_path)
        elif current_manifest != prior_manifest:
            prior_temp = record_path.parent / "prior-manifest.tmp"
            home_fs.write_file(prior_temp, prior_manifest, record["prior_manifest_mode"])
            if current_manifest is None:
                home_fs.publish_new(prior_temp, manifest_path)
            else:
                home_fs.replace(prior_temp, manifest_path)
        for directory in sorted(
            record["directories"],
            key=lambda item: len(Path(item["path"]).parts),
            reverse=True,
        ):
            if directory["created"]:
                home_fs.remove_empty_dir(Path(directory["path"]))
        manifest_temp = record_path.parent / "manifest.tmp"
        if home_fs.exists(manifest_temp):
            if home_fs.read_file(manifest_temp) != expected_manifest:
                raise IncompleteRollbackError("rollback incomplete: manifest temporary changed")
            home_fs.unlink(manifest_temp)
        for operation in record["operations"]:
            staged = Path(operation["staged"]) if operation["staged"] else None
            if staged is not None and home_fs.exists(staged):
                if not _file_matches(
                    home_fs,
                    staged,
                    operation["expected_fingerprint"],
                    operation["expected_mode"],
                ):
                    raise IncompleteRollbackError(
                        f"rollback incomplete: staged file changed: {staged}"
                    )
                home_fs.unlink(staged)
        for directory in record["directories"]:
            evidence = directory["prestate_evidence"]
            if evidence is not None:
                home_fs.unlink(Path(evidence))
        home_fs.unlink(record_path)
        _prune_transaction_directories(home_fs, record_path.parent, record)
    except IncompleteRollbackError:
        raise
    except OSError as exc:
        raise IncompleteRollbackError("rollback incomplete; recovery evidence retained") from exc


def _prune_transaction_directories(
    home_fs: _HomeFS,
    transaction: Path,
    record: dict[str, Any],
) -> None:
    candidates = {transaction / "rendered", transaction / "backups", transaction}
    candidates.update(
        {
            transaction / "prestate/directories",
            transaction / "prestate",
        }
    )
    for operation in record["operations"]:
        if operation["staged"]:
            current = Path(operation["staged"]).parent
            while current != transaction:
                candidates.add(current)
                current = current.parent
    for path in sorted(candidates, key=lambda item: len(item.parts), reverse=True):
        home_fs.remove_empty_dir(path)


def rollback_manifests(manifests: tuple[DeploymentManifest, ...]) -> None:
    for manifest in reversed(manifests):
        path = _TRANSACTION_PATHS.get(manifest.transaction_id)
        if path is None:
            raise ValueError(f"transaction recovery path is unavailable: {manifest.transaction_id}")
        home, relative, _record, recorded_manifest = _read_validated_record(path)
        if recorded_manifest != manifest:
            raise ValueError("transaction manifest does not match rollback request")
        try:
            with _target_lock(home) as home_fs:
                current = home_fs.read_file(relative)
                record, locked_manifest = _decode_record(
                    current,
                    home=home,
                    transaction_id=manifest.transaction_id,
                )
                if locked_manifest != manifest:
                    raise ValueError("transaction manifest changed before rollback")
                _rollback_record(home_fs, record, relative)
        except IncompleteRollbackError:
            raise
        except OSError as exc:
            raise IncompleteRollbackError(
                f"rollback incomplete; evidence retained at {path}"
            ) from exc
        _TRANSACTION_PATHS.pop(manifest.transaction_id, None)


def recover_transaction(path: Path) -> DeploymentManifest:
    home, relative, _record, manifest = _read_validated_record(path)
    with _target_lock(home) as home_fs:
        content = home_fs.read_file(relative)
        record, manifest = _decode_record(
            content,
            home=home,
            transaction_id=manifest.transaction_id,
        )
        _validate_transaction_evidence(home_fs, record, relative)
        expected_manifest = base64.b64decode(record["manifest_content"], validate=True)
        manifest_path = Path(record["manifest_path"])
        current_manifest = home_fs.read_optional(manifest_path)
        if current_manifest == expected_manifest:
            if record["state"] != "committed":
                record["state"] = "committed"
                home_fs.write_atomic(relative, _record_bytes(record), 0o600)
            _TRANSACTION_PATHS[manifest.transaction_id] = path
            return manifest
        prior_manifest = _prior_manifest_content(record)
        if current_manifest not in {None, prior_manifest}:
            raise PublicationIndeterminateError(
                "manifest publication remains indeterminate; unexpected manifest preserved"
            )
        for operation in record["operations"]:
            destination = Path(operation["destination"])
            if operation["kind"] in {"installed", "adopted"} and not _file_matches(
                home_fs,
                destination,
                operation["expected_fingerprint"],
                operation["expected_mode"],
            ):
                raise PublicationIndeterminateError(
                    f"transaction output changed; evidence retained: {destination}"
                )
            if operation["kind"] == "removal" and home_fs.exists(destination):
                raise PublicationIndeterminateError(
                    f"transaction removal is incomplete; evidence retained: {destination}"
                )
        recovery_temp = relative.parent / "recovery-manifest.tmp"
        home_fs.write_file(recovery_temp, expected_manifest, 0o600)
        if current_manifest is None:
            home_fs.publish_new(recovery_temp, manifest_path)
        else:
            home_fs.replace(recovery_temp, manifest_path)
        record["state"] = "committed"
        home_fs.write_atomic(relative, _record_bytes(record), 0o600)
        _TRANSACTION_PATHS[manifest.transaction_id] = path
        return manifest


def audit_provider_plans(plans: tuple[ProviderPlan, ...]) -> DeploymentAudit:
    groups = _validate_and_group(plans)
    if not groups:
        return DeploymentAudit(target_id="none", matches=True)
    if len(groups) != 1:
        raise ValueError("audit requires plans for exactly one target")
    group = groups[0]
    target = group.target
    files = group.files
    missing: list[str] = []
    changed: list[str] = []
    unexpected: set[str] = set()
    duplicates: list[str] = []
    validation_errors: list[str] = []
    try:
        home_fs = _HomeFS(target.home, _open_absolute_directory(target.home, create=False))
    except FileNotFoundError:
        return DeploymentAudit(
            target_id=target.id,
            matches=False,
            missing=tuple(item.path.as_posix() for item in files),
        )
    with home_fs:
        manifest_content = home_fs.read_optional(_manifest_path(target))
        if manifest_content is None:
            validation_errors.append("deployment manifest is missing")
        else:
            try:
                manifest_data = _validated_manifest_data(manifest_content, target=target)
                if manifest_data["source_revision"] != group.source_revision:
                    validation_errors.append(
                        "deployment manifest source revision does not match plan"
                    )
                if manifest_data["provider_ids"] != list(group.provider_ids):
                    validation_errors.append("deployment manifest providers do not match plan")
                expected_files = {
                    item.path.as_posix(): (item.fingerprint, item.mode) for item in files
                }
                recorded_files = {
                    item["path"]: (item["fingerprint"], item["mode"])
                    for item in manifest_data["files"]
                }
                if recorded_files != expected_files:
                    validation_errors.append("deployment manifest files do not match plan")
                expected_directory_paths = {
                    item.path.as_posix() for item in _directories(files)
                }
                installed_directories: dict[str, int] = {}
                for directory_path in expected_directory_paths:
                    try:
                        directory_stat = home_fs.stat(Path(directory_path))
                    except OSError:
                        continue
                    if stat.S_ISDIR(directory_stat.st_mode):
                        installed_directories[directory_path] = stat.S_IMODE(
                            directory_stat.st_mode
                        )
                recorded_directories = {
                    item["path"]: item["mode"]
                    for item in manifest_data["directories"]
                }
                if (
                    set(installed_directories) != expected_directory_paths
                    or recorded_directories != installed_directories
                ):
                    validation_errors.append(
                        "deployment manifest directories do not match installed directories"
                    )
            except ValueError as exc:
                validation_errors.append(str(exc))
        names: dict[str, list[str]] = {}
        for item in files:
            display = item.path.as_posix()
            try:
                item_stat = home_fs.stat(item.path)
                content = home_fs.read_file(item.path)
            except FileNotFoundError:
                missing.append(display)
                continue
            except OSError:
                changed.append(display)
                continue
            if (
                not stat.S_ISREG(item_stat.st_mode)
                or stat.S_IMODE(item_stat.st_mode) != item.mode
                or content != item.content
            ):
                changed.append(display)
            if item.path.name == "SKILL.md":
                name = _frontmatter_name(content)
                if name is not None:
                    names.setdefault(name, []).append(display)
        planned_paths = {item.path for item in files}
        top_roots = {Path(item.path.parts[0]) for item in files}
        for root in sorted(top_roots, key=str):
            try:
                installed = home_fs.scan_tree(root)
            except (FileNotFoundError, OSError):
                continue
            unexpected.update(
                (root / item).as_posix()
                for item in installed
                if root / item not in planned_paths
            )
        duplicates = [
            f"{name}: {', '.join(sorted(paths))}"
            for name, paths in sorted(names.items())
            if len(paths) > 1
        ]
    return DeploymentAudit(
        target_id=target.id,
        matches=not (missing or changed or unexpected or duplicates or validation_errors),
        missing=tuple(sorted(missing)),
        changed=tuple(sorted(changed)),
        unexpected=tuple(sorted(unexpected)),
        duplicates=tuple(duplicates),
        validation_errors=tuple(validation_errors),
    )
