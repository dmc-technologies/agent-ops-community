from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent_ops.deployment import (
    DeploymentAudit,
    DeploymentManifest,
    DeploymentPlan,
    DeploymentRequest,
    LegacyLinkTransition,
    ManifestDirectory,
    ManifestFile,
    PlannedFile,
    ProviderPlan,
    ProviderSourceClosure,
    SkillSourceClosure,
    SourceSnapshot,
    TargetChannelTransition,
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


def test_target_channel_transition_is_explicit_and_immutable() -> None:
    transition = TargetChannelTransition("codex-dev", "stable", "feature")

    assert transition.expected_prior_channel == "stable"
    assert transition.candidate_channel == "feature"
    with pytest.raises(FrozenInstanceError):
        transition.candidate_channel = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="prior channel"):
        TargetChannelTransition("codex-dev", "", "feature")


class _StringSubclass(str):
    pass


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_id", True),
        ("target_id", 1),
        ("expected_channel", Path("stable")),
        ("expected_link_text", _StringSubclass("configs/global/AGENTS.md")),
        ("replacement", bytearray(b"policy\n")),
        ("mode", True),
        ("mode", 0o1000),
    ],
)
def test_legacy_link_transition_rejects_nonexact_authority_fields(
    field: str, value: object
) -> None:
    values: dict[str, object] = {
        "provider_id": "private-config",
        "target_id": "codex-stable",
        "expected_channel": "stable",
        "destination": Path("AGENTS.md"),
        "expected_link_text": "configs/global/AGENTS.md",
        "replacement": b"policy\n",
        "mode": 0o644,
    }
    values[field] = value

    with pytest.raises(ValueError, match="legacy link"):
        LegacyLinkTransition(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_id", True),
        ("target_id", 1),
        ("target_id", Path("codex-dev")),
        ("target_id", _StringSubclass("codex-dev")),
        ("expected_prior_channel", True),
        ("expected_prior_channel", 1),
        ("expected_prior_channel", Path("stable")),
        ("expected_prior_channel", _StringSubclass("stable")),
        ("candidate_channel", True),
        ("candidate_channel", 1),
        ("candidate_channel", Path("feature")),
        ("candidate_channel", _StringSubclass("feature")),
    ],
)
def test_target_channel_transition_rejects_nonexact_string_fields(
    field: str, value: object
) -> None:
    values: dict[str, object] = {
        "target_id": "codex-dev",
        "expected_prior_channel": "stable",
        "candidate_channel": "feature",
    }
    values[field] = value

    with pytest.raises(ValueError, match="nonempty exact string"):
        TargetChannelTransition(**values)  # type: ignore[arg-type]


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


def test_preview_source_closure_binds_provider_skill_aliases_and_paths() -> None:
    skill = SkillSourceClosure(
        canonical_id="founder-brief",
        aliases=("brief",),
        paths=(Path("skills/founder-brief"), Path("configs/policy.yaml")),
    )
    closure = ProviderSourceClosure("public-skills", (skill,))

    assert closure.provider_id == "public-skills"
    assert closure.skills == (skill,)


def test_deployment_manifest_binds_exact_target_channel() -> None:
    manifest = DeploymentManifest(
        1,
        "codex-preview",
        Framework.CODEX,
        "preview-author",
        "a" * 64,
        ("public-skills",),
        (),
        (),
        "a" * 32,
        "unreviewed-local",
    )

    assert manifest.channel == "preview-author"
    with pytest.raises(ValueError, match="channel"):
        DeploymentManifest(
            **{**manifest.__dict__, "channel": ""}
        )


def test_preview_skill_closure_rejects_duplicate_identity_members() -> None:
    with pytest.raises(ValueError, match="aliases"):
        SkillSourceClosure(
            "founder-brief", ("brief", "brief"), (Path("skills/a"),)
        )
    with pytest.raises(ValueError, match="paths"):
        SkillSourceClosure(
            "founder-brief", (), (Path("skills/a"), Path("skills/a"))
        )


def test_preview_provider_closure_rejects_duplicate_canonical_ids_and_path_ownership() -> None:
    first = SkillSourceClosure("one", (), (Path("skills/one"),))
    duplicate = SkillSourceClosure("one", (), (Path("skills/two"),))
    overlapping = SkillSourceClosure("two", (), (Path("skills/one/resource.txt"),))

    with pytest.raises(ValueError, match="canonical"):
        ProviderSourceClosure("public-skills", (first, duplicate))
    with pytest.raises(ValueError, match="owned"):
        ProviderSourceClosure("public-skills", (first, overlapping))


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
