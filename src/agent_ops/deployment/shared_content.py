"""Compose complete pinned skill resources without running source-provided code."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from agent_ops.deployment.models import PlannedFile, SourceSnapshot
from agent_ops.deployment.registry import _StrictLoader
from agent_ops.deployment.shared_selection import SharedSelection
from agent_ops.deployment.source_store import (
    _DEFAULT_GIT_TIMEOUT,
    _open_provider_data_closure,
    _verify_exact_checkout,
)


def _skill_name(content: bytes) -> str:
    try:
        lines = content.decode("utf-8").splitlines()
        if not lines or lines[0] != "---":
            raise ValueError("skill requires YAML frontmatter")
        end = lines.index("---", 1)
        metadata = yaml.load("\n".join(lines[1:end]), Loader=_StrictLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ValueError("skill frontmatter must be strict UTF-8 YAML") from error
    if type(metadata) is not dict:
        raise ValueError("skill frontmatter must be a mapping")
    name = metadata.get("name")
    if type(name) is not str or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) is None:
        raise ValueError("skill name must be a lowercase hyphenated identifier")
    if type(metadata.get("description")) is not str or not metadata["description"].strip():
        raise ValueError("skill requires a nonempty description")
    return name


def build_shared_content(
    selection: SharedSelection, snapshots: tuple[SourceSnapshot, ...]
) -> tuple[PlannedFile, ...]:
    """Return immutable snapshot files with source mapping; installation is separate."""
    if type(selection) is not SharedSelection or type(snapshots) is not tuple:
        raise ValueError("shared content requires exact selection and snapshot tuple")
    if any(type(snapshot) is not SourceSnapshot for snapshot in snapshots):
        raise ValueError("shared content requires exact source snapshots")
    if len(snapshots) != len(selection.sources):
        raise ValueError("shared content requires one snapshot per selected source")
    prefix = Path("snapshots") / selection.fingerprint
    files: list[PlannedFile] = []
    mappings: list[dict[str, str]] = []
    names: set[str] = set()
    for binding in selection.sources:
        matches = [snapshot for snapshot in snapshots if snapshot.source_id == binding.source_id]
        if (
            len(matches) != 1
            or matches[0].commit != binding.commit
            or matches[0].ref != binding.commit
        ):
            raise ValueError("source snapshot must match its exact selected pin")
        snapshot = matches[0]
        roots = tuple(Path(path) for path in binding.skill_paths)
        if any(
            a in b.parents or b in a.parents for i, a in enumerate(roots) for b in roots[i + 1 :]
        ):
            raise ValueError("selected skill paths must not overlap")
        tracked = _verify_exact_checkout(
            snapshot.root, binding.commit, timeout=_DEFAULT_GIT_TIMEOUT
        )
        selected_files = tuple(
            Path(path)
            for path in sorted(tracked)
            if any(root in Path(path).parents for root in roots)
        )
        with _open_provider_data_closure(snapshot, roots + selected_files) as closure:
            entries = {entry.relative_path: entry for entry in closure.entries}
            for root in roots:
                if entries[root].kind != "directory":
                    raise ValueError("selected skill path must be a directory")
                skill = entries.get(root / "SKILL.md")
                if skill is None:
                    raise ValueError("selected skill requires SKILL.md")
                name = _skill_name(skill.read_bytes())
                if name in names:
                    raise ValueError("duplicate selected skill identity")
                names.add(name)
                mappings.append(
                    {
                        "name": name,
                        "source_id": binding.source_id,
                        "commit": binding.commit,
                        "path": root.as_posix(),
                    }
                )
                for path in selected_files:
                    if root in path.parents:
                        entry = entries[path]
                        if entry.kind != "file":
                            raise ValueError("skill resources must be regular files")
                        files.append(
                            PlannedFile(
                                prefix / "skills" / name / path.relative_to(root),
                                entry.read_bytes(),
                                entry.mode,
                            )
                        )
    mapping = {
        "schema_version": 1,
        "selection_fingerprint": selection.fingerprint,
        "skills": sorted(mappings, key=lambda item: item["name"]),
    }
    files.append(
        PlannedFile(
            prefix / "source-map.json",
            (json.dumps(mapping, sort_keys=True, indent=2) + "\n").encode(),
            0o644,
        )
    )
    return tuple(sorted(files, key=lambda item: item.path.as_posix()))
