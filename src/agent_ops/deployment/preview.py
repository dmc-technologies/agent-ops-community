"""Confined deployment previews from an explicit local Git checkout."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from agent_ops.deployment.engine import DeploymentAuditError, DeploymentRecoveryError
from agent_ops.deployment.models import (
    DeploymentManifest,
    DeploymentProvider,
    ProviderPlan,
    ProviderSourceClosure,
    SourceSnapshot,
    TargetSpec,
)
from agent_ops.deployment.providers import (
    load_deployment_providers,
    normalize_deployment_providers,
)
from agent_ops.deployment.registry import DeploymentRegistry, _is_preview_channel
from agent_ops.deployment.transaction import (
    _locked_provider_plan_targets,
    _preflight_provider_plans_read_only,
    _verify_locked_provider_plan_targets,
    audit_provider_plans,
    install_provider_plans,
    rollback_manifests,
)


@dataclass(frozen=True)
class PreviewResult:
    """Evidence for one installed, explicitly unreviewed local preview."""

    operation: str
    review_state: str
    target_id: str
    channel: str
    fingerprint: str
    source_revision: str
    providers: tuple[str, ...]
    paths: tuple[str, ...]


@dataclass(frozen=True)
class _CapturedEntry:
    path: Path
    kind: str
    mode: int
    content: bytes | None


def _before_capture_open(_path: Path) -> None:
    """Test hook immediately before a baseline path is pinned."""


def _before_preview_apply(_authority: _PreviewAuthority) -> None:
    """Test hook immediately before final source-authority verification."""


class PreviewEngine:
    """Plan and install selected working-tree data without a managed fetch."""

    def __init__(
        self,
        registry: DeploymentRegistry,
        providers: tuple[DeploymentProvider, ...] | None = None,
    ) -> None:
        if not isinstance(registry, DeploymentRegistry):
            raise TypeError("registry must be a DeploymentRegistry")
        if providers is not None and type(providers) is not tuple:
            raise TypeError("providers must be a tuple when explicitly supplied")
        discovered = load_deployment_providers() if providers is None else providers
        self._registry = registry
        self._providers = normalize_deployment_providers(discovered)

    def preview(
        self,
        source_checkout: Path,
        skills: tuple[str, ...],
        target_id: str,
    ) -> PreviewResult:
        checkout = _checkout_root(source_checkout)
        selection = _selection(skills)
        registry_snapshot = self._registry.load_snapshot()
        targets = {
            candidate.id: candidate for candidate in registry_snapshot.config.targets
        }
        try:
            target = targets[target_id]
        except KeyError as error:
            raise ValueError(f"unknown target: {target_id}") from error
        if not _is_preview_channel(target.channel):
            raise ValueError("local preview requires a preview-reserved target channel")
        try:
            target_mode = target.home.lstat().st_mode
        except FileNotFoundError:
            raise ValueError(
                "local preview requires an existing isolated preview target home"
            ) from None
        if stat.S_ISLNK(target_mode) or not stat.S_ISDIR(target_mode):
            raise ValueError(
                "local preview requires an existing isolated preview target home"
            )
        _reject_overlapping_roots(checkout, target.home)

        head = _git(("rev-parse", "--verify", "HEAD^{commit}"), checkout)
        provisional = SourceSnapshot(
            "unreviewed-local", "refs/heads/local-preview", head, checkout
        )
        supported = self._supported(provisional, target)
        closures: dict[str, ProviderSourceClosure] = {}
        all_declared: set[Path] = set()
        for provider in supported:
            closure = provider.source_closure(provisional, target, selection)
            if type(closure) is not ProviderSourceClosure:
                raise ValueError(
                    "preview provider source closure must be identity-bound"
                )
            if closure.provider_id != provider.provider_id:
                raise ValueError("preview source closure provider identity differs")
            closures[provider.provider_id] = closure
            all_declared.update(
                path for skill in closure.skills for path in skill.paths
            )
        _validate_selected_skill_closures(selection, tuple(closures.values()))
        with _capture_tracked_closure(
            checkout, tuple(all_declared), expected_head=head
        ) as authority:
            captured = authority.entries
            fingerprint = _closure_fingerprint(captured)

            plans: list[ProviderPlan] = []
            for provider in supported:
                provider_entries = _provider_entries(
                    captured,
                    tuple(
                        path
                        for skill in closures[provider.provider_id].skills
                        for path in skill.paths
                    ),
                )
                with tempfile.TemporaryDirectory(
                    prefix="agentops-local-preview-"
                ) as raw:
                    restricted_root = Path(raw)
                    _materialize(restricted_root, provider_entries)
                    restricted = SourceSnapshot(
                        "unreviewed-local",
                        "refs/heads/local-preview",
                        fingerprint,
                        restricted_root,
                    )
                    plan = provider.plan(restricted, target)
                    _validate_plan(plan, provider, target, fingerprint)
                    plans.append(plan)
                    _verify_materialized(restricted_root, provider_entries)

            ordered_plans = tuple(sorted(plans, key=lambda item: item.provider_id))
            _preflight_provider_plans_read_only(ordered_plans)
            manifests: tuple[DeploymentManifest, ...] = ()
            with _locked_provider_plan_targets(ordered_plans):
                _before_preview_apply(authority)
                authority.verify()
                try:
                    manifests = install_provider_plans(ordered_plans)
                    audit = audit_provider_plans(ordered_plans)
                    if not audit.matches:
                        raise DeploymentAuditError(
                            f"deployment audit did not match preview target {target.id!r}"
                        )
                    _verify_locked_provider_plan_targets(ordered_plans)
                    authority.verify()
                    return PreviewResult(
                        operation="preview",
                        review_state="unreviewed-local",
                        target_id=target.id,
                        channel=target.channel,
                        fingerprint=fingerprint,
                        source_revision=fingerprint,
                        providers=tuple(
                            plan.provider_id for plan in ordered_plans
                        ),
                        paths=tuple(
                            entry.path.as_posix()
                            for entry in captured
                            if entry.kind == "file"
                        ),
                    )
                except BaseException as error:
                    if manifests:
                        try:
                            rollback_manifests(manifests)
                        except BaseException as rollback_error:
                            if not isinstance(rollback_error, Exception):
                                rollback_error.add_note(
                                    "local preview recovery was incomplete; "
                                    f"original failure: {error}; transaction evidence "
                                    "was retained"
                                )
                                raise
                            if not isinstance(error, Exception):
                                error.add_note(
                                    "local preview recovery was incomplete; transaction "
                                    "evidence was retained"
                                )
                                raise error from rollback_error
                            raise DeploymentRecoveryError(
                                f"local preview failed: {error}; recovery incomplete: "
                                f"{rollback_error}; transaction evidence was retained"
                            ) from rollback_error
                    raise

    def _supported(
        self, snapshot: SourceSnapshot, target: TargetSpec
    ) -> tuple[DeploymentProvider, ...]:
        supported: list[DeploymentProvider] = []
        for provider in self._providers:
            decision = provider.supports(snapshot, target)
            if type(decision) is not bool:
                raise ValueError("provider supports decision must be boolean")
            if decision:
                supported.append(provider)
        if not supported:
            raise ValueError(f"no deployment provider supports target {target.id!r}")
        return tuple(supported)


def _validate_selected_skill_closures(
    selection: tuple[str, ...], closures: tuple[ProviderSourceClosure, ...]
) -> None:
    identities: dict[str, tuple[str, str]] = {}
    owned_paths: list[tuple[str, str, Path]] = []
    matched_by_request: dict[str, tuple[str, str]] = {}
    for closure in closures:
        for skill in closure.skills:
            owner = (closure.provider_id, skill.canonical_id)
            names = (skill.canonical_id, *skill.aliases)
            for name in names:
                prior = identities.get(name)
                if prior is not None and prior != owner:
                    raise ValueError("preview skill identity collision across providers")
                identities[name] = owner
            matching = tuple(request for request in selection if request in names)
            if not matching:
                raise ValueError("preview provider ignored the explicit skill selection")
            for request in matching:
                prior = matched_by_request.get(request)
                if prior is not None and prior != owner:
                    raise ValueError("requested skill must resolve exactly once")
                matched_by_request[request] = owner
            for path in skill.paths:
                for prior_provider, prior_skill, prior_path in owned_paths:
                    if (
                        path == prior_path
                        or path in prior_path.parents
                        or prior_path in path.parents
                    ):
                        raise ValueError(
                            "preview source path ownership collision between "
                            f"{prior_provider}:{prior_skill} and "
                            f"{closure.provider_id}:{skill.canonical_id}"
                        )
                owned_paths.append((closure.provider_id, skill.canonical_id, path))
    missing = [request for request in selection if request not in matched_by_request]
    if missing:
        raise ValueError(f"requested skill did not resolve exactly once: {missing[0]}")
    owners = tuple(matched_by_request[request] for request in selection)
    if len(set(owners)) != len(owners):
        raise ValueError("multiple requested skill aliases resolve to one canonical skill")


def _selection(skills: tuple[str, ...]) -> tuple[str, ...]:
    if type(skills) is not tuple or not skills:
        raise ValueError("an explicit nonempty skill selection is required")
    if any(type(skill) is not str or not skill or skill.strip() != skill for skill in skills):
        raise ValueError("skill selection entries must be nonempty exact strings")
    if len(set(skills)) != len(skills):
        raise ValueError("skill selection entries must be unique")
    return tuple(sorted(skills))


def _checkout_root(source_checkout: Path) -> Path:
    if not isinstance(source_checkout, Path):
        raise ValueError("source checkout must be a Path")
    try:
        checkout = source_checkout.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("source checkout must be an existing directory") from error
    if not checkout.is_dir():
        raise ValueError("source checkout must be an existing directory")
    top = _git(("rev-parse", "--show-toplevel"), checkout)
    if Path(top).resolve(strict=True) != checkout:
        raise ValueError("source checkout must explicitly name the Git worktree root")
    return checkout


def _git(arguments: tuple[str, ...], checkout: Path) -> str:
    environment = {
        name: value
        for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE")
        if (value := os.environ.get(name)) is not None
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    completed = subprocess.run(
        (
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "credential.interactive=false",
            "-c",
            "protocol.allow=never",
            *arguments,
        ),
        cwd=checkout,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode:
        raise ValueError(completed.stderr.strip() or "source checkout Git query failed")
    return completed.stdout.strip()


def _tracked_entries(checkout: Path) -> dict[str, tuple[str, str]]:
    listing = _git(("ls-files", "--stage", "-z"), checkout)
    entries: dict[str, tuple[str, str]] = {}
    for record in listing.split("\0"):
        if not record:
            continue
        descriptor, path = record.split("\t", 1)
        mode, object_id, stage = descriptor.split(" ", 2)
        if stage != "0":
            raise ValueError("selected source closure contains an unresolved Git entry")
        entries[path] = (mode, object_id)
    return entries


def _validate_relative(path: Path) -> str:
    if not isinstance(path, Path):
        raise ValueError("provider source closure entries must be Path values")
    raw = str(path)
    windows = PureWindowsPath(raw)
    if (
        raw in {"", "."}
        or path.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or "\\" in raw
        or "//" in raw
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != raw
        or path.parts[0] == ".git"
    ):
        raise ValueError("provider data path must be exact and repository-relative")
    return raw


class _PinnedAbsoluteFile:
    def __init__(self, path: Path, label: str) -> None:
        self._path = path
        self._label = label
        self._fds: list[int] = []
        self._chain: list[tuple[int, str, tuple[int, int, int]]] = []
        current = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._fds.append(current)
        try:
            for index, part in enumerate(path.parts[1:]):
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if index < len(path.parts[1:]) - 1:
                    flags |= os.O_DIRECTORY
                next_fd = os.open(part, flags, dir_fd=current)
                observed = os.fstat(next_fd)
                self._chain.append((current, part, _structural_identity(observed)))
                self._fds.append(next_fd)
                current = next_fd
            final = os.fstat(current)
            if not stat.S_ISREG(final.st_mode):
                raise RuntimeError(f"{label} must be a regular file")
            self._final = current
            self._identity = _identity(final)
            self._content = _read_descriptor(current)
        except BaseException:
            self.close()
            raise

    def verify(self) -> None:
        for parent, name, expected in self._chain:
            try:
                observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                raise RuntimeError(f"{self._label} changed") from None
            if _structural_identity(observed) != expected:
                raise RuntimeError(f"{self._label} changed")
        if (
            _identity(os.fstat(self._final)) != self._identity
            or _read_descriptor(self._final) != self._content
        ):
            raise RuntimeError(f"{self._label} changed")

    def close(self) -> None:
        for descriptor in reversed(self._fds):
            with suppress(OSError):
                os.close(descriptor)
        self._fds.clear()


class _PinnedPreviewEntry:
    def __init__(
        self,
        root_fd: int,
        path: Path,
        expected: tuple[int, int, int, int, int],
        git_mode: str | None,
    ) -> None:
        self.path = path
        self._fds: list[int] = [os.dup(root_fd)]
        self._chain: list[tuple[int, str, tuple[int, int, int, int, int]]] = []
        current = self._fds[0]
        try:
            _before_capture_open(path)
            for index, part in enumerate(path.parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW
                if index < len(path.parts) - 1 or git_mode is None:
                    flags |= os.O_DIRECTORY
                next_fd = os.open(part, flags, dir_fd=current)
                observed = os.fstat(next_fd)
                self._chain.append((current, part, _identity(observed)))
                self._fds.append(next_fd)
                current = next_fd
            observed = os.fstat(current)
            if _identity(observed) != expected:
                raise RuntimeError("selected source closure changed during capture")
            permission_mode = stat.S_IMODE(observed.st_mode)
            if observed.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                raise ValueError("selected source closure contains an unsafe mode")
            if permission_mode & 0o022:
                raise ValueError("selected source closure contains a writable unsafe mode")
            if git_mode is None:
                if not stat.S_ISDIR(observed.st_mode) or permission_mode & 0o700 != 0o700:
                    raise ValueError("selected source directory mode is invalid")
                kind = "directory"
                content = None
            else:
                if git_mode not in {"100644", "100755"}:
                    raise ValueError("selected Git entry mode is unsupported")
                if not stat.S_ISREG(observed.st_mode) or permission_mode & 0o400 == 0:
                    raise ValueError("selected source file mode is invalid")
                if bool(permission_mode & 0o111) != (git_mode == "100755"):
                    raise ValueError("selected Git entry mode differs from the worktree mode")
                kind = "file"
                content = _read_descriptor(current)
            self._final = current
            self._identity = _identity(observed)
            self.captured = _CapturedEntry(path, kind, permission_mode, content)
        except BaseException:
            self.close()
            raise

    def verify(self) -> None:
        for parent, name, expected in self._chain:
            try:
                observed = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                raise RuntimeError("selected source closure changed") from None
            if _identity(observed) != expected:
                raise RuntimeError("selected source closure changed")
        if _identity(os.fstat(self._final)) != self._identity:
            raise RuntimeError("selected source closure changed")
        if (
            self.captured.content is not None
            and _read_descriptor(self._final) != self.captured.content
        ):
            raise RuntimeError("selected source closure content changed")

    def close(self) -> None:
        for descriptor in reversed(self._fds):
            with suppress(OSError):
                os.close(descriptor)
        self._fds.clear()


class _PreviewAuthority:
    def __init__(
        self, checkout: Path, declared: tuple[Path, ...], expected_head: str
    ) -> None:
        self.checkout = checkout
        self.expected_head = expected_head
        self._entries: list[_PinnedPreviewEntry] = []
        self._root_fd = -1
        self._index: _PinnedAbsoluteFile | None = None
        self._head: _PinnedAbsoluteFile | None = None
        try:
            raw_declared = tuple(_validate_relative(path) for path in declared)
            if not raw_declared:
                raise ValueError("selected providers returned an empty source closure")
            if len(raw_declared) != len(set(raw_declared)):
                raise ValueError("provider data closure contains duplicate paths")
            tracked = _tracked_entries(checkout)
            self._tracked = tracked
            selected_files = {
                path
                for path in tracked
                if any(
                    path == root or path.startswith(f"{root}/")
                    for root in raw_declared
                )
            }
            if any(
                root not in tracked
                and not any(path.startswith(f"{root}/") for path in tracked)
                for root in raw_declared
            ):
                raise ValueError("every referenced preview resource must be Git-tracked")
            if any(tracked[path][0] == "120000" for path in selected_files):
                raise ValueError("selected source closure must not contain a symbolic link")
            if any(
                tracked[path][0] not in {"100644", "100755"}
                for path in selected_files
            ):
                raise ValueError("selected Git entry mode is unsupported")

            actual_files = _actual_selected_files(checkout, raw_declared)
            if actual_files != selected_files:
                raise ValueError("every referenced preview resource must be Git-tracked")
            directories = {
                Path(*Path(path).parts[:index])
                for path in selected_files
                for index in range(1, len(Path(path).parts))
            }
            paths = tuple(
                sorted(
                    directories | {Path(path) for path in selected_files},
                    key=lambda path: (len(path.parts), path.as_posix()),
                )
            )
            baseline = {
                path: _identity((checkout / path).lstat()) for path in paths
            }
            self._root_fd = os.open(
                checkout, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            self._root_identity = _identity(os.fstat(self._root_fd))
            self._head = _PinnedAbsoluteFile(
                _git_internal_path(checkout, "HEAD"), "Git HEAD identity"
            )
            self._index = _PinnedAbsoluteFile(
                _git_internal_path(checkout, "index"), "Git index identity"
            )
            for path in paths:
                git_mode = tracked[path.as_posix()][0] if path.as_posix() in tracked else None
                self._entries.append(
                    _PinnedPreviewEntry(
                        self._root_fd, path, baseline[path], git_mode
                    )
                )
            self.entries = tuple(entry.captured for entry in self._entries)
            self.verify()
        except BaseException:
            self.close()
            raise

    def verify(self) -> None:
        if (
            _identity(os.fstat(self._root_fd)) != self._root_identity
            or _identity(os.stat(self.checkout, follow_symlinks=False))
            != self._root_identity
        ):
            raise RuntimeError("selected source checkout changed")
        if self._head is None or self._index is None:
            raise RuntimeError("selected source authority is closed")
        self._head.verify()
        self._index.verify()
        if _git(("rev-parse", "--verify", "HEAD^{commit}"), self.checkout) != self.expected_head:
            raise RuntimeError("selected Git HEAD changed")
        if _tracked_entries(self.checkout) != self._tracked:
            raise RuntimeError("selected Git index changed")
        for entry in self._entries:
            entry.verify()

    def __enter__(self) -> _PreviewAuthority:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        for entry in reversed(self._entries):
            entry.close()
        self._entries.clear()
        if self._index is not None:
            self._index.close()
            self._index = None
        if self._head is not None:
            self._head.close()
            self._head = None
        if self._root_fd >= 0:
            with suppress(OSError):
                os.close(self._root_fd)
            self._root_fd = -1


def _capture_tracked_closure(
    checkout: Path, declared: tuple[Path, ...], *, expected_head: str
) -> _PreviewAuthority:
    return _PreviewAuthority(checkout, declared, expected_head)


def _actual_selected_files(checkout: Path, raw_declared: tuple[str, ...]) -> set[str]:
    actual_files: set[str] = set()
    for root in raw_declared:
        candidate = checkout / root
        try:
            root_stat = candidate.lstat()
        except FileNotFoundError:
            raise ValueError("every referenced preview resource must be Git-tracked") from None
        if stat.S_ISLNK(root_stat.st_mode):
            raise ValueError("selected source closure must not contain a symbolic link")
        if stat.S_ISREG(root_stat.st_mode):
            actual_files.add(root)
            continue
        if not stat.S_ISDIR(root_stat.st_mode):
            raise ValueError("selected source closure contains an unsupported file type")
        for current, directories, files in os.walk(candidate, followlinks=False):
            current_path = Path(current)
            for name in (*directories, *files):
                item = current_path / name
                relative = item.relative_to(checkout).as_posix()
                observed = item.lstat()
                if stat.S_ISLNK(observed.st_mode):
                    raise ValueError(
                        "selected source closure must not contain a symbolic link"
                    )
                if stat.S_ISREG(observed.st_mode):
                    actual_files.add(relative)
                elif not stat.S_ISDIR(observed.st_mode):
                    raise ValueError(
                        "selected source closure contains an unsupported file type"
                    )
    return actual_files


def _git_internal_path(checkout: Path, name: str) -> Path:
    raw = _git(("rev-parse", "--git-path", name), checkout)
    path = Path(raw)
    if not path.is_absolute():
        path = checkout / path
    return Path(os.path.abspath(path))


def _read_descriptor(descriptor: int) -> bytes:
    offset = 0
    chunks: list[bytes] = []
    while chunk := os.pread(descriptor, 64 * 1024, offset):
        chunks.append(chunk)
        offset += len(chunk)
    return b"".join(chunks)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)


def _structural_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode)


def _closure_fingerprint(entries: tuple[_CapturedEntry, ...]) -> str:
    fingerprint = hashlib.sha256()
    for entry in entries:
        metadata = json.dumps(
            {
                "kind": entry.kind,
                "mode": entry.mode,
                "path": entry.path.as_posix(),
                "size": len(entry.content or b""),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        fingerprint.update(len(metadata).to_bytes(8, "big"))
        fingerprint.update(metadata)
        if entry.content is not None:
            fingerprint.update(entry.content)
    return fingerprint.hexdigest()


def _provider_entries(
    captured: tuple[_CapturedEntry, ...], declared: tuple[Path, ...]
) -> tuple[_CapturedEntry, ...]:
    roots = tuple(path.as_posix() for path in declared)
    files = {
        entry.path
        for entry in captured
        if entry.kind == "file"
        and any(
            entry.path.as_posix() == root
            or entry.path.as_posix().startswith(f"{root}/")
            for root in roots
        )
    }
    included = files | {
        parent
        for path in files
        for parent in path.parents
        if parent != Path(".")
    }
    return tuple(entry for entry in captured if entry.path in included)


def _materialize(root: Path, entries: tuple[_CapturedEntry, ...]) -> None:
    for entry in entries:
        destination = root / entry.path
        if entry.kind == "directory":
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(entry.content or b"")
        destination.chmod(entry.mode)


def _verify_materialized(root: Path, entries: tuple[_CapturedEntry, ...]) -> None:
    expected = {entry.path: entry for entry in entries}
    observed: set[Path] = set()
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative_root = current_path.relative_to(root)
        for name in (*directories, *files):
            path = current_path / name
            relative = relative_root / name
            observed.add(relative)
            reference = expected.get(relative)
            item = path.lstat()
            if reference is None or stat.S_IMODE(item.st_mode) != reference.mode:
                raise RuntimeError("provider changed the restricted preview snapshot")
            if reference.kind == "directory" and not stat.S_ISDIR(item.st_mode):
                raise RuntimeError("provider changed the restricted preview snapshot")
            if reference.kind == "file" and (
                not stat.S_ISREG(item.st_mode)
                or path.read_bytes() != reference.content
            ):
                raise RuntimeError("provider changed the restricted preview snapshot")
    if observed != set(expected):
        raise RuntimeError("provider changed the restricted preview snapshot")


def _validate_plan(
    plan: ProviderPlan,
    provider: DeploymentProvider,
    target: TargetSpec,
    fingerprint: str,
) -> None:
    if type(plan) is not ProviderPlan:
        raise ValueError("provider plan must be an exact ProviderPlan")
    if plan.provider_id != provider.provider_id:
        raise ValueError("provider plan id does not match provider")
    if plan.target != target:
        raise ValueError("provider plan target does not match selected target")
    if plan.source_revision != fingerprint:
        raise ValueError("provider plan source revision does not match preview fingerprint")
    if not plan.files and not plan.removals:
        raise ValueError("provider plan must contain files or removals")


def _reject_overlapping_roots(checkout: Path, target_home: Path) -> None:
    target = target_home.expanduser().absolute()
    try:
        target = target.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ValueError("preview target home could not be resolved") from error
    if checkout == target or checkout.is_relative_to(target) or target.is_relative_to(checkout):
        raise ValueError("source checkout and preview target home must not overlap")


__all__ = ["PreviewEngine", "PreviewResult"]
