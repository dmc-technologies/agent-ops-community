"""Policy and skills share one pinned selection without treating policy as a skill."""

import hashlib
import json
from pathlib import Path

import pytest
from test_shared_content import fixture, git

from agent_ops.deployment.models import SourceSnapshot
from agent_ops.deployment.shared_content import build_shared_content
from agent_ops.deployment.shared_selection import SharedSelection


def policy_fixture(tmp_path, symlink=False):
    root, selection, _ = fixture(tmp_path)
    policy = root / "config/AGENTS.md"
    policy.parent.mkdir()
    if symlink:
        policy.symlink_to("../skills/example/SKILL.md")
    else:
        policy.write_text("Read project instructions.\n")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "Policy",
    )
    commit = git(root, "rev-parse", "HEAD")
    document = selection.model_dump()
    document["sources"][0]["commit"] = commit
    document["policy"] = {"source_id": "catalog", "path": "config/AGENTS.md"}
    return (
        root,
        SharedSelection.model_validate(document),
        (SourceSnapshot("catalog", commit, commit, root),),
    )


def test_policy_uses_same_pin_and_source_map(tmp_path):
    root, selection, snapshots = policy_fixture(tmp_path)
    files = build_shared_content(selection, snapshots)
    prefix = Path("snapshots") / selection.fingerprint
    by_path = {f.path: f for f in files}
    assert by_path[prefix / "policy/AGENTS.md"].content == b"Read project instructions.\n"
    assert (
        by_path[prefix / "skills/example/SKILL.md"].content
        == (root / "skills/example/SKILL.md").read_bytes()
    )
    mapping = json.loads(by_path[prefix / "source-map.json"].content)
    assert mapping["policy"] == {
        "source_id": "catalog",
        "commit": snapshots[0].commit,
        "path": "config/AGENTS.md",
    }
    assert len(mapping["skills"]) == 1


def test_absent_policy_preserves_legacy_fingerprint(tmp_path):
    _, selection, _ = fixture(tmp_path)
    old = {"schema_version": 1, "sources": [s.model_dump(mode="json") for s in selection.sources]}
    expected = hashlib.sha256(
        json.dumps(old, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert selection.fingerprint == expected


@pytest.mark.parametrize(
    "policy",
    [
        {"source_id": "unknown", "path": "config/AGENTS.md"},
        {"source_id": "catalog", "path": "../AGENTS.md"},
        {"source_id": "catalog", "path": "/AGENTS.md"},
        {"source_id": "catalog", "path": "config/AGENTS.md", "commit": "0" * 40},
        {"source_id": "catalog", "path": "skills/example/SKILL.md"},
    ],
)
def test_invalid_policy_binding_refuses(tmp_path, policy):
    _, selection, _ = fixture(tmp_path)
    with pytest.raises(ValueError):
        SharedSelection.model_validate({**selection.model_dump(), "policy": policy})


def test_committed_policy_symlink_refuses(tmp_path):
    _, selection, snapshots = policy_fixture(tmp_path, symlink=True)
    with pytest.raises(RuntimeError, match="symlink"):
        build_shared_content(selection, snapshots)


def test_missing_policy_refuses(tmp_path):
    _, selection, snapshots = policy_fixture(tmp_path)
    changed = SharedSelection.model_validate(
        {**selection.model_dump(), "policy": {"source_id": "catalog", "path": "config/missing.md"}}
    )
    with pytest.raises(RuntimeError, match="not tracked"):
        build_shared_content(changed, snapshots)


def test_policy_directory_refuses(tmp_path):
    _, selection, snapshots = policy_fixture(tmp_path)
    changed = SharedSelection.model_validate(
        {**selection.model_dump(), "policy": {"source_id": "catalog", "path": "config"}}
    )
    with pytest.raises(ValueError, match="regular file"):
        build_shared_content(changed, snapshots)


def test_dirty_policy_refuses(tmp_path):
    root, selection, snapshots = policy_fixture(tmp_path)
    (root / "config/AGENTS.md").write_text("local edit")
    with pytest.raises(RuntimeError, match="bytes differ"):
        build_shared_content(selection, snapshots)
