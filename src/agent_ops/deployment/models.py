from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PureWindowsPath
from typing import Protocol, runtime_checkable

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


@dataclass(frozen=True)
class TargetSpec:
    id: str
    framework: Framework
    home: Path
    channel: str

    def __post_init__(self) -> None:
        _nonempty(self.id, "target id")
        _nonempty(self.channel, "target channel")


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
    target: TargetSpec
    files: tuple[PlannedFile, ...]
    removals: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        _nonempty(self.provider_id, "provider id")
        _nonempty(self.source_revision, "source revision")
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(
            self,
            "removals",
            tuple(_repository_relative_path(path) for path in self.removals),
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
    source_revision: str
    provider_ids: tuple[str, ...]
    files: tuple[ManifestFile, ...]
    directories: tuple[ManifestDirectory, ...]
    transaction_id: str

    def __post_init__(self) -> None:
        _nonempty(self.target_id, "target id")
        _nonempty(self.source_revision, "source revision")
        _nonempty(self.transaction_id, "transaction id")
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
        _nonempty(self.channel, "target channel")
        _nonempty(self.source_id, "source id")
        _nonempty(self.ref, "source ref")
        _nonempty(self.commit, "source commit")


@dataclass(frozen=True)
class DeploymentPlan:
    snapshots: tuple[SourceSnapshot, ...]
    provider_plans: tuple[ProviderPlan, ...]
    target_sources: tuple[TargetSource, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshots", tuple(self.snapshots))
        object.__setattr__(self, "provider_plans", tuple(self.provider_plans))
        object.__setattr__(self, "target_sources", tuple(self.target_sources))


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
        _nonempty(self.channel, "target channel")


@dataclass(frozen=True)
class TargetReadiness:
    ready: bool
    prerequisite: str | None


@dataclass(frozen=True)
class DeploymentReceipt:
    operation: str
    commits: tuple[str, ...]
    targets: tuple[TargetStatus, ...]

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
    ) -> tuple[Path, ...]:
        """Return repository-relative source paths selected for deployment."""

    def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
        """Produce the provider's immutable deployment plan."""
