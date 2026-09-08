from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Protocol, runtime_checkable

from agent_ops.deployment.shared_selection import SharedSelection
from agent_ops.registries.models import Framework


def _repository_relative_path(path: Path) -> Path:
    windows_path = PureWindowsPath(path)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or windows_path.root
        or not path.parts
        or any(part == ".." for part in path.parts)
        or any(part == ".." for part in windows_path.parts)
    ):
        raise ValueError("managed path must be a normalized repository-relative path")
    return path


def _nonempty(value: str, name: str) -> None:
    if not value:
        raise ValueError(f"{name} must be nonempty")


def _exact_nonempty(value: str, name: str) -> None:
    if type(value) is not str or not value:
        raise ValueError(f"{name} must be nonempty exact string")


@dataclass(frozen=True)
class TargetSpec:
    id: str
    framework: Framework
    home: Path
    channel: str

    def __post_init__(self) -> None:
        _nonempty(self.id, "target id")
        _exact_nonempty(self.channel, "target channel")


class DeploymentTargetKind(StrEnum):
    SHARED = "shared"


@dataclass(frozen=True)
class SharedTargetSpec:
    id: str
    home: Path
    channel: str = "shared"

    def __post_init__(self) -> None:
        _exact_nonempty(self.id, "shared target id")
        _exact_nonempty(self.channel, "shared target channel")

    @property
    def framework(self) -> DeploymentTargetKind:
        return DeploymentTargetKind.SHARED


@dataclass(frozen=True)
class SharedSelectionActivation:
    """Activate one confined immutable snapshot through the fixed selector."""

    snapshot: Path

    def __post_init__(self) -> None:
        path = _repository_relative_path(self.snapshot)
        if (
            len(path.parts) != 2
            or path.parts[0] != "snapshots"
            or len(path.name) != 64
            or any(character not in "0123456789abcdef" for character in path.name)
        ):
            raise ValueError("shared selection snapshot must be snapshots/<64 lowercase hex>")
        object.__setattr__(self, "snapshot", path)

    @property
    def selector(self) -> Path:
        return Path("current")


@dataclass(frozen=True)
class TargetChannelTransition:
    target_id: str
    expected_prior_channel: str
    candidate_channel: str

    def __post_init__(self) -> None:
        _exact_nonempty(self.target_id, "transition target id")
        _exact_nonempty(self.expected_prior_channel, "expected prior channel")
        _exact_nonempty(self.candidate_channel, "candidate channel")


@dataclass(frozen=True)
class PlannedFile:
    path: Path
    content: bytes
    mode: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _repository_relative_path(self.path))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class LegacyLinkTransition:
    provider_id: str
    target_id: str
    expected_channel: str
    destination: Path
    expected_link_text: str
    replacement: bytes
    mode: int

    def __post_init__(self) -> None:
        _exact_nonempty(self.provider_id, "legacy link provider id")
        _exact_nonempty(self.target_id, "legacy link target id")
        _exact_nonempty(self.expected_channel, "legacy link expected channel")
        _exact_nonempty(self.expected_link_text, "legacy link text")
        object.__setattr__(self, "destination", _repository_relative_path(self.destination))
        if type(self.replacement) is not bytes or not self.replacement:
            raise ValueError("legacy link replacement must be nonempty bytes")
        if type(self.mode) is not int or not 0 <= self.mode <= 0o777 or not self.mode & 0o400:
            raise ValueError("legacy link mode must be an owner-readable permission mode")


@dataclass(frozen=True)
class PrimeGstackLegacyAdoption:
    """One-time migration from Prime gstack's retired owner-only manifest."""

    target_id: str
    expected_channel: str
    source_ref: str
    paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        if self.target_id != "public-skills:prime-agent":
            raise ValueError("Prime gstack legacy adoption requires the public Prime target")
        if self.expected_channel != "public":
            raise ValueError("Prime gstack legacy adoption requires the public channel")
        if (
            type(self.source_ref) is not str
            or len(self.source_ref) != 40
            or any(character not in "0123456789abcdef" for character in self.source_ref)
        ):
            raise ValueError("Prime gstack legacy adoption requires an exact commit ref")
        if type(self.paths) is not tuple or not self.paths:
            raise ValueError("Prime gstack legacy adoption requires owned paths")
        normalized = tuple(_repository_relative_path(path) for path in self.paths)
        if normalized != tuple(sorted(set(normalized), key=lambda path: path.as_posix())):
            raise ValueError("Prime gstack legacy adoption paths must be unique and canonical")
        for path in normalized:
            allowed = path.parts[:3] == (".agentops", "runtime", "gstack") or (
                path.parts[:1] == ("skills",)
                and len(path.parts) >= 3
                and (
                    path.parts[1] == "agentops-gstack"
                    or path.parts[1].startswith("agentops-gstack-")
                )
            )
            if not allowed:
                raise ValueError(f"Prime gstack legacy adoption path is outside its roots: {path}")
        object.__setattr__(self, "paths", normalized)


@dataclass(frozen=True)
class ManifestFile:
    path: Path
    fingerprint: str
    mode: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _repository_relative_path(self.path))


@dataclass(frozen=True)
class ManifestDirectory:
    path: Path
    mode: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _repository_relative_path(self.path))


@dataclass(frozen=True)
class ProviderPlan:
    provider_id: str
    source_revision: str
    target: TargetSpec | SharedTargetSpec
    files: tuple[PlannedFile, ...]
    removals: tuple[Path, ...] = ()
    audit_roots: tuple[Path, ...] = ()
    runtime_python_sources: tuple[Path, ...] = ()
    legacy_link_transition: LegacyLinkTransition | None = None
    prime_gstack_legacy_adoption: PrimeGstackLegacyAdoption | None = None
    selection_activation: SharedSelectionActivation | None = None

    def __post_init__(self) -> None:
        if self.selection_activation is not None:
            if type(self.selection_activation) is not SharedSelectionActivation:
                raise ValueError("shared activation must be an exact immutable value")
            if type(self.target) is not SharedTargetSpec:
                raise ValueError("shared activation requires a shared target")
        elif type(self.target) is SharedTargetSpec:
            raise ValueError("shared target requires selection activation")
        _nonempty(self.provider_id, "provider id")
        _nonempty(self.source_revision, "source revision")
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(
            self,
            "removals",
            tuple(_repository_relative_path(path) for path in self.removals),
        )
        object.__setattr__(
            self, "audit_roots", tuple(_repository_relative_path(path) for path in self.audit_roots)
        )
        object.__setattr__(
            self,
            "runtime_python_sources",
            tuple(_repository_relative_path(path) for path in self.runtime_python_sources),
        )
        if self.legacy_link_transition is not None:
            transition = self.legacy_link_transition
            if type(transition) is not LegacyLinkTransition:
                raise ValueError("legacy link transition must be an exact immutable value")
            if transition.provider_id != self.provider_id or transition.target_id != self.target.id:
                raise ValueError("legacy link transition must belong to its provider plan")
            if transition.expected_channel != self.target.channel:
                raise ValueError("legacy link transition channel must match its provider plan")
        if self.prime_gstack_legacy_adoption is not None:
            adoption = self.prime_gstack_legacy_adoption
            if type(adoption) is not PrimeGstackLegacyAdoption:
                raise ValueError("Prime gstack legacy adoption must be an exact immutable value")
            if (
                self.provider_id != "public-skill:gstack"
                or self.target.framework is not Framework.PRIME_AGENT
                or adoption.target_id != self.target.id
                or adoption.expected_channel != self.target.channel
            ):
                raise ValueError("Prime gstack legacy adoption must belong to its public plan")
            planned_paths = {item.path for item in self.files}
            allowed_addition = Path("skills/.agentops-public-provider-index.json")
            if planned_paths not in (
                set(adoption.paths),
                set(adoption.paths) | {allowed_addition},
            ):
                raise ValueError("Prime gstack legacy adoption must bind its planned destinations")


@dataclass(frozen=True)
class SkillSourceClosure:
    """Canonical preview identity and the source paths that it exclusively owns."""

    canonical_id: str
    aliases: tuple[str, ...]
    paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        _nonempty(self.canonical_id, "canonical skill id")
        if type(self.aliases) is not tuple or any(
            type(alias) is not str or not alias for alias in self.aliases
        ):
            raise ValueError("skill aliases must be nonempty strings")
        if len(set(self.aliases)) != len(self.aliases) or self.canonical_id in self.aliases:
            raise ValueError("skill aliases must be unique and differ from the canonical id")
        if type(self.paths) is not tuple or not self.paths:
            raise ValueError("skill source paths must be a nonempty tuple")
        normalized = tuple(_repository_relative_path(path) for path in self.paths)
        if len(set(normalized)) != len(normalized):
            raise ValueError("skill source paths must be unique")
        object.__setattr__(self, "aliases", tuple(sorted(self.aliases)))
        object.__setattr__(
            self, "paths", tuple(sorted(normalized, key=lambda path: path.as_posix()))
        )


@dataclass(frozen=True)
class ProviderSourceClosure:
    """Preview-only identity binding returned by an installed provider."""

    provider_id: str
    skills: tuple[SkillSourceClosure, ...]

    def __post_init__(self) -> None:
        _nonempty(self.provider_id, "provider source closure id")
        if type(self.skills) is not tuple or any(
            type(skill) is not SkillSourceClosure for skill in self.skills
        ):
            raise ValueError("provider source closure skills must be exact values")
        canonical = [skill.canonical_id for skill in self.skills]
        if len(set(canonical)) != len(canonical):
            raise ValueError("provider source closure has duplicate canonical skill identities")
        names: set[str] = set()
        owned: list[Path] = []
        for skill in self.skills:
            identities = {skill.canonical_id, *skill.aliases}
            if names & identities:
                raise ValueError("provider source closure has duplicate skill aliases")
            names.update(identities)
            for path in skill.paths:
                if any(
                    path == prior or path in prior.parents or prior in path.parents
                    for prior in owned
                ):
                    raise ValueError("provider source path is not exclusively owned by one skill")
                owned.append(path)
        object.__setattr__(
            self,
            "skills",
            tuple(sorted(self.skills, key=lambda skill: skill.canonical_id)),
        )


@dataclass(frozen=True)
class DeploymentRequest:
    source_id: str
    revision: str
    targets: tuple[TargetSpec, ...]

    def __post_init__(self) -> None:
        _nonempty(self.source_id, "source id")
        _nonempty(self.revision, "revision")
        object.__setattr__(self, "targets", tuple(self.targets))


@dataclass(frozen=True)
class SourceSpec:
    id: str
    url: str
    stable_ref: str = "refs/heads/main"

    def __post_init__(self) -> None:
        _nonempty(self.id, "source id")
        _nonempty(self.url, "source url")
        _nonempty(self.stable_ref, "stable ref")


@dataclass(frozen=True)
class SourceSnapshot:
    source_id: str
    ref: str
    commit: str
    root: Path

    def __post_init__(self) -> None:
        _nonempty(self.source_id, "source id")
        _nonempty(self.ref, "source ref")
        _nonempty(self.commit, "source commit")


@dataclass(frozen=True)
class RewriteAcceptance:
    old_commit: str
    new_commit: str

    def __post_init__(self) -> None:
        _nonempty(self.old_commit, "old commit")
        _nonempty(self.new_commit, "new commit")


@dataclass(frozen=True)
class DeploymentManifest:
    schema_version: int
    target_id: str
    framework: Framework
    channel: str
    source_revision: str
    provider_ids: tuple[str, ...]
    files: tuple[ManifestFile, ...]
    directories: tuple[ManifestDirectory, ...]
    transaction_id: str
    review_state: str | None = None
    selection_activation: SharedSelectionActivation | None = None

    def __post_init__(self) -> None:
        _nonempty(self.target_id, "target id")
        _exact_nonempty(self.channel, "manifest channel")
        _nonempty(self.source_revision, "source revision")
        _nonempty(self.transaction_id, "transaction id")
        if self.review_state not in {None, "unreviewed-local"}:
            raise ValueError("manifest review state is invalid")
        object.__setattr__(self, "provider_ids", tuple(self.provider_ids))
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "directories", tuple(self.directories))


@dataclass(frozen=True)
class TargetSource:
    target_id: str
    channel: str
    source_id: str
    ref: str
    commit: str

    def __post_init__(self) -> None:
        _nonempty(self.target_id, "target id")
        _exact_nonempty(self.channel, "target channel")
        _nonempty(self.source_id, "source id")
        _nonempty(self.ref, "source ref")
        _nonempty(self.commit, "source commit")


@dataclass(frozen=True)
class SharedTargetSource:
    """Bind a target to a validated selection without treating its fingerprint as Git."""

    target_id: str
    channel: str
    selection: SharedSelection

    def __post_init__(self) -> None:
        _exact_nonempty(self.target_id, "shared target id")
        _exact_nonempty(self.channel, "shared target channel")
        if type(self.selection) is not SharedSelection:
            raise ValueError("shared target selection must be an exact SharedSelection value")

    @property
    def selection_fingerprint(self) -> str:
        return self.selection.fingerprint


@dataclass(frozen=True)
class DeploymentPlan:
    snapshots: tuple[SourceSnapshot, ...]
    provider_plans: tuple[ProviderPlan, ...]
    target_sources: tuple[TargetSource | SharedTargetSource, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        object.__setattr__(self, "provider_plans", tuple(self.provider_plans))
        object.__setattr__(self, "target_sources", tuple(self.target_sources))
        if any(
            type(item) not in (TargetSource, SharedTargetSource) for item in self.target_sources
        ):
            raise ValueError("plan target sources must be exact source association values")
        planned_ids = {plan.target.id for plan in self.provider_plans}
        association_ids = [association.target_id for association in self.target_sources]
        if set(association_ids) != planned_ids or len(association_ids) != len(planned_ids):
            raise ValueError("plan requires exactly one source association per planned target")
        associations = {association.target_id: association for association in self.target_sources}
        for target_id in sorted(planned_ids):
            association = associations[target_id]
            target_plans = tuple(
                plan for plan in self.provider_plans if plan.target.id == target_id
            )
            if any(plan.target.channel != association.channel for plan in target_plans):
                raise ValueError("plan source association target channel does not match")
            if type(association) is SharedTargetSource:
                if any(
                    plan.source_revision != association.selection_fingerprint
                    for plan in target_plans
                ):
                    raise ValueError("shared provider revision must match selection fingerprint")
                for binding in association.selection.sources:
                    matches = tuple(
                        snapshot for snapshot in self.snapshots
                        if snapshot.source_id == binding.source_id
                    )
                    if (
                        len(matches) != 1
                        or matches[0].commit != binding.commit
                        or matches[0].ref != binding.commit
                    ):
                        raise ValueError("shared source requires one unique exact pinned snapshot")
                continue
            if any(plan.source_revision != association.commit for plan in target_plans):
                raise ValueError("plan source association does not match provider revision")
            matches = tuple(
                snapshot
                for snapshot in self.snapshots
                if (
                    snapshot.source_id,
                    snapshot.ref,
                    snapshot.commit,
                )
                == (
                    association.source_id,
                    association.ref,
                    association.commit,
                )
            )
            if len(matches) != 1:
                raise ValueError("plan source association must map to one unique source snapshot")


class TargetState(StrEnum):
    STABLE = "stable"
    BRANCH = "branch"
    PREVIEW = "preview"
    STALE = "stale"
    MODIFIED = "modified"
    FAILED = "failed"
    MISSING_REF = "missing-ref"


@dataclass(frozen=True)
class TargetStatus:
    target_id: str
    state: TargetState
    channel: str
    commit: str | None

    def __post_init__(self) -> None:
        _nonempty(self.target_id, "target id")
        _exact_nonempty(self.channel, "target channel")


@dataclass(frozen=True)
class SharedTargetStatus:
    """Observed shared selection identity, distinct from a constituent Git commit."""

    target_id: str
    state: TargetState
    channel: str
    selection_fingerprint: str | None

    def __post_init__(self) -> None:
        _exact_nonempty(self.target_id, "shared status target id")
        _exact_nonempty(self.channel, "shared status channel")
        if type(self.state) is not TargetState:
            raise ValueError("shared status state must be a TargetState")
        value = self.selection_fingerprint
        if value is not None and (
            type(value) is not str or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError("selection fingerprint must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True)
class TargetReadiness:
    ready: bool
    prerequisite: str | None


@dataclass(frozen=True)
class DeploymentReceipt:
    operation: str
    commits: tuple[str, ...]
    targets: tuple[TargetStatus | SharedTargetStatus, ...]

    def __post_init__(self) -> None:
        _nonempty(self.operation, "operation")
        object.__setattr__(self, "commits", tuple(self.commits))
        object.__setattr__(self, "targets", tuple(self.targets))


@dataclass(frozen=True)
class DeploymentAudit:
    target_id: str
    matches: bool
    missing: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    unexpected: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.target_id, "target id")
        object.__setattr__(self, "missing", tuple(self.missing))
        object.__setattr__(self, "changed", tuple(self.changed))
        object.__setattr__(self, "unexpected", tuple(self.unexpected))
        object.__setattr__(self, "duplicates", tuple(self.duplicates))
        object.__setattr__(self, "validation_errors", tuple(self.validation_errors))


@runtime_checkable
class DeploymentProvider(Protocol):
    provider_id: str

    def supports(self, snapshot: SourceSnapshot, target: TargetSpec) -> bool:
        """Return whether this provider can deploy the source to the target."""

    def source_closure(
        self,
        snapshot: SourceSnapshot,
        target: TargetSpec,
        selection: tuple[str, ...] | None,
    ) -> tuple[Path, ...] | ProviderSourceClosure:
        """Return repository-relative source paths selected for deployment."""

    def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
        """Produce the provider's immutable deployment plan."""
