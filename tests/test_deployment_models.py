from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent_ops.deployment import (
    DeploymentAudit,
    DeploymentRequest,
    ManifestDirectory,
    ManifestFile,
    PlannedFile,
    ProviderPlan,
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
