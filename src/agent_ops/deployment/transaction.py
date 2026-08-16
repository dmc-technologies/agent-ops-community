from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by import on non-POSIX systems
    fcntl = None  # type: ignore[assignment]

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
_TRANSACTION_SCHEMA_VERSION = 3
_DIRECTORY_EVIDENCE_SCHEMA_VERSION = 1
_OWNERSHIP_MANIFEST_MODE = 0o600
_METADATA = Path(".agentops/deployment")
_LOCK_NAME = ".agentops-deployment.lock"
_TRANSACTION_PATHS: dict[str, Path] = {}
_POSIX_SUPPORTED = os.name == "posix" and fcntl is not None
_CANONICAL_HOME_IDENTITY_ERROR = "deployment canonical home identity changed"


class PublicationIndeterminateError(OSError):
    """Manifest publication could not be classified safely."""


class IncompleteRollbackError(OSError):
    """Rollback stopped to preserve changed content or required evidence."""


class UnsupportedPlatformError(RuntimeError):
    """Descriptor-bound deployment transactions require a POSIX platform."""


def _require_supported_platform() -> None:
    if not _POSIX_SUPPORTED:
        raise UnsupportedPlatformError(
            "deployment transactions require a supported POSIX platform"
        )


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


def _canonical_relative_text(value: object, *, label: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"unsafe noncanonical {label} path")
    path = _safe_relative(Path(value))
    if path.as_posix() != value:
        raise ValueError(f"unsafe noncanonical {label} path")
    return path


def _absolute_home(path: Path, *, base: Path | None = None) -> Path:
    raw = os.fspath(path.expanduser())
    if raw in ("", "."):
        raise ValueError(f"unsafe target home: {path}")
    if not os.path.isabs(raw):
        if base is None:
            base = Path.cwd()
        raw = os.path.join(os.fspath(base), raw)
    home = Path(os.path.normcase(os.path.abspath(raw)))
    if home == Path("/"):
        raise ValueError(f"unsafe target home: {path}")
    return home


def _create_or_open_directory_at(parent: int, name: str, *, mode: int) -> int:
    with suppress(FileExistsError):
        os.mkdir(name, 0o700, dir_fd=parent)
    descriptor = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent,
    )
    try:
        opened = os.fstat(descriptor)
        canonical = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not stat.S_ISDIR(canonical.st_mode)
            or (opened.st_dev, opened.st_ino) != (canonical.st_dev, canonical.st_ino)
        ):
            raise ValueError(f"directory creation identity changed: {name}")
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.fsync(parent)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


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
                child = _create_or_open_directory_at(
                    descriptor,
                    part,
                    mode=mode,
                )
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
        return _create_or_open_directory_at(
            parent,
            name,
            mode=mode,
        )


class _HomeFS:
    def __init__(
        self,
        home: Path,
        descriptor: int,
        *,
        home_identity: tuple[int, int] | None = None,
        lock_name: str | None = None,
        lock_identity: tuple[int, int] | None = None,
        lock_descriptor: int | None = None,
    ) -> None:
        self.home = home
        self.descriptor = descriptor
        self._home_identity = home_identity
        self._lock_name = lock_name
        self._lock_identity = lock_identity
        self._lock_descriptor = lock_descriptor

    def __enter__(self) -> _HomeFS:
        return self

    def __exit__(self, *_args: object) -> None:
        os.close(self.descriptor)

    def verify_lock_identity(self) -> None:
        descriptors = (self.descriptor, self._lock_descriptor)
        if any(
            descriptor is not None and os.get_inheritable(descriptor)
            for descriptor in descriptors
        ):
            raise RuntimeError(
                "retained deployment authority descriptors must be close-on-exec"
            )
        if self._home_identity is None:
            return
        canonical_descriptor: int | None = None
        try:
            canonical_descriptor = _open_absolute_directory(self.home, create=False)
            home_item = os.fstat(canonical_descriptor)
            retained_home = os.fstat(self.descriptor)
        except OSError as exc:
            raise ValueError(_CANONICAL_HOME_IDENTITY_ERROR) from exc
        finally:
            if canonical_descriptor is not None:
                os.close(canonical_descriptor)
        if (
            not stat.S_ISDIR(home_item.st_mode)
            or (home_item.st_dev, home_item.st_ino) != self._home_identity
            or not stat.S_ISDIR(retained_home.st_mode)
            or (retained_home.st_dev, retained_home.st_ino) != self._home_identity
        ):
            raise ValueError(_CANONICAL_HOME_IDENTITY_ERROR)
        if self._lock_name is None and self._lock_identity is None:
            return
        if self._lock_name is None or self._lock_identity is None:
            raise ValueError("deployment lock identity changed")
        try:
            lock_item = os.stat(
                self._lock_name,
                dir_fd=self.descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise ValueError("deployment lock identity changed") from exc
        if (
            not stat.S_ISREG(lock_item.st_mode)
            or lock_item.st_nlink != 1
            or (lock_item.st_dev, lock_item.st_ino) != self._lock_identity
        ):
            raise ValueError("deployment lock identity changed")

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
        self.verify_lock_identity()
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
            descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=parent,
            )
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

    def matches_exact_file(self, relative: Path, content: bytes, mode: int) -> bool:
        with self.parent(relative) as (parent, leaf):
            descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            try:
                item = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(item.st_mode)
                    or stat.S_IMODE(item.st_mode) != mode
                ):
                    return False
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                return b"".join(chunks) == content
            finally:
                os.close(descriptor)

    def matches_fingerprint_file(
        self,
        relative: Path,
        fingerprint: str,
        mode: int,
    ) -> bool:
        with self.parent(relative) as (parent, leaf):
            descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            try:
                item = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(item.st_mode)
                    or stat.S_IMODE(item.st_mode) != mode
                ):
                    return False
                fingerprint_state = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    fingerprint_state.update(chunk)
                return fingerprint_state.hexdigest() == fingerprint
            finally:
                os.close(descriptor)

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
        self.verify_lock_identity()
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
        self.verify_lock_identity()
        with self.parent(relative) as (parent, leaf):
            item = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(item.st_mode):
                raise OSError(f"refusing to unlink non-regular file: {relative}")
            os.unlink(leaf, dir_fd=parent)
            os.fsync(parent)

    def remove_empty_dir(self, relative: Path) -> bool:
        self.verify_lock_identity()
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
        self.verify_lock_identity()
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
        self.verify_lock_identity()
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


_GROUP_HOME_LOCKS: ContextVar[dict[Path, _HomeFS] | None] = ContextVar(
    "deployment_group_home_locks",
    default=None,
)


@contextmanager
def _target_lock(home: Path) -> Iterator[_HomeFS]:
    home = _absolute_home(home)
    grouped = _GROUP_HOME_LOCKS.get()
    if grouped is not None and home in grouped:
        yield grouped[home]
        return
    parent = _open_absolute_directory(home.parent, create=True)
    try:
        assert fcntl is not None
        home_descriptor = _open_directory_at(
            parent,
            home.name,
            create=True,
            mode=0o700,
        )
        try:
            os.set_inheritable(home_descriptor, False)
            home_stat = os.fstat(home_descriptor)
            fcntl.flock(home_descriptor, fcntl.LOCK_EX)
            canonical_home = os.stat(
                home.name,
                dir_fd=parent,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(canonical_home.st_mode)
                or (canonical_home.st_dev, canonical_home.st_ino)
                != (home_stat.st_dev, home_stat.st_ino)
            ):
                raise ValueError("deployment home identity changed")
            lock_name = _LOCK_NAME
            lock = os.open(
                lock_name,
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=home_descriptor,
            )
            try:
                os.set_inheritable(lock, False)
                lock_stat = os.fstat(lock)
                if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
                    raise ValueError("deployment lock is not a regular file")
                os.fchmod(lock, 0o600)
                fcntl.flock(lock, fcntl.LOCK_EX)
                canonical = os.stat(
                    lock_name,
                    dir_fd=home_descriptor,
                    follow_symlinks=False,
                )
                if (canonical.st_dev, canonical.st_ino) != (
                    lock_stat.st_dev,
                    lock_stat.st_ino,
                ):
                    raise ValueError("deployment lock identity changed")
                yield _HomeFS(
                    home,
                    home_descriptor,
                    home_identity=(home_stat.st_dev, home_stat.st_ino),
                    lock_name=lock_name,
                    lock_identity=(lock_stat.st_dev, lock_stat.st_ino),
                    lock_descriptor=lock,
                )
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                os.close(lock)
        finally:
            fcntl.flock(home_descriptor, fcntl.LOCK_UN)
            os.close(home_descriptor)
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


def _strict_json_loads(content: bytes, *, label: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in {label}: {key}")
            result[key] = value
        return result

    try:
        return json.loads(content.decode(), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label} JSON: {exc}") from exc


def _require_exact_keys(
    value: dict[str, Any],
    allowed: set[str],
    *,
    label: str,
) -> None:
    keys = set(value)
    if keys != allowed:
        unknown = sorted(keys - allowed)
        missing = sorted(allowed - keys)
        if unknown:
            raise ValueError(f"unknown {label} member: {unknown[0]}")
        raise ValueError(f"missing {label} member: {missing[0]}")


def _validated_manifest_data(content: bytes, *, target: TargetSpec) -> dict[str, Any]:
    data = _strict_json_loads(content, label="deployment manifest")
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != _SCHEMA_VERSION
    ):
        raise ValueError("invalid deployment manifest schema")
    _require_exact_keys(
        data,
        {
            "schema_version",
            "target_id",
            "framework",
            "source_revision",
            "provider_ids",
            "transaction_id",
            "files",
            "directories",
        },
        label="deployment manifest",
    )
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
        _require_exact_keys(
            item,
            {"path", "fingerprint", "mode"},
            label="deployment manifest file",
        )
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
        _require_exact_keys(
            item,
            {"path", "mode"},
            label="deployment manifest directory",
        )
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
    file_order = [item["path"] for item in data["files"]]
    directory_order = [item["path"] for item in data["directories"]]
    if file_order != sorted(file_order):
        raise ValueError("deployment manifest files are not in canonical order")
    if directory_order != sorted(directory_order):
        raise ValueError("deployment manifest directories are not in canonical order")
    return data


def _valid_permission_mode(value: object) -> bool:
    return type(value) is int and 0 <= value <= 0o777


def _valid_file_mode(value: object) -> bool:
    return _valid_permission_mode(value) and bool(value & 0o400)


def _valid_directory_mode(value: object) -> bool:
    return _valid_permission_mode(value) and value & 0o700 == 0o700


def _valid_ownership_manifest_mode(value: object) -> bool:
    return type(value) is int and value == _OWNERSHIP_MANIFEST_MODE


def _validate_ownership_manifest_stat(item: os.stat_result) -> None:
    if not stat.S_ISREG(item.st_mode):
        raise ValueError("ownership manifest is not a regular file")
    if not _valid_ownership_manifest_mode(stat.S_IMODE(item.st_mode)):
        raise ValueError("ownership manifest mode must be 0o600")


def _read_ownership_manifest_for_audit(
    home_fs: _HomeFS,
    manifest_path: Path,
) -> tuple[bytes | None, str | None]:
    home_fs.verify_lock_identity()
    try:
        manifest_stat = home_fs.stat(manifest_path)
    except FileNotFoundError:
        return None, "deployment manifest is missing"
    except OSError:
        return None, "ownership manifest metadata could not be read safely"
    if stat.S_ISLNK(manifest_stat.st_mode):
        return None, "ownership manifest is a symbolic link"
    if not stat.S_ISREG(manifest_stat.st_mode):
        return None, "ownership manifest is not a regular file"
    try:
        _validate_ownership_manifest_stat(manifest_stat)
    except ValueError as exc:
        return None, str(exc)
    try:
        return home_fs.read_file(manifest_path), None
    except FileNotFoundError:
        return None, "ownership manifest disappeared during audit"
    except OSError:
        return None, "ownership manifest could not be read safely"


def _validated_manifest_path(value: str, *, kind: str) -> Path:
    try:
        return _canonical_relative_text(value, label=f"deployment manifest {kind}")
    except ValueError as exc:
        raise ValueError(
            f"invalid deployment manifest non-normalized {kind} path"
        ) from exc


def _frontmatter_name(content: bytes) -> str | None:
    try:
        lines = content.decode().splitlines()
    except UnicodeDecodeError:
        return None
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        data = yaml.load("\n".join(lines[1:end]), Loader=yaml.BaseLoader)
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
    home_targets: dict[Path, str] = {}
    preflight_cwd = Path.cwd()
    for plan in plans:
        home = _absolute_home(plan.target.home, base=preflight_cwd)
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
        prior_target = home_targets.setdefault(home, plan.target.id)
        if prior_target != plan.target.id:
            raise ValueError("different target IDs resolve to the same target home")
        files = groups.setdefault(key, {})
        providers.setdefault(key, set()).add(plan.provider_id)
        planned_removals = removals.setdefault(key, set())
        for item in plan.files:
            path = _safe_relative(item.path)
            if path.parts[0] in {".agentops", _LOCK_NAME}:
                raise ValueError(f"planned path overlaps reserved metadata: {path}")
            if not isinstance(item.content, bytes) or not _valid_file_mode(item.mode):
                raise ValueError(f"invalid planned file: {path}")
            prior = files.get(path)
            if prior is not None:
                if prior.content != item.content or prior.mode != item.mode:
                    raise ValueError(f"conflicting duplicate planned destination: {path}")
                continue
            files[path] = item
        for removal in plan.removals:
            removal_path = _safe_relative(removal)
            if removal_path.parts[0] in {".agentops", _LOCK_NAME}:
                raise ValueError(
                    f"planned removal overlaps reserved metadata: {removal_path}"
                )
            planned_removals.add(removal_path)
    for key, files in groups.items():
        file_paths = sorted(files, key=str)
        removal_paths = sorted(removals[key], key=str)
        for index, path in enumerate(file_paths):
            if any(
                path in other.parents or other in path.parents
                for other in file_paths[index + 1 :]
            ):
                raise ValueError(f"invalid plan topology at file destination: {path}")
        for index, path in enumerate(removal_paths):
            if any(
                path == other or path in other.parents or other in path.parents
                for other in removal_paths[index + 1 :]
            ):
                raise ValueError(f"invalid plan topology at removal destination: {path}")
        for file_path in file_paths:
            if any(
                file_path == removal
                or file_path in removal.parents
                or removal in file_path.parents
                for removal in removal_paths
            ):
                raise ValueError(f"invalid plan topology across file and removal: {file_path}")
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
    if not _valid_directory_mode(mode):
        raise ValueError(f"invalid directory mode: {path}")
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
    if not _valid_directory_mode(mode):
        raise ValueError(f"invalid directory evidence mode: {directory}")
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
        return home_fs.matches_fingerprint_file(path, fingerprint, mode)
    except OSError:
        return False


def _verify_manifest_files_and_directories(
    home_fs: _HomeFS,
    manifest_data: dict[str, Any],
    *,
    error_type: type[Exception],
    context: str,
) -> None:
    for item in manifest_data["files"]:
        path = Path(item["path"])
        if not _file_matches(home_fs, path, item["fingerprint"], item["mode"]):
            raise error_type(f"{context} managed file changed: {path}")
    for item in manifest_data["directories"]:
        path = Path(item["path"])
        try:
            directory_stat = home_fs.stat(path)
        except OSError as exc:
            raise error_type(f"{context} owned directory changed: {path}") from exc
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_IMODE(directory_stat.st_mode) != item["mode"]
        ):
            raise error_type(f"{context} owned directory changed: {path}")


def _verify_planned_final_state(
    home_fs: _HomeFS,
    manifest: DeploymentManifest,
    removals: tuple[Path, ...],
) -> None:
    _verify_manifest_files_and_directories(
        home_fs,
        _manifest_to_dict(manifest),
        error_type=ValueError,
        context="final deployment",
    )
    for path in removals:
        if home_fs.exists(path):
            raise ValueError(f"final deployment removal still exists: {path}")


def _verify_recovery_final_state(
    home_fs: _HomeFS,
    manifest: DeploymentManifest,
    record: dict[str, Any],
) -> None:
    removals = tuple(
        Path(operation["destination"])
        for operation in record["operations"]
        if operation["kind"] == "removal"
    )
    try:
        home_fs.verify_lock_identity()
        _verify_planned_final_state(home_fs, manifest, removals)
    except (OSError, ValueError) as exc:
        raise PublicationIndeterminateError(
            "recovery final state is invalid; evidence retained"
        ) from exc


def _verify_prior_prestate(
    home_fs: _HomeFS,
    prior_data: dict[str, Any],
    operations: list[dict[str, Any]],
) -> None:
    operations_by_path = {
        Path(operation["destination"]): operation for operation in operations
    }
    for item in prior_data["files"]:
        path = Path(item["path"])
        operation = operations_by_path.get(path)
        if operation is not None and operation["backup"] is not None:
            continue
        if not _file_matches(home_fs, path, item["fingerprint"], item["mode"]):
            raise IncompleteRollbackError(f"prior managed file changed: {path}")
    for item in prior_data["directories"]:
        path = Path(item["path"])
        try:
            directory_stat = home_fs.stat(path)
        except OSError as exc:
            raise IncompleteRollbackError(f"prior owned directory changed: {path}") from exc
        if (
            not stat.S_ISDIR(directory_stat.st_mode)
            or stat.S_IMODE(directory_stat.st_mode) != item["mode"]
        ):
            raise IncompleteRollbackError(f"prior owned directory changed: {path}")


def _verify_completed_rollback(
    home_fs: _HomeFS,
    record: dict[str, Any],
    prior_data: dict[str, Any] | None,
    prior_manifest: bytes | None,
) -> None:
    manifest_path = Path(record["manifest_path"])
    if prior_data is None:
        if home_fs.exists(manifest_path):
            raise IncompleteRollbackError(
                "rollback completion does not match absent prior manifest"
            )
        for operation in record["operations"]:
            destination = Path(operation["destination"])
            if operation["kind"] == "installed" and home_fs.exists(destination):
                raise IncompleteRollbackError(
                    f"rollback completion has installed destination: {destination}"
                )
            if operation["kind"] == "adopted" and not _file_matches(
                home_fs,
                destination,
                operation["expected_fingerprint"],
                operation["expected_mode"],
            ):
                raise IncompleteRollbackError(
                    f"rollback completion changed adopted destination: {destination}"
                )
        for directory in record["directories"]:
            path = Path(directory["path"])
            if directory["created"] and home_fs.exists(path):
                raise IncompleteRollbackError(
                    f"rollback completion has created owned directory: {path}"
                )
        return
    assert prior_manifest is not None
    try:
        manifest_matches = home_fs.matches_exact_file(
            manifest_path,
            prior_manifest,
            _OWNERSHIP_MANIFEST_MODE,
        )
    except OSError:
        manifest_matches = False
    if not manifest_matches:
        raise IncompleteRollbackError("rollback completion prior manifest changed")
    _verify_manifest_files_and_directories(
        home_fs,
        prior_data,
        error_type=IncompleteRollbackError,
        context="rollback completion prior",
    )


def _publication_outcome(
    home_fs: _HomeFS,
    manifest_temp: Path,
    manifest_path: Path,
    expected_content: bytes,
) -> str:
    try:
        home_fs.verify_lock_identity()
        if home_fs.exists(manifest_temp):
            return "not-published"
        published = home_fs.matches_exact_file(
            manifest_path,
            expected_content,
            _OWNERSHIP_MANIFEST_MODE,
        )
    except (OSError, ValueError):
        return "indeterminate"
    if published:
        return "committed"
    return "indeterminate"


def install_provider_plans(
    plans: tuple[ProviderPlan, ...],
) -> tuple[DeploymentManifest, ...]:
    _require_supported_platform()
    manifests: list[DeploymentManifest] = []
    groups = _validate_and_group(plans)
    with _locked_provider_plan_targets(plans):
        try:
            _install_provider_plan_groups(groups, manifests)
        except BaseException as install_error:
            if not manifests:
                raise
            try:
                rollback_manifests(tuple(manifests))
            except BaseException as rollback_error:
                if not isinstance(rollback_error, Exception):
                    rollback_error.add_note(
                        "grouped deployment recovery incomplete after deployment "
                        f"failure: {install_error}; transaction evidence retained"
                    )
                    raise
                if not isinstance(install_error, Exception):
                    install_error.add_note(
                        "grouped rollback failed; recovery evidence retained for "
                        "earlier targets"
                    )
                    raise install_error from rollback_error
                raise IncompleteRollbackError(
                    f"installation failed: {install_error}; grouped rollback "
                    "incomplete; recovery evidence retained for earlier targets"
                ) from rollback_error
            raise
    return tuple(manifests)


@contextmanager
def _locked_provider_plan_targets(
    plans: tuple[ProviderPlan, ...],
) -> Iterator[None]:
    """Hold one sorted home-lock set for a caller-defined grouped operation."""
    _require_supported_platform()
    groups = _validate_and_group(plans)
    homes = tuple(_absolute_home(group.target.home) for group in groups)
    active = _GROUP_HOME_LOCKS.get()
    if active is not None:
        if any(home not in active for home in homes):
            raise RuntimeError("active deployment lock set does not cover every target")
        yield
        return
    with ExitStack() as locks:
        grouped_locks = {
            home: locks.enter_context(_target_lock(home)) for home in homes
        }
        token = _GROUP_HOME_LOCKS.set(grouped_locks)
        try:
            yield
        finally:
            _GROUP_HOME_LOCKS.reset(token)


def _verify_locked_provider_plan_targets(plans: tuple[ProviderPlan, ...]) -> None:
    """Revalidate every retained home and lock identity for grouped success."""
    groups = _validate_and_group(plans)
    active = _GROUP_HOME_LOCKS.get()
    if active is None:
        raise RuntimeError("deployment target lock set is not active")
    homes = sorted(
        {_absolute_home(group.target.home) for group in groups},
        key=str,
    )
    for home in homes:
        try:
            home_fs = active[home]
        except KeyError as error:
            raise RuntimeError(
                "active deployment lock set does not cover every target"
            ) from error
        home_fs.verify_lock_identity()


def _install_provider_plan_groups(
    groups: tuple[_PlanGroup, ...],
    manifests: list[DeploymentManifest],
) -> None:
    for group in groups:
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
            _validate_group_current_state(home_fs, group)
            manifest_path = _manifest_path(target)
            prior_manifest_content = home_fs.read_optional(manifest_path)
            prior_data = None
            prior_manifest_mode = None
            if prior_manifest_content is not None:
                prior_manifest_stat = home_fs.stat(manifest_path)
                _validate_ownership_manifest_stat(prior_manifest_stat)
                prior_manifest_mode = stat.S_IMODE(prior_manifest_stat.st_mode)
                prior_data = _validated_manifest_data(prior_manifest_content, target=target)
                _verify_manifest_files_and_directories(
                    home_fs,
                    prior_data,
                    error_type=ValueError,
                    context="prior",
                )
            managed = {
                Path(item["path"]): (item["fingerprint"], item["mode"])
                for item in prior_data["files"]
            } if prior_data is not None else {}
            for directory in manifest.directories:
                if not home_fs.exists(directory.path):
                    continue
                directory_stat = home_fs.stat(directory.path)
                if stat.S_ISLNK(directory_stat.st_mode):
                    raise OSError(f"managed directory is a symbolic link: {directory.path}")
                if not stat.S_ISDIR(directory_stat.st_mode):
                    raise ValueError(f"managed directory is not a directory: {directory.path}")
                if not _valid_directory_mode(stat.S_IMODE(directory_stat.st_mode)):
                    raise ValueError(f"invalid managed directory mode: {directory.path}")
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
            for removal in group.removals:
                prior = managed.get(removal)
                if prior is None:
                    if home_fs.exists(removal):
                        raise ValueError(f"refusing to remove unmanaged destination: {removal}")
                    continue
                index = len(operations)
                operation = {
                    "kind": "removal",
                    "index": index,
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
                home_fs.write_file(
                    manifest_temp,
                    manifest_content,
                    _OWNERSHIP_MANIFEST_MODE,
                )
                try:
                    _before_manifest_replace(
                        home_fs,
                        record_path,
                        manifest_temp,
                        manifest_path,
                    )
                    home_fs.verify_lock_identity()
                    if not home_fs.matches_exact_file(
                        manifest_temp,
                        manifest_content,
                        _OWNERSHIP_MANIFEST_MODE,
                    ):
                        raise ValueError("ownership manifest candidate changed before publication")
                    _verify_planned_final_state(home_fs, manifest, group.removals)
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
                        _TRANSACTION_PATHS[transaction_id] = target.home / record_path
                        try:
                            home_fs.write_atomic(record_path, _record_bytes(record), 0o600)
                        except BaseException as evidence_error:
                            if not isinstance(publication_error, Exception):
                                publication_error.add_note(
                                    "publication indeterminate; prepared recovery evidence "
                                    "retained on the pinned deployment home"
                                )
                                raise publication_error from evidence_error
                            raise PublicationIndeterminateError(
                                "manifest publication is indeterminate; prepared recovery "
                                "evidence retained on the pinned deployment home"
                            ) from evidence_error
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
                home_fs.verify_lock_identity()
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


def _validate_group_current_state(home_fs: _HomeFS, group: _PlanGroup) -> None:
    manifest_path = _manifest_path(group.target)
    prior_content = home_fs.read_optional(manifest_path)
    prior_data = None
    if prior_content is not None:
        _validate_ownership_manifest_stat(home_fs.stat(manifest_path))
        prior_data = _validated_manifest_data(prior_content, target=group.target)
        _verify_manifest_files_and_directories(
            home_fs,
            prior_data,
            error_type=ValueError,
            context="prior",
        )
    managed = (
        {
            Path(item["path"]): (item["fingerprint"], item["mode"])
            for item in prior_data["files"]
        }
        if prior_data is not None
        else {}
    )
    for directory in _directories(group.files):
        if not home_fs.exists(directory.path):
            continue
        item = home_fs.stat(directory.path)
        if stat.S_ISLNK(item.st_mode):
            raise OSError(f"managed directory is a symbolic link: {directory.path}")
        if not stat.S_ISDIR(item.st_mode):
            raise ValueError(f"managed directory is not a directory: {directory.path}")
        if not _valid_directory_mode(stat.S_IMODE(item.st_mode)):
            raise ValueError(f"invalid managed directory mode: {directory.path}")
    for item in group.files:
        if not home_fs.exists(item.path):
            continue
        installed_stat = home_fs.stat(item.path)
        if stat.S_ISLNK(installed_stat.st_mode):
            raise ValueError(f"destination is a symbolic link: {item.path}")
        if not stat.S_ISREG(installed_stat.st_mode):
            raise ValueError(f"destination is not a regular file: {item.path}")
        installed = home_fs.read_file(item.path)
        installed_mode = stat.S_IMODE(installed_stat.st_mode)
        prior = managed.get(item.path)
        if prior is None:
            if installed != item.content or installed_mode != item.mode:
                raise ValueError(f"unmanaged destination conflicts with plan: {item.path}")
        elif _fingerprint(installed) != prior[0] or installed_mode != prior[1]:
            raise ValueError(f"managed destination changed: {item.path}")
    for removal in group.removals:
        prior = managed.get(removal)
        if prior is None:
            if home_fs.exists(removal):
                raise ValueError(f"refusing to remove unmanaged destination: {removal}")
            continue
        if not home_fs.exists(removal):
            continue
        item = home_fs.stat(removal)
        content = home_fs.read_file(removal)
        if (
            not stat.S_ISREG(item.st_mode)
            or _fingerprint(content) != prior[0]
            or stat.S_IMODE(item.st_mode) != prior[1]
        ):
            raise ValueError(f"managed destination changed: {removal}")


def _preflight_provider_plans_read_only(plans: tuple[ProviderPlan, ...]) -> None:
    """Validate every live-apply precondition without creating target state."""
    _require_supported_platform()
    for group in _validate_and_group(plans):
        try:
            descriptor = _open_absolute_directory(group.target.home, create=False)
        except FileNotFoundError:
            continue
        opened = os.fstat(descriptor)
        home_fs = _HomeFS(
            group.target.home,
            descriptor,
            home_identity=(opened.st_dev, opened.st_ino),
        )
        with home_fs:
            lock_path = Path(_LOCK_NAME)
            if home_fs.exists(lock_path):
                lock_stat = home_fs.stat(lock_path)
                if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
                    raise ValueError("deployment lock is not a regular single-link file")
            _validate_group_current_state(home_fs, group)


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
    record = _strict_json_loads(content, label="transaction record")
    if (
        not isinstance(record, dict)
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != _TRANSACTION_SCHEMA_VERSION
        or record.get("state")
        not in {"prepared", "committed", "indeterminate", "rolled-back"}
        or not isinstance(record.get("manifest"), dict)
    ):
        raise ValueError("invalid transaction record schema")
    _require_exact_keys(
        record,
        {
            "schema_version",
            "state",
            "manifest",
            "manifest_path",
            "manifest_content",
            "prior_manifest_content",
            "prior_manifest_mode",
            "directories",
            "operations",
        },
        label="transaction record",
    )
    target, manifest = _manifest_from_data(record["manifest"], home=home)
    if manifest.transaction_id != transaction_id:
        raise ValueError("invalid transaction record identifier")
    transaction = _METADATA / "transactions" / transaction_id
    manifest_path = _canonical_relative_text(
        record.get("manifest_path"),
        label="transaction manifest",
    )
    if manifest_path != _manifest_path(target):
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
        if not _valid_ownership_manifest_mode(record.get("prior_manifest_mode")):
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
        _require_exact_keys(
            operation,
            {
                "kind",
                "destination",
                "staged",
                "backup",
                "expected_fingerprint",
                "expected_mode",
                "prior_fingerprint",
                "prior_mode",
                "prior_exists",
                "index",
            },
            label="transaction operation",
        )
        destination = _canonical_relative_text(
            operation.get("destination"),
            label="transaction destination",
        )
        if destination in destinations:
            raise ValueError("invalid duplicate transaction destination")
        destinations.add(destination)
        operation_index = operation.get("index")
        if type(operation_index) is not int or operation_index < 0:
            raise ValueError("invalid transaction operation index")
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
        staged_path = (
            _canonical_relative_text(staged, label="transaction staged")
            if staged is not None
            else None
        )
        expected_staged = (
            transaction / "rendered" / destination
            if operation["kind"] == "installed"
            else None
        )
        if staged_path != expected_staged:
            raise ValueError("unsafe transaction staged path")
        backup = operation.get("backup")
        if backup is not None:
            backup_path = _canonical_relative_text(
                backup,
                label="transaction backup",
            )
            if backup_path in backup_paths:
                raise ValueError("invalid duplicate transaction backup")
            if backup_path != transaction / "backups" / f"{operation_index:04d}":
                raise ValueError("unsafe transaction backup path")
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
        _require_exact_keys(
            directory,
            {"path", "created", "mode", "prestate_evidence"},
            label="transaction directories do not match manifest",
        )
        directory_path = _canonical_relative_text(
            directory["path"],
            label="transaction directory",
        )
        if directory_path in directory_paths:
            raise ValueError("transaction directories do not match manifest")
        directory_paths.add(directory_path)
        evidence = directory.get("prestate_evidence")
        evidence_path = (
            _canonical_relative_text(
                evidence,
                label="transaction directory evidence",
            )
            if evidence is not None
            else None
        )
        expected_evidence = _directory_evidence_path(transaction, directory_path)
        if (
            directory["created"]
            and evidence_path is not None
            or not directory["created"]
            and evidence_path != expected_evidence
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
    if any(
        type(operation["index"]) is not int or operation["index"] != index
        for index, operation in enumerate(operations)
    ):
        raise ValueError("transaction operations are not in canonical order")
    operation_order = [
        (operation["kind"] == "removal", operation["destination"])
        for operation in operations
    ]
    if operation_order != sorted(operation_order):
        raise ValueError("transaction operations are not in canonical order")
    if [item["path"] for item in directories] != sorted(
        item["path"] for item in directories
    ):
        raise ValueError("transaction directories are not in canonical order")
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
    allow_missing: bool = False,
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
    if missing_backups and not allow_missing:
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
    if actual_directories - expected_directories or (
        not allow_missing and expected_directories - actual_directories
    ):
        raise ValueError("transaction directory pre-state evidence is not one-to-one")
    for directory in record["directories"]:
        evidence_text = directory["prestate_evidence"]
        if evidence_text is None:
            continue
        evidence_path = Path(evidence_text)
        if evidence_path not in actual_directories:
            continue
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


def _rollback_link_path(record_path: Path, operation: dict[str, Any]) -> Path:
    return record_path.parent / "rollback" / f"{operation['index']:04d}"


def _rollback_is_complete(
    home_fs: _HomeFS,
    record: dict[str, Any],
    prior_data: dict[str, Any] | None,
    prior_manifest: bytes | None,
) -> bool:
    try:
        _verify_completed_rollback(home_fs, record, prior_data, prior_manifest)
    except IncompleteRollbackError:
        return False
    return True


def _cleanup_rollback_evidence(
    home_fs: _HomeFS,
    record: dict[str, Any],
    record_path: Path,
    expected_manifest: bytes,
) -> None:
    _validate_transaction_evidence(
        home_fs,
        record,
        record_path,
        missing_backup_error=IncompleteRollbackError,
        allow_missing=True,
    )
    manifest_temp = record_path.parent / "manifest.tmp"
    if home_fs.exists(manifest_temp):
        if not home_fs.matches_exact_file(
            manifest_temp,
            expected_manifest,
            _OWNERSHIP_MANIFEST_MODE,
        ):
            raise IncompleteRollbackError("rollback cleanup manifest temporary changed")
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
                    f"rollback cleanup staged file changed: {staged}"
                )
            home_fs.unlink(staged)
        rollback_link = _rollback_link_path(record_path, operation)
        if home_fs.exists(rollback_link):
            if not _file_matches(
                home_fs,
                rollback_link,
                operation["prior_fingerprint"],
                operation["prior_mode"],
            ):
                raise IncompleteRollbackError(
                    f"rollback cleanup restore link changed: {rollback_link}"
                )
            home_fs.unlink(rollback_link)
        backup = Path(operation["backup"]) if operation["backup"] else None
        if backup is not None and home_fs.exists(backup):
            if not _file_matches(
                home_fs,
                backup,
                operation["prior_fingerprint"],
                operation["prior_mode"],
            ):
                raise IncompleteRollbackError(
                    f"rollback cleanup backup changed: {backup}"
                )
            home_fs.unlink(backup)
    for directory in record["directories"]:
        evidence = directory["prestate_evidence"]
        if evidence is not None and home_fs.exists(Path(evidence)):
            home_fs.unlink(Path(evidence))
    _prune_transaction_directories(home_fs, record_path.parent, record)


def _rollback_record(
    home_fs: _HomeFS,
    record: dict[str, Any],
    record_path: Path,
    *,
    retain_completed: bool = False,
) -> None:
    home_fs.verify_lock_identity()
    prior_manifest = _prior_manifest_content(record)
    prior_manifest_mode = record.get("prior_manifest_mode")
    prior_data: dict[str, Any] | None = None
    if (
        prior_manifest is None
        and prior_manifest_mode is not None
        or prior_manifest is not None
        and not _valid_ownership_manifest_mode(prior_manifest_mode)
    ):
        raise IncompleteRollbackError("rollback incomplete: invalid prior manifest mode")
    if prior_manifest is not None:
        recovery_target = TargetSpec(
            record["manifest"]["target_id"],
            Framework(record["manifest"]["framework"]),
            home_fs.home,
            "recovery",
        )
        prior_data = _validated_manifest_data(prior_manifest, target=recovery_target)
    expected_manifest = base64.b64decode(record["manifest_content"], validate=True)
    if record["state"] == "rolled-back":
        _verify_completed_rollback(home_fs, record, prior_data, prior_manifest)
        try:
            _cleanup_rollback_evidence(
                home_fs,
                record,
                record_path,
                expected_manifest,
            )
        except (OSError, ValueError) as exc:
            raise IncompleteRollbackError(
                "rollback cleanup incomplete; evidence retained"
            ) from exc
        home_fs.verify_lock_identity()
        return
    _validate_transaction_evidence(
        home_fs,
        record,
        record_path,
        missing_backup_error=IncompleteRollbackError,
    )
    manifest_path = Path(record["manifest_path"])
    current_manifest = home_fs.read_optional(manifest_path)
    if current_manifest not in {expected_manifest, prior_manifest, None}:
        raise IncompleteRollbackError("rollback incomplete: deployment manifest changed")
    for operation in record["operations"]:
        destination = Path(operation["destination"])
        backup = Path(operation["backup"]) if operation["backup"] else None
        kind = operation["kind"]
        if kind == "installed" and home_fs.exists(destination):
            matches_expected = _file_matches(
                home_fs,
                destination,
                operation["expected_fingerprint"],
                operation["expected_mode"],
            )
            matches_prior = backup is not None and _file_matches(
                home_fs,
                destination,
                operation["prior_fingerprint"],
                operation["prior_mode"],
            )
            if not matches_expected and not matches_prior:
                raise IncompleteRollbackError(
                    f"rollback incomplete: installed destination changed: {destination}"
                )
        if (
            kind == "removal"
            and home_fs.exists(destination)
            and not _file_matches(
                home_fs,
                destination,
                operation["prior_fingerprint"],
                operation["prior_mode"],
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
    if prior_data is not None:
        _verify_prior_prestate(home_fs, prior_data, record["operations"])
    try:
        if _rollback_is_complete(home_fs, record, prior_data, prior_manifest):
            record["state"] = "rolled-back"
            home_fs.write_atomic(record_path, _record_bytes(record), 0o600)
            _cleanup_rollback_evidence(
                home_fs,
                record,
                record_path,
                expected_manifest,
            )
            if not retain_completed:
                home_fs.unlink(record_path)
                _prune_transaction_directories(home_fs, record_path.parent, record)
            home_fs.verify_lock_identity()
            return
        for operation in reversed(record["operations"]):
            destination = Path(operation["destination"])
            backup = Path(operation["backup"]) if operation["backup"] else None
            if (
                operation["kind"] == "installed"
                and backup is None
                and home_fs.exists(destination)
            ):
                home_fs.unlink(destination)
            if backup is not None and not _file_matches(
                home_fs,
                destination,
                operation["prior_fingerprint"],
                operation["prior_mode"],
            ):
                rollback_link = _rollback_link_path(record_path, operation)
                _ensure_directory(home_fs, rollback_link.parent, 0o700)
                if home_fs.exists(rollback_link):
                    if not _file_matches(
                        home_fs,
                        rollback_link,
                        operation["prior_fingerprint"],
                        operation["prior_mode"],
                    ):
                        raise IncompleteRollbackError(
                            f"rollback incomplete: restore link changed: {rollback_link}"
                        )
                else:
                    home_fs.write_file(
                        rollback_link,
                        home_fs.read_file(backup),
                        operation["prior_mode"],
                    )
                if home_fs.exists(destination):
                    home_fs.replace(rollback_link, destination)
                else:
                    home_fs.publish_new(rollback_link, destination)
        if prior_data is not None:
            _verify_manifest_files_and_directories(
                home_fs,
                prior_data,
                error_type=IncompleteRollbackError,
                context="prior",
            )
        else:
            for operation in record["operations"]:
                if operation["kind"] == "installed" and home_fs.exists(
                    Path(operation["destination"])
                ):
                    raise IncompleteRollbackError(
                        "rollback incomplete: newly installed destination remains"
                    )
        current_manifest = home_fs.read_optional(manifest_path)
        if prior_manifest is None:
            if current_manifest == expected_manifest:
                home_fs.unlink(manifest_path)
        elif current_manifest != prior_manifest:
            prior_temp = record_path.parent / "prior-manifest.tmp"
            home_fs.write_file(prior_temp, prior_manifest, _OWNERSHIP_MANIFEST_MODE)
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
        _verify_completed_rollback(home_fs, record, prior_data, prior_manifest)
        record["state"] = "rolled-back"
        home_fs.write_atomic(record_path, _record_bytes(record), 0o600)
        _cleanup_rollback_evidence(
            home_fs,
            record,
            record_path,
            expected_manifest,
        )
        if not retain_completed:
            home_fs.unlink(record_path)
            _prune_transaction_directories(home_fs, record_path.parent, record)
        home_fs.verify_lock_identity()
    except IncompleteRollbackError:
        raise
    except OSError as exc:
        raise IncompleteRollbackError("rollback incomplete; recovery evidence retained") from exc


def _prune_transaction_directories(
    home_fs: _HomeFS,
    transaction: Path,
    record: dict[str, Any],
) -> None:
    candidates = {
        transaction / "rendered",
        transaction / "backups",
        transaction / "rollback",
        transaction,
    }
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
        if home_fs.exists(path):
            home_fs.remove_empty_dir(path)


def rollback_manifests(manifests: tuple[DeploymentManifest, ...]) -> None:
    _require_supported_platform()
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
                _rollback_record(home_fs, record, relative, retain_completed=True)
        except IncompleteRollbackError:
            raise
        except OSError as exc:
            raise IncompleteRollbackError(
                f"rollback incomplete; evidence retained at {path}"
            ) from exc


def recover_transaction(path: Path) -> DeploymentManifest:
    _require_supported_platform()
    home, relative, _record, manifest = _read_validated_record(path)
    with _target_lock(home) as home_fs:
        content = home_fs.read_file(relative)
        record, manifest = _decode_record(
            content,
            home=home,
            transaction_id=manifest.transaction_id,
        )
        if record["state"] == "rolled-back":
            _rollback_record(home_fs, record, relative, retain_completed=True)
            _TRANSACTION_PATHS[manifest.transaction_id] = path
            home_fs.verify_lock_identity()
            return manifest
        _validate_transaction_evidence(home_fs, record, relative)
        expected_manifest = base64.b64decode(record["manifest_content"], validate=True)
        manifest_path = Path(record["manifest_path"])
        try:
            current_manifest = home_fs.read_optional(manifest_path)
        except OSError as exc:
            raise PublicationIndeterminateError(
                "manifest publication remains indeterminate; "
                "ownership manifest is not a readable regular file"
            ) from exc
        if current_manifest == expected_manifest:
            try:
                manifest_is_exact = home_fs.matches_exact_file(
                    manifest_path,
                    expected_manifest,
                    _OWNERSHIP_MANIFEST_MODE,
                )
            except OSError:
                manifest_is_exact = False
            if not manifest_is_exact:
                raise PublicationIndeterminateError(
                    "manifest publication remains indeterminate; "
                    "ownership manifest type or mode is invalid"
                )
            _verify_recovery_final_state(home_fs, manifest, record)
            if record["state"] != "committed":
                record["state"] = "committed"
                home_fs.write_atomic(relative, _record_bytes(record), 0o600)
            _TRANSACTION_PATHS[manifest.transaction_id] = path
            home_fs.verify_lock_identity()
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
        if not home_fs.exists(recovery_temp):
            home_fs.write_file(
                recovery_temp,
                expected_manifest,
                _OWNERSHIP_MANIFEST_MODE,
            )
        try:
            candidate_is_exact = home_fs.matches_exact_file(
                recovery_temp,
                expected_manifest,
                _OWNERSHIP_MANIFEST_MODE,
            )
        except OSError:
            candidate_is_exact = False
        if not candidate_is_exact:
            raise PublicationIndeterminateError(
                "recovery manifest candidate is invalid; evidence retained"
            )
        _verify_recovery_final_state(home_fs, manifest, record)
        if current_manifest is None:
            home_fs.publish_new(recovery_temp, manifest_path)
        else:
            home_fs.replace(recovery_temp, manifest_path)
        record["state"] = "committed"
        home_fs.write_atomic(relative, _record_bytes(record), 0o600)
        _TRANSACTION_PATHS[manifest.transaction_id] = path
        home_fs.verify_lock_identity()
        return manifest


def audit_provider_plans(plans: tuple[ProviderPlan, ...]) -> DeploymentAudit:
    _require_supported_platform()
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
    active = _GROUP_HOME_LOCKS.get()
    retained_home = (
        active.get(_absolute_home(target.home)) if active is not None else None
    )
    if retained_home is not None:
        try:
            retained_home.verify_lock_identity()
            home_descriptor = os.dup(retained_home.descriptor)
        except (OSError, ValueError):
            return DeploymentAudit(
                target_id=target.id,
                matches=False,
                validation_errors=(_CANONICAL_HOME_IDENTITY_ERROR,),
            )
    else:
        try:
            home_descriptor = _open_absolute_directory(target.home, create=False)
        except FileNotFoundError:
            return DeploymentAudit(
                target_id=target.id,
                matches=False,
                missing=tuple(item.path.as_posix() for item in files),
            )
        except OSError:
            return DeploymentAudit(
                target_id=target.id,
                matches=False,
                validation_errors=(_CANONICAL_HOME_IDENTITY_ERROR,),
            )
    try:
        opened_home = os.fstat(home_descriptor)
    except OSError:
        os.close(home_descriptor)
        return DeploymentAudit(
            target_id=target.id,
            matches=False,
            validation_errors=(_CANONICAL_HOME_IDENTITY_ERROR,),
        )
    except BaseException:
        os.close(home_descriptor)
        raise
    home_fs = _HomeFS(
        target.home,
        home_descriptor,
        home_identity=(
            retained_home._home_identity
            if retained_home is not None
            else (opened_home.st_dev, opened_home.st_ino)
        ),
        lock_name=retained_home._lock_name if retained_home is not None else None,
        lock_identity=(
            retained_home._lock_identity if retained_home is not None else None
        ),
    )
    with home_fs:
        manifest_path = _manifest_path(target)
        try:
            manifest_content, manifest_error = _read_ownership_manifest_for_audit(
                home_fs,
                manifest_path,
            )
        except (OSError, ValueError):
            return DeploymentAudit(
                target_id=target.id,
                matches=False,
                validation_errors=(_CANONICAL_HOME_IDENTITY_ERROR,),
            )
        if manifest_error is not None:
            validation_errors.append(manifest_error)
        else:
            assert manifest_content is not None
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
        try:
            home_fs.verify_lock_identity()
        except (OSError, ValueError):
            return DeploymentAudit(
                target_id=target.id,
                matches=False,
                validation_errors=(_CANONICAL_HOME_IDENTITY_ERROR,),
            )
    return DeploymentAudit(
        target_id=target.id,
        matches=not (missing or changed or unexpected or duplicates or validation_errors),
        missing=tuple(sorted(missing)),
        changed=tuple(sorted(changed)),
        unexpected=tuple(sorted(unexpected)),
        duplicates=tuple(duplicates),
        validation_errors=tuple(validation_errors),
    )
