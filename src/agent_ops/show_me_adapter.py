from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
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
OWNERSHIP_MANIFEST_RELATIVE = Path(
    ".agentops/skill-dependencies/humanlayer-show-me.json"
)
_LOCK_NAME = "humanlayer-show-me.lock"
_TRANSACTION_NAME = "humanlayer-show-me-transaction.json"
_STAGE_PREFIX = ".humanlayer-show-me-stage-"
_BACKUP_PREFIX = ".humanlayer-show-me-backup-"


class ShowMeCollisionError(RuntimeError):
    """Installation would overwrite a skill Agent Ops cannot prove it owns."""


def install_show_me(
    upstream_root: Path,
    destination: Path,
    *,
    upstream_ref: str = PINNED_REF,
    collision_roots: tuple[Path, ...] = (),
    flat_markdown: bool = False,
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
        )

    profile_root.mkdir(parents=True, exist_ok=True)
    root_fd = _open_absolute_directory_no_follow(profile_root)
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
            )
        current = _preflight(
            skills_fd,
            dependencies_fd,
            flat_markdown=flat_markdown,
        )
        _install_transaction(skills_fd, dependencies_fd, stage_name, manifest, current)
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
) -> dict[str, Any]:
    _ensure_windows_directory(profile_root)
    skills_root = profile_root / "skills"
    agentops_root = profile_root / ".agentops"
    dependencies_root = profile_root / OWNERSHIP_MANIFEST_RELATIVE.parent
    _ensure_windows_directory(skills_root)
    _ensure_windows_directory(agentops_root)
    _ensure_windows_directory(dependencies_root)
    lock_path = dependencies_root / _LOCK_NAME
    lock_fd = _open_windows_lock(lock_path)
    with os.fdopen(lock_fd, "a+b") as lock_stream:
        _lock_file(lock_stream.fileno())
        _recover_windows_transaction(skills_root, dependencies_root)
        stage = dependencies_root / f"{_STAGE_PREFIX}{uuid.uuid4().hex}"
        try:
            shutil.copytree(source, stage)
            _adapt_instructions(stage / "SKILL.md")
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
                )
            current = _preflight_windows(
                skills_root,
                dependencies_root,
                destination,
                flat_markdown=flat_markdown,
            )
            _install_windows_transaction(
                skills_root,
                dependencies_root,
                stage,
                destination,
                manifest,
                current,
            )
            return manifest
        finally:
            _remove_unreferenced_windows_stage(stage, dependencies_root)


def _is_windows_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _ensure_windows_directory(path: Path) -> None:
    parent = path.parent
    if parent != path and not parent.exists():
        _ensure_windows_directory(parent)
    if path.is_symlink() or _is_windows_reparse_point(path):
        raise ShowMeCollisionError(
            f"selected profile path contains a symbolic link or junction: {path}"
        )
    if path.exists():
        if not path.is_dir():
            raise ShowMeCollisionError(
                f"selected profile path contains a non-directory: {path}"
            )
        return
    path.mkdir()
    if path.is_symlink() or _is_windows_reparse_point(path) or not path.is_dir():
        raise ShowMeCollisionError(f"could not create confined profile directory: {path}")


def _open_windows_lock(path: Path) -> int:
    expected_path = os.path.abspath(path)
    if path.is_symlink() or _is_windows_reparse_point(path):
        raise ShowMeCollisionError(f"unsafe show-me lock path: {path}")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ShowMeCollisionError(f"unsafe show-me lock path: {path}")
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            import ctypes

            handle = msvcrt.get_osfhandle(descriptor)
            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetFinalPathNameByHandleW(
                handle,
                buffer,
                len(buffer),
                0,
            )
            if length == 0 or length >= len(buffer):
                raise ShowMeCollisionError(f"cannot verify show-me lock path: {path}")
            resolved = buffer.value.removeprefix("\\\\?\\")
            if os.path.normcase(os.path.abspath(resolved)) != os.path.normcase(
                expected_path
            ):
                raise ShowMeCollisionError(f"show-me lock escaped profile: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_windows_profile(profile_root: Path) -> None:
    cursor = Path(profile_root.anchor)
    for part in profile_root.parts[1:]:
        cursor /= part
        if cursor.is_symlink() or _is_windows_reparse_point(cursor):
            raise ShowMeCollisionError(
                f"selected profile path contains a symbolic link or junction: {cursor}"
            )
        if cursor.exists() and not cursor.is_dir():
            raise ShowMeCollisionError(
                f"selected profile path contains a non-directory: {cursor}"
            )


def _preflight_windows(
    skills_root: Path,
    dependencies_root: Path,
    destination: Path,
    *,
    flat_markdown: bool,
) -> dict[str, Any] | None:
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
        return None
    if not destination.is_dir() or current is None:
        raise ShowMeCollisionError(f"user-owned collision at {destination}")
    _validate_manifest(current)
    if _tree_fingerprint(destination) != current["installed_fingerprint"]:
        raise ShowMeCollisionError("managed show-me skill changed since installation")
    return current


def _reject_external_collision_root_windows(
    root: Path,
    *,
    flat_markdown: bool,
    allowed_fingerprint: str,
) -> None:
    _validate_windows_profile(root)
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
            if not relative.parts and exclude_managed and (
                child.name == SKILL_NAME
                or child.name.startswith(_STAGE_PREFIX)
                or child.name.startswith(_BACKUP_PREFIX)
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
            f"logical skill-name collision for {SKILL_NAME} "
            f"at {_skill_location(relative)}"
        )


def _install_windows_transaction(
    skills_root: Path,
    dependencies_root: Path,
    stage: Path,
    destination: Path,
    manifest: dict[str, Any],
    current: dict[str, Any] | None,
) -> None:
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
    _write_json_path(transaction, state)
    try:
        _validate_windows_profile(skills_root)
        _validate_windows_profile(dependencies_root)
        if current is not None:
            os.replace(destination, backup)
        os.replace(stage, destination)
        _write_json_path(
            dependencies_root / OWNERSHIP_MANIFEST_RELATIVE.name,
            manifest,
        )
        state["phase"] = "committed"
        _write_json_path(transaction, state)
        _recover_windows_transaction(skills_root, dependencies_root)
    except BaseException:
        _recover_windows_transaction(skills_root, dependencies_root)
        raise


def _recover_windows_transaction(skills_root: Path, dependencies_root: Path) -> None:
    _validate_windows_profile(skills_root)
    _validate_windows_profile(dependencies_root)
    transaction = dependencies_root / _TRANSACTION_NAME
    state = _read_json_path(transaction)
    if state is None:
        _cleanup_windows_dependency_garbage(dependencies_root)
        return
    _validate_transaction_state(state)
    destination = skills_root / SKILL_NAME
    stage = dependencies_root / state["stage"]
    backup = dependencies_root / state["backup"]
    old_manifest = state["old_manifest"]
    new_fingerprint = state["new_manifest"]["installed_fingerprint"]

    if _windows_entry_exists(stage):
        _verify_windows_transaction_tree(stage, new_fingerprint, "recovery stage")
    if _windows_entry_exists(backup):
        if not state["had_destination"] or old_manifest is None:
            raise ShowMeCollisionError("unexpected show-me recovery backup")
        _verify_windows_transaction_tree(
            backup,
            old_manifest["installed_fingerprint"],
            "recovery backup",
        )

    if state["phase"] == "committed":
        _verify_windows_transaction_tree(
            destination,
            new_fingerprint,
            "committed transaction target",
        )
        transaction.unlink()
        _cleanup_windows_dependency_garbage(dependencies_root)
        return

    if _windows_entry_exists(destination):
        _verify_windows_transaction_tree_shape(destination, "uncommitted transaction target")
        target_fingerprint = _tree_fingerprint(destination)
        old_fingerprint = old_manifest["installed_fingerprint"] if old_manifest else None
        if target_fingerprint == new_fingerprint:
            if _windows_entry_exists(stage):
                raise ShowMeCollisionError(
                    "duplicate uncommitted show-me target; preserving transaction data"
                )
            os.replace(destination, stage)
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
            os.replace(backup, destination)
        _write_json_path(
            dependencies_root / OWNERSHIP_MANIFEST_RELATIVE.name,
            old_manifest,
        )
    else:
        if _windows_entry_exists(destination):
            raise ShowMeCollisionError("fresh show-me transaction has an unexpected target")
        (dependencies_root / OWNERSHIP_MANIFEST_RELATIVE.name).unlink(missing_ok=True)

    transaction.unlink()
    _cleanup_windows_dependency_garbage(dependencies_root)


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


def _cleanup_windows_dependency_garbage(dependencies_root: Path) -> None:
    if _windows_entry_exists(dependencies_root / _TRANSACTION_NAME):
        return
    for path in sorted(dependencies_root.iterdir(), key=lambda candidate: candidate.name):
        if path.name.startswith(_STAGE_PREFIX) or path.name.startswith(_BACKUP_PREFIX):
            _verify_windows_transaction_tree_shape(path, "show-me transaction garbage")
            shutil.rmtree(path)


def _remove_unreferenced_windows_stage(stage: Path, dependencies_root: Path) -> None:
    try:
        state = _read_json_path(dependencies_root / _TRANSACTION_NAME)
    except ShowMeCollisionError:
        return
    if isinstance(state, dict) and state.get("stage") == stage.name:
        return
    if _windows_entry_exists(stage):
        _verify_windows_transaction_tree_shape(stage, "unreferenced installation stage")
        shutil.rmtree(stage)


def _write_json_path(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_json_path(path: Path) -> Any | None:
    if path.is_symlink() or _is_windows_reparse_point(path):
        raise ShowMeCollisionError(f"unsafe show-me state path: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ShowMeCollisionError(f"unsafe show-me state path: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShowMeCollisionError(f"invalid show-me state file: {path}") from exc


def _validate_destination(destination: Path) -> tuple[Path, Path]:
    raw = Path(os.path.abspath(destination.expanduser()))
    if raw.name != SKILL_NAME or raw.parent.name != "skills":
        raise ShowMeCollisionError(
            "HumanLayer show-me destination must be <profile>/skills/show-me"
        )
    return raw, raw.parent.parent


def _open_absolute_directory_no_follow(path: Path) -> int:
    if not path.is_absolute():
        raise ShowMeCollisionError("selected profile must be an absolute path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
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
            raise ValueError(
                f"pinned HumanLayer checkout is at {head}, expected {upstream_ref}"
            )
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
) -> dict[str, Any] | None:
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
        return None
    if not stat.S_ISDIR(target.st_mode):
        raise ShowMeCollisionError("user-owned collision at skills/show-me")
    if current is None:
        raise ShowMeCollisionError("user-owned collision at skills/show-me")
    _validate_manifest(current)
    actual = _tree_fingerprint(_fd_path(skills_fd, SKILL_NAME))
    if actual != current["installed_fingerprint"]:
        raise ShowMeCollisionError("managed show-me skill changed since installation")
    return current


def _reject_external_collision_root(
    root: Path,
    *,
    flat_markdown: bool,
    allowed_fingerprint: str,
) -> None:
    descriptor = _open_existing_absolute_directory_no_follow(root.resolve())
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
            if not relative.parts and exclude_managed and (
                name == SKILL_NAME
                or name.startswith(_STAGE_PREFIX)
                or name.startswith(_BACKUP_PREFIX)
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
    if not stat.S_ISREG(skill.st_mode) and not (
        follow_symlinks and stat.S_ISLNK(skill.st_mode)
    ):
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
        raise ShowMeCollisionError(
            f"logical skill-name collision for {SKILL_NAME} at {location}"
        )


def _frontmatter_skill_name(text: str, location: str) -> str | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        closing = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        value = yaml.safe_load("\n".join(lines[1:closing]))
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


def _install_transaction(
    skills_fd: int,
    dependencies_fd: int,
    stage_name: str,
    manifest: dict[str, Any],
    current: dict[str, Any] | None,
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
            raise ShowMeCollisionError(
                "uncommitted transaction target is not a regular directory"
            )
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
            raise ShowMeCollisionError(
                "fresh show-me transaction has an unexpected target"
            )
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
