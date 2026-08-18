"""Native Windows filesystem authority for managed deployment targets.

The implementation pins every ancestor directory with a handle that denies
delete sharing before it uses a pathname below that directory. Windows then
refuses a concurrent rename or junction replacement for the lifetime of the
pin. Reparse points are opened themselves and rejected rather than followed.
"""

from __future__ import annotations

import ctypes
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_LIST_DIRECTORY = 0x0001
FILE_READ_ATTRIBUTES = 0x0080
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
OPEN_ALWAYS = 4
CREATE_NEW = 1
LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
ERROR_LOCK_VIOLATION = 33
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _ByHandleFileInformation(ctypes.Structure):
    _fields_ = [
        ("attributes", ctypes.c_uint32),
        ("creation_time_low", ctypes.c_uint32),
        ("creation_time_high", ctypes.c_uint32),
        ("access_time_low", ctypes.c_uint32),
        ("access_time_high", ctypes.c_uint32),
        ("write_time_low", ctypes.c_uint32),
        ("write_time_high", ctypes.c_uint32),
        ("volume_serial", ctypes.c_uint32),
        ("size_high", ctypes.c_uint32),
        ("size_low", ctypes.c_uint32),
        ("links", ctypes.c_uint32),
        ("file_index_high", ctypes.c_uint32),
        ("file_index_low", ctypes.c_uint32),
    ]


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_void_p),
        ("internal_high", ctypes.c_void_p),
        ("offset", ctypes.c_uint32),
        ("offset_high", ctypes.c_uint32),
        ("event", ctypes.c_void_p),
    ]


@dataclass(frozen=True)
class WindowsIdentity:
    volume: int
    index: int
    attributes: int
    links: int
    size: int

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_reparse_point(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _kernel32() -> Any:
    if os.name != "nt":
        raise OSError("native Windows filesystem authority is unavailable")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.restype = ctypes.c_int
    kernel.GetFileInformationByHandle.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    kernel.GetFileInformationByHandle.restype = ctypes.c_int
    kernel.LockFileEx.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_Overlapped),
    )
    kernel.LockFileEx.restype = ctypes.c_int
    kernel.UnlockFileEx.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_Overlapped),
    )
    kernel.UnlockFileEx.restype = ctypes.c_int
    kernel.ReadFile.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    )
    kernel.ReadFile.restype = ctypes.c_int
    kernel.WriteFile.argtypes = (
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    )
    kernel.WriteFile.restype = ctypes.c_int
    kernel.FlushFileBuffers.argtypes = (ctypes.c_void_p,)
    kernel.FlushFileBuffers.restype = ctypes.c_int
    return kernel


class WindowsHandle:
    def __init__(self, value: int) -> None:
        self.value = value
        self._closed = False

    def __enter__(self) -> WindowsHandle:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self._closed:
            if not _kernel32().CloseHandle(self.value):
                raise ctypes.WinError(ctypes.get_last_error())
            self._closed = True

    def identity(self) -> WindowsIdentity:
        information = _ByHandleFileInformation()
        if not _kernel32().GetFileInformationByHandle(
            self.value,
            ctypes.byref(information),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return WindowsIdentity(
            information.volume_serial,
            information.file_index_high << 32 | information.file_index_low,
            information.attributes,
            information.links,
            information.size_high << 32 | information.size_low,
        )


def _open(
    path: Path,
    *,
    access: int,
    disposition: int = OPEN_EXISTING,
    directory: bool = False,
) -> WindowsHandle:
    flags = FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= FILE_FLAG_BACKUP_SEMANTICS
    value = _kernel32().CreateFileW(
        str(path),
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        disposition,
        flags,
        None,
    )
    if value == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    return WindowsHandle(value)


def _normal_absolute(path: Path) -> Path:
    raw = os.path.abspath(os.fspath(path.expanduser()))
    normalized = Path(os.path.normcase(raw))
    windows = PureWindowsPath(normalized)
    if not windows.is_absolute() or windows.anchor == str(windows):
        raise ValueError(f"unsafe Windows managed path: {path}")
    return normalized


class WindowsPathPins:
    """Retained non-delete-sharing handles for one canonical directory chain."""

    def __init__(self, directory: Path, *, create: bool) -> None:
        self.path = _normal_absolute(directory)
        self._stack = ExitStack()
        self.identities: list[WindowsIdentity] = []
        windows = PureWindowsPath(self.path)
        current = Path(windows.anchor)
        self._pin_directory(current)
        for part in windows.parts[1:]:
            current /= part
            try:
                self._pin_directory(current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(current)
                self._pin_directory(current)

    def _pin_directory(self, path: Path) -> None:
        handle = self._stack.enter_context(
            _open(
                path,
                access=FILE_LIST_DIRECTORY | FILE_READ_ATTRIBUTES,
                directory=True,
            )
        )
        identity = handle.identity()
        if identity.is_reparse_point or not identity.is_directory:
            raise ValueError(f"reparse-point managed directory is not allowed: {path}")
        self.identities.append(identity)

    def __enter__(self) -> WindowsPathPins:
        return self

    def __exit__(self, *_args: object) -> None:
        self._stack.close()


class WindowsTargetLock:
    def __init__(
        self,
        home: Path,
        *,
        shared: bool = False,
        create: bool = True,
        lock_name: str = ".agentops-deployment.lock",
    ) -> None:
        self.home = _normal_absolute(home)
        self.shared = shared
        self.create = create
        if not lock_name or Path(lock_name).name != lock_name:
            raise ValueError("Windows lock name must be one path component")
        self.lock_name = lock_name
        self._stack = ExitStack()
        self._overlapped = _Overlapped()
        self._locked = False

    def __enter__(self) -> WindowsTargetLock:
        home_pins = self._stack.enter_context(WindowsPathPins(self.home, create=self.create))
        self.home_identity = home_pins.identities[-1]
        lock_path = self.home / self.lock_name
        lock = self._stack.enter_context(
            _open(
                lock_path,
                access=GENERIC_READ | GENERIC_WRITE,
                disposition=OPEN_ALWAYS if self.create else OPEN_EXISTING,
            )
        )
        lock_identity = lock.identity()
        if lock_identity.is_directory or lock_identity.is_reparse_point or lock_identity.links != 1:
            raise ValueError("deployment lock is not a regular single-link file")
        flags = 0 if self.shared else LOCKFILE_EXCLUSIVE_LOCK
        if not _kernel32().LockFileEx(
            lock.value,
            flags,
            0,
            1,
            0,
            ctypes.byref(self._overlapped),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        self._locked = True
        self.lock = lock
        self.lock_identity = lock_identity
        return self

    def __exit__(self, *_args: object) -> None:
        if self._locked:
            if not _kernel32().UnlockFileEx(
                self.lock.value,
                0,
                1,
                0,
                ctypes.byref(self._overlapped),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            self._locked = False
        self._stack.close()

    def verify(self) -> None:
        if self.lock.identity() != self.lock_identity:
            raise ValueError("deployment lock identity changed")
        with WindowsPathPins(self.home, create=False) as observed:
            current_home = observed.identities[-1]
            if (current_home.volume, current_home.index) != (
                self.home_identity.volume,
                self.home_identity.index,
            ):
                raise ValueError("deployment canonical home identity changed")

    @contextmanager
    def pin_parent(self, relative: Path, *, create: bool = False) -> Iterator[Path]:
        relative = safe_relative(relative)
        parent = self.home / relative.parent
        with WindowsPathPins(parent, create=create):
            self.verify()
            yield parent

    def exists(self, relative: Path) -> bool:
        relative = safe_relative(relative)
        try:
            with (
                self.pin_parent(relative),
                _open(
                    self.home / relative,
                    access=FILE_READ_ATTRIBUTES,
                    directory=True,
                ) as handle,
            ):
                if handle.identity().is_reparse_point:
                    raise ValueError(f"reparse-point managed entry is not allowed: {relative}")
            return True
        except FileNotFoundError:
            return False

    @contextmanager
    def open_entry(self, relative: Path, *, directory: bool = False) -> Iterator[WindowsHandle]:
        relative = safe_relative(relative)
        with self.pin_parent(relative):
            handle = _open(
                self.home / relative,
                access=(FILE_LIST_DIRECTORY if directory else GENERIC_READ) | FILE_READ_ATTRIBUTES,
                directory=directory,
            )
            try:
                identity = handle.identity()
                if identity.is_reparse_point:
                    raise ValueError(f"reparse-point managed entry is not allowed: {relative}")
                if identity.is_directory != directory:
                    raise ValueError(f"managed entry has the wrong type: {relative}")
                yield handle
            finally:
                handle.close()

    def identity(self, relative: Path, *, directory: bool = False) -> WindowsIdentity:
        with self.open_entry(relative, directory=directory) as handle:
            return handle.identity()

    def read_file(self, relative: Path, *, maximum: int = 64 * 1024 * 1024) -> bytes:
        with self.open_entry(relative) as handle:
            identity = handle.identity()
            if identity.size > maximum:
                raise ValueError(f"managed file exceeds supported size: {relative}")
            remaining = identity.size
            chunks: list[bytes] = []
            while remaining:
                size = min(remaining, 1024 * 1024)
                buffer = ctypes.create_string_buffer(size)
                read = ctypes.c_uint32()
                if not _kernel32().ReadFile(
                    handle.value,
                    buffer,
                    size,
                    ctypes.byref(read),
                    None,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if read.value == 0:
                    break
                chunks.append(buffer.raw[: read.value])
                remaining -= read.value
            content = b"".join(chunks)
            if len(content) != identity.size:
                raise OSError(f"managed file changed while reading: {relative}")
            if handle.identity() != identity:
                raise OSError(f"managed file identity changed while reading: {relative}")
            return content

    def read_optional(self, relative: Path) -> bytes | None:
        try:
            return self.read_file(relative)
        except FileNotFoundError:
            return None

    def write_new(self, relative: Path, content: bytes) -> None:
        relative = safe_relative(relative)
        with (
            self.pin_parent(relative, create=True),
            _open(
                self.home / relative,
                access=GENERIC_READ | GENERIC_WRITE,
                disposition=CREATE_NEW,
            ) as handle,
        ):
            if handle.identity().is_reparse_point:
                raise ValueError(f"reparse-point staged file is not allowed: {relative}")
            view = memoryview(content)
            while view:
                chunk = view[: 1024 * 1024]
                buffer = ctypes.create_string_buffer(chunk.tobytes())
                written = ctypes.c_uint32()
                if not _kernel32().WriteFile(
                    handle.value,
                    buffer,
                    len(chunk),
                    ctypes.byref(written),
                    None,
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if written.value != len(chunk):
                    raise OSError(f"short staged write: {relative}")
                view = view[written.value :]
            if not _kernel32().FlushFileBuffers(handle.value):
                raise ctypes.WinError(ctypes.get_last_error())
        if self.read_file(relative) != content:
            raise OSError(f"staged file content mismatch: {relative}")

    def write_atomic(self, relative: Path, content: bytes) -> None:
        relative = safe_relative(relative)
        temporary = relative.with_name(f".{relative.name}.{os.urandom(16).hex()}.tmp")
        self.write_new(temporary, content)
        self.replace(temporary, relative, replace=True)

    def replace(self, source: Path, destination: Path, *, replace: bool) -> None:
        source = safe_relative(source)
        destination = safe_relative(destination)
        with ExitStack() as pins:
            pins.enter_context(self.pin_parent(source))
            pins.enter_context(self.pin_parent(destination, create=True))
            source_identity = self.identity(source)
            if self.exists(destination):
                destination_identity = self.identity(destination)
                if destination_identity.is_reparse_point:
                    raise ValueError(f"reparse-point destination is not allowed: {destination}")
                if not replace:
                    raise FileExistsError(destination)
            os.replace(self.home / source, self.home / destination)
            if self.identity(destination) != source_identity:
                raise OSError(f"replacement identity changed: {destination}")

    def unlink(self, relative: Path) -> None:
        relative = safe_relative(relative)
        with self.pin_parent(relative):
            self.identity(relative)
            os.unlink(self.home / relative)

    def remove_empty_dir(self, relative: Path) -> bool:
        relative = safe_relative(relative)
        with self.pin_parent(relative):
            try:
                os.rmdir(self.home / relative)
            except FileNotFoundError:
                return False
            except OSError as error:
                if error.winerror in {145}:  # directory not empty
                    return False
                raise
            return True

    def move_directory(self, source: Path, destination: Path) -> None:
        source = safe_relative(source)
        destination = safe_relative(destination)
        with ExitStack() as pins:
            pins.enter_context(self.pin_parent(source))
            pins.enter_context(self.pin_parent(destination, create=True))
            source_identity = self.identity(source, directory=True)
            if self.exists(destination):
                raise FileExistsError(destination)
            os.replace(self.home / source, self.home / destination)
            if self.identity(destination, directory=True) != source_identity:
                raise OSError(f"directory move identity changed: {destination}")

    def remove_tree(self, relative: Path) -> None:
        relative = safe_relative(relative)
        if not self.exists(relative):
            return
        entries = self.scan(relative)
        for child, kind in sorted(
            entries.items(),
            key=lambda item: len(item[0].parts),
            reverse=True,
        ):
            path = relative / child
            if kind == "directory":
                if not self.remove_empty_dir(path):
                    raise OSError(f"managed directory is not empty: {path}")
            else:
                self.unlink(path)
        if not self.remove_empty_dir(relative):
            raise OSError(f"managed directory is not empty: {relative}")

    def scan(self, relative: Path) -> dict[Path, str]:
        relative = safe_relative(relative)
        found: dict[Path, str] = {}

        def visit(prefix: Path) -> None:
            with (
                self.open_entry(relative / prefix, directory=True),
                os.scandir(self.home / relative / prefix) as entries,
            ):
                for entry in entries:
                    child = prefix / entry.name
                    authored = relative / child
                    if entry.is_symlink():
                        raise ValueError(f"reparse-point managed entry is not allowed: {authored}")
                    identity = self.identity(authored, directory=entry.is_dir(False))
                    if identity.is_directory:
                        found[child] = "directory"
                        visit(child)
                    else:
                        found[child] = "regular"

        visit(Path("."))
        return found


def safe_relative(path: Path) -> Path:
    windows = PureWindowsPath(path)
    if (
        path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or not path.parts
        or path == Path(".")
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(part == ".." for part in windows.parts)
    ):
        raise ValueError(f"unsafe managed path: {path}")
    return path


def synthetic_mode(identity: WindowsIdentity, planned_mode: int) -> int:
    kind = stat.S_IFDIR if identity.is_directory else stat.S_IFREG
    return kind | planned_mode
