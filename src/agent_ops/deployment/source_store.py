"""Confined Git source mirrors and immutable deployment snapshots."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from agent_ops.deployment.models import RewriteAcceptance, SourceSnapshot, SourceSpec

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]


_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}\Z")
_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\\\[]")
_DRIVE_PATH = re.compile(r"[A-Za-z]:")
_REF_KEYS = frozenset({"source_id", "ref", "commit"})
_SNAPSHOT_KEYS = frozenset({"source_id", "ref", "commit"})


class _GitFailure(RuntimeError):
    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str) -> None:
        detail = stderr.strip() or f"Git exited with status {returncode}"
        super().__init__(detail)
        self.args_run = args
        self.returncode = returncode
        self.stderr = stderr


def _git_environment() -> dict[str, str]:
    """Build the deliberately small, non-interactive Git environment."""
    environment: dict[str, str] = {}
    for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE"):
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    for name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH", "GIT_SSH_COMMAND"):
        value = os.environ.get(name)
        if value is not None:
            environment[name] = value
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def _run_git(
    args: tuple[str, ...],
    *,
    cwd: Path | None = None,
    accepted_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        (
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "credential.interactive=false",
            *args,
        ),
        cwd=cwd,
        env=_git_environment(),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode not in accepted_returncodes:
        raise _GitFailure(args, completed.returncode, completed.stderr)
    return completed


def _validate_source_id(source_id: str) -> None:
    if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
        raise ValueError("source id must be a safe filesystem component")
    if source_id in {".", ".."}:
        raise ValueError("source id must be a safe filesystem component")


def _validate_ref(ref: str) -> None:
    invalid = (
        not isinstance(ref, str)
        or not ref.startswith("refs/")
        or ref.startswith("refs/agentops/")
        or ref.endswith(("/", ".", ".lock"))
        or "//" in ref
        or ".." in ref
        or "@{" in ref
        or _REF_FORBIDDEN.search(ref) is not None
    )
    if not invalid:
        parts = ref.split("/")
        invalid = any(
            not part or part.startswith(".") or part.endswith(".lock")
            for part in parts
        )
    if invalid:
        raise ValueError("source ref must be a fully qualified Git ref")


def _normalize_commit(commit: str) -> str:
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise ValueError("source commit must be a full 40-hex object id")
    return commit.lower()


def _require_directory(path: Path, label: str, *, create: bool = False) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        if not create:
            raise RuntimeError(f"{label} is missing") from None
        with suppress(FileExistsError):
            path.mkdir()
        mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
        raise RuntimeError(f"{label} must be a non-symlink directory")


def _ensure_store_root(state_root: Path) -> Path:
    if state_root.exists() or state_root.is_symlink():
        _require_directory(state_root, "source store root")
    else:
        state_root.mkdir(parents=True, exist_ok=True)
        _require_directory(state_root, "source store root")
    sources = state_root / "sources"
    _require_directory(sources, "source store sources directory", create=True)
    return sources


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path, *, label: str, keys: frozenset[str]) -> dict[str, str]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        raise RuntimeError(f"{label} is missing") from None
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise RuntimeError(f"{label} must be a regular file")
    try:
        value = json.loads(path.read_text(), object_pairs_hook=_strict_object)
    except RuntimeError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict) or set(value) != keys:
        raise RuntimeError(f"invalid {label}: unexpected keys")
    if any(not isinstance(item, str) for item in value.values()):
        raise RuntimeError(f"invalid {label}: values must be strings")
    return value


def _canonical_json(value: dict[str, str]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    ).encode()


def _write_json_atomic(path: Path, value: dict[str, str]) -> None:
    if path.exists() or path.is_symlink():
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            raise RuntimeError(f"metadata path is not a regular file: {path.name}")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_json(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _canonical_local_url(url: str) -> Path | None:
    parsed = urlparse(url)
    if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
        return Path(unquote(parsed.path)).resolve(strict=True)
    if parsed.scheme or (":" in url and not _DRIVE_PATH.match(url)):
        return None
    candidate = Path(url).expanduser()
    try:
        return candidate.resolve(strict=True)
    except OSError:
        return None


def _urls_equivalent(expected: str, observed: str) -> bool:
    if expected == observed:
        return True
    expected_local = _canonical_local_url(expected)
    observed_local = _canonical_local_url(observed)
    return (
        expected_local is not None
        and observed_local is not None
        and expected_local == observed_local
    )


def _remove_owned_tree(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        path.unlink()
        return
    shutil.rmtree(path)


def _verify_data_tree(directory_fd: int, label: str) -> None:
    for name in os.listdir(directory_fd):
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(observed.st_mode):
            raise RuntimeError(f"provider data directory contains a symlink: {label}")
        is_directory = stat.S_ISDIR(observed.st_mode)
        if not (is_directory or stat.S_ISREG(observed.st_mode)):
            raise RuntimeError(
                f"provider data directory contains a nonregular entry: {label}"
            )
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        if is_directory:
            flags |= os.O_DIRECTORY
        try:
            child_fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as error:
            if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise RuntimeError(
                    f"provider data directory contains a symlink: {label}"
                ) from error
            raise
        try:
            pinned = os.fstat(child_fd)
            if (pinned.st_dev, pinned.st_ino) != (observed.st_dev, observed.st_ino):
                raise RuntimeError(f"provider data entry changed during validation: {label}")
            if is_directory:
                _verify_data_tree(child_fd, f"{label}/{name}")
        finally:
            os.close(child_fd)


class SourceStore:
    """Manage per-source bare mirrors and commit-addressed snapshots."""

    def __init__(self, state_root: Path):
        self._state_root = Path(state_root)

    def fetch(
        self,
        source: SourceSpec,
        ref: str,
        *,
        rewrite: RewriteAcceptance | None = None,
    ) -> SourceSnapshot:
        _validate_source_id(source.id)
        _validate_ref(source.stable_ref)
        _validate_ref(ref)
        if rewrite is not None:
            old_commit = _normalize_commit(rewrite.old_commit)
            new_commit = _normalize_commit(rewrite.new_commit)
            if rewrite.old_commit != old_commit or rewrite.new_commit != new_commit:
                raise ValueError(
                    "rewrite acceptance commits must be exact lowercase object ids"
                )
        if fcntl is None:
            raise RuntimeError("source store locking is unsupported on this platform")

        sources = _ensure_store_root(self._state_root)
        with self._source_lock(sources, source.id):
            source_root = sources / source.id
            _require_directory(source_root, "source directory", create=True)
            mirror = self._prepare_mirror(source_root, source.url)
            previous = self._read_ref_state(source_root, source.id, ref)
            candidate = f"refs/agentops/candidate/{uuid.uuid4().hex}"
            try:
                commit = self._fetch_candidate(mirror, ref, candidate)
                self._check_refresh(
                    mirror,
                    source,
                    ref,
                    previous,
                    commit,
                    rewrite,
                )
                snapshot = self._ensure_snapshot(source_root, source.id, ref, commit)
                _run_git(
                    ("--git-dir", str(mirror), "update-ref", "refs/agentops/fetched", commit)
                )
                self._write_ref_state(source_root, source.id, ref, commit)
                return snapshot
            finally:
                with suppress(_GitFailure):
                    _run_git(
                        (
                            "--git-dir",
                            str(mirror),
                            "update-ref",
                            "-d",
                            candidate,
                        )
                    )

    def snapshot(self, source_id: str, commit: str) -> SourceSnapshot:
        _validate_source_id(source_id)
        normalized = _normalize_commit(commit)
        _require_directory(self._state_root, "source store root")
        sources = self._state_root / "sources"
        _require_directory(sources, "source store sources directory")
        source_root = sources / source_id
        _require_directory(source_root, "source directory")
        return self._load_snapshot(source_root, source_id, normalized)

    @contextmanager
    def _source_lock(self, sources: Path, source_id: str) -> Iterator[None]:
        locks = sources / ".locks"
        _require_directory(locks, "source lock directory", create=True)
        lock_path = locks / f"{source_id}.lock"
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            mode = os.fstat(descriptor).st_mode
            if not stat.S_ISREG(mode):
                raise RuntimeError("source lock must be a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _prepare_mirror(self, source_root: Path, url: str) -> Path:
        mirror = source_root / "mirror.git"
        if mirror.exists() or mirror.is_symlink():
            _require_directory(mirror, "source mirror")
            try:
                observed = _run_git(
                    ("--git-dir", str(mirror), "config", "--get", "remote.origin.url")
                ).stdout.strip()
            except _GitFailure as error:
                raise RuntimeError("source mirror has no readable origin URL") from error
            if not _urls_equivalent(url, observed):
                raise RuntimeError("source mirror origin URL differs from SourceSpec.url")
            return mirror

        staging_parent = Path(tempfile.mkdtemp(prefix=".tmp-mirror-", dir=source_root))
        staged_mirror = staging_parent / "mirror.git"
        try:
            _run_git(("init", "--bare", str(staged_mirror)))
            _run_git(
                (
                    "--git-dir",
                    str(staged_mirror),
                    "config",
                    "remote.origin.url",
                    url,
                )
            )
            os.replace(staged_mirror, mirror)
        finally:
            _remove_owned_tree(staging_parent)
        return mirror

    def _fetch_candidate(self, mirror: Path, ref: str, candidate: str) -> str:
        try:
            _run_git(
                (
                    "--git-dir",
                    str(mirror),
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "origin",
                    f"+{ref}:{candidate}",
                )
            )
        except _GitFailure as error:
            raise RuntimeError(f"requested Git ref {ref!r} was not found or fetched") from error
        try:
            commit = _run_git(
                (
                    "--git-dir",
                    str(mirror),
                    "rev-parse",
                    "--verify",
                    f"{candidate}^{{commit}}",
                )
            ).stdout.strip()
        except _GitFailure as error:
            raise RuntimeError(f"requested Git ref {ref!r} did not resolve to a commit") from error
        return _normalize_commit(commit)

    def _check_refresh(
        self,
        mirror: Path,
        source: SourceSpec,
        ref: str,
        previous: str | None,
        commit: str,
        rewrite: RewriteAcceptance | None,
    ) -> None:
        if previous is None or previous == commit:
            return
        ancestor = _run_git(
            ("--git-dir", str(mirror), "merge-base", "--is-ancestor", previous, commit),
            accepted_returncodes=frozenset({0, 1}),
        )
        if ancestor.returncode == 0:
            return
        if ref == source.stable_ref:
            raise RuntimeError("stable ref rejected a non-fast-forward rewrite")
        accepted = (
            rewrite is not None
            and rewrite.old_commit.lower() == previous
            and rewrite.new_commit.lower() == commit
        )
        if not accepted:
            raise RuntimeError(
                "development ref rewrite acceptance must exactly match the prior and new commits"
            )

    def _ref_path(self, source_root: Path, ref: str) -> Path:
        key = hashlib.sha256(ref.encode()).hexdigest()
        return source_root / "refs" / f"{key}.json"

    def _read_ref_state(
        self, source_root: Path, source_id: str, ref: str
    ) -> str | None:
        refs = source_root / "refs"
        _require_directory(refs, "source ref state directory", create=True)
        path = self._ref_path(source_root, ref)
        if not path.exists() and not path.is_symlink():
            return None
        value = _read_json(path, label="source ref metadata", keys=_REF_KEYS)
        if value["source_id"] != source_id or value["ref"] != ref:
            raise RuntimeError("invalid source ref metadata: identity mismatch")
        try:
            return _normalize_commit(value["commit"])
        except ValueError as error:
            raise RuntimeError("invalid source ref metadata: invalid commit") from error

    def _write_ref_state(
        self, source_root: Path, source_id: str, ref: str, commit: str
    ) -> None:
        _write_json_atomic(
            self._ref_path(source_root, ref),
            {"source_id": source_id, "ref": ref, "commit": commit},
        )

    def _ensure_snapshot(
        self, source_root: Path, source_id: str, ref: str, commit: str
    ) -> SourceSnapshot:
        snapshots = source_root / "snapshots"
        metadata_root = source_root / "snapshot-metadata"
        _require_directory(snapshots, "snapshot directory", create=True)
        _require_directory(metadata_root, "snapshot metadata directory", create=True)
        destination = snapshots / commit
        metadata = metadata_root / f"{commit}.json"
        if (
            destination.exists()
            or destination.is_symlink()
            or metadata.exists()
            or metadata.is_symlink()
        ):
            existing = self._load_snapshot(source_root, source_id, commit)
            return SourceSnapshot(source_id, ref, commit, existing.root)

        staging = Path(tempfile.mkdtemp(prefix=".tmp-snapshot-", dir=source_root))
        staged_snapshot = staging / "snapshot"
        promoted = False
        try:
            _run_git(("init", str(staged_snapshot)))
            _run_git(
                (
                    "-c",
                    "protocol.file.allow=always",
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    str(source_root / "mirror.git"),
                    commit,
                ),
                cwd=staged_snapshot,
            )
            _run_git(("checkout", "--detach", commit), cwd=staged_snapshot)
            self._verify_checkout(staged_snapshot, commit)
            os.replace(staged_snapshot, destination)
            promoted = True
            try:
                _write_json_atomic(
                    metadata,
                    {"source_id": source_id, "ref": ref, "commit": commit},
                )
            except BaseException:
                _remove_owned_tree(destination)
                promoted = False
                raise
        finally:
            _remove_owned_tree(staging)
        if not promoted:  # pragma: no cover - defensive invariant
            raise RuntimeError("snapshot promotion did not complete")
        return self._load_snapshot(source_root, source_id, commit)

    def _verify_checkout(self, root: Path, commit: str) -> None:
        _require_directory(root / ".git", "snapshot Git directory")
        observed = _run_git(
            ("rev-parse", "--verify", "HEAD^{commit}"), cwd=root
        ).stdout.strip().lower()
        if observed != commit:
            raise RuntimeError("snapshot HEAD does not match requested commit")
        symbolic = _run_git(
            ("symbolic-ref", "-q", "HEAD"),
            cwd=root,
            accepted_returncodes=frozenset({0, 1}),
        )
        if symbolic.returncode == 0:
            raise RuntimeError("snapshot HEAD is not detached")
        dirty = _run_git(
            ("status", "--porcelain", "--untracked-files=no"), cwd=root
        ).stdout
        if dirty:
            raise RuntimeError("snapshot tracked tree is not clean")

    def _load_snapshot(
        self, source_root: Path, source_id: str, commit: str
    ) -> SourceSnapshot:
        root = source_root / "snapshots" / commit
        _require_directory(root, "snapshot directory")
        metadata = _read_json(
            source_root / "snapshot-metadata" / f"{commit}.json",
            label="snapshot metadata",
            keys=_SNAPSHOT_KEYS,
        )
        if metadata["source_id"] != source_id or metadata["commit"] != commit:
            raise RuntimeError("invalid snapshot metadata: identity mismatch")
        _validate_ref(metadata["ref"])
        self._verify_checkout(root, commit)
        return SourceSnapshot(source_id, metadata["ref"], commit, root)


def _validate_provider_data_closure(
    snapshot: SourceSnapshot, declared: tuple[Path, ...]
) -> tuple[Path, ...]:
    """Confine declared inert provider data beneath a verified snapshot root."""
    _validate_source_id(snapshot.source_id)
    _validate_ref(snapshot.ref)
    _normalize_commit(snapshot.commit)
    if not isinstance(declared, tuple):
        raise ValueError("provider data closure must be a tuple of paths")
    if fcntl is None or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("provider data closure validation is unsupported")

    root = Path(snapshot.root)
    _require_directory(root, "snapshot root")
    try:
        if root.resolve(strict=True) != root.absolute():
            raise RuntimeError("snapshot root contains a symlinked path component")
    except OSError as error:
        raise RuntimeError("snapshot root cannot be resolved safely") from error
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    validated: list[Path] = []
    seen: set[str] = set()
    try:
        for path in declared:
            if not isinstance(path, Path):
                raise ValueError("provider data closure entries must be Path values")
            raw = str(path)
            parts = path.parts
            invalid = (
                raw in {"", "."}
                or path.is_absolute()
                or "\\" in raw
                or "//" in raw
                or _DRIVE_PATH.match(raw) is not None
                or any(part in {"", ".", ".."} for part in parts)
                or path.as_posix() != raw
                or raw in seen
            )
            if invalid:
                raise ValueError("provider data path must be exact and repository-relative")
            seen.add(raw)
            current_fd = os.dup(root_fd)
            try:
                for index, part in enumerate(parts):
                    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
                    if index < len(parts) - 1:
                        flags |= os.O_DIRECTORY
                    try:
                        next_fd = os.open(part, flags, dir_fd=current_fd)
                    except FileNotFoundError:
                        raise RuntimeError(f"provider data path is missing: {raw}") from None
                    except OSError as error:
                        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                            raise RuntimeError(
                                f"provider data path contains a symlink or non-directory: {raw}"
                            ) from error
                        raise
                    os.close(current_fd)
                    current_fd = next_fd
                mode = os.fstat(current_fd).st_mode
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise RuntimeError(
                        f"provider data path is not a regular file or directory: {raw}"
                    )
                if stat.S_ISDIR(mode):
                    _verify_data_tree(current_fd, raw)
            finally:
                os.close(current_fd)
            validated.append(root / path)
    finally:
        os.close(root_fd)
    return tuple(validated)
