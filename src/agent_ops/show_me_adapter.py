from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

PINNED_REPO = "https://github.com/humanlayer/skills.git"
PINNED_REF = "4d8d644ca747517973f58d7953f58d7cd07520cd"
SKILL_NAME = "show-me"
SOURCE_RELATIVE = Path("plugins/show-me/skills/show-me")
OWNERSHIP_MANIFEST_RELATIVE = Path(
    ".agentops/skill-dependencies/humanlayer-show-me.json"
)


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

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".agentops-show-me-stage-", dir=profile_root
    ) as temporary:
        transaction = Path(temporary)
        staged = transaction / SKILL_NAME
        shutil.copytree(source, staged)
        _adapt_instructions(staged / "SKILL.md")
        manifest = {
            "schema_version": 1,
            "upstream_repo": PINNED_REPO,
            "upstream_ref": upstream_ref,
            "skill": SKILL_NAME,
            "source_fingerprint": _tree_fingerprint(source),
            "installed_fingerprint": _tree_fingerprint(staged),
        }
        _preflight(
            destination=destination,
            profile_root=profile_root,
            manifest=manifest,
        )
        _install_staged(staged, destination, profile_root, manifest, transaction)
    return manifest


def _validate_destination(destination: Path) -> tuple[Path, Path]:
    raw = destination.expanduser().absolute()
    profile_root = raw.parent.parent.resolve()
    skills_root = profile_root / raw.parent.name
    confined = skills_root / raw.name
    if raw.parent.name != "skills" or confined != raw:
        raise ShowMeCollisionError(
            "HumanLayer show-me destination must be <profile>/skills/show-me"
        )
    cursor = profile_root
    for part in confined.relative_to(profile_root).parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise ShowMeCollisionError(
                f"symbolic link blocks confined show-me installation: {cursor}"
            )
        if cursor.exists() and not cursor.is_dir():
            raise ShowMeCollisionError(
                f"non-directory blocks confined show-me installation: {cursor}"
            )
    manifest_parent = profile_root / OWNERSHIP_MANIFEST_RELATIVE.parent
    cursor = profile_root
    for part in manifest_parent.relative_to(profile_root).parts:
        cursor /= part
        if cursor.is_symlink():
            raise ShowMeCollisionError(
                f"symbolic link blocks confined show-me installation: {cursor}"
            )
        if cursor.exists() and not cursor.is_dir():
            raise ShowMeCollisionError(
                f"non-directory blocks confined show-me installation: {cursor}"
            )
    return confined, profile_root


def _validate_source(upstream_root: Path, source: Path, upstream_ref: str) -> None:
    if upstream_ref != PINNED_REF:
        raise ValueError(f"expected pinned HumanLayer ref {PINNED_REF}, got {upstream_ref!r}")
    if not (source / "SKILL.md").is_file():
        raise FileNotFoundError(source / "SKILL.md")
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
        status = subprocess.run(
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
        if status:
            raise ValueError("pinned HumanLayer checkout contains changed or untracked files")


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
    *,
    destination: Path,
    profile_root: Path,
    manifest: dict[str, Any],
) -> None:
    validated, validated_root = _validate_destination(destination)
    if validated != destination or validated_root != profile_root:
        raise ShowMeCollisionError("show-me destination changed during installation")
    skills_root = destination.parent
    _reject_logical_collisions(skills_root, destination)
    ownership_path = profile_root / OWNERSHIP_MANIFEST_RELATIVE
    current = _read_manifest(ownership_path)

    if destination.is_symlink():
        raise ShowMeCollisionError(f"user-owned collision at {destination}")
    if not destination.exists():
        if current is not None:
            raise ShowMeCollisionError(f"managed skill {destination} is missing")
        return
    if not destination.is_dir():
        raise ShowMeCollisionError(f"user-owned collision at {destination}")

    actual = _tree_fingerprint(destination)
    if current is None:
        if actual != manifest["source_fingerprint"]:
            raise ShowMeCollisionError(f"user-owned collision at {destination}")
        return
    if not isinstance(current, dict):
        raise ShowMeCollisionError(f"invalid show-me ownership manifest: {ownership_path}")
    required = {"schema_version", "upstream_repo", "upstream_ref", "skill", "installed_fingerprint"}
    if not required <= set(current) or current.get("schema_version") != 1:
        raise ShowMeCollisionError(f"invalid show-me ownership manifest: {ownership_path}")
    if current.get("skill") != SKILL_NAME or current.get("upstream_repo") != PINNED_REPO:
        raise ShowMeCollisionError(f"invalid show-me ownership manifest: {ownership_path}")
    if actual != current.get("installed_fingerprint"):
        raise ShowMeCollisionError(f"managed skill {destination} changed since installation")


def _reject_logical_collisions(skills_root: Path, destination: Path) -> None:
    if not skills_root.exists():
        return
    for skill_file in sorted(skills_root.rglob("SKILL.md")):
        try:
            skill_file.relative_to(destination)
        except ValueError:
            pass
        else:
            continue
        if skill_file.is_symlink() or not skill_file.is_file():
            continue
        try:
            text = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        match = re.search(r'(?m)^name\s*:\s*[\'"]?([^\'"\s]+)', text)
        if match and match.group(1) == SKILL_NAME:
            raise ShowMeCollisionError(
                f"logical skill-name collision for {SKILL_NAME} at {skill_file}"
            )


def _install_staged(
    staged: Path,
    destination: Path,
    profile_root: Path,
    manifest: dict[str, Any],
    transaction: Path,
) -> None:
    ownership_path = profile_root / OWNERSHIP_MANIFEST_RELATIVE
    old_manifest = ownership_path.read_bytes() if ownership_path.exists() else None
    backup = transaction / "backup"
    installed = False
    backed_up = False
    try:
        if destination.exists():
            os.replace(destination, backup)
            backed_up = True
        os.replace(staged, destination)
        installed = True
        validated, validated_root = _validate_destination(destination)
        if validated != destination or validated_root != profile_root:
            raise ShowMeCollisionError("show-me destination changed during installation")
        ownership_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_manifest = transaction / "ownership.json"
        temporary_manifest.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(temporary_manifest, ownership_path)
    except BaseException:
        if installed and destination.exists():
            shutil.rmtree(destination)
        if backed_up:
            os.replace(backup, destination)
        if old_manifest is None:
            ownership_path.unlink(missing_ok=True)
        else:
            ownership_path.parent.mkdir(parents=True, exist_ok=True)
            ownership_path.write_bytes(old_manifest)
        raise


def _read_manifest(path: Path) -> Any | None:
    if path.is_symlink():
        raise ShowMeCollisionError(f"symbolic link blocks ownership manifest: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise ShowMeCollisionError(f"invalid ownership manifest path: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShowMeCollisionError(f"invalid show-me ownership manifest: {path}") from exc


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
