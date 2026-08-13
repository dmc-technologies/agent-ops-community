from __future__ import annotations

import contextlib
import fcntl
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
) -> dict[str, Any]:
    """Adapt and transactionally install the pinned HumanLayer show-me skill."""

    upstream_root = Path(upstream_root)
    destination, profile_root = _validate_destination(Path(destination))
    source = upstream_root / SOURCE_RELATIVE
    _validate_source(upstream_root, source, upstream_ref)

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
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _recover_transaction(skills_fd, dependencies_fd)
        _stage_skill(source, skills_fd, stage_name)
        staged = _fd_path(skills_fd, stage_name)
        _adapt_instructions(staged / "SKILL.md")
        manifest = {
            "schema_version": 1,
            "upstream_repo": PINNED_REPO,
            "upstream_ref": upstream_ref,
            "skill": SKILL_NAME,
            "source_fingerprint": _tree_fingerprint(source),
            "installed_fingerprint": _tree_fingerprint(staged),
        }
        current = _preflight(skills_fd, dependencies_fd)
        _install_transaction(skills_fd, dependencies_fd, stage_name, manifest, current)
        return manifest
    finally:
        if skills_fd is not None:
            _remove_directory_if_present(skills_fd, stage_name)
        if lock_fd is not None:
            os.close(lock_fd)
        if dependencies_fd is not None:
            os.close(dependencies_fd)
        if skills_fd is not None:
            os.close(skills_fd)
        os.close(root_fd)


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


def _preflight(skills_fd: int, dependencies_fd: int) -> dict[str, Any] | None:
    _reject_logical_collisions(skills_fd)
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


def _reject_logical_collisions(skills_fd: int) -> None:
    root = _fd_path(skills_fd)
    for name in sorted(os.listdir(skills_fd)):
        if name == SKILL_NAME or name.startswith(_STAGE_PREFIX) or name.startswith(_BACKUP_PREFIX):
            continue
        skill_file = root / name / "SKILL.md"
        try:
            text = skill_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeDecodeError) as exc:
            entry = _entry_stat(skills_fd, name)
            if entry is not None and stat.S_ISLNK(entry.st_mode):
                raise ShowMeCollisionError(
                    f"cannot verify symlinked skill identity at skills/{name}"
                ) from exc
            continue
        match = re.search(r'(?m)^name\s*:\s*[\'\"]?([^\'\"\s]+)', text)
        if match and match.group(1) == SKILL_NAME:
            raise ShowMeCollisionError(
                f"logical skill-name collision for {SKILL_NAME} at skills/{name}"
            )


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
    os.fsync(dependencies_fd)
    try:
        if current is not None:
            os.rename(SKILL_NAME, backup_name, src_dir_fd=skills_fd, dst_dir_fd=skills_fd)
            os.fsync(skills_fd)
        os.rename(stage_name, SKILL_NAME, src_dir_fd=skills_fd, dst_dir_fd=skills_fd)
        os.fsync(skills_fd)
        _write_json_at(
            dependencies_fd,
            OWNERSHIP_MANIFEST_RELATIVE.name,
            manifest,
        )
        state["phase"] = "committed"
        _write_json_at(dependencies_fd, _TRANSACTION_NAME, state)
        _remove_directory_if_present(skills_fd, backup_name)
        _unlink_if_present(dependencies_fd, _TRANSACTION_NAME)
        os.fsync(skills_fd)
        os.fsync(dependencies_fd)
    except BaseException:
        _recover_transaction(skills_fd, dependencies_fd)
        raise


def _recover_transaction(skills_fd: int, dependencies_fd: int) -> None:
    state = _read_json_at(dependencies_fd, _TRANSACTION_NAME)
    if state is None:
        return
    _validate_transaction_state(state)
    stage = state["stage"]
    backup = state["backup"]
    target_stat = _entry_stat(skills_fd, SKILL_NAME)
    backup_stat = _entry_stat(skills_fd, backup)

    if state["phase"] == "committed":
        if target_stat is None or not stat.S_ISDIR(target_stat.st_mode):
            raise ShowMeCollisionError("committed show-me transaction is missing its target")
        actual = _tree_fingerprint(_fd_path(skills_fd, SKILL_NAME))
        if actual != state["new_manifest"]["installed_fingerprint"]:
            raise ShowMeCollisionError("committed show-me transaction target changed")
        _remove_directory_if_present(skills_fd, backup)
        _remove_directory_if_present(skills_fd, stage)
        _unlink_if_present(dependencies_fd, _TRANSACTION_NAME)
        os.fsync(skills_fd)
        os.fsync(dependencies_fd)
        return

    if backup_stat is not None:
        if not stat.S_ISDIR(backup_stat.st_mode):
            raise ShowMeCollisionError("show-me recovery backup is not a regular directory")
        _remove_directory_if_present(skills_fd, SKILL_NAME)
        os.rename(backup, SKILL_NAME, src_dir_fd=skills_fd, dst_dir_fd=skills_fd)
    elif not state["had_destination"] and target_stat is not None:
        if not stat.S_ISDIR(target_stat.st_mode):
            raise ShowMeCollisionError("show-me recovery target is not a regular directory")
        actual = _tree_fingerprint(_fd_path(skills_fd, SKILL_NAME))
        if actual != state["new_manifest"]["installed_fingerprint"]:
            raise ShowMeCollisionError("uncommitted show-me transaction target changed")
        _remove_directory_if_present(skills_fd, SKILL_NAME)
    elif state["had_destination"] and target_stat is None:
        raise ShowMeCollisionError("show-me transaction cannot recover its prior skill")

    _remove_directory_if_present(skills_fd, stage)
    old_manifest = state["old_manifest"]
    if old_manifest is None:
        _unlink_if_present(dependencies_fd, OWNERSHIP_MANIFEST_RELATIVE.name)
    else:
        _write_json_at(
            dependencies_fd,
            OWNERSHIP_MANIFEST_RELATIVE.name,
            old_manifest,
        )
    _unlink_if_present(dependencies_fd, _TRANSACTION_NAME)
    os.fsync(skills_fd)
    os.fsync(dependencies_fd)


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
        if not isinstance(name, str) or not name.startswith(prefix) or "/" in name:
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
    root = Path(f"/proc/self/fd/{directory_fd}")
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
