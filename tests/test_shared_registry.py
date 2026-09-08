"""Shared machine choices use the existing atomic registry boundary."""

from dataclasses import replace

import pytest
import yaml

from agent_ops.deployment.models import SourceSpec
from agent_ops.deployment.registry import (
    DeploymentRegistry,
    RegistryConfig,
    SharedSelectionConfig,
    _dump_registry,
    _parse_registry,
)
from agent_ops.deployment.shared_selection import SharedSelection


def selection(commit="a" * 40, source_id="catalog"):
    return SharedSelection(sources=({
        "source_id": source_id, "commit": commit, "skill_paths": ("skills/example",),
    },))


def config(tmp_path):
    return RegistryConfig(
        2, (SourceSpec("catalog", "https://example.invalid/catalog.git"),), (), (),
        SharedSelectionConfig("machine", tmp_path / "shared", selection()),
    )


def test_shared_registry_atomic_round_trip_preserves_previous(tmp_path):
    registry = DeploymentRegistry(tmp_path / "registry.yaml")
    original = config(tmp_path)
    first = registry.save(original)
    changed = replace(original, shared_selection=replace(
        original.shared_selection, selected=selection("b" * 40), previous=selection(),
    ))
    second = registry.save(changed, expected_snapshot=first)
    assert registry.load() == changed
    assert second.fingerprint != first.fingerprint
    assert registry.load().shared_selection.target.home == tmp_path / "shared"
    with pytest.raises(ValueError, match="snapshot"):
        registry.save(original, expected_snapshot=first)
    assert registry.load() == changed


@pytest.mark.parametrize("change", ["unknown-source", "previous-source", "schema1", "empty"])
def test_shared_registry_refuses_invalid_selection_config(tmp_path, change):
    current = config(tmp_path)
    with pytest.raises(ValueError):
        if change == "unknown-source":
            replace(current, shared_selection=replace(
                current.shared_selection, selected=selection(source_id="absent")))
        elif change == "previous-source":
            replace(current, shared_selection=replace(
                current.shared_selection, previous=selection(source_id="absent")))
        elif change == "schema1":
            replace(current, schema_version=1)
        else:
            DeploymentRegistry(tmp_path / "registry.yaml").save(
                replace(current, shared_selection=None))


@pytest.mark.parametrize("change", ["extra", "missing", "nested-extra", "version", "bool"])
def test_shared_registry_strict_wire_keys(tmp_path, change):
    data = yaml.safe_load(_dump_registry(config(tmp_path)))
    if change == "extra":
        data["other"] = True
    elif change == "missing":
        del data["shared_selection"]
    elif change == "nested-extra":
        data["shared_selection"]["framework"] = "codex"
    elif change == "version":
        data["schema_version"] = 3
    else:
        data["schema_version"] = True
    with pytest.raises(ValueError):
        _parse_registry(yaml.safe_dump(data).encode())


def test_legacy_schema_round_trip_stays_unchanged(tmp_path):
    from agent_ops.deployment.models import TargetSpec
    from agent_ops.deployment.registry import ChannelSpec
    from agent_ops.registries.models import Framework

    legacy = RegistryConfig(
        1, (SourceSpec("catalog", "https://example.invalid/catalog.git"),),
        (ChannelSpec("stable", "catalog", "refs/heads/main"),),
        (TargetSpec("client", Framework.CODEX, tmp_path / "client", "stable"),),
    )
    data = _dump_registry(legacy)
    assert "shared_selection" not in yaml.safe_load(data)
    assert _parse_registry(data) == legacy


@pytest.mark.parametrize("collision", ["id", "home"])
def test_shared_target_cannot_overlap_legacy_target(tmp_path, collision):
    from agent_ops.deployment.models import TargetSpec
    from agent_ops.deployment.registry import ChannelSpec
    from agent_ops.registries.models import Framework

    current = config(tmp_path)
    target = TargetSpec(
        "machine" if collision == "id" else "client", Framework.CODEX,
        tmp_path / ("shared" if collision == "home" else "client"), "stable",
    )
    with pytest.raises(ValueError):
        DeploymentRegistry(tmp_path / "registry.yaml").save(replace(
            current, channels=(ChannelSpec("stable", "catalog", "refs/heads/main"),),
            targets=(target,),
        ))
