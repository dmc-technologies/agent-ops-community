import json

import pytest

from agent_ops.deployment.models import (
    DeploymentReceipt,
    SharedTargetStatus,
    TargetState,
    TargetStatus,
)
from agent_ops.deployment.registry import (
    _dump_receipt_wrapper,
    _parse_receipt_wrapper,
    _receipt_to_data,
)


def receipt():
    return DeploymentReceipt(
        "shared-sync",
        ("a" * 40,),
        (SharedTargetStatus("machine", TargetState.STABLE, "shared", "b" * 64),),
    )


def test_shared_receipt_round_trip_keeps_commit_and_selection_distinct():
    expected = receipt()
    content = _dump_receipt_wrapper(_receipt_to_data(expected), "c" * 64)
    data = json.loads(content)
    assert data["schema_version"] == 2
    assert data["receipt"]["commits"] == ["a" * 40]
    assert data["receipt"]["targets"][0]["selection_fingerprint"] == "b" * 64
    assert "commit" not in data["receipt"]["targets"][0]
    assert _parse_receipt_wrapper(content) == ("c" * 64, expected)


def test_legacy_receipt_preserves_wire_shape():
    legacy = DeploymentReceipt(
        "sync", ("a" * 40,), (TargetStatus("client", TargetState.STABLE, "stable", "a" * 40),)
    )
    content = _dump_receipt_wrapper(_receipt_to_data(legacy), "c" * 64)
    data = json.loads(content)
    assert data["schema_version"] == 1
    assert set(data["receipt"]["targets"][0]) == {"target_id", "state", "channel", "commit"}
    assert _parse_receipt_wrapper(content) == ("c" * 64, legacy)


@pytest.mark.parametrize("value", ["a" * 40, "B" * 64, "g" * 64, "", True])
def test_shared_status_refuses_invalid_fingerprint(value):
    with pytest.raises(ValueError):
        SharedTargetStatus("machine", TargetState.STABLE, "shared", value)


@pytest.mark.parametrize("change", ["state", "extra", "kind", "schema", "fingerprint"])
def test_shared_receipt_refuses_invalid_wire_values(change):
    data = json.loads(_dump_receipt_wrapper(_receipt_to_data(receipt()), "c" * 64))
    target = data["receipt"]["targets"][0]
    if change == "state":
        target["state"] = "unknown"
    elif change == "extra":
        target["commit"] = "a" * 40
    elif change == "kind":
        target["kind"] = "other"
    elif change == "schema":
        data["schema_version"] = 1
    else:
        target["selection_fingerprint"] = "a" * 40
    with pytest.raises(ValueError):
        _parse_receipt_wrapper(json.dumps(data).encode())


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("valid", TargetState.STABLE),
        ("old", TargetState.STALE),
        ("absent", TargetState.STALE),
        ("mapping", TargetState.MODIFIED),
        ("no-audit", TargetState.MODIFIED),
        ("failure", TargetState.FAILED),
    ],
)
def test_shared_status_requires_observed_mapping_and_audit(tmp_path, scenario, expected):
    from agent_ops.deployment.models import (
        DeploymentAudit,
        DeploymentManifest,
        SharedSelectionActivation,
        SourceSpec,
    )
    from agent_ops.deployment.registry import (
        DeploymentRegistry,
        RegistryConfig,
        SharedSelectionConfig,
    )
    from agent_ops.deployment.shared_selection import SharedSelection

    selection = SharedSelection(
        sources=({"source_id": "catalog", "commit": "a" * 40, "skill_paths": ("skills/example",)},)
    )
    config = SharedSelectionConfig("machine", tmp_path / "home", selection)
    registry = DeploymentRegistry(tmp_path / "registry.yaml")
    snapshot = registry.save(
        RegistryConfig(2, (SourceSpec("catalog", "https://example.invalid/r"),), (), (), config)
    )
    observed = "b" * 64 if scenario == "old" else selection.fingerprint
    from pathlib import Path

    manifest = DeploymentManifest(
        2,
        config.id,
        config.target.framework,
        config.channel,
        observed,
        ("shared-skills",),
        (),
        (),
        "c" * 32,
        selection_activation=SharedSelectionActivation(Path("snapshots") / observed),
    )
    audit = DeploymentAudit(config.id, True)
    status = registry.shared_status(
        manifest=None if scenario == "absent" else manifest,
        audit=None if scenario == "no-audit" else audit,
        source_mapping_fingerprint=None if scenario == "mapping" else observed,
        failure="read failed" if scenario == "failure" else None,
        snapshot=snapshot,
    )
    assert status.state is expected
    assert status.selection_fingerprint == (None if scenario == "absent" else observed)
