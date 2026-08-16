from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent_ops.deployment import (
    DeploymentAudit,
    DeploymentPlan,
    DeploymentRequest,
    ManifestDirectory,
    ManifestFile,
    PlannedFile,
    ProviderPlan,
    SourceSnapshot,
    TargetSource,
    TargetSpec,
)
from agent_ops.deployment.providers import load_deployment_providers
from agent_ops.registries.models import Framework


def test_target_and_planned_file_are_immutable_and_repo_relative() -> None:
    target = TargetSpec(
        id="codex-dev",
        framework=Framework.CODEX,
        home=Path("/tmp/codex"),
        channel="feature",
    )
    request = DeploymentRequest(source_id="example", revision="a" * 40, targets=(target,))
    planned = PlannedFile(path=Path("skills/example/SKILL.md"), content=b"example\n", mode=0o644)

    assert request.targets == (target,)
    assert planned.path.as_posix() == "skills/example/SKILL.md"
    assert planned.fingerprint == "13550350a8681c84c861aac2e5b440161c2b33a3e4f302ac680ca5b686de48de"

    with pytest.raises(FrozenInstanceError):
        target.channel = "stable"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        planned.mode = 0o600  # type: ignore[misc]


@pytest.mark.parametrize(
    "path",
    (
        Path("/managed"),
        Path("../managed"),
        Path("."),
        Path("C:managed"),
        Path(r"\managed"),
        Path(r"managed\..\other"),
    ),
)
def test_managed_paths_reject_unsafe_repository_relative_values(path: Path) -> None:
    with pytest.raises(ValueError, match="repository-relative"):
        PlannedFile(path=path, content=b"example\n", mode=0o644)
    with pytest.raises(ValueError, match="repository-relative"):
        ManifestFile(path=path, fingerprint="a" * 64, mode=0o644)
    with pytest.raises(ValueError, match="repository-relative"):
        ManifestDirectory(path=path, mode=0o755)
    with pytest.raises(ValueError, match="repository-relative"):
        ProviderPlan(
            provider_id="example",
            source_revision="a" * 40,
            target=TargetSpec(
                id="codex-dev",
                framework=Framework.CODEX,
                home=Path("/tmp/codex"),
                channel="feature",
            ),
            files=(),
            removals=(path,),
        )


def test_deployment_audit_reports_boolean_match_with_empty_diagnostics() -> None:
    audit = DeploymentAudit(target_id="codex-dev", matches=True)

    assert audit.matches is True
    assert audit.missing == ()
    assert audit.changed == ()
    assert audit.unexpected == ()
    assert audit.duplicates == ()
    assert audit.validation_errors == ()

    with pytest.raises(FrozenInstanceError):
        audit.matches = False  # type: ignore[misc]


def test_deployment_provider_entry_points_are_separate_from_runner_plugins(monkeypatch) -> None:
    requested_groups: list[str] = []

    def empty_entry_points(*, group: str) -> tuple[()]:
        requested_groups.append(group)
        return ()

    monkeypatch.setattr("agent_ops.deployment.providers.entry_points", empty_entry_points)

    assert load_deployment_providers() == []
    assert requested_groups == ["agent_ops.deployment_providers"]


def _valid_deployment_plan_parts():
    target = TargetSpec("codex-dev", Framework.CODEX, Path("/tmp/codex"), "feature")
    snapshot = SourceSnapshot(
        "community", "refs/heads/feature", "a" * 40, Path("/snapshot")
    )
    provider = ProviderPlan(
        "skills", snapshot.commit, target, (), (Path("obsolete"),)
    )
    association = TargetSource(
        target.id,
        target.channel,
        snapshot.source_id,
        snapshot.ref,
        snapshot.commit,
    )
    return target, snapshot, provider, association


def test_deployment_plan_requires_target_source_associations() -> None:
    _target, snapshot, provider, _association = _valid_deployment_plan_parts()

    with pytest.raises(TypeError):
        DeploymentPlan((snapshot,), (provider,))


@pytest.mark.parametrize(
    "change, message",
    [
        ("missing", "exactly one source association"),
        ("duplicate", "exactly one source association"),
        ("extra", "exactly one source association"),
        ("commit", "provider revision"),
        ("channel", "target channel"),
        ("snapshot", "unique source snapshot"),
        ("duplicate-snapshot", "unique source snapshot"),
    ],
)
def test_deployment_plan_rejects_invalid_target_source_associations(
    change: str, message: str
) -> None:
    target, snapshot, provider, association = _valid_deployment_plan_parts()
    snapshots = (snapshot,)
    providers = (provider,)
    associations = (association,)

    if change == "missing":
        associations = ()
    elif change == "duplicate":
        associations = (association, association)
    elif change == "extra":
        associations = associations + (
            TargetSource("other", "feature", "community", snapshot.ref, snapshot.commit),
        )
    elif change == "commit":
        associations = (
            TargetSource(target.id, target.channel, "community", snapshot.ref, "b" * 40),
        )
        snapshots = snapshots + (
            SourceSnapshot("community", snapshot.ref, "b" * 40, Path("/other")),
        )
    elif change == "channel":
        associations = (
            TargetSource(target.id, "other", "community", snapshot.ref, snapshot.commit),
        )
    elif change == "snapshot":
        associations = (
            TargetSource(
                target.id,
                target.channel,
                "other",
                snapshot.ref,
                snapshot.commit,
            ),
        )
    elif change == "duplicate-snapshot":
        snapshots = snapshots + (
            SourceSnapshot(
                snapshot.source_id,
                snapshot.ref,
                snapshot.commit,
                Path("/duplicate"),
            ),
        )

    with pytest.raises(ValueError, match=message):
        DeploymentPlan(snapshots, providers, associations)
