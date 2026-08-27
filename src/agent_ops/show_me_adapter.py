from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None

PINNED_REPO = "https://github.com/humanlayer/skills.git"
PINNED_REF = "4d8d644ca747517973f58d7953f58d7cd07520cd"
SKILL_NAME = "show-me"
SOURCE_RELATIVE = Path("plugins/show-me/skills/show-me")
OWNERSHIP_MANIFEST_RELATIVE = Path(".agentops/skill-dependencies/humanlayer-show-me.json")
_LOCK_NAME = "humanlayer-show-me.lock"
_TRANSACTION_NAME = "humanlayer-show-me-transaction.json"
_STAGE_PREFIX = ".humanlayer-show-me-stage-"
_BACKUP_PREFIX = ".humanlayer-show-me-backup-"
_DELETE_PREFIX = ".humanlayer-show-me-preserved-"


class ShowMeCollisionError(RuntimeError):
    """Installation would overwrite a skill Agent Ops cannot prove it owns."""


@dataclass(frozen=True)
class _WindowsDirectoryPin:
    path: Path
    identity: tuple[int, int]
    native_handle: int | None = None
    can_rename: bool = False


@contextmanager
def _pin_windows_directory(
    path: Path,
    *,
    allow_rename: bool = False,
) -> Iterator[_WindowsDirectoryPin]:
    """Keep a directory immutable while Windows path APIs operate below it."""

    if os.name != "nt":
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            raise ShowMeCollisionError(f"unsafe Windows transaction directory: {path}")
        pin = _WindowsDirectoryPin(
            path=path,
            identity=(info.st_dev, info.st_ino),
            can_rename=allow_rename,
        )
        yield pin
        return

    pin = _open_native_windows_directory_pin(path, allow_rename=allow_rename)
    try:
        yield pin
    finally:  # pragma: no cover - exercised on Windows
        import ctypes
        from ctypes import wintypes

        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(pin.native_handle))


def _open_native_windows_directory_pin(
    path: Path,
    *,
    allow_rename: bool = False,
    observe_only: bool = False,
) -> _WindowsDirectoryPin:
    """Open a no-follow directory handle that denies rename/delete sharing."""

    import ctypes
    from ctypes import wintypes

    delete_access = 0x00010000
    file_list_directory = 0x0001
    file_add_file = 0x0002
    file_add_subdirectory = 0x0004
    file_read_attributes = 0x0080
    file_share_read = 0x00000001
    file_share_write = 0x00000002
    file_share_delete = 0x00000004
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        file_list_directory
        | file_add_file
        | file_add_subdirectory
        | file_read_attributes
        | (delete_access if allow_rename else 0),
        file_share_read | file_share_write | (file_share_delete if observe_only else 0),
        None,
        open_existing,
        file_flag_open_reparse_point | file_flag_backup_semantics,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ShowMeCollisionError(f"cannot pin Windows transaction directory: {path}")
    try:
        identity, attributes = _windows_handle_identity(handle)
        directory_attribute = 0x00000010
        reparse_attribute = 0x00000400
        if not attributes & directory_attribute or attributes & reparse_attribute:
            raise ShowMeCollisionError(f"unsafe Windows transaction directory: {path}")
        return _WindowsDirectoryPin(
            path=path,
            identity=identity,
            native_handle=int(handle),
            can_rename=allow_rename,
        )
    except BaseException:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise


def _windows_handle_identity(handle: int) -> tuple[tuple[int, int], int]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    information = ByHandleFileInformation()
    get_information = ctypes.windll.kernel32.GetFileInformationByHandle
    get_information.argtypes = (wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation))
    get_information.restype = wintypes.BOOL
    if not get_information(wintypes.HANDLE(handle), ctypes.byref(information)):
        raise ShowMeCollisionError("cannot read Windows directory identity")
    file_id = (information.nFileIndexHigh << 32) | information.nFileIndexLow
    return (
        (information.dwVolumeSerialNumber, file_id),
        information.dwFileAttributes,
    )


def _observe_windows_directory(path: Path) -> _WindowsDirectoryPin:
    if os.name != "nt":
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or path.is_symlink():
            raise ShowMeCollisionError(f"unsafe Windows transaction directory: {path}")
        return _WindowsDirectoryPin(
            path=path,
            identity=(info.st_dev, info.st_ino),
        )
    return _open_native_windows_directory_pin(  # pragma: no cover - Windows only
        path,
        observe_only=True,
    )


def _close_observed_windows_directory(pin: _WindowsDirectoryPin) -> None:
    if pin.native_handle is None:
        return
    import ctypes  # pragma: no cover - Windows only
    from ctypes import wintypes

    ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(pin.native_handle))


def _verify_windows_directory_pin(pin: _WindowsDirectoryPin) -> None:
    try:
        observed = _observe_windows_directory(pin.path)
    except (FileNotFoundError, OSError, ShowMeCollisionError) as exc:
        raise ShowMeCollisionError(
            f"Windows transaction directory identity changed: {pin.path}"
        ) from exc
    try:
        if observed.identity != pin.identity:
            raise ShowMeCollisionError(
                f"Windows transaction directory identity changed: {pin.path}"
            )
    finally:
        _close_observed_windows_directory(observed)


def _verify_windows_directory_pins(*pins: _WindowsDirectoryPin) -> None:
    for pin in pins:
        _verify_windows_directory_pin(pin)


def install_show_me(
    upstream_root: Path,
    destination: Path,
    *,
    upstream_ref: str = PINNED_REF,
    collision_roots: tuple[Path, ...] = (),
    flat_markdown: bool = False,
    collision_policy: str = "generic",
    collision_limits: dict[str, int] | None = None,
    collision_allowed_symlink_targets: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """Adapt and transactionally install the pinned HumanLayer show-me skill."""

    upstream_root = Path(upstream_root)
    destination, profile_root = _validate_destination(Path(destination))
    source = upstream_root / SOURCE_RELATIVE
    _validate_source(upstream_root, source, upstream_ref)
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        return _install_show_me_windows(
            source,
            destination,
            profile_root,
            upstream_ref,
            collision_roots,
            flat_markdown,
            collision_policy,
            collision_limits,
            collision_allowed_symlink_targets,
        )

    root_fd = _ensure_absolute_directory_no_follow(profile_root)
    skills_fd = dependencies_fd = lock_fd = None
    stage_name = f"{_STAGE_PREFIX}{uuid.uuid4().hex}"
    try:
        skills_fd = _ensure_directory(root_fd, "skills")
        agentops_fd = _ensure_directory(root_fd, ".agentops")
        try:
            dependencies_fd = _ensure_directory(agentops_fd, "skill-dependencies")
        finally:
            os.close(agentops_fd)
        lock_fd = os.open(
            _LOCK_NAME,
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
            dir_fd=dependencies_fd,
        )
        _lock_file(lock_fd)
        _recover_transaction(skills_fd, dependencies_fd)
        _stage_skill(source, dependencies_fd, stage_name)
        staged = _fd_path(dependencies_fd, stage_name)
        _copy_upstream_license(upstream_root, staged)
        _adapt_instructions(staged / "SKILL.md")
        manifest = {
            "schema_version": 1,
            "upstream_repo": PINNED_REPO,
            "upstream_ref": upstream_ref,
            "skill": SKILL_NAME,
            "source_fingerprint": _tree_fingerprint(source),
            "installed_fingerprint": _tree_fingerprint(staged),
        }
        for collision_root in collision_roots:
            _reject_external_collision_root(
                collision_root,
                flat_markdown=flat_markdown,
                allowed_fingerprint=manifest["installed_fingerprint"],
                policy=collision_policy,
                limits=collision_limits,
                allowed_symlink_targets=collision_allowed_symlink_targets,
            )
        current, target_fd = _preflight(
            skills_fd,
            dependencies_fd,
            flat_markdown=flat_markdown,
        )
        try:
            _install_transaction(
                skills_fd,
                dependencies_fd,
                stage_name,
                manifest,
                current,
                target_fd=target_fd,
            )
        finally:
            if target_fd is not None:
                os.close(target_fd)
        return manifest
    finally:
        if skills_fd is not None and dependencies_fd is not None:
            _remove_unreferenced_stage(dependencies_fd, dependencies_fd, stage_name)
        if lock_fd is not None:
            os.close(lock_fd)
        if dependencies_fd is not None:
            os.close(dependencies_fd)
        if skills_fd is not None:
            os.close(skills_fd)
        os.close(root_fd)


def _lock_file(descriptor: int) -> None:
    if fcntl is not None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    if msvcrt is None:  # pragma: no cover - defensive platform guard
        raise ShowMeCollisionError("no supported file-locking API is available")
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.fstat(descriptor).st_size == 0:
        os.write(descriptor, b"\0")
        os.fsync(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)


def _install_show_me_windows(
    source: Path,
    destination: Path,
    profile_root: Path,
    upstream_ref: str,
    collision_roots: tuple[Path, ...] = (),
    flat_markdown: bool = False,
    collision_policy: str = "generic",
    collision_limits: dict[str, int] | None = None,
    collision_allowed_symlink_targets: tuple[Path, ...] = (),
) -> dict[str, Any]:
    _ensure_windows_directory(profile_root)
    skills_root = profile_root / "skills"
    agentops_root = profile_root / ".agentops"
    dependencies_root = profile_root / OWNERSHIP_MANIFEST_RELATIVE.parent
    with contextlib.ExitStack() as directory_handles:
        profile_pin = directory_handles.enter_context(_pin_windows_directory(profile_root))
        _ensure_windows_directory(skills_root)
        skills_pin = directory_handles.enter_context(_pin_windows_directory(skills_root))
        _ensure_windows_directory(agentops_root)
        agentops_pin = directory_handles.enter_context(_pin_windows_directory(agentops_root))
        _ensure_windows_directory(dependencies_root)
        dependencies_pin = directory_handles.enter_context(
            _pin_windows_directory(dependencies_root)
        )
        directory_pins = (profile_pin, skills_pin, agentops_pin, dependencies_pin)
        _verify_windows_directory_pins(*directory_pins)
        lock_path = dependencies_root / _LOCK_NAME
        lock_fd = _open_windows_lock(lock_path, dependencies_pin)
        with os.fdopen(lock_fd, "a+b") as lock_stream:
            _lock_file(lock_stream.fileno())
            _recover_windows_transaction(
                skills_root,
                dependencies_root,
                directory_pins=directory_pins,
            )
            stage = dependencies_root / f"{_STAGE_PREFIX}{uuid.uuid4().hex}"
            try:
                _verify_windows_directory_pins(*directory_pins)
                shutil.copytree(source, stage)
                _copy_upstream_license(source.parents[3], stage)
                _adapt_instructions(stage / "SKILL.md")
                _verify_windows_directory_pins(*directory_pins)
                manifest = {
                    "schema_version": 1,
                    "upstream_repo": PINNED_REPO,
                    "upstream_ref": upstream_ref,
                    "skill": SKILL_NAME,
                    "source_fingerprint": _tree_fingerprint(source),
                    "installed_fingerprint": _tree_fingerprint(stage),
                }
                for collision_root in collision_roots:
                    _reject_external_collision_root_windows(
                        collision_root,
                        flat_markdown=flat_markdown,
                        allowed_fingerprint=manifest["installed_fingerprint"],
                        policy=collision_policy,
                        limits=collision_limits,
                        allowed_symlink_targets=collision_allowed_symlink_targets,
                    )
                current = _preflight_windows(
                    skills_root,
                    dependencies_root,
                    destination,
                    flat_markdown=flat_markdown,
                    directory_pins=directory_pins,
                )
                _install_windows_transaction(
                    skills_root,
                    dependencies_root,
                    stage,
                    destination,
                    manifest,
                    current,
                    directory_pins=directory_pins,
                )
                return manifest
            finally:
                _remove_unreferenced_windows_stage(
                    stage,
                    dependencies_root,
                    directory_pins=directory_pins,
                )


def _is_windows_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _ensure_windows_directory(path: Path) -> None:
    parent = path.parent
    if parent != path:
        _ensure_windows_directory(parent)
    if path.is_symlink() or _is_windows_reparse_point(path):
        raise ShowMeCollisionError(
            f"selected profile path contains a symbolic link or junction: {path}"
        )
    if path.exists():
        if not path.is_dir():
            raise ShowMeCollisionError(f"selected profile path contains a non-directory: {path}")
        return
    path.mkdir()
    if path.is_symlink() or _is_windows_reparse_point(path) or not path.is_dir():
        raise ShowMeCollisionError(f"could not create confined profile directory: {path}")


def _open_native_windows_regular_handle(
    path: Path,
    *,
    desired_access: int,
    share_mode: int,
    creation_disposition: int,
) -> int:
    import ctypes
    from ctypes import wintypes

    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(path),
        desired_access,
        share_mode,
        None,
        creation_disposition,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.windll.kernel32.GetLastError()
        if error in {2, 3}:
            raise FileNotFoundError(path)
        raise ShowMeCollisionError(f"cannot open safe Windows state file: {path}")
    try:
        _, attributes = _windows_handle_identity(handle)
        directory_attribute = 0x00000010
        reparse_attribute = 0x00000400
        if attributes & (directory_attribute | reparse_attribute):
            raise ShowMeCollisionError(f"unsafe show-me state path: {path}")
        return int(handle)
    except BaseException:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))
        raise


def _close_native_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))


def _delete_native_windows_handle(handle: int, path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    class FileDispositionInformation(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    information = FileDispositionInformation(True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    file_disposition_info = 4
    if not set_information(
        wintypes.HANDLE(handle),
        file_disposition_info,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        raise ShowMeCollisionError(
            f"cannot delete pinned Windows state file {path}: Windows error {error}"
        )


def _open_windows_lock(
    path: Path,
    parent_pin: _WindowsDirectoryPin | None = None,
) -> int:
    if path.is_symlink() or _is_windows_reparse_point(path):
        raise ShowMeCollisionError(f"unsafe show-me lock path: {path}")
    if parent_pin is not None:
        _verify_windows_directory_pin(parent_pin)

    if os.name != "nt":
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags, 0o600)
    else:  # pragma: no cover - exercised on Windows
        import ctypes
        from ctypes import wintypes

        generic_read = 0x80000000
        generic_write = 0x40000000
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        open_always = 4
        file_attribute_normal = 0x00000080
        file_flag_open_reparse_point = 0x00200000
        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            generic_read | generic_write,
            file_share_read | file_share_write,
            None,
            open_always,
            file_attribute_normal | file_flag_open_reparse_point,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle == invalid_handle:
            raise ShowMeCollisionError(f"cannot open safe show-me lock: {path}")
        try:
            _, attributes = _windows_handle_identity(handle)
            directory_attribute = 0x00000010
            reparse_attribute = 0x00000400
            if attributes & (directory_attribute | reparse_attribute):
                raise ShowMeCollisionError(f"unsafe show-me lock path: {path}")
            descriptor = msvcrt.open_osfhandle(
                int(handle),
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(handle))
            raise

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ShowMeCollisionError(f"unsafe show-me lock path: {path}")
        if parent_pin is not None:
            _verify_windows_directory_pin(parent_pin)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _normalize_windows_final_path(value: str) -> str:
    unc_prefix = "\\\\?\\UNC\\"
    device_prefix = "\\\\?\\"
    if value.upper().startswith(unc_prefix.upper()):
        return "\\\\" + value[len(unc_prefix) :]
    if value.startswith(device_prefix):
        return value[len(device_prefix) :]
    return value


def _validate_windows_profile(profile_root: Path) -> None:
    cursor = Path(profile_root.anchor)
    for part in profile_root.parts[1:]:
        cursor /= part
        if cursor.is_symlink() or _is_windows_reparse_point(cursor):
            raise ShowMeCollisionError(
                f"selected profile path contains a symbolic link or junction: {cursor}"
            )
        if cursor.exists() and not cursor.is_dir():
            raise ShowMeCollisionError(f"selected profile path contains a non-directory: {cursor}")


def _preflight_windows(
    skills_root: Path,
    dependencies_root: Path,
    destination: Path,
    *,
    flat_markdown: bool,
    directory_pins: tuple[_WindowsDirectoryPin, ...] = (),
) -> dict[str, Any] | None:
    _verify_windows_directory_pins(*directory_pins)
    _validate_windows_profile(skills_root)
    _validate_windows_profile(dependencies_root)
    _reject_logical_collisions_path(
        skills_root,
        exclude_managed=True,
        flat_markdown=flat_markdown,
    )
    manifest_path = dependencies_root / OWNERSHIP_MANIFEST_RELATIVE.name
    current = _read_json_path(manifest_path)
    if destination.is_symlink() or _is_windows_reparse_point(destination):
        raise ShowMeCollisionError(f"user-owned collision at {destination}")
    if not destination.exists():
        if current is not None:
            raise ShowMeCollisionError("managed show-me skill is missing")
        _verify_windows_directory_pins(*directory_pins)
        return None
    if not destination.is_dir() or current is None:
        raise ShowMeCollisionError(f"user-owned collision at {destination}")
    _validate_manifest(current)
    if _tree_fingerprint(destination) != current["installed_fingerprint"]:
        raise ShowMeCollisionError("managed show-me skill changed since installation")
    _verify_windows_directory_pins(*directory_pins)
    return current


def _reject_external_collision_root_windows(
    root: Path,
    *,
    flat_markdown: bool,
    allowed_fingerprint: str,
    policy: str = "generic",
    limits: dict[str, int] | None = None,
    allowed_symlink_targets: tuple[Path, ...] = (),
) -> None:
    if policy != "generic":
        if not root.exists():
            return
        _reject_host_visible_collisions_path(
            root,
            policy=policy,
            limits=limits,
            allowed_symlink_targets=allowed_symlink_targets,
            allowed_fingerprint=allowed_fingerprint,
        )
        return
    if not root.exists():
        return
    _reject_logical_collisions_path(
        root,
        exclude_managed=False,
        flat_markdown=flat_markdown,
        allowed_fingerprint=allowed_fingerprint,
    )


def _reject_logical_collisions_path(
    skills_root: Path,
    *,
    exclude_managed: bool,
    flat_markdown: bool,
    allowed_fingerprint: str | None = None,
) -> None:
    visited: set[tuple[int, int]] = set()

    def visit(directory: Path, relative: Path) -> None:
        identity = directory.stat()
        inode = (identity.st_dev, identity.st_ino)
        if inode in visited:
            return
        visited.add(inode)
        for child in sorted(directory.iterdir(), key=lambda path: path.name):
            child_relative = relative / child.name
            if (
                not relative.parts
                and exclude_managed
                and (
                    child.name == SKILL_NAME
                    or child.name.startswith(_STAGE_PREFIX)
                    or child.name.startswith(_BACKUP_PREFIX)
                )
            ):
                continue
            try:
                is_directory = child.is_dir()
            except OSError as exc:
                raise ShowMeCollisionError(
                    f"cannot verify skill entry at skills/{child_relative.as_posix()}"
                ) from exc
            if is_directory:
                if child.name == SKILL_NAME:
                    if (
                        not relative.parts
                        and allowed_fingerprint is not None
                        and _tree_fingerprint(child) == allowed_fingerprint
                    ):
                        continue
                    raise ShowMeCollisionError(
                        "logical skill-path collision for show-me at "
                        f"skills/{child_relative.as_posix()}"
                    )
                skill_file = child / "SKILL.md"
                if skill_file.is_file():
                    _reject_show_me_frontmatter_path(skill_file, child_relative)
                visit(child, child_relative)
            elif flat_markdown and not relative.parts and child.suffix.lower() == ".md":
                if child.name.lower() == f"{SKILL_NAME}.md":
                    raise ShowMeCollisionError(
                        f"logical skill-path collision for show-me at skills/{child.name}"
                    )
                _reject_show_me_frontmatter_path(child, Path(child.name))

    root_skill = skills_root / "SKILL.md"
    if root_skill.is_file():
        _reject_show_me_frontmatter_path(root_skill, Path())
    elif root_skill.is_symlink() or _is_windows_reparse_point(root_skill):
        raise ShowMeCollisionError("cannot verify linked skill identity at skills/SKILL.md")
    visit(skills_root, Path())


def _reject_show_me_frontmatter_path(skill_file: Path, relative: Path) -> None:
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ShowMeCollisionError(
            f"cannot verify skill identity at {_skill_location(relative)}"
        ) from exc
    if _frontmatter_skill_name(text, _skill_location(relative)) == SKILL_NAME:
        raise ShowMeCollisionError(
            f"logical skill-name collision for {SKILL_NAME} at {_skill_location(relative)}"
        )


def _load_frontmatter_mapping(
    skill_file: Path, *, max_bytes: int | None = None
) -> dict[str, Any] | None:
    try:
        if max_bytes is not None and skill_file.stat().st_size > max_bytes:
            return None
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = text.removeprefix("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        value = yaml.load("\n".join(lines[1:closing]), Loader=yaml.BaseLoader)
    except (StopIteration, yaml.YAMLError):
        return None
    return value if isinstance(value, dict) else None


def _host_visible_skill_name(skill_file: Path, *, max_bytes: int | None = None) -> str | None:
    value = _load_frontmatter_mapping(skill_file, max_bytes=max_bytes)
    if value is None:
        return None
    name = value.get("name")
    return name.strip() if isinstance(name, str) else None


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _is_agentops_managed_show_me(skill_dir: Path) -> bool:
    profile_root = skill_dir.parent.parent
    manifest_path = profile_root / OWNERSHIP_MANIFEST_RELATIVE
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
        _validate_manifest(value)
    except (OSError, ValueError, ShowMeCollisionError):
        return False
    return _tree_fingerprint(skill_dir) == value["installed_fingerprint"]


def _reject_candidate_skill(
    skill_file: Path,
    *,
    allowed_fingerprint: str,
    max_bytes: int | None = None,
) -> bool:
    """Return whether an immediate SKILL.md made this directory terminal."""
    if not skill_file.exists():
        return False
    if not skill_file.is_file():
        return True
    name = _host_visible_skill_name(skill_file, max_bytes=max_bytes)
    if name == SKILL_NAME:
        parent = skill_file.parent
        if parent.name == SKILL_NAME:
            if _tree_fingerprint(parent) == allowed_fingerprint or _is_agentops_managed_show_me(
                parent
            ):
                return True
            raise ShowMeCollisionError(f"logical skill-path collision for {SKILL_NAME} at {parent}")
        raise ShowMeCollisionError(f"logical skill-name collision for {SKILL_NAME} at {skill_file}")
    return True


def _reject_host_visible_collisions_path(
    root: Path,
    *,
    policy: str,
    limits: dict[str, int] | None,
    allowed_symlink_targets: tuple[Path, ...],
    allowed_fingerprint: str,
) -> None:
    if policy == "opencode":
        _reject_opencode_collisions(root, allowed_fingerprint=allowed_fingerprint)
    elif policy == "codex":
        _reject_codex_collisions(root, allowed_fingerprint=allowed_fingerprint)
    elif policy == "openclaw":
        _reject_openclaw_collisions(
            root,
            limits=limits or {},
            allowed_symlink_targets=allowed_symlink_targets,
            allowed_fingerprint=allowed_fingerprint,
        )
    else:
        raise ValueError(f"unknown skill collision policy: {policy}")


def _reject_opencode_collisions(root: Path, *, allowed_fingerprint: str) -> None:
    visited: set[tuple[int, int]] = set()

    def visit(directory: Path) -> None:
        try:
            identity = directory.stat()
        except OSError:
            return
        inode = (identity.st_dev, identity.st_ino)
        if inode in visited:
            return
        visited.add(inode)
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            return
        for child in children:
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    visit(child)
                elif child.name == "SKILL.md" and child.is_file():
                    _reject_candidate_skill(
                        child,
                        allowed_fingerprint=allowed_fingerprint,
                    )
            except OSError:
                continue

    root_skill = root / "SKILL.md"
    if root_skill.is_file():
        _reject_candidate_skill(root_skill, allowed_fingerprint=allowed_fingerprint)
    visit(root)


def _reject_codex_collisions(root: Path, *, allowed_fingerprint: str) -> None:
    root = root.resolve()
    queue: list[tuple[Path, int]] = [(root, 0)]
    visited: set[tuple[int, int]] = set()
    directories = entries = response_bytes = 0
    while queue and directories < 2_000 and entries < 20_000 and response_bytes < 4 * 1024 * 1024:
        directory, depth = queue.pop(0)
        try:
            identity = directory.stat()
        except OSError:
            continue
        inode = (identity.st_dev, identity.st_ino)
        if inode in visited:
            continue
        visited.add(inode)
        directories += 1
        children: list[Path] = []
        try:
            with os.scandir(directory) as scanned:
                for entry in scanned:
                    entries += 1
                    child = directory / entry.name
                    response_bytes += len(os.fsencode(str(child))) + 64
                    if entries > 20_000 or response_bytes > 4 * 1024 * 1024:
                        break
                    children.append(child)
        except OSError:
            continue
        for child in sorted(children, key=lambda item: item.name):
            try:
                child_stat = child.lstat()
                if stat.S_ISDIR(child_stat.st_mode) or stat.S_ISLNK(child_stat.st_mode):
                    if depth < 6 and not child.name.startswith(".") and child.is_dir():
                        queue.append((child, depth + 1))
                elif child.name == "SKILL.md" and stat.S_ISREG(child_stat.st_mode):
                    _reject_candidate_skill(child, allowed_fingerprint=allowed_fingerprint)
            except OSError:
                continue


def _reject_openclaw_collisions(
    root: Path,
    *,
    limits: dict[str, int],
    allowed_symlink_targets: tuple[Path, ...],
    allowed_fingerprint: str,
) -> None:
    root = root.resolve()
    max_candidates = limits.get("maxCandidatesPerRoot", 300)
    max_skills = limits.get("maxSkillsLoadedPerSource", 200)
    max_bytes = limits.get("maxSkillFileBytes", 256_000)
    allowed = tuple(target.resolve() for target in allowed_symlink_targets if target.exists())
    loaded = 0
    visited: set[tuple[int, int]] = set()

    def allowed_directory(directory: Path) -> Path | None:
        try:
            resolved = directory.resolve(strict=True)
        except OSError:
            return None
        if _is_within(root, resolved) or any(_is_within(target, resolved) for target in allowed):
            return resolved
        return None

    def visit(directory: Path, depth: int, max_depth: int) -> None:
        nonlocal loaded
        if loaded >= max_skills:
            return
        resolved = allowed_directory(directory)
        if resolved is None:
            return
        try:
            identity = resolved.stat()
        except OSError:
            return
        inode = (identity.st_dev, identity.st_ino)
        if inode in visited:
            return
        visited.add(inode)
        skill_file = directory / "SKILL.md"
        if skill_file.exists():
            try:
                skill_real = skill_file.resolve(strict=True)
            except OSError:
                return
            if _is_within(resolved, skill_real):
                terminal = _reject_candidate_skill(
                    skill_file,
                    allowed_fingerprint=allowed_fingerprint,
                    max_bytes=max_bytes,
                )
                if terminal and _host_visible_skill_name(skill_file, max_bytes=max_bytes):
                    loaded += 1
            return
        if depth >= max_depth:
            return
        raw_limit = min(10_000, max(1_000, 10 * max_candidates))
        children: list[Path] = []
        try:
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= raw_limit:
                        break
                    if (
                        not entry.name.startswith(".")
                        and entry.name != "node_modules"
                        and entry.is_dir(follow_symlinks=True)
                    ):
                        children.append(directory / entry.name)
        except OSError:
            return
        for child in sorted(children, key=lambda item: item.name)[:max_candidates]:
            is_skills_root = child.name == "skills" and not (child / "SKILL.md").exists()
            child_depth = 0 if is_skills_root else depth + 1
            child_max = 6 if is_skills_root or directory.name == "skills" else max_depth
            visit(child, child_depth, child_max)
            if loaded >= max_skills:
                return

    visit(root, 0, 6 if root.name == "skills" else 2)


def _rename_native_windows_handle(
    handle: int,
    destination_parent: _WindowsDirectoryPin,
    destination_name: str,
    *,
    replace: bool,
    label: Path,
) -> None:
    import ctypes
    from ctypes import wintypes

    destination = destination_parent.path / destination_name
    destination_text = str(destination)

    class RenameControl(ctypes.Union):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("Flags", wintypes.DWORD),
        ]

    class FileRenameInformation(ctypes.Structure):
        _anonymous_ = ("Control",)
        _fields_ = [
            ("Control", RenameControl),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * (len(destination_text) + 1)),
        ]

    # Windows documents NULL as the common RootDirectory case. The absolute path is
    # safe here because the parent handle remains pinned and its identity is checked
    # immediately before and after this exact-handle rename.
    information = FileRenameInformation()
    information.ReplaceIfExists = replace
    information.RootDirectory = None
    information.FileNameLength = len(destination_text.encode("utf-16-le"))
    information.FileName = destination_text
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    file_rename_info = 3
    if not set_information(
        wintypes.HANDLE(handle),
        file_rename_info,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        raise ShowMeCollisionError(
            f"cannot rename pinned Windows object {label}: Windows error {error}"
        )


def _rename_pinned_windows_directory(
    source_pin: _WindowsDirectoryPin,
    destination_parent: _WindowsDirectoryPin,
    destination_name: str,
) -> Path:
    """Rename one verified tree without ever resolving its source name again."""

    if not source_pin.can_rename:
        raise ShowMeCollisionError("Windows directory handle lacks rename access")
    if Path(destination_name).name != destination_name or destination_name in {"", ".", ".."}:
        raise ShowMeCollisionError("unsafe Windows transaction destination name")
    _verify_windows_directory_pin(source_pin)
    _verify_windows_directory_pin(destination_parent)
    destination = destination_parent.path / destination_name
    if _windows_entry_exists(destination):
        raise ShowMeCollisionError(f"Windows transaction destination already exists: {destination}")

    if source_pin.native_handle is None:
        source_pin.path.rename(destination)
    else:  # pragma: no cover - exercised on Windows
        _rename_native_windows_handle(
            source_pin.native_handle,
            destination_parent,
            destination_name,
            replace=False,
            label=source_pin.path,
        )

    # The exact source handle remains open and therefore continues to identify and
    # protect the renamed tree. Reopening the destination with DELETE access would
    # correctly fail against that handle's delete-sharing denial.
    _verify_windows_directory_pin(destination_parent)
    return destination


def _checked_windows_unlink(
    path: Path,
    directory_pins: tuple[_WindowsDirectoryPin, ...],
    *,
    missing_ok: bool = False,
) -> None:
    _verify_windows_directory_pins(*directory_pins)
    if os.name != "nt":
        path.unlink(missing_ok=missing_ok)
    else:  # pragma: no cover - exercised on Windows
        delete_access = 0x00010000
        file_read_attributes = 0x00000080
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        open_existing = 3
        try:
            handle = _open_native_windows_regular_handle(
                path,
                desired_access=delete_access | file_read_attributes,
                share_mode=file_share_read | file_share_write,
                creation_disposition=open_existing,
            )
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        try:
            _delete_native_windows_handle(handle, path)
        finally:
            _close_native_windows_handle(handle)
    _verify_windows_directory_pins(*directory_pins)


def _windows_pin_for_path(
    path: Path,
    directory_pins: tuple[_WindowsDirectoryPin, ...],
) -> _WindowsDirectoryPin:
    for pin in directory_pins:
        if pin.path == path:
            return pin
    raise ShowMeCollisionError(f"Windows transaction directory is not pinned: {path}")


def _install_windows_transaction(
    skills_root: Path,
    dependencies_root: Path,
    stage: Path,
    destination: Path,
    manifest: dict[str, Any],
    current: dict[str, Any] | None,
    *,
    directory_pins: tuple[_WindowsDirectoryPin, ...] = (),
) -> None:
    _verify_windows_directory_pins(*directory_pins)
    skills_pin = _windows_pin_for_path(skills_root, directory_pins)
    dependencies_pin = _windows_pin_for_path(dependencies_root, directory_pins)
    backup = dependencies_root / f"{_BACKUP_PREFIX}{uuid.uuid4().hex}"
    state = {
        "schema_version": 1,
        "phase": "prepared",
        "stage": stage.name,
        "backup": backup.name,
        "had_destination": current is not None,
        "old_manifest": current,
        "new_manifest": manifest,
    }
    transaction = dependencies_root / _TRANSACTION_NAME
    try:
        _validate_windows_profile(skills_root)
        _validate_windows_profile(dependencies_root)
        with contextlib.ExitStack() as exact_trees:
            stage_pin = exact_trees.enter_context(_pin_windows_directory(stage, allow_rename=True))
            _verify_windows_transaction_tree(
                stage,
                manifest["installed_fingerprint"],
                "installation stage",
            )
            destination_pin = None
            if current is not None:
                destination_pin = exact_trees.enter_context(
                    _pin_windows_directory(destination, allow_rename=True)
                )
                _verify_windows_transaction_tree(
                    destination,
                    current["installed_fingerprint"],
                    "managed show-me target",
                )
            elif _windows_entry_exists(destination):
                raise ShowMeCollisionError(f"user-owned collision at {destination}")

            _write_json_path(transaction, state, directory_pins=directory_pins)
            if destination_pin is not None:
                _rename_pinned_windows_directory(
                    destination_pin,
                    dependencies_pin,
                    backup.name,
                )
            _rename_pinned_windows_directory(stage_pin, skills_pin, SKILL_NAME)
            _verify_windows_transaction_tree(
                destination,
                manifest["installed_fingerprint"],
                "installed show-me target",
            )
            _write_json_path(
                dependencies_root / OWNERSHIP_MANIFEST_RELATIVE.name,
                manifest,
                directory_pins=directory_pins,
            )
            state["phase"] = "committed"
            _write_json_path(transaction, state, directory_pins=directory_pins)
        _recover_windows_transaction(
            skills_root,
            dependencies_root,
            directory_pins=directory_pins,
        )
    except BaseException:
        # Never run path-based recovery after an anchored directory changed.
        _verify_windows_directory_pins(*directory_pins)
        _recover_windows_transaction(
            skills_root,
            dependencies_root,
            directory_pins=directory_pins,
        )
        raise


def _recover_windows_transaction(
    skills_root: Path,
    dependencies_root: Path,
    *,
    directory_pins: tuple[_WindowsDirectoryPin, ...] = (),
) -> None:
    _verify_windows_directory_pins(*directory_pins)
    skills_pin = _windows_pin_for_path(skills_root, directory_pins)
    dependencies_pin = _windows_pin_for_path(dependencies_root, directory_pins)
    _validate_windows_profile(skills_root)
    _validate_windows_profile(dependencies_root)
    transaction = dependencies_root / _TRANSACTION_NAME
    state = _read_json_path(transaction)
    if state is None:
        _cleanup_windows_dependency_garbage(
            dependencies_root,
            directory_pins=directory_pins,
        )
        return
    _validate_transaction_state(state)
    destination = skills_root / SKILL_NAME
    stage = dependencies_root / state["stage"]
    backup = dependencies_root / state["backup"]
    old_manifest = state["old_manifest"]
    new_fingerprint = state["new_manifest"]["installed_fingerprint"]

    if state["phase"] == "committed":
        with _pin_windows_directory(destination) as destination_pin:
            _verify_windows_transaction_tree(
                destination,
                new_fingerprint,
                "committed transaction target",
            )
            _verify_windows_directory_pin(destination_pin)
        _checked_windows_unlink(transaction, directory_pins)
        _cleanup_windows_dependency_garbage(
            dependencies_root,
            directory_pins=directory_pins,
        )
        return

    if _windows_entry_exists(destination):
        old_fingerprint = old_manifest["installed_fingerprint"] if old_manifest else None
        with _pin_windows_directory(destination, allow_rename=True) as destination_pin:
            _verify_windows_transaction_tree_shape(
                destination,
                "uncommitted transaction target",
            )
            target_fingerprint = _tree_fingerprint(destination)
            if target_fingerprint == new_fingerprint:
                if _windows_entry_exists(stage):
                    raise ShowMeCollisionError(
                        "duplicate uncommitted show-me target; preserving transaction data"
                    )
                _rename_pinned_windows_directory(
                    destination_pin,
                    dependencies_pin,
                    stage.name,
                )
            elif old_fingerprint is None or target_fingerprint != old_fingerprint:
                raise ShowMeCollisionError(
                    "uncommitted transaction target changed; preserving transaction data"
                )

    if state["had_destination"]:
        if old_manifest is None:
            raise ShowMeCollisionError("transaction is missing its prior manifest")
        if not _windows_entry_exists(destination):
            if not _windows_entry_exists(backup):
                raise ShowMeCollisionError("show-me transaction cannot recover its prior skill")
            with _pin_windows_directory(backup, allow_rename=True) as backup_pin:
                _verify_windows_transaction_tree(
                    backup,
                    old_manifest["installed_fingerprint"],
                    "recovery backup",
                )
                _rename_pinned_windows_directory(backup_pin, skills_pin, SKILL_NAME)
        _write_json_path(
            dependencies_root / OWNERSHIP_MANIFEST_RELATIVE.name,
            old_manifest,
            directory_pins=directory_pins,
        )
    else:
        if _windows_entry_exists(destination):
            raise ShowMeCollisionError("fresh show-me transaction has an unexpected target")
        _checked_windows_unlink(
            dependencies_root / OWNERSHIP_MANIFEST_RELATIVE.name,
            directory_pins,
            missing_ok=True,
        )

    _checked_windows_unlink(transaction, directory_pins)
    _cleanup_windows_dependency_garbage(
        dependencies_root,
        directory_pins=directory_pins,
    )


def _windows_entry_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink() or _is_windows_reparse_point(path)


def _verify_windows_transaction_tree_shape(path: Path, label: str) -> None:
    if (
        not _windows_entry_exists(path)
        or path.is_symlink()
        or _is_windows_reparse_point(path)
        or not path.is_dir()
    ):
        raise ShowMeCollisionError(f"{label} is missing or not a regular directory")


def _verify_windows_transaction_tree(
    path: Path,
    expected_fingerprint: str,
    label: str,
) -> None:
    _verify_windows_transaction_tree_shape(path, label)
    if _tree_fingerprint(path) != expected_fingerprint:
        raise ShowMeCollisionError(f"{label} changed; preserving transaction data")


def _preserve_windows_transaction_tree(
    path: Path,
    dependencies_pin: _WindowsDirectoryPin,
) -> Path:
    """Quarantine an exact tree; never recurse through Windows reparse points."""

    with _pin_windows_directory(path, allow_rename=True) as tree_pin:
        _verify_windows_transaction_tree_shape(path, "show-me transaction garbage")
        preserved_name = f"{_DELETE_PREFIX}{uuid.uuid4().hex}"
        return _rename_pinned_windows_directory(
            tree_pin,
            dependencies_pin,
            preserved_name,
        )


def _cleanup_windows_dependency_garbage(
    dependencies_root: Path,
    *,
    directory_pins: tuple[_WindowsDirectoryPin, ...] = (),
) -> None:
    _verify_windows_directory_pins(*directory_pins)
    if _windows_entry_exists(dependencies_root / _TRANSACTION_NAME):
        return
    dependencies_pin = _windows_pin_for_path(dependencies_root, directory_pins)
    for path in sorted(dependencies_root.iterdir(), key=lambda candidate: candidate.name):
        if path.name.startswith(_STAGE_PREFIX) or path.name.startswith(_BACKUP_PREFIX):
            _preserve_windows_transaction_tree(path, dependencies_pin)


def _remove_unreferenced_windows_stage(
    stage: Path,
    dependencies_root: Path,
    *,
    directory_pins: tuple[_WindowsDirectoryPin, ...] = (),
) -> None:
    try:
        _verify_windows_directory_pins(*directory_pins)
        state = _read_json_path(dependencies_root / _TRANSACTION_NAME)
    except ShowMeCollisionError:
        return
    if isinstance(state, dict) and state.get("stage") == stage.name:
        return
    if _windows_entry_exists(stage):
        dependencies_pin = _windows_pin_for_path(dependencies_root, directory_pins)
        _preserve_windows_transaction_tree(stage, dependencies_pin)


def _write_json_path(
    path: Path,
    value: Any,
    *,
    directory_pins: tuple[_WindowsDirectoryPin, ...] = (),
) -> None:
    _verify_windows_directory_pins(*directory_pins)
    payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if os.name != "nt":
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    else:  # pragma: no cover - exercised on Windows
        delete_access = 0x00010000
        generic_write = 0x40000000
        file_share_read = 0x00000001
        create_new = 1
        parent_pin = _windows_pin_for_path(path.parent, directory_pins)
        temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
        temporary = path.with_name(temporary_name)
        handle = _open_native_windows_regular_handle(
            temporary,
            desired_access=delete_access | generic_write,
            share_mode=file_share_read,
            creation_disposition=create_new,
        )
        try:
            descriptor = msvcrt.open_osfhandle(
                handle,
                os.O_WRONLY | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            _close_native_windows_handle(handle)
            raise
        try:
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
            _rename_native_windows_handle(
                msvcrt.get_osfhandle(descriptor),
                parent_pin,
                path.name,
                replace=True,
                label=temporary,
            )
        finally:
            os.close(descriptor)
    _verify_windows_directory_pins(*directory_pins)


def _read_json_path(path: Path) -> Any | None:
    if os.name != "nt":
        if path.is_symlink() or _is_windows_reparse_point(path):
            raise ShowMeCollisionError(f"unsafe show-me state path: {path}")
        if not path.exists():
            return None
        if not path.is_file():
            raise ShowMeCollisionError(f"unsafe show-me state path: {path}")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ShowMeCollisionError(f"invalid show-me state file: {path}") from exc
    else:  # pragma: no cover - exercised on Windows
        generic_read = 0x80000000
        file_share_read = 0x00000001
        open_existing = 3
        try:
            handle = _open_native_windows_regular_handle(
                path,
                desired_access=generic_read,
                share_mode=file_share_read,
                creation_disposition=open_existing,
            )
        except FileNotFoundError:
            return None
        try:
            descriptor = msvcrt.open_osfhandle(
                handle,
                os.O_RDONLY | getattr(os, "O_BINARY", 0),
            )
        except BaseException:
            _close_native_windows_handle(handle)
            raise
        try:
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 64 * 1024):
                chunks.append(chunk)
            text = b"".join(chunks).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ShowMeCollisionError(f"invalid show-me state file: {path}") from exc
        finally:
            os.close(descriptor)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ShowMeCollisionError(f"invalid show-me state file: {path}") from exc


def _validate_destination(destination: Path) -> tuple[Path, Path]:
    raw = Path(os.path.abspath(destination.expanduser()))
    if raw.name != SKILL_NAME or raw.parent.name != "skills":
        raise ShowMeCollisionError(
            "HumanLayer show-me destination must be <profile>/skills/show-me"
        )
    return raw, raw.parent.parent


def _ensure_absolute_directory_no_follow(path: Path) -> int:
    if not path.is_absolute():
        raise ShowMeCollisionError("selected profile must be an absolute path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as exc:
        os.close(descriptor)
        raise ShowMeCollisionError(
            f"selected profile path contains a symbolic link or non-directory: {path}"
        ) from exc
    return descriptor


def _open_existing_absolute_directory_no_follow(path: Path) -> int | None:
    if not path.is_absolute():
        raise ShowMeCollisionError("collision root must be an absolute path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                next_descriptor = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                os.close(descriptor)
                return None
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as exc:
        os.close(descriptor)
        raise ShowMeCollisionError(
            f"collision root contains a symbolic link or non-directory: {path}"
        ) from exc
    return descriptor


def _ensure_directory(parent_fd: int, name: str) -> int:
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise ShowMeCollisionError(
            f"non-directory or symbolic link blocks show-me installation: {name}"
        ) from exc


def _validate_source(upstream_root: Path, source: Path, upstream_ref: str) -> None:
    if upstream_ref != PINNED_REF:
        raise ValueError(f"expected pinned HumanLayer ref {PINNED_REF}, got {upstream_ref!r}")
    if not (source / "SKILL.md").is_file():
        raise FileNotFoundError(source / "SKILL.md")
    _tree_fingerprint(source)
    if (upstream_root / ".git").exists():
        head = subprocess.run(
            ["git", "-C", str(upstream_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if head != upstream_ref:
            raise ValueError(f"pinned HumanLayer checkout is at {head}, expected {upstream_ref}")
        status_output = subprocess.run(
            [
                "git",
                "-C",
                str(upstream_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if status_output:
            raise ValueError("pinned HumanLayer checkout contains changed or untracked files")


def _stage_skill(source: Path, skills_fd: int, stage_name: str) -> None:
    os.mkdir(stage_name, 0o700, dir_fd=skills_fd)
    shutil.copytree(source, _fd_path(skills_fd, stage_name), dirs_exist_ok=True)


def _copy_upstream_license(upstream_root: Path, staged: Path) -> None:
    license_file = upstream_root / "LICENSE"
    if not license_file.is_file():
        raise ValueError("pinned HumanLayer checkout is missing its LICENSE notice")
    shutil.copy2(license_file, staged / "LICENSE")


def _adapt_instructions(skill_file: Path) -> None:
    text = skill_file.read_text(encoding="utf-8")
    old = """Then open it for the user:

```
Bash(open path/to/show-me-{description}.html)
```"""
    new = (
        "Then give the user its absolute path and use the current agent host's "
        "supported artifact preview or file-opening capability. If the host has no "
        "safe preview capability, provide the path without inventing a tool call."
    )
    if text.count(old) != 1:
        raise ValueError("pinned show-me HTML opener has an unexpected shape")
    skill_file.write_text(text.replace(old, new), encoding="utf-8", newline="\n")


def _preflight(
    skills_fd: int,
    dependencies_fd: int,
    *,
    flat_markdown: bool,
) -> tuple[dict[str, Any] | None, int | None]:
    _reject_logical_collisions(
        skills_fd,
        exclude_managed=True,
        flat_markdown=flat_markdown,
    )
    current = _read_json_at(dependencies_fd, OWNERSHIP_MANIFEST_RELATIVE.name)
    target = _entry_stat(skills_fd, SKILL_NAME)
    if target is None:
        if current is not None:
            raise ShowMeCollisionError("managed show-me skill is missing")
        return None, None
    if not stat.S_ISDIR(target.st_mode):
        raise ShowMeCollisionError("user-owned collision at skills/show-me")
    if current is None:
        raise ShowMeCollisionError("user-owned collision at skills/show-me")
    _validate_manifest(current)
    try:
        target_fd = os.open(
            SKILL_NAME,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=skills_fd,
        )
    except OSError as exc:
        raise ShowMeCollisionError("managed show-me target changed before installation") from exc
    try:
        pinned = os.fstat(target_fd)
        if (pinned.st_dev, pinned.st_ino) != (target.st_dev, target.st_ino):
            raise ShowMeCollisionError("managed show-me target changed before installation")
        actual = _tree_fingerprint(_fd_path(target_fd))
        if actual != current["installed_fingerprint"]:
            raise ShowMeCollisionError("managed show-me skill changed since installation")
    except BaseException:
        os.close(target_fd)
        raise
    return current, target_fd


def _reject_external_collision_root(
    root: Path,
    *,
    flat_markdown: bool,
    allowed_fingerprint: str,
    policy: str = "generic",
    limits: dict[str, int] | None = None,
    allowed_symlink_targets: tuple[Path, ...] = (),
) -> None:
    root = root.resolve()
    if policy != "generic":
        if root.is_dir():
            _reject_host_visible_collisions_path(
                root,
                policy=policy,
                limits=limits,
                allowed_symlink_targets=allowed_symlink_targets,
                allowed_fingerprint=allowed_fingerprint,
            )
        return
    descriptor = _open_existing_absolute_directory_no_follow(root)
    if descriptor is None:
        return
    try:
        _reject_logical_collisions(
            descriptor,
            exclude_managed=False,
            flat_markdown=flat_markdown,
            allowed_fingerprint=allowed_fingerprint,
        )
    finally:
        os.close(descriptor)


def _reject_logical_collisions(
    skills_fd: int,
    *,
    exclude_managed: bool,
    flat_markdown: bool,
    allowed_fingerprint: str | None = None,
) -> None:
    visited: set[tuple[int, int]] = set()

    def visit(directory_fd: int, relative: Path) -> None:
        identity = os.fstat(directory_fd)
        inode = (identity.st_dev, identity.st_ino)
        if inode in visited:
            return
        visited.add(inode)
        for name in sorted(os.listdir(directory_fd)):
            if (
                not relative.parts
                and exclude_managed
                and (
                    name == SKILL_NAME
                    or name.startswith(_STAGE_PREFIX)
                    or name.startswith(_BACKUP_PREFIX)
                )
            ):
                continue
            child_relative = relative / name
            entry = _entry_stat(directory_fd, name)
            if entry is None:
                continue
            child_fd: int | None = None
            if stat.S_ISDIR(entry.st_mode) or stat.S_ISLNK(entry.st_mode):
                try:
                    flags = os.O_RDONLY | os.O_DIRECTORY
                    if stat.S_ISDIR(entry.st_mode):
                        flags |= os.O_NOFOLLOW
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                except (NotADirectoryError, FileNotFoundError):
                    child_fd = None
                except OSError as exc:
                    raise ShowMeCollisionError(
                        f"cannot inspect skill entry at skills/{child_relative.as_posix()}"
                    ) from exc
            if child_fd is not None:
                try:
                    if name == SKILL_NAME:
                        if (
                            not relative.parts
                            and allowed_fingerprint is not None
                            and _tree_fingerprint(_fd_path(child_fd)) == allowed_fingerprint
                        ):
                            continue
                        raise ShowMeCollisionError(
                            "logical skill-path collision for show-me at "
                            f"skills/{child_relative.as_posix()}"
                        )
                    _reject_show_me_frontmatter_at(child_fd, child_relative)
                    visit(child_fd, child_relative)
                finally:
                    os.close(child_fd)
            elif (
                flat_markdown
                and not relative.parts
                and (stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode))
                and name.lower().endswith(".md")
            ):
                if name.lower() == f"{SKILL_NAME}.md":
                    raise ShowMeCollisionError(
                        f"logical skill-path collision for show-me at skills/{name}"
                    )
                _reject_show_me_file_at(
                    directory_fd,
                    name,
                    f"skills/{name}",
                    follow_symlinks=stat.S_ISLNK(entry.st_mode),
                )

    _reject_show_me_frontmatter_at(skills_fd, Path())
    visit(skills_fd, Path())


def _skill_location(relative: Path) -> str:
    if not relative.parts:
        return "skills/SKILL.md"
    return f"skills/{relative.as_posix()}"


def _reject_show_me_frontmatter_at(directory_fd: int, relative: Path) -> None:
    entry = _entry_stat(directory_fd, "SKILL.md")
    _reject_show_me_file_at(
        directory_fd,
        "SKILL.md",
        _skill_location(relative),
        follow_symlinks=entry is not None and stat.S_ISLNK(entry.st_mode),
    )


def _reject_show_me_file_at(
    directory_fd: int,
    name: str,
    location: str,
    *,
    follow_symlinks: bool = False,
) -> None:
    skill = _entry_stat(directory_fd, name)
    if skill is None:
        return
    if not stat.S_ISREG(skill.st_mode) and not (follow_symlinks and stat.S_ISLNK(skill.st_mode)):
        raise ShowMeCollisionError(f"cannot verify skill identity at {location}")
    try:
        flags = os.O_RDONLY | (0 if follow_symlinks else os.O_NOFOLLOW)
        descriptor = os.open(
            name,
            flags,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            text = stream.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise ShowMeCollisionError(f"cannot verify skill identity at {location}") from exc
    if _frontmatter_skill_name(text, location) == SKILL_NAME:
        raise ShowMeCollisionError(f"logical skill-name collision for {SKILL_NAME} at {location}")


def _frontmatter_skill_name(text: str, location: str) -> str | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        value = yaml.load("\n".join(lines[1:closing]), Loader=yaml.BaseLoader)
    except (StopIteration, yaml.YAMLError) as exc:
        raise ShowMeCollisionError(f"cannot parse skill frontmatter at {location}") from exc
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ShowMeCollisionError(f"cannot parse skill frontmatter at {location}")
    name = value.get("name")
    if name is None:
        return None
    if not isinstance(name, str):
        raise ShowMeCollisionError(f"cannot parse skill name at {location}")
    return name.strip()


def _rename_directory_no_replace(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        if (
            renameat2(
                source_fd,
                os.fsencode(source_name),
                destination_fd,
                os.fsencode(destination_name),
                1,
            )
            == 0
        ):
            return
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise ShowMeCollisionError(
                "concurrent show-me target appeared; preserving transaction data"
            )
        raise OSError(error, os.strerror(error))
    if _entry_stat(destination_fd, destination_name) is not None:
        raise ShowMeCollisionError(
            "concurrent show-me target appeared; preserving transaction data"
        )
    os.rename(
        source_name,
        destination_name,
        src_dir_fd=source_fd,
        dst_dir_fd=destination_fd,
    )


def _install_transaction(
    skills_fd: int,
    dependencies_fd: int,
    stage_name: str,
    manifest: dict[str, Any],
    current: dict[str, Any] | None,
    *,
    target_fd: int | None,
) -> None:
    backup_name = f"{_BACKUP_PREFIX}{uuid.uuid4().hex}"
    state = {
        "schema_version": 1,
        "phase": "prepared",
        "stage": stage_name,
        "backup": backup_name,
        "had_destination": current is not None,
        "old_manifest": current,
        "new_manifest": manifest,
    }
    _write_json_at(dependencies_fd, _TRANSACTION_NAME, state)
    try:
        if current is not None:
            os.rename(
                SKILL_NAME,
                backup_name,
                src_dir_fd=skills_fd,
                dst_dir_fd=dependencies_fd,
            )
            backup_stat = _entry_stat(dependencies_fd, backup_name)
            pinned_stat = os.fstat(target_fd) if target_fd is not None else None
            backup_fingerprint = (
                _tree_fingerprint(_fd_path(target_fd)) if target_fd is not None else None
            )
            if (
                backup_stat is None
                or pinned_stat is None
                or (backup_stat.st_dev, backup_stat.st_ino)
                != (pinned_stat.st_dev, pinned_stat.st_ino)
                or backup_fingerprint != current["installed_fingerprint"]
            ):
                _rename_directory_no_replace(
                    dependencies_fd,
                    backup_name,
                    skills_fd,
                    SKILL_NAME,
                )
                os.fsync(skills_fd)
                os.fsync(dependencies_fd)
                _unlink_if_present(dependencies_fd, _TRANSACTION_NAME)
                os.fsync(dependencies_fd)
                raise ShowMeCollisionError(
                    "managed show-me target changed before replacement; restored replacement"
                )
            os.fsync(skills_fd)
            os.fsync(dependencies_fd)
        os.rename(
            stage_name,
            SKILL_NAME,
            src_dir_fd=dependencies_fd,
            dst_dir_fd=skills_fd,
        )
        os.fsync(skills_fd)
        os.fsync(dependencies_fd)
        _write_json_at(
            dependencies_fd,
            OWNERSHIP_MANIFEST_RELATIVE.name,
            manifest,
        )
        state["phase"] = "committed"
        _write_json_at(dependencies_fd, _TRANSACTION_NAME, state)
        _recover_transaction(skills_fd, dependencies_fd)
    except BaseException:
        _recover_transaction(skills_fd, dependencies_fd)
        raise


def _recover_transaction(skills_fd: int, dependencies_fd: int) -> None:
    state = _read_json_at(dependencies_fd, _TRANSACTION_NAME)
    if state is None:
        _cleanup_dependency_garbage(dependencies_fd)
        return
    _validate_transaction_state(state)
    stage = state["stage"]
    backup = state["backup"]
    target_stat = _entry_stat(skills_fd, SKILL_NAME)
    stage_stat = _entry_stat(dependencies_fd, stage)
    backup_stat = _entry_stat(dependencies_fd, backup)
    old_manifest = state["old_manifest"]
    new_fingerprint = state["new_manifest"]["installed_fingerprint"]

    if stage_stat is not None:
        _verify_transaction_tree(
            dependencies_fd,
            stage,
            new_fingerprint,
            "recovery stage",
        )
    if backup_stat is not None:
        if not state["had_destination"] or old_manifest is None:
            raise ShowMeCollisionError("unexpected show-me recovery backup")
        _verify_transaction_tree(
            dependencies_fd,
            backup,
            old_manifest["installed_fingerprint"],
            "recovery backup",
        )

    if state["phase"] == "committed":
        _verify_transaction_tree(
            skills_fd,
            SKILL_NAME,
            new_fingerprint,
            "committed transaction target",
        )
        _unlink_if_present(dependencies_fd, _TRANSACTION_NAME)
        os.fsync(dependencies_fd)
        _cleanup_dependency_garbage(dependencies_fd)
        return

    if target_stat is not None:
        if not stat.S_ISDIR(target_stat.st_mode):
            raise ShowMeCollisionError("uncommitted transaction target is not a regular directory")
        target_fingerprint = _tree_fingerprint(_fd_path(skills_fd, SKILL_NAME))
        old_fingerprint = old_manifest["installed_fingerprint"] if old_manifest else None
        if target_fingerprint == new_fingerprint:
            if stage_stat is not None:
                raise ShowMeCollisionError(
                    "duplicate uncommitted show-me target; preserving transaction data"
                )
            os.rename(
                SKILL_NAME,
                stage,
                src_dir_fd=skills_fd,
                dst_dir_fd=dependencies_fd,
            )
            os.fsync(skills_fd)
            os.fsync(dependencies_fd)
            target_stat = None
            stage_stat = _entry_stat(dependencies_fd, stage)
        elif old_fingerprint is None or target_fingerprint != old_fingerprint:
            raise ShowMeCollisionError(
                "uncommitted transaction target changed; preserving transaction data"
            )

    if state["had_destination"]:
        if old_manifest is None:
            raise ShowMeCollisionError("transaction is missing its prior manifest")
        if target_stat is None:
            if backup_stat is None:
                raise ShowMeCollisionError("show-me transaction cannot recover its prior skill")
            os.rename(
                backup,
                SKILL_NAME,
                src_dir_fd=dependencies_fd,
                dst_dir_fd=skills_fd,
            )
            os.fsync(skills_fd)
            os.fsync(dependencies_fd)
        _write_json_at(
            dependencies_fd,
            OWNERSHIP_MANIFEST_RELATIVE.name,
            old_manifest,
        )
    else:
        if target_stat is not None:
            raise ShowMeCollisionError("fresh show-me transaction has an unexpected target")
        _unlink_if_present(dependencies_fd, OWNERSHIP_MANIFEST_RELATIVE.name)

    _unlink_if_present(dependencies_fd, _TRANSACTION_NAME)
    os.fsync(dependencies_fd)
    _cleanup_dependency_garbage(dependencies_fd)


def _verify_transaction_tree(
    directory_fd: int,
    name: str,
    expected_fingerprint: str,
    label: str,
) -> None:
    entry = _entry_stat(directory_fd, name)
    if entry is None or not stat.S_ISDIR(entry.st_mode):
        raise ShowMeCollisionError(f"{label} is missing or not a regular directory")
    actual = _tree_fingerprint(_fd_path(directory_fd, name))
    if actual != expected_fingerprint:
        raise ShowMeCollisionError(f"{label} changed; preserving transaction data")


def _cleanup_dependency_garbage(dependencies_fd: int) -> None:
    if _entry_stat(dependencies_fd, _TRANSACTION_NAME) is not None:
        return
    for name in sorted(os.listdir(dependencies_fd)):
        if name.startswith(_STAGE_PREFIX) or name.startswith(_BACKUP_PREFIX):
            _remove_directory_if_present(dependencies_fd, name)
    os.fsync(dependencies_fd)


def _remove_unreferenced_stage(
    stage_fd: int,
    dependencies_fd: int,
    stage_name: str,
) -> None:
    try:
        state = _read_json_at(dependencies_fd, _TRANSACTION_NAME)
    except ShowMeCollisionError:
        return
    if isinstance(state, dict) and state.get("stage") == stage_name:
        return
    _remove_directory_if_present(stage_fd, stage_name)


def _validate_manifest(value: Any) -> None:
    required = {
        "schema_version",
        "upstream_repo",
        "upstream_ref",
        "skill",
        "installed_fingerprint",
    }
    if not isinstance(value, dict) or not required <= set(value):
        raise ShowMeCollisionError("invalid show-me ownership manifest")
    if (
        value["schema_version"] != 1
        or value["upstream_repo"] != PINNED_REPO
        or value["skill"] != SKILL_NAME
        or not isinstance(value["installed_fingerprint"], str)
    ):
        raise ShowMeCollisionError("invalid show-me ownership manifest")


def _validate_transaction_state(value: Any) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ShowMeCollisionError("invalid show-me transaction state")
    if value.get("phase") not in {"prepared", "committed"}:
        raise ShowMeCollisionError("invalid show-me transaction phase")
    for key, prefix in (("stage", _STAGE_PREFIX), ("backup", _BACKUP_PREFIX)):
        name = value.get(key)
        if not isinstance(name, str) or not re.fullmatch(
            re.escape(prefix) + r"[A-Za-z0-9_-]+", name
        ):
            raise ShowMeCollisionError("invalid show-me transaction path")
    if not isinstance(value.get("had_destination"), bool):
        raise ShowMeCollisionError("invalid show-me transaction ownership state")
    _validate_manifest(value.get("new_manifest"))
    if value.get("old_manifest") is not None:
        _validate_manifest(value["old_manifest"])


def _write_json_at(directory_fd: int, name: str, value: Any) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        payload = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    os.fsync(directory_fd)


def _read_json_at(directory_fd: int, name: str) -> Any | None:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ShowMeCollisionError(f"unsafe show-me state path: {name}") from exc
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
            return json.load(stream)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShowMeCollisionError(f"invalid show-me state file: {name}") from exc
    finally:
        os.close(descriptor)


def _entry_stat(directory_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _remove_directory_if_present(directory_fd: int, name: str) -> None:
    entry = _entry_stat(directory_fd, name)
    if entry is None:
        return
    if not stat.S_ISDIR(entry.st_mode):
        raise ShowMeCollisionError(f"unsafe show-me transaction entry: {name}")
    shutil.rmtree(_fd_path(directory_fd, name))


def _unlink_if_present(directory_fd: int, name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=directory_fd)


def _fd_path(directory_fd: int, child: str | None = None) -> Path:
    if fcntl is not None and hasattr(fcntl, "F_GETPATH"):
        try:
            buffer = fcntl.fcntl(directory_fd, fcntl.F_GETPATH, b"\0" * 1024)
            encoded = buffer.split(b"\0", 1)[0]
            if not encoded:
                raise OSError("empty descriptor path")
            root = Path(os.fsdecode(encoded))
            pinned = os.fstat(directory_fd)
            resolved = root.stat(follow_symlinks=False)
        except OSError as exc:
            raise ShowMeCollisionError(
                "could not resolve pinned show-me directory"
            ) from exc
        if not stat.S_ISDIR(resolved.st_mode) or (
            pinned.st_dev,
            pinned.st_ino,
        ) != (resolved.st_dev, resolved.st_ino):
            raise ShowMeCollisionError("pinned show-me directory identity changed")
    else:
        root = Path(f"/dev/fd/{directory_fd}")
    return root if child is None else root / child


def _tree_fingerprint(path: Path) -> str:
    value = hashlib.sha256()

    def visit(directory: Path, relative: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                entry_relative = relative / entry.name
                encoded = entry_relative.as_posix().encode("utf-8")
                if entry.is_symlink():
                    raise ShowMeCollisionError(
                        f"show-me tree contains unsupported symbolic link: {entry.path}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    value.update(b"D\0" + encoded + b"\0")
                    visit(Path(entry.path), entry_relative)
                elif entry.is_file(follow_symlinks=False):
                    value.update(b"F\0" + encoded + b"\0")
                    value.update(Path(entry.path).read_bytes())
                    value.update(b"\0")
                else:
                    raise ShowMeCollisionError(
                        f"show-me tree contains unsupported entry: {entry.path}"
                    )

    visit(path, Path())
    return value.hexdigest()
