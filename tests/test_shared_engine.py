from __future__ import annotations

import json
import subprocess

import pytest

from agent_ops.deployment.engine import DeploymentEngine
from agent_ops.deployment.models import SourceSpec, TargetState
from agent_ops.deployment.registry import DeploymentRegistry, RegistryConfig, SharedSelectionConfig
from agent_ops.deployment.shared_selection import SharedSelection, SharedSourceBinding
from agent_ops.deployment.source_store import SourceStore


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(root, value):
    skill = root / "skills/example/SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: example\ndescription: Example skill\n---\n" + value)
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        value,
    )
    return git(root, "rev-parse", "HEAD")


def selection(sha):
    return SharedSelection(
        sources=(
            SharedSourceBinding(source_id="source", commit=sha, skill_paths=("skills/example",)),
        )
    )


def fixture(tmp_path):
    assert hasattr(DeploymentEngine, "shared_sync"), "shared engine lifecycle is missing"
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init")
    first = selection(commit(source, "first"))
    home = tmp_path / "shared"
    registry = DeploymentRegistry(tmp_path / "registry.yaml")
    registry.save(
        RegistryConfig(
            2,
            (SourceSpec("source", str(source)),),
            (),
            (),
            SharedSelectionConfig("shared-skills", home, first),
        )
    )
    engine = DeploymentEngine(registry, SourceStore(tmp_path / "store"), providers=())
    return engine, registry, source, home, first


def test_shared_sync_update_and_idempotence(tmp_path):
    engine, registry, source, home, first = fixture(tmp_path)
    assert engine.shared_status().state is TargetState.STALE
    receipt = engine.shared_sync()
    assert receipt.commits == (first.sources[0].commit,)
    assert engine.shared_status().state is TargetState.STABLE
    second = selection(commit(source, "second"))
    engine.shared_sync(second)
    assert registry.load().shared_selection.previous == first
    (manifest_path,) = (home / ".agentops/deployment/manifests").glob("*.json")
    before = manifest_path.read_bytes()
    engine.shared_sync()
    assert manifest_path.read_bytes() == before
    assert registry.load().shared_selection.previous == first
    assert (home / "current/skills/example/SKILL.md").read_text().endswith("second")
    mapping = json.loads((home / "current/source-map.json").read_text())
    assert mapping["skills"][0]["commit"] == second.sources[0].commit


def test_shared_rollback_uses_retained_content_without_source_fetch(tmp_path, monkeypatch):
    engine, registry, source, home, first = fixture(tmp_path)
    engine.shared_sync()
    second = selection(commit(source, "second"))
    engine.shared_sync(second)
    monkeypatch.setattr(
        engine._source_store, "fetch_pinned", lambda *args: pytest.fail("rollback fetched")
    )
    result = engine.shared_rollback()
    assert result.commits == (first.sources[0].commit,)
    assert registry.load().shared_selection.selected == first
    assert registry.load().shared_selection.previous == second
    assert (home / "current/skills/example/SKILL.md").read_text().endswith("first")


def test_shared_sync_refuses_dirty_target(tmp_path):
    engine, registry, source, home, first = fixture(tmp_path)
    engine.shared_sync()
    (home / "current/skills/example/SKILL.md").write_text("edited")
    assert engine.shared_status().state is TargetState.MODIFIED
    with pytest.raises((ValueError, OSError)):
        engine.shared_sync(selection(commit(source, "second")))
    assert registry.load().shared_selection.selected == first
    assert (home / "current/skills/example/SKILL.md").read_text() == "edited"


def test_shared_source_failure_preserves_installed_selection(tmp_path, monkeypatch):
    engine, registry, source, home, first = fixture(tmp_path)
    engine.shared_sync()

    def fail(*args):
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(engine._source_store, "fetch_pinned", fail)
    with pytest.raises(RuntimeError, match="source unavailable"):
        engine.shared_sync(selection("f" * 40))
    assert registry.load().shared_selection.selected == first
    assert (home / "current/skills/example/SKILL.md").read_text().endswith("first")


def test_shared_receipt_failure_restores_registry_and_selector(tmp_path, monkeypatch):
    engine, registry, source, home, first = fixture(tmp_path)
    engine.shared_sync()

    def fail(*args, **kwargs):
        raise OSError("receipt unavailable")

    monkeypatch.setattr(registry, "append_receipt", fail)
    with pytest.raises(OSError, match="receipt unavailable"):
        engine.shared_sync(selection(commit(source, "second")))
    assert registry.load().shared_selection.selected == first
    assert (home / "current/skills/example/SKILL.md").read_text().endswith("first")


def test_shared_rollback_refuses_modified_previous_snapshot(tmp_path):
    engine, registry, source, home, first = fixture(tmp_path)
    engine.shared_sync()
    engine.shared_sync(selection(commit(source, "second")))
    (home / "snapshots" / first.fingerprint / "skills/example/SKILL.md").write_text("edited")
    with pytest.raises((ValueError, OSError)):
        engine.shared_rollback()
    assert (home / "current/skills/example/SKILL.md").read_text().endswith("second")


def test_previous_selection_comes_from_installed_source_map(tmp_path):
    from dataclasses import replace

    engine, registry, source, home, first = fixture(tmp_path)
    engine.shared_sync()
    second = selection(commit(source, "second"))
    config = registry.load()
    registry.save(
        replace(config, shared_selection=replace(config.shared_selection, selected=second))
    )
    assert engine.shared_status().state is TargetState.STALE
    engine.shared_sync()
    assert registry.load().shared_selection.previous == first


def test_uninstalled_previous_selection_is_not_preserved_by_idempotent_sync(tmp_path):
    from dataclasses import replace

    engine, registry, source, home, first = fixture(tmp_path)
    engine.shared_sync()
    uninstalled = selection(commit(source, "never installed"))
    config = registry.load()
    registry.save(
        replace(config, shared_selection=replace(config.shared_selection, previous=uninstalled))
    )
    with pytest.raises(ValueError, match="source map"):
        engine.shared_sync()
    assert (home / "current/skills/example/SKILL.md").read_text().endswith("first")


def test_shared_policy_and_skills_change_together_and_rollback_offline(tmp_path, monkeypatch):
    from agent_ops.deployment import shared_selection as selections

    assert hasattr(selections, "SharedPolicyBinding"), "shared policy binding is missing"
    engine, registry, source, home, _ = fixture(tmp_path)
    (source / "AGENTS.md").write_text("First global policy\n")
    first = selections.SharedSelection(
        sources=selection(commit(source, "first policy skills")).sources,
        policy=selections.SharedPolicyBinding(source_id="source", path="AGENTS.md"),
    )
    engine.shared_sync(first)
    assert engine.shared_status().state is TargetState.STABLE
    assert (home / "current/policy/AGENTS.md").read_text() == "First global policy\n"
    (source / "AGENTS.md").write_text("Second global policy\n")
    second = selections.SharedSelection(
        sources=selection(commit(source, "second policy skills")).sources,
        policy=selections.SharedPolicyBinding(source_id="source", path="AGENTS.md"),
    )
    engine.shared_sync(second)
    assert (home / "current/policy/AGENTS.md").read_text() == "Second global policy\n"
    assert (home / "current/skills/example/SKILL.md").read_text().endswith("second policy skills")
    assert registry.load().shared_selection.previous == first
    monkeypatch.setattr(
        engine._source_store, "fetch_pinned", lambda *args: pytest.fail("rollback fetched")
    )
    engine.shared_rollback()
    assert engine.shared_status().state is TargetState.STABLE
    assert (home / "current/policy/AGENTS.md").read_text() == "First global policy\n"
    assert (home / "current/skills/example/SKILL.md").read_text().endswith("first policy skills")


def test_policy_mapping_requires_owned_policy_file():
    from pathlib import Path

    from agent_ops.deployment.engine import _selection_from_shared_files
    from agent_ops.deployment.models import PlannedFile
    from agent_ops.deployment.shared_selection import SharedPolicyBinding

    chosen = SharedSelection(
        sources=selection("a" * 40).sources,
        policy=SharedPolicyBinding(source_id="source", path="AGENTS.md"),
    )
    mapping = {
        "schema_version": 1,
        "selection_fingerprint": chosen.fingerprint,
        "skills": [
            {"name": "example", "source_id": "source", "commit": "a" * 40, "path": "skills/example"}
        ],
        "policy": {"source_id": "source", "commit": "a" * 40, "path": "AGENTS.md"},
    }
    map_file = PlannedFile(
        Path("snapshots") / chosen.fingerprint / "source-map.json",
        json.dumps(mapping).encode(),
        0o644,
    )
    with pytest.raises(ValueError, match="policy file"):
        _selection_from_shared_files((map_file,), chosen.fingerprint)
