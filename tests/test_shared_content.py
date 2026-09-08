import json
import subprocess
from pathlib import Path

import pytest

from agent_ops.deployment.models import SourceSnapshot
from agent_ops.deployment.shared_content import build_shared_content
from agent_ops.deployment.shared_selection import SharedSelection


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def fixture(
    tmp_path, *, text="---\nname: example\ndescription: Example skill\n---\nRead resource."
):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init")
    skill = root / "skills" / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(text)
    (skill / "run.sh").write_bytes(b"#!/bin/sh\nprintf resource\n")
    (skill / "run.sh").chmod(0o755)
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Fixture",
    )
    git(root, "checkout", "--detach")
    commit = git(root, "rev-parse", "HEAD")
    selected = SharedSelection(
        sources=({"source_id": "catalog", "commit": commit, "skill_paths": ("skills/example",)},)
    )
    return root, selected, (SourceSnapshot("catalog", commit, commit, root),)


def test_composes_pinned_bytes_modes_and_source_mapping(tmp_path):
    root, selected, snapshots = fixture(tmp_path)
    files = build_shared_content(selected, snapshots)
    prefix = Path("snapshots") / selected.fingerprint
    by_path = {f.path: f for f in files}
    assert len(files) == 3
    script = by_path[prefix / "skills/example/run.sh"]
    assert script.content == (root / "skills/example/run.sh").read_bytes()
    assert script.mode == 0o755
    mapping = json.loads(by_path[prefix / "source-map.json"].content)
    assert mapping["skills"][0] == {
        "name": "example",
        "source_id": "catalog",
        "commit": snapshots[0].commit,
        "path": "skills/example",
    }
    assert str(root) not in json.dumps(mapping)
    assert files == build_shared_content(selected, snapshots)


@pytest.mark.parametrize("change", ["dirty", "missing", "symlink", "untracked", "snapshot"])
def test_refuses_untrusted_sources(tmp_path, change):
    root, selected, snapshots = fixture(tmp_path)
    resource = root / "skills/example/run.sh"
    if change == "dirty":
        resource.write_text("changed")
    elif change == "missing":
        resource.unlink()
    elif change == "symlink":
        resource.unlink()
        resource.symlink_to("SKILL.md")
    elif change == "untracked":
        (root / "skills/example/new").write_text("untracked")
    else:
        snapshots = ()
    with pytest.raises((ValueError, RuntimeError)):
        build_shared_content(selected, snapshots)


@pytest.mark.parametrize(
    "text",
    [
        "No metadata",
        "---\nname: ../outside\n---\nbody",
        "---\nname: example\nname: other\n---\nbody",
        "---\nname: &n example\ndescription: *n\n---\nbody",
    ],
)
def test_refuses_malformed_skill_metadata(tmp_path, text):
    _, selected, snapshots = fixture(tmp_path, text=text)
    with pytest.raises(ValueError):
        build_shared_content(selected, snapshots)


def test_refuses_duplicate_names_across_sources(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _, selection_a, snapshots_a = fixture(first)
    _, selection_b, snapshots_b = fixture(second)
    binding_b = selection_b.sources[0].model_copy(update={"source_id": "extras"})
    selected = SharedSelection(sources=(*selection_a.sources, binding_b))
    snapshot_b = SourceSnapshot("extras", binding_b.commit, binding_b.commit, snapshots_b[0].root)
    with pytest.raises(ValueError, match="duplicate"):
        build_shared_content(selected, (*snapshots_a, snapshot_b))


def test_refuses_overlapping_selected_paths(tmp_path):
    _, selected, snapshots = fixture(tmp_path)
    binding = selected.sources[0].model_copy(update={"skill_paths": ("skills", "skills/example")})
    with pytest.raises(ValueError, match="overlap"):
        build_shared_content(SharedSelection(sources=(binding,)), snapshots)


def test_refuses_committed_symlink_resource(tmp_path):
    root, _, _ = fixture(tmp_path)
    resource = root / "skills/example/run.sh"
    resource.unlink()
    resource.symlink_to("SKILL.md")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Symlink resource",
    )
    commit = git(root, "rev-parse", "HEAD")
    selected = SharedSelection(
        sources=({"source_id": "catalog", "commit": commit, "skill_paths": ("skills/example",)},)
    )
    with pytest.raises(RuntimeError, match="symlink"):
        build_shared_content(selected, (SourceSnapshot("catalog", commit, commit, root),))
