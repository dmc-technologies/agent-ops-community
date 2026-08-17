from __future__ import annotations

import base64
import errno
import hashlib
import importlib.util
import json
import marshal
import os
import stat
import sys
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
    LegacyLinkTransition,
    ManifestDirectory,
    ManifestFile,
    PlannedFile,
    ProviderPlan,
    TargetChannelTransition,
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
_TRANSACTION_SCHEMA_VERSION = 7
_LEGACY_TRANSACTION_SCHEMA_VERSION = 3
_LEGACY_OPERATION_CURSOR_SCHEMA_VERSION = 5
_OPERATION_CURSOR_SCHEMA_VERSION = 6
_DIRECTORY_EVIDENCE_SCHEMA_VERSION = 1
_LEGACY_LINK_EVIDENCE_SCHEMA_VERSION = 1
_OWNERSHIP_MANIFEST_MODE = 0o600
_METADATA = Path(".agentops/deployment")
_LOCK_NAME = ".agentops-deployment.lock"
_TRANSACTION_PATHS: dict[str, Path] = {}
_POSIX_SUPPORTED = os.name == "posix" and fcntl is not None
_CANONICAL_HOME_IDENTITY_ERROR = "deployment canonical home identity changed"
_RUNTIME_CACHE_REMOVAL = "runtime-cache-removal"
_REMOVAL_KINDS = frozenset({"removal", _RUNTIME_CACHE_REMOVAL})


class PublicationIndeterminateError(OSError):
    """Manifest publication could not be classified safely."""


class IncompleteRollbackError(OSError):
    """Rollback stopped to preserve changed content or required evidence."""


class UnsupportedPlatformError(RuntimeError):
    """Descriptor-bound deployment transactions require a POSIX platform."""


def _require_supported_platform() -> None:
    if not _POSIX_SUPPORTED:
        raise UnsupportedPlatformError("deployment transactions require a supported POSIX platform")


def _before_manifest_replace(
    _home_fs: _HomeFS,
    _record_path: Path,
    _manifest_temp: Path,
    _manifest_path: Path,
) -> None:
    """Internal fault-injection boundary immediately before publication."""


def _before_committed_record_write(
    _home_fs: _HomeFS,
    _record_path: Path,
    _record: dict[str, Any],
) -> None:
    """Internal fault-injection boundary after publication, before commit evidence."""


def _after_legacy_link_move(
    _home_fs: _HomeFS,
    _destination: Path,
    _retained_entry: Path,
) -> None:
    """Internal fault-injection boundary after retaining the legacy link entry."""


def _before_operation_mutation(
    _home_fs: _HomeFS,
    _record_path: Path,
    _operation: dict[str, Any],
) -> None:
    """Internal fault-injection boundary after durable applying phase."""


def _after_operation_backup(
    _home_fs: _HomeFS,
    _record_path: Path,
    _operation: dict[str, Any],
) -> None:
    """Internal fault-injection boundary after a recorded backup move."""


def _after_runtime_cache_backup_move(
    _home_fs: _HomeFS,
    _destination: Path,
    _backup: Path,
    _operation: dict[str, Any],
) -> None:
    """Internal fault-injection boundary before cache backup authorization."""


def _after_operation_mutation(
    _home_fs: _HomeFS,
    _record_path: Path,
    _operation: dict[str, Any],
) -> None:
    """Internal fault-injection boundary before durable ready phase."""


def _after_preview_status_manifest_open(_home_fs: _HomeFS, _path: Path, _descriptor: int) -> None:
    """Internal fault-injection boundary after the preview manifest is pinned."""


def _after_preview_status_owned_open(_path: Path, _kind: str, _descriptor: int) -> None:
    """Internal fault-injection boundary after an owned preview path is pinned."""


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
            descriptor is not None and os.get_inheritable(descriptor) for descriptor in descriptors
        ):
            raise RuntimeError("retained deployment authority descriptors must be close-on-exec")
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

    def set_mtime_seconds(self, relative: Path, seconds: int) -> None:
        self.verify_lock_identity()
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
                os.utime(
                    descriptor,
                    ns=(item.st_atime_ns, seconds * 1_000_000_000),
                )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

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
                if not stat.S_ISREG(item.st_mode) or stat.S_IMODE(item.st_mode) != mode:
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
                if not stat.S_ISREG(item.st_mode) or stat.S_IMODE(item.st_mode) != mode:
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
        return {path for path, kind in self.scan_entries(relative).items() if kind != "directory"}

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

    def matches_symlink(self, relative: Path, expected_link_text: str) -> bool:
        try:
            with self.parent(relative) as (parent, leaf):
                item = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                return (
                    stat.S_ISLNK(item.st_mode)
                    and os.readlink(leaf, dir_fd=parent) == expected_link_text
                )
        except OSError:
            return False

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

    def move_new(self, source: Path, destination: Path) -> None:
        """Atomically move one entry without replacing an existing destination."""
        self.verify_lock_identity()
        with (
            self.parent(source) as (source_parent, source_leaf),
            self.parent(destination, create=True) as (destination_parent, destination_leaf),
        ):
            _rename_noreplace_at(
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


def _rename_noreplace_at(
    source: str,
    destination: str,
    *,
    source_dir_fd: int,
    destination_dir_fd: int,
) -> None:
    import ctypes

    native, flag = _atomic_noreplace_backend()
    if (
        native(
            source_dir_fd,
            os.fsencode(source),
            destination_dir_fd,
            os.fsencode(destination),
            flag,
        )
        == 0
    ):
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination)
    if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise UnsupportedPlatformError("atomic no-replace move is unavailable")
    raise OSError(error, os.strerror(error))


def _atomic_noreplace_backend() -> tuple[Any, int]:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        native = getattr(libc, "renameatx_np", None)
        flag = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        native = getattr(libc, "renameat2", None)
        flag = 1  # RENAME_NOREPLACE
    else:
        native = None
        flag = 0
    if native is None:
        raise UnsupportedPlatformError("atomic no-replace move is unavailable")
    native.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    native.restype = ctypes.c_int
    return native, flag


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
            if not stat.S_ISDIR(canonical_home.st_mode) or (
                canonical_home.st_dev,
                canonical_home.st_ino,
            ) != (home_stat.st_dev, home_stat.st_ino):
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


@contextmanager
def _target_read_lock(home: Path) -> Iterator[_HomeFS]:
    """Retain the established target lock cooperatively without creating state."""
    home = _absolute_home(home)
    parent = _open_absolute_directory(home.parent, create=False)
    try:
        assert fcntl is not None
        home_descriptor = _open_directory_at(parent, home.name, create=False, mode=0o700)
        try:
            os.set_inheritable(home_descriptor, False)
            home_stat = os.fstat(home_descriptor)
            fcntl.flock(home_descriptor, fcntl.LOCK_SH)
            canonical_home = os.stat(home.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(canonical_home.st_mode) or (
                canonical_home.st_dev,
                canonical_home.st_ino,
            ) != (home_stat.st_dev, home_stat.st_ino):
                raise ValueError("deployment home identity changed")
            lock = os.open(
                _LOCK_NAME,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=home_descriptor,
            )
            try:
                os.set_inheritable(lock, False)
                lock_stat = os.fstat(lock)
                if (
                    not stat.S_ISREG(lock_stat.st_mode)
                    or lock_stat.st_nlink != 1
                    or stat.S_IMODE(lock_stat.st_mode) != 0o600
                ):
                    raise ValueError("deployment lock is not valid read authority")
                fcntl.flock(lock, fcntl.LOCK_SH)
                canonical_lock = os.stat(
                    _LOCK_NAME,
                    dir_fd=home_descriptor,
                    follow_symlinks=False,
                )
                if (canonical_lock.st_dev, canonical_lock.st_ino) != (
                    lock_stat.st_dev,
                    lock_stat.st_ino,
                ) or canonical_lock.st_mode != lock_stat.st_mode:
                    raise ValueError("deployment lock identity changed")
                home_fs = _HomeFS(
                    home,
                    home_descriptor,
                    home_identity=(home_stat.st_dev, home_stat.st_ino),
                    lock_name=_LOCK_NAME,
                    lock_identity=(lock_stat.st_dev, lock_stat.st_ino),
                    lock_descriptor=lock,
                )
                yield home_fs
                home_fs.verify_lock_identity()
                terminal_lock = os.fstat(lock)
                if (
                    terminal_lock.st_mode != lock_stat.st_mode
                    or terminal_lock.st_nlink != lock_stat.st_nlink
                ):
                    raise ValueError("deployment lock authority changed")
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
    result = {
        "schema_version": manifest.schema_version,
        "target_id": manifest.target_id,
        "framework": manifest.framework.value,
        "channel": manifest.channel,
        "source_revision": manifest.source_revision,
        "provider_ids": list(manifest.provider_ids),
        "transaction_id": manifest.transaction_id,
        "directories": [
            {"path": item.path.as_posix(), "mode": item.mode} for item in manifest.directories
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
    if manifest.review_state is not None:
        result["review_state"] = manifest.review_state
    return result


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
    manifest_keys = {
        "schema_version",
        "target_id",
        "framework",
        "channel",
        "source_revision",
        "provider_ids",
        "transaction_id",
        "files",
        "directories",
    }
    data_keys = set(data)
    allowed_manifest_keys = manifest_keys | {"review_state"}
    unknown_manifest_keys = sorted(data_keys - allowed_manifest_keys)
    missing_manifest_keys = sorted(manifest_keys - data_keys)
    if unknown_manifest_keys:
        raise ValueError(f"unknown deployment manifest member: {unknown_manifest_keys[0]}")
    if missing_manifest_keys:
        raise ValueError(f"missing deployment manifest member: {missing_manifest_keys[0]}")
    review_state = data.get("review_state")
    if review_state not in {None, "unreviewed-local"}:
        raise ValueError("invalid deployment manifest review state")
    if data.get("target_id") != target.id or data.get("framework") != target.framework.value:
        raise ValueError("deployment manifest target does not match plan")
    if data.get("channel") != target.channel:
        raise ValueError("deployment manifest channel does not match expected channel")
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


def _validated_prior_manifest_data(
    content: bytes, *, target: TargetSpec, expected_channel: str
) -> dict[str, Any]:
    prior_target = TargetSpec(
        target.id,
        target.framework,
        target.home,
        expected_channel,
    )
    return _validated_manifest_data(content, target=prior_target)


def _channel_transitions(
    groups: tuple[_PlanGroup, ...],
    supplied: tuple[TargetChannelTransition, ...] | None,
) -> dict[str, TargetChannelTransition]:
    transitions = (
        tuple(
            TargetChannelTransition(
                group.target.id,
                group.target.channel,
                group.target.channel,
            )
            for group in groups
        )
        if supplied is None
        else supplied
    )
    if type(transitions) is not tuple or any(
        type(item) is not TargetChannelTransition for item in transitions
    ):
        raise ValueError("channel transitions must be exact immutable values")
    by_target = {item.target_id: item for item in transitions}
    if len(by_target) != len(transitions) or set(by_target) != {
        group.target.id for group in groups
    }:
        raise ValueError("channel transitions must name each planned target exactly once")
    for group in groups:
        if by_target[group.target.id].candidate_channel != group.target.channel:
            raise ValueError("candidate channel does not match planned target channel")
    return by_target


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
        raise ValueError(f"invalid deployment manifest non-normalized {kind} path") from exc


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
    audit_roots: tuple[Path, ...]
    runtime_python_sources: tuple[Path, ...]
    runtime_cache_removals: tuple[tuple[Path, Path], ...]
    legacy_link_transition: LegacyLinkTransition | None


def _public_gstack_runtime_path(provider_id: str, path: Path) -> bool:
    return provider_id == "public-skill:gstack" and path.parts[:3] == (
        ".agentops",
        "runtime",
        "gstack",
    )


def _validate_and_group(
    plans: tuple[ProviderPlan, ...],
) -> tuple[_PlanGroup, ...]:
    groups: dict[tuple[str, Framework, Path, str, str], dict[Path, PlannedFile]] = {}
    providers: dict[tuple[str, Framework, Path, str, str], set[str]] = {}
    removals: dict[tuple[str, Framework, Path, str, str], set[Path]] = {}
    audit_roots: dict[tuple[str, Framework, Path, str, str], set[Path]] = {}
    runtime_python_sources: dict[tuple[str, Framework, Path, str, str], set[Path]] = {}
    runtime_cache_removals: dict[
        tuple[str, Framework, Path, str, str], dict[Path, Path]
    ] = {}
    legacy_links: dict[tuple[str, Framework, Path, str, str], LegacyLinkTransition] = {}
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
        roots = audit_roots.setdefault(key, set())
        roots.update(plan.audit_roots or (Path(item.path.parts[0]) for item in plan.files))
        sources = runtime_python_sources.setdefault(key, set())
        sources.update(plan.runtime_python_sources)
        if plan.legacy_link_transition is not None:
            if key in legacy_links:
                raise ValueError("only one legacy link transition is allowed per target")
            legacy_links[key] = plan.legacy_link_transition
        for item in plan.files:
            path = _safe_relative(item.path)
            if path.parts[0] in {".agentops", _LOCK_NAME} and not _public_gstack_runtime_path(
                plan.provider_id, path
            ):
                raise ValueError(f"planned path overlaps reserved metadata: {path}")
            if not isinstance(item.content, bytes) or not _valid_file_mode(item.mode):
                raise ValueError(f"invalid planned file: {path}")
            prior = files.get(path)
            if prior is not None:
                if prior.content != item.content or prior.mode != item.mode:
                    raise ValueError(f"conflicting duplicate planned destination: {path}")
                continue
            files[path] = item
        normalized_removals = tuple(_safe_relative(removal) for removal in plan.removals)
        normalized_removal_set = set(normalized_removals)
        cache_removal_pairs = runtime_cache_removals.setdefault(key, {})
        for removal_path in normalized_removals:
            if removal_path.parts[0] in {
                ".agentops",
                _LOCK_NAME,
            } and not _public_gstack_runtime_path(plan.provider_id, removal_path):
                raise ValueError(f"planned removal overlaps reserved metadata: {removal_path}")
            planned_removals.add(removal_path)
            runtime_source = _runtime_python_source_for_cache(removal_path)
            if runtime_source is not None and runtime_source in normalized_removal_set:
                cache_removal_pairs[removal_path] = runtime_source
    for key, files in groups.items():
        file_paths = sorted(files, key=str)
        removal_paths = sorted(removals[key], key=str)
        legacy = legacy_links.get(key)
        if legacy is not None:
            item = files.get(legacy.destination)
            if (
                item is None
                or item.content != legacy.replacement
                or item.mode != legacy.mode
                or legacy.destination in removals[key]
            ):
                raise ValueError("legacy link transition must bind one installed planned file")
        for index, path in enumerate(file_paths):
            if any(
                path in other.parents or other in path.parents for other in file_paths[index + 1 :]
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
                file_path == removal or file_path in removal.parents or removal in file_path.parents
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
                audit_roots=tuple(sorted(audit_roots[key], key=str)),
                runtime_python_sources=tuple(sorted(runtime_python_sources[key], key=str)),
                runtime_cache_removals=tuple(sorted(runtime_cache_removals[key].items())),
                legacy_link_transition=legacy_links.get(key),
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
            (transaction / "rendered" / item.path).as_posix() if kind == "installed" else None
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


def _legacy_link_evidence_path(transaction: Path) -> Path:
    return transaction / "prestate" / "legacy-link.json"


def _legacy_link_entry_path(transaction: Path) -> Path:
    return transaction / "prestate" / "legacy-link.entry"


def _legacy_link_rollback_candidate_path(transaction: Path) -> Path:
    return transaction / "rollback" / "legacy-link.candidate"


def _legacy_link_evidence_bytes(destination: Path, link_text: str) -> bytes:
    evidence = {
        "schema_version": _LEGACY_LINK_EVIDENCE_SCHEMA_VERSION,
        "destination": destination.as_posix(),
        "link_text": link_text,
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
        if operation["kind"] in _REMOVAL_KINDS
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
    operations_by_path = {Path(operation["destination"]): operation for operation in operations}
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
    legacy = record.get("legacy_link_transition")
    legacy_destination = Path(legacy["destination"]) if legacy is not None else None
    if legacy_destination is not None and not home_fs.matches_symlink(
        legacy_destination,
        legacy["expected_link_text"],
    ):
        raise IncompleteRollbackError(
            f"rollback completion changed legacy link: {legacy_destination}"
        )
    if legacy is not None and home_fs.exists(
        _legacy_link_rollback_candidate_path(
            _METADATA / "transactions" / record["manifest"]["transaction_id"]
        )
    ):
        raise IncompleteRollbackError("rollback completion retains legacy candidate")
    if prior_data is None:
        if home_fs.exists(manifest_path):
            raise IncompleteRollbackError(
                "rollback completion does not match absent prior manifest"
            )
        for operation in record["operations"]:
            destination = Path(operation["destination"])
            if legacy_destination is not None and destination == legacy_destination:
                continue
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
    for operation in record["operations"]:
        if operation["kind"] != _RUNTIME_CACHE_REMOVAL:
            continue
        destination = Path(operation["destination"])
        if not _file_matches(
            home_fs,
            destination,
            operation["prior_fingerprint"],
            operation["prior_mode"],
        ):
            raise IncompleteRollbackError(
                f"rollback completion changed runtime cache: {destination}"
            )
        if not _recorded_runtime_cache_is_valid(home_fs, record, operation):
            raise IncompleteRollbackError(
                f"rollback completion has invalid runtime cache: {destination}"
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
    *,
    channel_transitions: tuple[TargetChannelTransition, ...] | None = None,
) -> tuple[DeploymentManifest, ...]:
    _require_supported_platform()
    manifests: list[DeploymentManifest] = []
    groups = _validate_and_group(plans)
    transitions = _channel_transitions(groups, channel_transitions)
    with _locked_provider_plan_targets(plans):
        try:
            _install_provider_plan_groups(groups, manifests, transitions)
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
                        "grouped rollback failed; recovery evidence retained for earlier targets"
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
        grouped_locks = {home: locks.enter_context(_target_lock(home)) for home in homes}
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
            raise RuntimeError("active deployment lock set does not cover every target") from error
        home_fs.verify_lock_identity()


def _install_provider_plan_groups(
    groups: tuple[_PlanGroup, ...],
    manifests: list[DeploymentManifest],
    transitions: dict[str, TargetChannelTransition],
) -> None:
    for group in groups:
        target = group.target
        transition = transitions[target.id]
        files = group.files
        transaction_id = uuid.uuid4().hex
        manifest = DeploymentManifest(
            schema_version=_SCHEMA_VERSION,
            target_id=target.id,
            framework=target.framework,
            channel=target.channel,
            source_revision=group.source_revision,
            provider_ids=group.provider_ids,
            files=tuple(ManifestFile(item.path, item.fingerprint, item.mode) for item in files),
            directories=_directories(files),
            transaction_id=transaction_id,
            review_state=(
                "unreviewed-local"
                if target.channel == "preview"
                or target.channel.startswith("preview-")
                or target.channel.startswith("unreviewed-local")
                else None
            ),
        )
        with _target_lock(target.home) as home_fs:
            legacy_active = (
                group.legacy_link_transition
                if group.legacy_link_transition is not None
                and home_fs.matches_symlink(
                    group.legacy_link_transition.destination,
                    group.legacy_link_transition.expected_link_text,
                )
                else None
            )
            if legacy_active is not None:
                _atomic_noreplace_backend()
            _validate_group_current_state(home_fs, group, transition)
            manifest_path = _manifest_path(target)
            prior_manifest_content = home_fs.read_optional(manifest_path)
            prior_data = None
            prior_manifest_mode = None
            if prior_manifest_content is not None:
                prior_manifest_stat = home_fs.stat(manifest_path)
                _validate_ownership_manifest_stat(prior_manifest_stat)
                prior_manifest_mode = stat.S_IMODE(prior_manifest_stat.st_mode)
                prior_data = _validated_prior_manifest_data(
                    prior_manifest_content,
                    target=target,
                    expected_channel=transition.expected_prior_channel,
                )
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
                if legacy_active is not None and item.path == legacy_active.destination:
                    operations.append(_operation(item, transaction_id, index, kind="installed"))
                    continue
                if destination_exists:
                    installed_stat = home_fs.stat(item.path)
                    if stat.S_ISLNK(installed_stat.st_mode) and not (
                        legacy_active is not None and item.path == legacy_active.destination
                    ):
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
                        raise ValueError(f"unmanaged destination conflicts with plan: {item.path}")
                    if _fingerprint(installed) != prior[0] or installed_mode != prior[1]:
                        raise ValueError(f"managed destination changed: {item.path}")
                    if installed == item.content and installed_mode == item.mode:
                        operations.append(_operation(item, transaction_id, index, kind="adopted"))
                        continue
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
                    if not home_fs.exists(removal):
                        continue
                    runtime_source = _authorized_runtime_cache_removal_source(
                        home_fs,
                        removal,
                        managed=managed,
                        authorized_sources=dict(group.runtime_cache_removals),
                    )
                    if runtime_source is None:
                        raise ValueError(f"refusing to remove unmanaged destination: {removal}")
                    index = len(operations)
                    removal_stat = home_fs.stat(removal)
                    removal_content = home_fs.read_file(removal)
                    source_stat = home_fs.stat(runtime_source)
                    source_content = home_fs.read_file(runtime_source)
                    runtime_cache_provenance = _runtime_cache_provenance(
                        removal,
                        runtime_source,
                        removal_content,
                        removal_stat,
                        source_content,
                        source_stat,
                    )
                    if runtime_cache_provenance is None:
                        raise ValueError(f"invalid runtime cache removal: {removal}")
                    operations.append(
                        {
                            "kind": _RUNTIME_CACHE_REMOVAL,
                            "index": index,
                            "destination": removal.as_posix(),
                            "runtime_source": runtime_source.as_posix(),
                            "runtime_cache_provenance": runtime_cache_provenance,
                            "staged": None,
                            "backup": (
                                _METADATA
                                / "transactions"
                                / transaction_id
                                / "backups"
                                / f"{index:04d}"
                            ).as_posix(),
                            "expected_fingerprint": None,
                            "expected_mode": None,
                            "prior_fingerprint": _fingerprint(removal_content),
                            "prior_mode": stat.S_IMODE(removal_stat.st_mode),
                            "prior_exists": True,
                        }
                    )
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
                    _directory_evidence_path(transaction, directory.path) if exists else None
                )
                directory_records.append(
                    {
                        "path": directory.path.as_posix(),
                        "created": not exists,
                        "mode": directory_mode,
                        "prestate_evidence": (
                            evidence_path.as_posix() if evidence_path is not None else None
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
                channel=manifest.channel,
                source_revision=manifest.source_revision,
                provider_ids=manifest.provider_ids,
                files=manifest.files,
                directories=tuple(actual_directories),
                transaction_id=manifest.transaction_id,
                review_state=manifest.review_state,
            )
            legacy_evidence_path = (
                _legacy_link_evidence_path(transaction) if legacy_active is not None else None
            )
            legacy_entry_path = (
                _legacy_link_entry_path(transaction) if legacy_active is not None else None
            )
            if legacy_active is not None:
                assert legacy_evidence_path is not None
                home_fs.write_file(
                    legacy_evidence_path,
                    _legacy_link_evidence_bytes(
                        legacy_active.destination,
                        legacy_active.expected_link_text,
                    ),
                    0o600,
                )
                legacy_operation_index = next(
                    operation["index"]
                    for operation in operations
                    if operation["destination"] == legacy_active.destination.as_posix()
                )
            else:
                legacy_operation_index = None
            record = {
                "schema_version": _TRANSACTION_SCHEMA_VERSION,
                "state": "prepared",
                "operation_cursor": 0,
                "operation_phase": "ready",
                "expected_prior_channel": transition.expected_prior_channel,
                "candidate_channel": transition.candidate_channel,
                "manifest": _manifest_to_dict(manifest),
                "manifest_path": manifest_path.as_posix(),
                "manifest_content": base64.b64encode(_manifest_bytes(manifest)).decode(),
                "prior_manifest_content": (
                    base64.b64encode(prior_manifest_content).decode()
                    if prior_manifest_content is not None
                    else None
                ),
                "prior_manifest_mode": prior_manifest_mode,
                "legacy_link_transition": (
                    None
                    if legacy_active is None
                    else {
                        "provider_id": legacy_active.provider_id,
                        "target_id": legacy_active.target_id,
                        "expected_channel": legacy_active.expected_channel,
                        "destination": legacy_active.destination.as_posix(),
                        "expected_link_text": legacy_active.expected_link_text,
                        "replacement_fingerprint": hashlib.sha256(
                            legacy_active.replacement
                        ).hexdigest(),
                        "replacement_mode": legacy_active.mode,
                        "operation_index": legacy_operation_index,
                        "prestate_evidence": legacy_evidence_path.as_posix(),
                        "retained_entry": legacy_entry_path.as_posix(),
                        "operation_cursor": 0,
                        "operation_phase": "ready",
                    }
                ),
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
                        item for item in files if item.path.as_posix() == operation["destination"]
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
                    record["operation_cursor"] = operation["index"]
                    record["operation_phase"] = "applying"
                    if legacy_active is not None:
                        record["legacy_link_transition"]["operation_cursor"] = operation["index"]
                        record["legacy_link_transition"]["operation_phase"] = "applying"
                    home_fs.write_atomic(record_path, _record_bytes(record), 0o600)
                    pinned_cache = (
                        _PinnedRuntimeCache(home_fs, destination, operation)
                        if operation["kind"] == _RUNTIME_CACHE_REMOVAL
                        else None
                    )
                    try:
                        _before_operation_mutation(home_fs, record_path, operation)
                        if backup is not None:
                            if pinned_cache is not None:
                                _retain_runtime_cache_removal(
                                    home_fs,
                                    destination,
                                    backup,
                                    operation,
                                    pinned_cache,
                                )
                            else:
                                home_fs.replace(destination, backup)
                            record["operation_phase"] = "backup-created"
                            if legacy_active is not None:
                                record["legacy_link_transition"]["operation_phase"] = (
                                    "backup-created"
                                )
                            home_fs.write_atomic(record_path, _record_bytes(record), 0o600)
                            _after_operation_backup(home_fs, record_path, operation)
                    finally:
                        if pinned_cache is not None:
                            pinned_cache.close()
                    if operation["kind"] == "installed":
                        staged = Path(operation["staged"])
                        try:
                            if (
                                legacy_active is not None
                                and destination == legacy_active.destination
                            ):
                                assert legacy_entry_path is not None
                                home_fs.move_new(destination, legacy_entry_path)
                                if not home_fs.matches_symlink(
                                    legacy_entry_path,
                                    legacy_active.expected_link_text,
                                ):
                                    if not home_fs.exists(destination):
                                        home_fs.move_new(legacy_entry_path, destination)
                                    raise ValueError(
                                        f"legacy link changed before retention: {destination}"
                                    )
                                _after_legacy_link_move(
                                    home_fs,
                                    destination,
                                    legacy_entry_path,
                                )
                            home_fs.publish_new(staged, destination)
                        except FileExistsError as exc:
                            raise ValueError(
                                f"new unmanaged destination appeared: {destination}"
                            ) from exc
                    _after_operation_mutation(home_fs, record_path, operation)
                    record["operation_cursor"] = operation["index"] + 1
                    record["operation_phase"] = "ready"
                    if legacy_active is not None:
                        record["legacy_link_transition"]["operation_cursor"] = (
                            operation["index"] + 1
                        )
                        record["legacy_link_transition"]["operation_phase"] = "ready"
                    home_fs.write_atomic(record_path, _record_bytes(record), 0o600)
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
                _before_committed_record_write(home_fs, record_path, record)
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


def _validate_group_current_state(
    home_fs: _HomeFS,
    group: _PlanGroup,
    transition: TargetChannelTransition,
) -> None:
    legacy = group.legacy_link_transition
    manifest_path = _manifest_path(group.target)
    prior_content = home_fs.read_optional(manifest_path)
    prior_data = None
    if prior_content is not None:
        _validate_ownership_manifest_stat(home_fs.stat(manifest_path))
        prior_data = _validated_prior_manifest_data(
            prior_content,
            target=group.target,
            expected_channel=transition.expected_prior_channel,
        )
        _verify_manifest_files_and_directories(
            home_fs,
            prior_data,
            error_type=ValueError,
            context="prior",
        )
    managed = (
        {Path(item["path"]): (item["fingerprint"], item["mode"]) for item in prior_data["files"]}
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
        prior = managed.get(item.path)
        if stat.S_ISLNK(installed_stat.st_mode):
            if (
                legacy is not None
                and prior is None
                and item.path == legacy.destination
                and home_fs.matches_symlink(item.path, legacy.expected_link_text)
            ):
                continue
            raise ValueError(f"destination is a symbolic link: {item.path}")
        if not stat.S_ISREG(installed_stat.st_mode):
            raise ValueError(f"destination is not a regular file: {item.path}")
        installed = home_fs.read_file(item.path)
        installed_mode = stat.S_IMODE(installed_stat.st_mode)
        if prior is None:
            if installed != item.content or installed_mode != item.mode:
                raise ValueError(f"unmanaged destination conflicts with plan: {item.path}")
        elif _fingerprint(installed) != prior[0] or installed_mode != prior[1]:
            raise ValueError(f"managed destination changed: {item.path}")
    for removal in group.removals:
        prior = managed.get(removal)
        if prior is None:
            if not home_fs.exists(removal):
                continue
            if (
                _authorized_runtime_cache_removal_source(
                    home_fs,
                    removal,
                    managed=managed,
                    authorized_sources=dict(group.runtime_cache_removals),
                )
                is None
            ):
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


def _preflight_provider_plans_read_only(
    plans: tuple[ProviderPlan, ...],
    *,
    channel_transitions: tuple[TargetChannelTransition, ...] | None = None,
) -> None:
    """Validate every live-apply precondition without creating target state."""
    _require_supported_platform()
    groups = _validate_and_group(plans)
    transitions = _channel_transitions(groups, channel_transitions)
    for group in groups:
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
            _validate_group_current_state(home_fs, group, transitions[group.target.id])


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
        channel = data["channel"]
        if not isinstance(channel, str) or not channel:
            raise ValueError("invalid target channel")
        target = TargetSpec(target_id, framework, home, channel)
        validated = _validated_manifest_data(
            (json.dumps(data, sort_keys=True) + "\n").encode(),
            target=target,
        )
        manifest = DeploymentManifest(
            schema_version=validated["schema_version"],
            target_id=target_id,
            framework=framework,
            channel=channel,
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
            review_state=validated.get("review_state"),
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
        or record["schema_version"]
        not in {
            _LEGACY_TRANSACTION_SCHEMA_VERSION,
            4,
            _LEGACY_OPERATION_CURSOR_SCHEMA_VERSION,
            6,
            _TRANSACTION_SCHEMA_VERSION,
        }
        or record.get("state") not in {"prepared", "committed", "indeterminate", "rolled-back"}
        or not isinstance(record.get("manifest"), dict)
    ):
        raise ValueError("invalid transaction record schema")
    record_keys = {
        "schema_version",
        "state",
        "expected_prior_channel",
        "candidate_channel",
        "manifest",
        "manifest_path",
        "manifest_content",
        "prior_manifest_content",
        "prior_manifest_mode",
        "directories",
        "operations",
    }
    if record["schema_version"] >= 4:
        record_keys.add("legacy_link_transition")
    if record["schema_version"] >= _OPERATION_CURSOR_SCHEMA_VERSION:
        record_keys.update({"operation_cursor", "operation_phase"})
    _require_exact_keys(record, record_keys, label="transaction record")
    target, manifest = _manifest_from_data(record["manifest"], home=home)
    expected_prior_channel = record.get("expected_prior_channel")
    candidate_channel = record.get("candidate_channel")
    if (
        not isinstance(expected_prior_channel, str)
        or not expected_prior_channel
        or not isinstance(candidate_channel, str)
        or not candidate_channel
        or candidate_channel != manifest.channel
    ):
        raise ValueError("invalid transaction record channel transition")
    if manifest.transaction_id != transaction_id:
        raise ValueError("invalid transaction record identifier")
    legacy = record.get("legacy_link_transition")
    if legacy is not None:
        if not isinstance(legacy, dict):
            raise ValueError("invalid legacy link transition")
        legacy_keys = {
            "provider_id", "target_id", "expected_channel", "destination",
            "expected_link_text", "replacement_fingerprint", "replacement_mode",
            "operation_index", "prestate_evidence", "retained_entry",
        }
        if record["schema_version"] >= _LEGACY_OPERATION_CURSOR_SCHEMA_VERSION:
            legacy_keys.update({"operation_cursor", "operation_phase"})
        _require_exact_keys(
            legacy,
            legacy_keys,
            label="legacy link transition",
        )
        if (
            type(legacy["provider_id"]) is not str
            or legacy["provider_id"] not in manifest.provider_ids
            or type(legacy["target_id"]) is not str
            or legacy["target_id"] != manifest.target_id
            or type(legacy["expected_channel"]) is not str
            or legacy["expected_channel"] != manifest.channel
            or type(legacy["expected_link_text"]) is not str
            or not legacy["expected_link_text"]
            or _canonical_relative_text(legacy["destination"], label="legacy link destination")
            not in {item.path for item in manifest.files}
            or type(legacy["replacement_fingerprint"]) is not str
            or len(legacy["replacement_fingerprint"]) != 64
            or any(
                character not in "0123456789abcdef"
                for character in legacy["replacement_fingerprint"]
            )
            or not _valid_file_mode(legacy["replacement_mode"])
            or type(legacy["operation_index"]) is not int
            or legacy["operation_index"] < 0
        ):
            raise ValueError("invalid legacy link transition")
    transaction = _METADATA / "transactions" / transaction_id
    if legacy is not None:
        evidence = _canonical_relative_text(
            legacy["prestate_evidence"],
            label="legacy link prestate evidence",
        )
        if evidence != _legacy_link_evidence_path(transaction):
            raise ValueError("invalid legacy link transition evidence")
        retained_entry = _canonical_relative_text(
            legacy["retained_entry"],
            label="legacy link retained entry",
        )
        if retained_entry != _legacy_link_entry_path(transaction):
            raise ValueError("invalid legacy link retained entry")
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
        prior_data = _validated_prior_manifest_data(
            prior_content,
            target=target,
            expected_channel=expected_prior_channel,
        )
        if not _valid_ownership_manifest_mode(record.get("prior_manifest_mode")):
            raise ValueError("invalid transaction record prior manifest mode")
    elif record.get("prior_manifest_mode") is not None:
        raise ValueError("invalid transaction record prior manifest mode")
    operations = record.get("operations")
    directories = record.get("directories")
    if not isinstance(operations, list) or not isinstance(directories, list):
        raise ValueError("invalid transaction record operations")
    if record["schema_version"] >= _LEGACY_OPERATION_CURSOR_SCHEMA_VERSION and legacy is not None:
        cursor = legacy["operation_cursor"]
        phase = legacy["operation_phase"]
        if (
            type(cursor) is not int
            or cursor < 0
            or cursor > len(operations)
            or type(phase) is not str
            or phase not in {"ready", "applying", "backup-created"}
            or phase in {"applying", "backup-created"}
            and cursor == len(operations)
        ):
            raise ValueError("invalid legacy link operation phase")
    if record["schema_version"] >= _OPERATION_CURSOR_SCHEMA_VERSION:
        cursor = record["operation_cursor"]
        phase = record["operation_phase"]
        if (
            type(cursor) is not int
            or cursor < 0
            or cursor > len(operations)
            or type(phase) is not str
            or phase not in {"ready", "applying", "backup-created"}
            or phase in {"applying", "backup-created"}
            and cursor == len(operations)
        ):
            raise ValueError("invalid transaction operation phase")
    destinations: set[Path] = set()
    backup_paths: set[Path] = set()
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("kind") not in {
            "installed",
            "adopted",
            "removal",
            _RUNTIME_CACHE_REMOVAL,
        }:
            raise ValueError("invalid transaction record operation")
        operation_keys = {
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
        }
        if operation["kind"] == _RUNTIME_CACHE_REMOVAL:
            operation_keys.add("runtime_source")
            if record["schema_version"] >= _TRANSACTION_SCHEMA_VERSION:
                operation_keys.add("runtime_cache_provenance")
        _require_exact_keys(
            operation,
            operation_keys,
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
        if operation["kind"] in _REMOVAL_KINDS:
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
            transaction / "rendered" / destination if operation["kind"] == "installed" else None
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
        if operation["kind"] == _RUNTIME_CACHE_REMOVAL:
            runtime_source = _canonical_relative_text(
                operation.get("runtime_source"),
                label="transaction runtime cache source",
            )
            if record["schema_version"] >= _TRANSACTION_SCHEMA_VERSION:
                provenance = _decode_runtime_cache_provenance(
                    operation.get("runtime_cache_provenance"),
                    destination=destination,
                    source=runtime_source,
                )
                if provenance["source"] != runtime_source.as_posix():
                    raise ValueError("invalid runtime cache removal source")
            elif _runtime_python_source_for_cache(destination) != runtime_source:
                raise ValueError("invalid runtime cache removal source")
    manifest_files = {
        Path(item["path"]): (item["fingerprint"], item["mode"])
        for item in record["manifest"]["files"]
    }
    prior_files = (
        {Path(item["path"]): (item["fingerprint"], item["mode"]) for item in prior_data["files"]}
        if prior_data is not None
        else {}
    )
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
                or kind != _RUNTIME_CACHE_REMOVAL
                and prior_files.get(destination)
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
            raise ValueError("transaction operations prior existence does not match prior manifest")
        if kind == "removal" and destination not in prior_files:
            raise ValueError("transaction operations do not match manifests")
        if kind == _RUNTIME_CACHE_REMOVAL and (
            destination in prior_files
            or operation["prior_mode"] != 0o644
        ):
            raise ValueError("transaction runtime cache removal does not match manifests")
    removals_by_destination = {
        Path(operation["destination"]): operation
        for operation in operations
        if operation["kind"] == "removal"
    }
    for operation in operations:
        if operation["kind"] != _RUNTIME_CACHE_REMOVAL:
            continue
        source = Path(operation["runtime_source"])
        source_operation = removals_by_destination.get(source)
        if source not in prior_files or source_operation is None:
            raise ValueError("transaction runtime cache removal is not bound to source removal")
    if installed_destinations != set(manifest_files):
        raise ValueError("transaction operations do not match manifest")
    if legacy is not None:
        operation_index = legacy["operation_index"]
        if operation_index >= len(operations):
            raise ValueError("invalid legacy link transition operation")
        operation = operations[operation_index]
        destination = Path(legacy["destination"])
        if (
            operation["index"] != operation_index
            or operation["kind"] != "installed"
            or Path(operation["destination"]) != destination
            or operation["expected_fingerprint"] != legacy["replacement_fingerprint"]
            or operation["expected_mode"] != legacy["replacement_mode"]
            or operation["prior_exists"]
            or operation["backup"] is not None
            or operation["prior_fingerprint"] is not None
            or operation["prior_mode"] is not None
            or manifest_files.get(destination)
            != (legacy["replacement_fingerprint"], legacy["replacement_mode"])
        ):
            raise ValueError("invalid legacy link transition operation binding")
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
    recorded_directories = {Path(item["path"]): item["mode"] for item in directories}
    if recorded_directories != manifest_directories:
        raise ValueError("transaction directories do not match manifest")
    if any(
        type(operation["index"]) is not int or operation["index"] != index
        for index, operation in enumerate(operations)
    ):
        raise ValueError("transaction operations are not in canonical order")
    operation_order = [
        (operation["kind"] in _REMOVAL_KINDS, operation["destination"])
        for operation in operations
    ]
    if operation_order != sorted(operation_order):
        raise ValueError("transaction operations are not in canonical order")
    if [item["path"] for item in directories] != sorted(item["path"] for item in directories):
        raise ValueError("transaction directories are not in canonical order")
    if record["schema_version"] >= _LEGACY_OPERATION_CURSOR_SCHEMA_VERSION and legacy is not None:
        cursor = legacy["operation_cursor"]
        phase = legacy["operation_phase"]
        if (
            phase == "backup-created"
            and operations[cursor]["backup"] is None
            or record["state"] == "committed"
            and (phase != "ready" or cursor != len(operations))
        ):
            raise ValueError("invalid legacy link operation phase binding")
    if record["schema_version"] >= _OPERATION_CURSOR_SCHEMA_VERSION:
        cursor = record["operation_cursor"]
        phase = record["operation_phase"]
        if (
            phase == "backup-created"
            and operations[cursor]["backup"] is None
            or record["state"] == "committed"
            and (phase != "ready" or cursor != len(operations))
        ):
            raise ValueError("invalid transaction operation phase binding")
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


def _operation_proven_unstarted(
    home_fs: _HomeFS,
    record: dict[str, Any],
    operation: dict[str, Any],
) -> bool:
    if (
        record["schema_version"] >= _OPERATION_CURSOR_SCHEMA_VERSION
        and operation["backup"] is not None
        and operation["prior_exists"]
    ):
        cursor = record["operation_cursor"]
        phase = record["operation_phase"]
        index = operation["index"]
        if index < cursor or phase == "backup-created" and index == cursor:
            return False
        return _file_matches(
            home_fs,
            Path(operation["destination"]),
            operation["prior_fingerprint"],
            operation["prior_mode"],
        )
    legacy = record.get("legacy_link_transition")
    if (
        legacy is None
        or "operation_cursor" not in legacy
        or operation["backup"] is None
        or not operation["prior_exists"]
    ):
        return False
    cursor = legacy["operation_cursor"]
    phase = legacy["operation_phase"]
    index = operation["index"]
    if index < cursor or phase == "backup-created" and index == cursor:
        return False
    return _file_matches(
        home_fs,
        Path(operation["destination"]),
        operation["prior_fingerprint"],
        operation["prior_mode"],
    )


def _validate_legacy_restoration_evidence(
    home_fs: _HomeFS,
    legacy: dict[str, Any],
) -> None:
    evidence_path = Path(legacy["prestate_evidence"])
    retained_entry = Path(legacy["retained_entry"])
    try:
        evidence_stat = home_fs.stat(evidence_path)
        evidence_content = home_fs.read_file(evidence_path)
    except OSError as exc:
        raise ValueError("legacy link prestate evidence is missing") from exc
    if (
        not stat.S_ISREG(evidence_stat.st_mode)
        or stat.S_IMODE(evidence_stat.st_mode) != 0o600
        or evidence_content
        != _legacy_link_evidence_bytes(
            Path(legacy["destination"]),
            legacy["expected_link_text"],
        )
        or not home_fs.matches_symlink(
            retained_entry,
            legacy["expected_link_text"],
        )
    ):
        raise ValueError("legacy link restoration evidence is invalid")


def _restore_legacy_destination_before_evidence(
    home_fs: _HomeFS,
    record: dict[str, Any],
    record_path: Path,
) -> None:
    legacy = record["legacy_link_transition"]
    destination = Path(legacy["destination"])
    retained_entry = Path(legacy["retained_entry"])
    if home_fs.matches_symlink(destination, legacy["expected_link_text"]):
        return
    if not home_fs.matches_symlink(retained_entry, legacy["expected_link_text"]):
        return
    _validate_legacy_restoration_evidence(home_fs, legacy)
    candidate = _legacy_link_rollback_candidate_path(record_path.parent)
    if home_fs.exists(destination):
        if not _file_matches(
            home_fs,
            destination,
            legacy["replacement_fingerprint"],
            legacy["replacement_mode"],
        ):
            return
        _ensure_directory(home_fs, candidate.parent, 0o700)
        home_fs.move_new(destination, candidate)
        if not _file_matches(
            home_fs,
            candidate,
            legacy["replacement_fingerprint"],
            legacy["replacement_mode"],
        ):
            if not home_fs.exists(destination):
                home_fs.move_new(candidate, destination)
            raise IncompleteRollbackError(
                "rollback incomplete: retained legacy replacement changed"
            )
    try:
        home_fs.move_new(retained_entry, destination)
    except FileExistsError as exc:
        raise IncompleteRollbackError(
            "rollback incomplete: concurrent legacy destination preserved"
        ) from exc


def _operation_prestate_evidence_path(
    home_fs: _HomeFS,
    record: dict[str, Any],
    operation: dict[str, Any],
    *,
    allow_missing: bool,
) -> Path:
    backup = Path(operation["backup"])
    if home_fs.exists(backup):
        return backup
    destination = Path(operation["destination"])
    if _operation_proven_unstarted(home_fs, record, operation) or (
        allow_missing
        and record["state"] == "rolled-back"
        and _file_matches(
            home_fs,
            destination,
            operation["prior_fingerprint"],
            operation["prior_mode"],
        )
    ):
        return destination
    raise ValueError("runtime cache removal evidence is missing")


def _validate_runtime_cache_removal_evidence(
    home_fs: _HomeFS,
    record: dict[str, Any],
    *,
    allow_missing: bool,
) -> None:
    operations = {
        Path(operation["destination"]): operation for operation in record["operations"]
    }
    for operation in record["operations"]:
        if operation["kind"] != _RUNTIME_CACHE_REMOVAL:
            continue
        source_operation = operations[Path(operation["runtime_source"])]
        cache_evidence = _operation_prestate_evidence_path(
            home_fs,
            record,
            operation,
            allow_missing=allow_missing,
        )
        source_evidence = _operation_prestate_evidence_path(
            home_fs,
            record,
            source_operation,
            allow_missing=allow_missing,
        )
        try:
            cache_stat = home_fs.stat(cache_evidence)
            cache = home_fs.read_file(cache_evidence)
            source_stat = home_fs.stat(source_evidence)
            source = home_fs.read_file(source_evidence)
        except OSError as exc:
            raise ValueError("runtime cache removal evidence is unreadable") from exc
        cache_content_valid = (
            _runtime_cache_matches_provenance(
                cache,
                cache_stat,
                source,
                source_stat,
                _decode_runtime_cache_provenance(
                    operation["runtime_cache_provenance"],
                    destination=Path(operation["destination"]),
                    source=Path(operation["runtime_source"]),
                ),
            )
            if record["schema_version"] >= _TRANSACTION_SCHEMA_VERSION
            else _runtime_python_cache_content_is_valid(
                cache,
                cache_stat,
                source,
                source_stat,
            )
        )
        if (
            _fingerprint(cache) != operation["prior_fingerprint"]
            or stat.S_IMODE(cache_stat.st_mode) != operation["prior_mode"]
            or _fingerprint(source) != source_operation["prior_fingerprint"]
            or stat.S_IMODE(source_stat.st_mode) != source_operation["prior_mode"]
            or not cache_content_valid
        ):
            raise ValueError("runtime cache removal evidence is invalid")


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
    invalid_backups = {path: kind for path, kind in backup_entries.items() if kind != "regular"}
    if invalid_backups:
        raise ValueError("transaction backup evidence has invalid entries")
    actual_backups = set(backup_entries)
    missing_backups = expected_backups - actual_backups
    missing_backups = {
        path
        for path in missing_backups
        if not any(
            Path(operation["backup"]) == path
            and _operation_proven_unstarted(home_fs, record, operation)
            or Path(operation["backup"]) == path
            and record["state"] == "rolled-back"
            and operation["kind"] == _RUNTIME_CACHE_REMOVAL
            and _recorded_runtime_cache_is_valid(home_fs, record, operation)
            for operation in record["operations"]
            if operation["backup"] is not None
        )
    }
    if missing_backups and not allow_missing:
        missing = min(missing_backups, key=str)
        raise missing_backup_error(f"rollback incomplete: backup evidence is missing: {missing}")
    if actual_backups - expected_backups:
        raise ValueError("transaction backup evidence has orphan entries")
    _validate_runtime_cache_removal_evidence(
        home_fs,
        record,
        allow_missing=allow_missing,
    )

    legacy = record.get("legacy_link_transition")
    prestate_root = transaction / "prestate"
    expected_prestate = {
        prestate_root / "directories": "directory",
        **{
            Path(directory["prestate_evidence"]): "regular"
            for directory in record["directories"]
            if directory["prestate_evidence"] is not None
        },
    }
    if legacy is not None:
        expected_prestate[Path(legacy["prestate_evidence"])] = "regular"
        expected_prestate[Path(legacy["retained_entry"])] = "symlink"
    actual_prestate = _evidence_entries(home_fs, prestate_root)
    unexpected_prestate = set(actual_prestate) - set(expected_prestate)
    invalid_prestate = {
        path
        for path, kind in actual_prestate.items()
        if expected_prestate.get(path) != kind
    }
    missing_prestate = set(expected_prestate) - set(actual_prestate)
    if legacy is not None:
        retained_entry = Path(legacy["retained_entry"])
        if retained_entry in missing_prestate and home_fs.matches_symlink(
            Path(legacy["destination"]),
            legacy["expected_link_text"],
        ):
            missing_prestate.remove(retained_entry)
    prestate_problems = unexpected_prestate | invalid_prestate
    if not allow_missing:
        prestate_problems |= missing_prestate
    if prestate_problems:
        directory_root = prestate_root / "directories"
        if all(
            path == directory_root or directory_root in path.parents
            for path in prestate_problems
        ):
            raise ValueError("transaction directory pre-state evidence is not one-to-one")
        raise ValueError("transaction prestate evidence is not one-to-one")

    if legacy is not None:
        evidence_path = Path(legacy["prestate_evidence"])
        try:
            evidence_stat = home_fs.stat(evidence_path)
            evidence_content = home_fs.read_file(evidence_path)
        except OSError as exc:
            if allow_missing and not home_fs.exists(evidence_path):
                evidence_content = None
            else:
                raise ValueError("legacy link prestate evidence is missing") from exc
        if evidence_content is not None and (
            not stat.S_ISREG(evidence_stat.st_mode)
            or stat.S_IMODE(evidence_stat.st_mode) != 0o600
            or evidence_content
            != _legacy_link_evidence_bytes(
                Path(legacy["destination"]),
                legacy["expected_link_text"],
            )
        ):
            raise ValueError("legacy link prestate evidence is invalid")
        retained_entry = Path(legacy["retained_entry"])
        if home_fs.exists(retained_entry) and not home_fs.matches_symlink(
            retained_entry,
            legacy["expected_link_text"],
        ):
            raise ValueError("legacy link retained entry is invalid")
        rollback_candidate = _legacy_link_rollback_candidate_path(transaction)
        if home_fs.exists(rollback_candidate) and not _file_matches(
            home_fs,
            rollback_candidate,
            legacy["replacement_fingerprint"],
            legacy["replacement_mode"],
        ):
            raise ValueError("legacy link rollback candidate is invalid")

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


def _restore_runtime_cache_source_timestamps(
    home_fs: _HomeFS,
    record: dict[str, Any],
) -> None:
    operations = {
        Path(operation["destination"]): operation for operation in record["operations"]
    }
    for operation in record["operations"]:
        if operation["kind"] != _RUNTIME_CACHE_REMOVAL:
            continue
        cache = Path(operation["destination"])
        source = Path(operation["runtime_source"])
        if _recorded_runtime_cache_is_valid(home_fs, record, operation):
            continue
        source_operation = operations[source]
        if not _file_matches(
            home_fs,
            cache,
            operation["prior_fingerprint"],
            operation["prior_mode"],
        ) or not _file_matches(
            home_fs,
            source,
            source_operation["prior_fingerprint"],
            source_operation["prior_mode"],
        ):
            raise IncompleteRollbackError("rollback runtime cache prestate changed")
        if record["schema_version"] >= _TRANSACTION_SCHEMA_VERSION:
            provenance = _decode_runtime_cache_provenance(
                operation["runtime_cache_provenance"],
                destination=cache,
                source=source,
            )
            home_fs.set_mtime_seconds(source, provenance["timestamp"])
        else:
            content = home_fs.read_file(cache)
            if (
                len(content) < 16
                or content[:4] != importlib.util.MAGIC_NUMBER
                or content[4:8] != b"\0\0\0\0"
            ):
                raise IncompleteRollbackError("rollback runtime cache evidence is invalid")
            home_fs.set_mtime_seconds(source, int.from_bytes(content[8:12], "little"))
        if not _recorded_runtime_cache_is_valid(home_fs, record, operation):
            raise IncompleteRollbackError("rollback runtime cache restoration is invalid")


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
                raise IncompleteRollbackError(f"rollback cleanup staged file changed: {staged}")
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
                raise IncompleteRollbackError(f"rollback cleanup backup changed: {backup}")
            home_fs.unlink(backup)
    for directory in record["directories"]:
        evidence = directory["prestate_evidence"]
        if evidence is not None and home_fs.exists(Path(evidence)):
            home_fs.unlink(Path(evidence))
    legacy = record.get("legacy_link_transition")
    if legacy is not None:
        evidence = Path(legacy["prestate_evidence"])
        if home_fs.exists(evidence):
            home_fs.unlink(evidence)
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
        prior_raw = _strict_json_loads(prior_manifest, label="prior deployment manifest")
        if (
            not isinstance(prior_raw, dict)
            or not isinstance(prior_raw.get("channel"), str)
            or not prior_raw["channel"]
        ):
            raise IncompleteRollbackError("rollback incomplete: invalid prior manifest channel")
        recovery_target = TargetSpec(
            record["manifest"]["target_id"],
            Framework(record["manifest"]["framework"]),
            home_fs.home,
            prior_raw["channel"],
        )
        prior_data = _validated_manifest_data(prior_manifest, target=recovery_target)
    expected_manifest = base64.b64decode(record["manifest_content"], validate=True)
    legacy = record.get("legacy_link_transition")
    legacy_destination = Path(legacy["destination"]) if legacy is not None else None
    legacy_entry = Path(legacy["retained_entry"]) if legacy is not None else None
    legacy_candidate = (
        _legacy_link_rollback_candidate_path(record_path.parent)
        if legacy is not None
        else None
    )
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
            raise IncompleteRollbackError("rollback cleanup incomplete; evidence retained") from exc
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
            if (
                legacy_destination is not None
                and destination == legacy_destination
                and home_fs.matches_symlink(destination, legacy["expected_link_text"])
            ):
                continue
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
                legacy_destination is not None
                and destination == legacy_destination
                and (
                    legacy_entry is None
                    or not home_fs.matches_symlink(
                        legacy_entry,
                        legacy["expected_link_text"],
                    )
                )
            ):
                raise IncompleteRollbackError(
                    f"rollback incomplete: retained legacy link changed: {legacy_entry}"
                )
        if (
            kind in _REMOVAL_KINDS
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
        if (
            kind == _RUNTIME_CACHE_REMOVAL
            and record["schema_version"] >= _TRANSACTION_SCHEMA_VERSION
            and home_fs.exists(destination)
        ):
            provenance = _decode_runtime_cache_provenance(
                operation["runtime_cache_provenance"],
                destination=destination,
                source=Path(operation["runtime_source"]),
            )
            identity = provenance["identity"]
            observed = home_fs.stat(destination)
            if (observed.st_dev, observed.st_ino) != (
                identity["device"],
                identity["inode"],
            ):
                raise IncompleteRollbackError(
                    f"rollback incomplete: removed destination identity changed: {destination}"
                )
        if backup is not None:
            if not home_fs.exists(backup):
                if _operation_proven_unstarted(home_fs, record, operation):
                    continue
                raise IncompleteRollbackError(f"rollback incomplete: backup is missing: {backup}")
            if not _file_matches(
                home_fs,
                backup,
                operation["prior_fingerprint"],
                operation["prior_mode"],
            ):
                raise IncompleteRollbackError(f"rollback incomplete: backup changed: {backup}")
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
            if operation["kind"] == _RUNTIME_CACHE_REMOVAL and backup is not None:
                if not home_fs.exists(destination):
                    home_fs.publish_new(backup, destination)
                continue
            if operation["kind"] == "installed" and backup is None:
                if legacy_destination is not None and destination == legacy_destination:
                    if home_fs.matches_symlink(destination, legacy["expected_link_text"]):
                        if legacy_candidate is not None and home_fs.exists(legacy_candidate):
                            if not _file_matches(
                                home_fs,
                                legacy_candidate,
                                legacy["replacement_fingerprint"],
                                legacy["replacement_mode"],
                            ):
                                raise IncompleteRollbackError(
                                    "rollback incomplete: legacy candidate changed"
                                )
                            home_fs.unlink(legacy_candidate)
                        continue
                    assert legacy_entry is not None
                    assert legacy_candidate is not None
                    if home_fs.exists(destination):
                        _ensure_directory(home_fs, legacy_candidate.parent, 0o700)
                        if home_fs.exists(legacy_candidate):
                            raise IncompleteRollbackError(
                                f"rollback incomplete: legacy candidate already exists: "
                                f"{legacy_candidate}"
                            )
                        home_fs.move_new(destination, legacy_candidate)
                    elif not home_fs.exists(legacy_candidate):
                        if not home_fs.matches_symlink(
                            legacy_entry,
                            legacy["expected_link_text"],
                        ):
                            raise IncompleteRollbackError(
                                f"rollback incomplete: retained legacy link changed: "
                                f"{legacy_entry}"
                            )
                        home_fs.move_new(legacy_entry, destination)
                        continue
                    if not _file_matches(
                        home_fs,
                        legacy_candidate,
                        legacy["replacement_fingerprint"],
                        legacy["replacement_mode"],
                    ):
                        if not home_fs.exists(destination):
                            home_fs.move_new(legacy_candidate, destination)
                        raise IncompleteRollbackError(
                            f"rollback incomplete: legacy replacement changed: {destination}"
                        )
                    if not home_fs.matches_symlink(
                        legacy_entry,
                        legacy["expected_link_text"],
                    ):
                        raise IncompleteRollbackError(
                            f"rollback incomplete: retained legacy link changed: {legacy_entry}"
                        )
                    home_fs.move_new(legacy_entry, destination)
                    home_fs.unlink(legacy_candidate)
                elif home_fs.exists(destination):
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
        _restore_runtime_cache_source_timestamps(home_fs, record)
        if prior_data is not None:
            _verify_manifest_files_and_directories(
                home_fs,
                prior_data,
                error_type=IncompleteRollbackError,
                context="prior",
            )
        else:
            for operation in record["operations"]:
                destination = Path(operation["destination"])
                if legacy_destination is not None and destination == legacy_destination:
                    if not home_fs.matches_symlink(destination, legacy["expected_link_text"]):
                        raise IncompleteRollbackError(
                            f"rollback incomplete: legacy link changed: {destination}"
                        )
                    continue
                if operation["kind"] == "installed" and home_fs.exists(destination):
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
                manifest_pin = _PinnedRecoveryManifest(
                    home_fs,
                    manifest_path,
                    expected_manifest,
                )
            except (OSError, ValueError) as exc:
                raise PublicationIndeterminateError(
                    "manifest publication remains indeterminate; "
                    "ownership manifest type or mode is invalid"
                ) from exc
            try:
                _validate_transaction_evidence(home_fs, record, relative)
                _verify_recovery_final_state(home_fs, manifest, record)
                if record["state"] != "committed":
                    _before_committed_record_write(home_fs, relative, record)
                    manifest_pin.verify()
                    _verify_recovery_final_state(home_fs, manifest, record)
                    record["state"] = "committed"
                    home_fs.write_atomic(relative, _record_bytes(record), 0o600)
                manifest_pin.verify()
                _TRANSACTION_PATHS[manifest.transaction_id] = path
                home_fs.verify_lock_identity()
                return manifest
            finally:
                manifest_pin.close()
        prior_manifest = _prior_manifest_content(record)
        if current_manifest not in {None, prior_manifest}:
            raise PublicationIndeterminateError(
                "manifest publication remains indeterminate; unexpected manifest preserved"
            )
        legacy = record.get("legacy_link_transition")
        if (
            legacy is not None
            and record["state"] in {"prepared", "indeterminate"}
        ):
            _restore_legacy_destination_before_evidence(
                home_fs,
                record,
                relative,
            )
        _restore_unauthorized_runtime_cache_before_evidence(home_fs, record)
        _validate_transaction_evidence(home_fs, record, relative)
        if (
            record["state"] in {"prepared", "indeterminate"}
            and current_manifest == prior_manifest
            and any(
                operation["kind"] == _RUNTIME_CACHE_REMOVAL
                for operation in record["operations"]
            )
        ):
            _rollback_record(home_fs, record, relative, retain_completed=True)
            _TRANSACTION_PATHS[manifest.transaction_id] = path
            home_fs.verify_lock_identity()
            return manifest
        if (
            legacy is not None
            and record["state"] in {"prepared", "indeterminate"}
            and (
                home_fs.matches_symlink(
                    Path(legacy["destination"]),
                    legacy["expected_link_text"],
                )
                or not home_fs.exists(Path(legacy["destination"]))
                and home_fs.matches_symlink(
                    Path(legacy["retained_entry"]),
                    legacy["expected_link_text"],
                )
            )
        ):
            _rollback_record(home_fs, record, relative, retain_completed=True)
            _TRANSACTION_PATHS[manifest.transaction_id] = path
            home_fs.verify_lock_identity()
            return manifest
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
            if operation["kind"] in _REMOVAL_KINDS and home_fs.exists(destination):
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
        _before_committed_record_write(home_fs, relative, record)
        record["state"] = "committed"
        home_fs.write_atomic(relative, _record_bytes(record), 0o600)
        _TRANSACTION_PATHS[manifest.transaction_id] = path
        home_fs.verify_lock_identity()
        return manifest


class _PinnedRecoveryManifest:
    """Descriptor-bound candidate manifest retained through recovery classification."""

    def __init__(self, home_fs: _HomeFS, path: Path, expected: bytes) -> None:
        self._home_fs = home_fs
        self.path = path
        self.expected = expected
        with home_fs.parent(path) as (parent, leaf):
            self.descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=parent,
            )
        try:
            os.set_inheritable(self.descriptor, False)
            observed = os.fstat(self.descriptor)
            self.identity = _status_identity(observed)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_nlink != 1
                or stat.S_IMODE(observed.st_mode) != _OWNERSHIP_MANIFEST_MODE
                or _read_status_descriptor(self.descriptor) != expected
            ):
                raise ValueError("recovery ownership manifest is not exact")
        except BaseException:
            os.close(self.descriptor)
            raise

    def verify(self) -> None:
        if os.get_inheritable(self.descriptor):
            raise RuntimeError("recovery manifest descriptor must be close-on-exec")
        try:
            canonical = self._home_fs.stat(self.path)
        except OSError as exc:
            raise PublicationIndeterminateError(
                "manifest publication changed during recovery; evidence retained"
            ) from exc
        if (
            _status_identity(canonical) != self.identity
            or _status_identity(os.fstat(self.descriptor)) != self.identity
            or _read_status_descriptor(self.descriptor) != self.expected
        ):
            raise PublicationIndeterminateError(
                "manifest publication changed during recovery; evidence retained"
            )

    def close(self) -> None:
        with suppress(OSError):
            os.close(self.descriptor)


def _runtime_python_source_for_cache(candidate: Path) -> Path | None:
    cache_tag = sys.implementation.cache_tag
    if (
        type(cache_tag) is not str
        or not cache_tag
        or candidate.parent.name != "__pycache__"
    ):
        return None
    suffix = f".{cache_tag}.pyc"
    if not candidate.name.endswith(suffix):
        return None
    source_name = candidate.name.removesuffix(suffix)
    if not source_name:
        return None
    source = candidate.parent.parent / f"{source_name}.py"
    try:
        expected = Path(importlib.util.cache_from_source(str(source)))
    except (NotImplementedError, ValueError):
        return None
    return source if expected == candidate else None


def _is_valid_runtime_python_cache(
    home_fs: _HomeFS, candidate: Path, sources: tuple[Path, ...]
) -> bool:
    source = _runtime_python_source_for_cache(candidate)
    if source is None or source not in sources:
        return False
    try:
        cache_stat = home_fs.stat(candidate)
        source_stat = home_fs.stat(source)
        cache = home_fs.read_file(candidate)
        source_bytes = home_fs.read_file(source)
    except OSError:
        return False
    return _runtime_python_cache_content_is_valid(
        cache,
        cache_stat,
        source_bytes,
        source_stat,
    )


def _runtime_python_cache_content_is_valid(
    cache: bytes,
    cache_stat: os.stat_result,
    source_bytes: bytes,
    source_stat: os.stat_result,
) -> bool:
    if (
        not stat.S_ISREG(cache_stat.st_mode)
        or stat.S_IMODE(cache_stat.st_mode) != 0o644
        or not stat.S_ISREG(source_stat.st_mode)
    ):
        return False
    if len(cache) < 16 or cache[:4] != importlib.util.MAGIC_NUMBER or cache[4:8] != b"\0\0\0\0":
        return False
    timestamp = int.from_bytes(cache[8:12], "little")
    size = int.from_bytes(cache[12:16], "little")
    if timestamp != int(source_stat.st_mtime) or size != len(source_bytes):
        return False
    try:
        observed = marshal.loads(cache[16:])
        if not isinstance(observed, type(compile("", "", "exec"))):
            return False
        expected = compile(source_bytes, observed.co_filename, "exec", dont_inherit=True)
    except (SyntaxError, ValueError, TypeError):
        return False
    return observed == expected and marshal.dumps(observed) == cache[16:]


def _runtime_cache_provenance(
    candidate: Path,
    source: Path,
    cache: bytes,
    cache_stat: os.stat_result,
    source_bytes: bytes,
    source_stat: os.stat_result,
) -> dict[str, object] | None:
    """Return interpreter-independent evidence after live authorization."""
    if not _runtime_python_cache_content_is_valid(cache, cache_stat, source_bytes, source_stat):
        return None
    cache_tag = _runtime_cache_tag_for_source(candidate, source)
    if cache_tag is None:
        return None
    return {
        "source": source.as_posix(),
        "cache_tag": cache_tag,
        "magic_number": cache[:4].hex(),
        "timestamp": int.from_bytes(cache[8:12], "little"),
        "source_size": int.from_bytes(cache[12:16], "little"),
        "identity": {"device": cache_stat.st_dev, "inode": cache_stat.st_ino},
    }


def _runtime_cache_tag_for_source(candidate: Path, source: Path) -> str | None:
    if candidate.parent != source.parent / "__pycache__":
        return None
    prefix = f"{source.stem}."
    suffix = ".pyc"
    if not candidate.name.startswith(prefix) or not candidate.name.endswith(suffix):
        return None
    cache_tag = candidate.name[len(prefix) : -len(suffix)]
    if (
        not cache_tag
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in cache_tag)
    ):
        return None
    return cache_tag


def _decode_runtime_cache_provenance(
    value: object,
    *,
    destination: Path,
    source: Path,
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid runtime cache removal provenance")
    _require_exact_keys(
        value,
        {"source", "cache_tag", "magic_number", "timestamp", "source_size", "identity"},
        label="runtime cache removal provenance",
    )
    source_text = value.get("source")
    cache_tag = value.get("cache_tag")
    magic_number = value.get("magic_number")
    timestamp = value.get("timestamp")
    source_size = value.get("source_size")
    identity = value.get("identity")
    if (
        type(source_text) is not str
        or _canonical_relative_text(source_text, label="runtime cache provenance source") != source
        or type(cache_tag) is not str
        or _runtime_cache_tag_for_source(destination, source) != cache_tag
        or type(magic_number) is not str
        or len(magic_number) != 8
        or any(character not in "0123456789abcdef" for character in magic_number)
        or type(timestamp) is not int
        or not 0 <= timestamp < 2**32
        or type(source_size) is not int
        or not 0 <= source_size < 2**32
        or not isinstance(identity, dict)
    ):
        raise ValueError("invalid runtime cache removal provenance")
    _require_exact_keys(identity, {"device", "inode"}, label="runtime cache identity")
    if (
        type(identity.get("device")) is not int
        or identity["device"] < 0
        or type(identity.get("inode")) is not int
        or identity["inode"] < 0
    ):
        raise ValueError("invalid runtime cache removal provenance")
    return value


def _runtime_cache_matches_provenance(
    cache: bytes,
    cache_stat: os.stat_result,
    source_bytes: bytes,
    source_stat: os.stat_result,
    provenance: dict[str, object],
) -> bool:
    return (
        stat.S_ISREG(cache_stat.st_mode)
        and stat.S_IMODE(cache_stat.st_mode) == 0o644
        and stat.S_ISREG(source_stat.st_mode)
        and len(cache) >= 16
        and (cache_stat.st_dev, cache_stat.st_ino)
        == (
            provenance["identity"]["device"],
            provenance["identity"]["inode"],
        )
        and cache[:4].hex() == provenance["magic_number"]
        and cache[4:8] == b"\0\0\0\0"
        and int.from_bytes(cache[8:12], "little") == provenance["timestamp"]
        and int.from_bytes(cache[12:16], "little") == provenance["source_size"]
        and int(source_stat.st_mtime) == provenance["timestamp"]
        and len(source_bytes) == provenance["source_size"]
    )


def _recorded_runtime_cache_is_valid(
    home_fs: _HomeFS,
    record: dict[str, Any],
    operation: dict[str, Any],
) -> bool:
    cache = Path(operation["destination"])
    source = Path(operation["runtime_source"])
    if record["schema_version"] < _TRANSACTION_SCHEMA_VERSION:
        return _is_valid_runtime_python_cache(home_fs, cache, (source,))
    try:
        cache_stat = home_fs.stat(cache)
        cache_content = home_fs.read_file(cache)
        source_stat = home_fs.stat(source)
        source_content = home_fs.read_file(source)
    except OSError:
        return False
    return _runtime_cache_matches_provenance(
        cache_content,
        cache_stat,
        source_content,
        source_stat,
        _decode_runtime_cache_provenance(
            operation["runtime_cache_provenance"],
            destination=cache,
            source=source,
        ),
    )


class _PinnedRuntimeCache:
    """No-follow cache descriptor retained across the mutation boundary."""

    def __init__(self, home_fs: _HomeFS, path: Path, operation: dict[str, Any]) -> None:
        self._home_fs = home_fs
        self.path = path
        self.fingerprint = operation["prior_fingerprint"]
        self.mode = operation["prior_mode"]
        provenance = _decode_runtime_cache_provenance(
            operation["runtime_cache_provenance"],
            destination=path,
            source=Path(operation["runtime_source"]),
        )
        identity = provenance["identity"]
        self.identity = identity["device"], identity["inode"]
        with home_fs.parent(path) as (parent, leaf):
            self.descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=parent,
            )
        try:
            os.set_inheritable(self.descriptor, False)
            self._verify_descriptor()
            current = home_fs.stat(path)
            if (current.st_dev, current.st_ino) != self.identity:
                raise ValueError(f"runtime cache removal changed before mutation: {path}")
        except BaseException:
            os.close(self.descriptor)
            raise

    def _verify_descriptor(self) -> None:
        observed = os.fstat(self.descriptor)
        if (
            os.get_inheritable(self.descriptor)
            or not stat.S_ISREG(observed.st_mode)
            or stat.S_IMODE(observed.st_mode) != self.mode
            or (observed.st_dev, observed.st_ino) != self.identity
            or _fingerprint(_read_status_descriptor(self.descriptor)) != self.fingerprint
        ):
            raise ValueError(f"runtime cache removal changed before mutation: {self.path}")

    def matches_backup(self, backup: Path) -> bool:
        try:
            self._verify_descriptor()
            moved = self._home_fs.stat(backup)
            return (
                stat.S_ISREG(moved.st_mode)
                and stat.S_IMODE(moved.st_mode) == self.mode
                and (moved.st_dev, moved.st_ino) == self.identity
                and self._home_fs.matches_fingerprint_file(backup, self.fingerprint, self.mode)
            )
        except (OSError, ValueError):
            return False

    def close(self) -> None:
        with suppress(OSError):
            os.close(self.descriptor)


def _retain_runtime_cache_removal(
    home_fs: _HomeFS,
    destination: Path,
    backup: Path,
    operation: dict[str, Any],
    pinned_cache: _PinnedRuntimeCache,
) -> None:
    """Move only the authorized cache; restore any replacement before failing."""
    home_fs.replace(destination, backup)
    _after_runtime_cache_backup_move(home_fs, destination, backup, operation)
    if pinned_cache.matches_backup(backup):
        return
    if not home_fs.exists(destination):
        home_fs.move_new(backup, destination)
    raise ValueError(f"runtime cache removal changed during mutation: {destination}")


def _restore_unauthorized_runtime_cache_before_evidence(
    home_fs: _HomeFS,
    record: dict[str, Any],
) -> None:
    """Keep a raced cache reachable before rejecting its transaction authority."""
    if record["schema_version"] < _TRANSACTION_SCHEMA_VERSION:
        return
    for operation in record["operations"]:
        if operation["kind"] != _RUNTIME_CACHE_REMOVAL:
            continue
        destination = Path(operation["destination"])
        backup = Path(operation["backup"])
        provenance = _decode_runtime_cache_provenance(
            operation["runtime_cache_provenance"],
            destination=destination,
            source=Path(operation["runtime_source"]),
        )
        identity = provenance["identity"]
        expected_identity = identity["device"], identity["inode"]
        if home_fs.exists(backup):
            observed = home_fs.stat(backup)
            if (observed.st_dev, observed.st_ino) == expected_identity:
                continue
            if not home_fs.exists(destination):
                with suppress(FileExistsError):
                    home_fs.move_new(backup, destination)
            raise IncompleteRollbackError(
                f"rollback incomplete: unauthorized runtime cache preserved: {destination}"
            )
        if home_fs.exists(destination):
            observed = home_fs.stat(destination)
            if (observed.st_dev, observed.st_ino) != expected_identity:
                raise IncompleteRollbackError(
                    f"rollback incomplete: unauthorized runtime cache preserved: {destination}"
                )


def _authorized_runtime_cache_removal_source(
    home_fs: _HomeFS,
    candidate: Path,
    *,
    managed: dict[Path, tuple[str, int]],
    authorized_sources: dict[Path, Path],
) -> Path | None:
    source = _runtime_python_source_for_cache(candidate)
    if (
        source is None
        or authorized_sources.get(candidate) != source
        or source not in managed
        or not _is_valid_runtime_python_cache(home_fs, candidate, (source,))
    ):
        return None
    return source


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
    retained_home = active.get(_absolute_home(target.home)) if active is not None else None
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
        lock_identity=(retained_home._lock_identity if retained_home is not None else None),
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
                expected_directory_paths = {item.path.as_posix() for item in _directories(files)}
                installed_directories: dict[str, int] = {}
                for directory_path in expected_directory_paths:
                    try:
                        directory_stat = home_fs.stat(Path(directory_path))
                    except OSError:
                        continue
                    if stat.S_ISDIR(directory_stat.st_mode):
                        installed_directories[directory_path] = stat.S_IMODE(directory_stat.st_mode)
                recorded_directories = {
                    item["path"]: item["mode"] for item in manifest_data["directories"]
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
        for root in group.audit_roots:
            try:
                installed = home_fs.scan_tree(root)
            except (FileNotFoundError, OSError):
                continue
            for item in installed:
                candidate = root / item
                if candidate in planned_paths:
                    continue
                if _is_valid_runtime_python_cache(home_fs, candidate, group.runtime_python_sources):
                    continue
                unexpected.add(candidate.as_posix())
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


def _status_identity(item: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
    )


def _read_status_descriptor(descriptor: int) -> bytes:
    offset = 0
    chunks: list[bytes] = []
    while chunk := os.pread(descriptor, 1024 * 1024, offset):
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


class _PinnedStatusFile:
    def __init__(
        self,
        home_fs: _HomeFS,
        path: Path,
        *,
        manifest: bool = False,
    ) -> None:
        self._home_fs = home_fs
        self.path = path
        with home_fs.parent(path) as (parent, leaf):
            self.descriptor = os.open(
                leaf,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=parent,
            )
        try:
            os.set_inheritable(self.descriptor, False)
            observed = os.fstat(self.descriptor)
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                raise ValueError(f"preview evidence file is not regular: {path}")
            if manifest and stat.S_IMODE(observed.st_mode) != _OWNERSHIP_MANIFEST_MODE:
                raise ValueError("ownership manifest mode must be 0o600")
            self.identity = _status_identity(observed)
            if manifest:
                _after_preview_status_manifest_open(home_fs, path, self.descriptor)
            else:
                _after_preview_status_owned_open(path, "file", self.descriptor)
            self.content = _read_status_descriptor(self.descriptor)
        except BaseException:
            os.close(self.descriptor)
            raise

    def verify(self) -> None:
        if os.get_inheritable(self.descriptor):
            raise RuntimeError("preview evidence descriptor must be close-on-exec")
        try:
            canonical = self._home_fs.stat(self.path)
        except OSError as error:
            raise ValueError(f"preview evidence path changed: {self.path}") from error
        if (
            _status_identity(canonical) != self.identity
            or _status_identity(os.fstat(self.descriptor)) != self.identity
            or _read_status_descriptor(self.descriptor) != self.content
        ):
            raise ValueError(f"preview evidence path changed: {self.path}")

    def close(self) -> None:
        with suppress(OSError):
            os.close(self.descriptor)


class _PinnedStatusDirectory:
    def __init__(self, home_fs: _HomeFS, path: Path) -> None:
        self._home_fs = home_fs
        self.path = path
        self.descriptor = home_fs.open_dir(path)
        try:
            os.set_inheritable(self.descriptor, False)
            observed = os.fstat(self.descriptor)
            if not stat.S_ISDIR(observed.st_mode):
                raise ValueError(f"preview evidence directory is invalid: {path}")
            self.identity = _status_identity(observed)
            _after_preview_status_owned_open(path, "directory", self.descriptor)
        except BaseException:
            os.close(self.descriptor)
            raise

    def verify(self) -> None:
        if os.get_inheritable(self.descriptor):
            raise RuntimeError("preview evidence descriptor must be close-on-exec")
        try:
            canonical = self._home_fs.stat(self.path)
        except OSError as error:
            raise ValueError(f"preview evidence path changed: {self.path}") from error
        if (
            _status_identity(canonical) != self.identity
            or _status_identity(os.fstat(self.descriptor)) != self.identity
        ):
            raise ValueError(f"preview evidence path changed: {self.path}")

    def close(self) -> None:
        with suppress(OSError):
            os.close(self.descriptor)


class _RetainedProviderPlanEvidence:
    """Descriptor-backed audit evidence retained until terminal handoff."""

    def __init__(
        self,
        plans: tuple[ProviderPlan, ...],
        pinned: list[_PinnedStatusFile | _PinnedStatusDirectory],
        *,
        require_matches: bool,
        expected_audits: dict[str, DeploymentAudit] | None,
    ) -> None:
        self._plans = plans
        self._pinned = pinned
        self._require_matches = require_matches
        self._expected_audits = expected_audits

    def verify(self) -> None:
        _verify_locked_provider_plan_targets(self._plans)
        try:
            for evidence in self._pinned:
                evidence.verify()
            for target_id in sorted({plan.target.id for plan in self._plans}):
                audit = audit_provider_plans(
                    tuple(plan for plan in self._plans if plan.target.id == target_id)
                )
                expected = (
                    None if self._expected_audits is None else self._expected_audits[target_id]
                )
                if expected is not None and audit != expected:
                    raise ValueError(
                        f"retained audit evidence no longer matches target {target_id!r}"
                    )
                if self._require_matches and not audit.matches:
                    raise ValueError(
                        f"retained audit evidence no longer matches target {target_id!r}"
                    )
            for evidence in self._pinned:
                evidence.verify()
        except ValueError as error:
            if str(error).startswith("retained audit evidence"):
                raise
            raise ValueError(f"retained audit evidence changed: {error}") from error


@contextmanager
def retain_provider_plan_evidence(
    plans: tuple[ProviderPlan, ...],
    *,
    require_matches: bool = True,
    expected_audits: dict[str, DeploymentAudit] | None = None,
) -> Iterator[_RetainedProviderPlanEvidence]:
    """Pin audited manifests, files, and directories through terminal success."""
    _require_supported_platform()
    groups = _validate_and_group(plans)
    active = _GROUP_HOME_LOCKS.get()
    if active is None:
        raise RuntimeError("deployment target lock set is not active")
    target_ids = {group.target.id for group in groups}
    if expected_audits is not None and set(expected_audits) != target_ids:
        raise ValueError("retained audit evidence must name every planned target")
    pinned: list[_PinnedStatusFile | _PinnedStatusDirectory] = []
    try:
        for group in groups:
            home = _absolute_home(group.target.home)
            try:
                home_fs = active[home]
            except KeyError as error:
                raise RuntimeError(
                    "active deployment lock set does not cover every target"
                ) from error
            expected = None if expected_audits is None else expected_audits[group.target.id]
            if expected is None or not expected.validation_errors:
                pinned.append(
                    _PinnedStatusFile(home_fs, _manifest_path(group.target), manifest=True)
                )
            missing = set() if expected is None else set(expected.missing)
            pinned.extend(
                _PinnedStatusFile(home_fs, item.path)
                for item in group.files
                if item.path.as_posix() not in missing
            )
            for directory in _directories(group.files):
                try:
                    pinned.append(_PinnedStatusDirectory(home_fs, directory.path))
                except FileNotFoundError:
                    covered_files = tuple(
                        item.path.as_posix()
                        for item in group.files
                        if item.path.is_relative_to(directory.path)
                    )
                    if (
                        expected is None
                        or not covered_files
                        or not all(path in missing for path in covered_files)
                    ):
                        raise
        authority = _RetainedProviderPlanEvidence(
            plans,
            pinned,
            require_matches=require_matches,
            expected_audits=expected_audits,
        )
        authority.verify()
        yield authority
    finally:
        for evidence in reversed(pinned):
            evidence.close()


def _read_preview_status_evidence(
    target: TargetSpec,
) -> tuple[DeploymentManifest | None, DeploymentAudit | None]:
    """Read and audit one preview manifest under retained cooperative authority."""
    _require_supported_platform()
    if not (
        target.channel == "preview"
        or target.channel.startswith("preview-")
        or target.channel.startswith("unreviewed-local")
    ):
        raise ValueError("preview status evidence requires a preview target")
    try:
        with _target_read_lock(target.home) as home_fs:
            pinned: list[_PinnedStatusFile | _PinnedStatusDirectory] = []
            try:
                manifest_pin = _PinnedStatusFile(home_fs, _manifest_path(target), manifest=True)
                pinned.append(manifest_pin)
                data = _validated_manifest_data(manifest_pin.content, target=target)
                revision = data["source_revision"]
                if (
                    data.get("review_state") != "unreviewed-local"
                    or len(revision) != 64
                    or any(character not in "0123456789abcdef" for character in revision)
                ):
                    raise ValueError("invalid preview manifest review state or fingerprint")
                manifest = DeploymentManifest(
                    schema_version=data["schema_version"],
                    target_id=target.id,
                    framework=target.framework,
                    channel=target.channel,
                    source_revision=revision,
                    provider_ids=tuple(data["provider_ids"]),
                    files=tuple(
                        ManifestFile(Path(item["path"]), item["fingerprint"], item["mode"])
                        for item in data["files"]
                    ),
                    directories=tuple(
                        ManifestDirectory(Path(item["path"]), item["mode"])
                        for item in data["directories"]
                    ),
                    transaction_id=data["transaction_id"],
                    review_state="unreviewed-local",
                )
                missing: list[str] = []
                changed: list[str] = []
                for item in manifest.files:
                    try:
                        evidence = _PinnedStatusFile(home_fs, item.path)
                    except FileNotFoundError:
                        missing.append(item.path.as_posix())
                        continue
                    except (OSError, ValueError):
                        changed.append(item.path.as_posix())
                        continue
                    pinned.append(evidence)
                    if (
                        stat.S_IMODE(os.fstat(evidence.descriptor).st_mode) != item.mode
                        or hashlib.sha256(evidence.content).hexdigest() != item.fingerprint
                    ):
                        changed.append(item.path.as_posix())
                for item in manifest.directories:
                    try:
                        evidence = _PinnedStatusDirectory(home_fs, item.path)
                    except FileNotFoundError:
                        missing.append(item.path.as_posix())
                        continue
                    except (OSError, ValueError):
                        changed.append(item.path.as_posix())
                        continue
                    pinned.append(evidence)
                    if stat.S_IMODE(os.fstat(evidence.descriptor).st_mode) != item.mode:
                        changed.append(item.path.as_posix())
                home_fs.verify_lock_identity()
                for evidence in pinned:
                    evidence.verify()
                audit = DeploymentAudit(
                    target_id=target.id,
                    matches=not (missing or changed),
                    missing=tuple(sorted(missing)),
                    changed=tuple(sorted(changed)),
                )
                return manifest, audit
            finally:
                for evidence in reversed(pinned):
                    evidence.close()
    except FileNotFoundError:
        return None, None
