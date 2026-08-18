"""Confined Git source mirrors and immutable deployment snapshots."""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit
from urllib.request import url2pathname

from agent_ops.deployment.models import RewriteAcceptance, SourceSnapshot, SourceSpec

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX hosts fail closed
    fcntl = None  # type: ignore[assignment]

_WINDOWS_SUPPORTED = os.name == "nt"


def _windows_source_store_backend() -> Any:
    from agent_ops.deployment import windows_source_store

    return windows_source_store


_SOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}\Z")
_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f~^:?*\\\[]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DRIVE_PATH = re.compile(r"[A-Za-z]:")
_REMOTE_HELPER = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*::")
_SCP_URL = re.compile(
    r"(?:(?:[A-Za-z0-9._-]+)@)?"
    r"(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):"
    r"[^:\s]+\Z"
)
_REF_KEYS = frozenset({"source_id", "ref", "commit"})
_SNAPSHOT_KEYS = frozenset({"source_id", "ref", "commit"})
_SNAPSHOT_METADATA = "agentops-snapshot.json"
_DEFAULT_GIT_TIMEOUT = 30.0


class _GitFailure(RuntimeError):
    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str) -> None:
        detail = stderr.strip() or f"Git exited with status {returncode}"
        super().__init__(detail)
        self.args_run = args
        self.returncode = returncode
        self.stderr = stderr


class _GitTimeout(RuntimeError):
    pass


class _MissingSnapshotMetadata(RuntimeError):
    pass


def _git_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE"):
        if (value := os.environ.get(name)) is not None:
            environment[name] = value
    for name in ("SSH_AUTH_SOCK", "SSH_AGENT_PID", "GIT_SSH", "GIT_SSH_COMMAND"):
        if (value := os.environ.get(name)) is not None:
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


def _terminate_process_group(
    process: subprocess.Popen[str], process_group: int, *, grace: float = 0.5
) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGTERM)
    with suppress(BaseException):
        process.communicate(timeout=grace)
    with suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGKILL)
    with suppress(BaseException):
        process.communicate(timeout=grace)
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            with suppress(OSError):
                pipe.close()
    with suppress(BaseException):
        process.wait(timeout=grace)


def _run_git(
    args: tuple[str, ...],
    *,
    cwd: Path | None = None,
    accepted_returncodes: frozenset[int] = frozenset({0}),
    timeout: float = _DEFAULT_GIT_TIMEOUT,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = (
        "git",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "credential.interactive=false",
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "protocol.file.allow=always",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "protocol.ssh.allow=always",
        *args,
    )
    git_environment = _git_environment()
    if environment is not None:
        git_environment.update(environment)
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=git_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    process_group = process.pid
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        with suppress(BaseException):
            _terminate_process_group(process, process_group)
        raise _GitTimeout(f"Git command timed out after {timeout:g} seconds") from error
    except (KeyboardInterrupt, SystemExit):
        with suppress(BaseException):
            _terminate_process_group(process, process_group)
        raise
    completed = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
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
        invalid = any(
            not part or part.startswith(".") or part.endswith(".lock") for part in ref.split("/")
        )
    if invalid:
        raise ValueError("source ref must be a fully qualified Git ref")


def _normalize_commit(commit: str) -> str:
    if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
        raise ValueError("source commit must be a full 40-hex object id")
    return commit.lower()


def _normalize_source_url(url: str) -> str:
    if not isinstance(url, str) or not url or _CONTROL.search(url):
        raise ValueError("source URL must be a supported safe Git location")
    if url.startswith("-") or _REMOTE_HELPER.match(url):
        raise ValueError("source URL must be a supported safe Git location")
    if "://" not in url and not _DRIVE_PATH.match(url) and _SCP_URL.fullmatch(url):
        if url.rsplit(":", 1)[1].startswith("-"):
            raise ValueError("source URL contains an option-like remote path")
        return url
    parsed = urlsplit(url)
    if parsed.scheme:
        scheme = parsed.scheme.lower()
        if scheme == "file":
            if parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
                raise ValueError("source URL must be a safe local file URL")
            try:
                local = Path(url2pathname(parsed.path)).resolve(strict=True)
            except OSError as error:
                raise ValueError("source URL local path does not exist") from error
            if not local.is_dir():
                raise ValueError("source URL local path must be a directory")
            return local.as_uri()
        if scheme not in {"https", "ssh"}:
            raise ValueError("source URL uses an unsupported Git protocol")
        if not parsed.netloc or not parsed.path or parsed.query or parsed.fragment:
            raise ValueError("source URL is malformed")
        return url
    if ":" in url and not _DRIVE_PATH.match(url):
        raise ValueError("source URL is malformed")
    try:
        local = Path(url).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("source URL local path does not exist") from error
    if not local.is_dir():
        raise ValueError("source URL local path must be a directory")
    return str(local)


def _canonical_local_url(url: str) -> Path | None:
    parsed = urlsplit(url)
    try:
        if parsed.scheme == "file" and parsed.netloc in {"", "localhost"}:
            return Path(url2pathname(parsed.path)).resolve(strict=True)
        if not parsed.scheme and not _SCP_URL.fullmatch(url):
            return Path(url).resolve(strict=True)
    except OSError:
        return None
    return None


def _persisted_remote_url_and_transient_auth(url: str) -> tuple[str, dict[str, str]]:
    """Keep HTTPS userinfo out of storage and process arguments."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or parsed.username is None:
        return url, {}
    host = parsed.hostname
    if host is None:
        raise ValueError("source URL is malformed")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    persisted = urlunsplit(("https", host, parsed.path, "", ""))
    credentials = base64.b64encode(
        f"{unquote(parsed.username)}:{unquote(parsed.password or '')}".encode()
    ).decode("ascii")
    return persisted, {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{persisted}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
    }


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


def _require_owner_only_directory(path: Path, label: str, *, create: bool = False) -> None:
    try:
        item = path.lstat()
    except FileNotFoundError:
        if not create:
            raise RuntimeError(f"{label} is missing") from None
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        item = path.lstat()
    if (
        not stat.S_ISDIR(item.st_mode)
        or stat.S_ISLNK(item.st_mode)
        or item.st_uid != os.geteuid()
        or stat.S_IMODE(item.st_mode) != 0o700
    ):
        raise RuntimeError(f"{label} must be an owner-only 0o700 directory")


def _ensure_store_root(state_root: Path) -> Path:
    _require_owner_only_directory(state_root, "source store root", create=True)
    sources = state_root / "sources"
    _require_owner_only_directory(sources, "source store sources directory", create=True)
    return sources


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_fd_bytes(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 64 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns)


def _read_json_at(
    directory_fd: int,
    name: str,
    *,
    label: str,
    keys: frozenset[str],
    missing_type: type[RuntimeError] = RuntimeError,
) -> dict[str, str]:
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        raise missing_type(f"{label} is missing") from None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RuntimeError(f"{label} must be a regular file") from error
        raise
    try:
        pinned = os.fstat(descriptor)
        if not stat.S_ISREG(pinned.st_mode) or _identity(before) != _identity(pinned):
            raise RuntimeError(f"{label} must be a pinned regular file")
        raw = _read_fd_bytes(descriptor)
        after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _identity(after) != _identity(pinned):
            raise RuntimeError(f"{label} changed during read")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode(), object_pairs_hook=_strict_object)
    except RuntimeError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict) or set(value) != keys:
        raise RuntimeError(f"invalid {label}: unexpected keys")
    if any(not isinstance(item, str) for item in value.values()):
        raise RuntimeError(f"invalid {label}: values must be strings")
    return value


def _canonical_json(value: dict[str, str]) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def _write_json_at(directory_fd: int, name: str, value: dict[str, str]) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        _write_all(descriptor, _canonical_json(value))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(directory_fd)


def _write_json_atomic(directory: Path, name: str, value: dict[str, str]) -> None:
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".tmp-{uuid.uuid4().hex}"
    try:
        _write_json_at(directory_fd, temporary, value)
        try:
            existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(existing.st_mode):
                raise RuntimeError(f"metadata path is not a regular file: {name}")
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)


def _remove_owned_tree(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        path.unlink()
        return
    shutil.rmtree(path)


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for current, child_directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories.append(current_path)
        for name in files:
            path = current_path / name
            mode = path.lstat().st_mode
            if stat.S_ISREG(mode):
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        child_directories[:] = [
            name for name in child_directories if not (current_path / name).is_symlink()
        ]
    for path in reversed(directories):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _blob_oid(data: bytes, algorithm: str) -> str:
    payload = f"blob {len(data)}\0".encode() + data
    return hashlib.new(algorithm, payload).hexdigest()


@dataclass(frozen=True)
class _HeadEntry:
    mode: str
    object_type: str
    object_id: str


def _head_entries(root: Path, *, timeout: float) -> dict[str, _HeadEntry]:
    listing = _run_git(("ls-tree", "-rz", "--full-tree", "HEAD"), cwd=root, timeout=timeout).stdout
    result: dict[str, _HeadEntry] = {}
    for record in listing.split("\0"):
        if not record:
            continue
        descriptor, path = record.split("\t", 1)
        mode, object_type, object_id = descriptor.split(" ", 2)
        result[path] = _HeadEntry(mode, object_type, object_id)
    return result


def _read_regular_at(directory_fd: int, name: str, observed: os.stat_result) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        pinned = os.fstat(descriptor)
        if _identity(pinned) != _identity(observed):
            raise RuntimeError("snapshot worktree changed during verification")
        data = _read_fd_bytes(descriptor)
        if _identity(os.fstat(descriptor)) != _identity(pinned):
            raise RuntimeError("snapshot worktree changed during verification")
        return data
    finally:
        os.close(descriptor)


def _worktree_entries(
    directory_fd: int,
    *,
    prefix: str = "",
    root: bool = False,
) -> tuple[dict[str, tuple[str, int, bytes]], set[str]]:
    entries: dict[str, tuple[str, int, bytes]] = {}
    directories: set[str] = set()
    for name in os.listdir(directory_fd):
        if root and name == ".git":
            continue
        path = f"{prefix}/{name}" if prefix else name
        observed = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(observed.st_mode):
            child_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                children, child_directories = _worktree_entries(child_fd, prefix=path)
            finally:
                os.close(child_fd)
            entries.update(children)
            directories.add(path)
            directories.update(child_directories)
        elif stat.S_ISREG(observed.st_mode):
            entries[path] = (
                "blob",
                stat.S_IMODE(observed.st_mode),
                _read_regular_at(directory_fd, name, observed),
            )
        elif stat.S_ISLNK(observed.st_mode):
            target = os.readlink(name, dir_fd=directory_fd).encode()
            entries[path] = ("symlink", 0o777, target)
        else:
            raise RuntimeError(f"snapshot worktree contains unsupported type: {path}")
    return entries, directories


def _verify_exact_checkout(root: Path, commit: str, *, timeout: float) -> dict[str, _HeadEntry]:
    _require_directory(root / ".git", "snapshot Git directory")
    observed = (
        _run_git(("rev-parse", "--verify", "HEAD^{commit}"), cwd=root, timeout=timeout)
        .stdout.strip()
        .lower()
    )
    if observed != commit:
        raise RuntimeError("snapshot HEAD does not match requested commit")
    symbolic = _run_git(
        ("symbolic-ref", "-q", "HEAD"),
        cwd=root,
        accepted_returncodes=frozenset({0, 1}),
        timeout=timeout,
    )
    if symbolic.returncode == 0:
        raise RuntimeError("snapshot HEAD is not detached")
    staged = _run_git(
        ("diff-index", "--cached", "--quiet", "HEAD", "--"),
        cwd=root,
        accepted_returncodes=frozenset({0, 1}),
        timeout=timeout,
    )
    if staged.returncode:
        raise RuntimeError("snapshot worktree index differs from HEAD")
    flags = _run_git(("ls-files", "-v", "-z"), cwd=root, timeout=timeout).stdout
    if any(record and not record.startswith("H ") for record in flags.split("\0")):
        raise RuntimeError("snapshot worktree index contains mutable flags")

    expected = _head_entries(root, timeout=timeout)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        actual, directories = _worktree_entries(root_fd, root=True)
    finally:
        os.close(root_fd)
    if set(actual) != set(expected):
        raise RuntimeError("snapshot worktree paths do not exactly match HEAD")
    if any(
        not any(path.startswith(f"{directory}/") for path in expected) for directory in directories
    ):
        raise RuntimeError("snapshot worktree contains an untracked directory")
    algorithm = _run_git(
        ("rev-parse", "--show-object-format"), cwd=root, timeout=timeout
    ).stdout.strip()
    for path, head in expected.items():
        kind, mode, data = actual[path]
        if head.object_type != "blob":
            raise RuntimeError(f"snapshot worktree has unsupported HEAD entry: {path}")
        expected_kind = "symlink" if head.mode == "120000" else "blob"
        expected_executable = head.mode == "100755"
        actual_executable = bool(mode & 0o111)
        if kind != expected_kind or (
            expected_kind == "blob" and actual_executable != expected_executable
        ):
            raise RuntimeError(f"snapshot worktree type or mode differs from HEAD: {path}")
        if _blob_oid(data, algorithm) != head.object_id:
            raise RuntimeError(f"snapshot worktree bytes differ from HEAD: {path}")
    return expected


class SourceStore:
    """Manage per-source mirrors and commit-addressed snapshots."""

    def __init__(self, state_root: Path, *, git_timeout: float = _DEFAULT_GIT_TIMEOUT):
        if isinstance(git_timeout, bool) or not isinstance(git_timeout, (int, float)):
            raise ValueError("Git timeout must be a finite positive number")
        if not math.isfinite(git_timeout) or git_timeout <= 0:
            raise ValueError("Git timeout must be a finite positive number")
        self._state_root = Path(os.path.abspath(Path(state_root).expanduser()))
        self._git_timeout = float(git_timeout)

    def fetch(
        self,
        source: SourceSpec,
        ref: str,
        *,
        rewrite: RewriteAcceptance | None = None,
    ) -> SourceSnapshot:
        if _WINDOWS_SUPPORTED:
            return _windows_source_store_backend().fetch(
                self,
                source,
                ref,
                rewrite=rewrite,
            )
        _validate_source_id(source.id)
        _validate_ref(source.stable_ref)
        _validate_ref(ref)
        url = _normalize_source_url(source.url)
        if rewrite is not None:
            old_commit = _normalize_commit(rewrite.old_commit)
            new_commit = _normalize_commit(rewrite.new_commit)
            if rewrite.old_commit != old_commit or rewrite.new_commit != new_commit:
                raise ValueError("rewrite acceptance commits must be exact lowercase object ids")
        if fcntl is None:
            raise RuntimeError("source store locking is unsupported on this platform")

        sources = _ensure_store_root(self._state_root)
        with self._source_lock(sources, source.id):
            source_root = sources / source.id
            _require_owner_only_directory(source_root, "source directory", create=True)
            self._cleanup_partial_state(source_root)
            mirror = self._prepare_mirror(source_root, url)
            previous = self._read_ref_state(source_root, source.id, ref)
            accepted_ref = self._accepted_ref(ref)
            accepted_before = self._reconcile_accepted_ref(
                mirror, accepted_ref, previous, ref, source
            )
            candidate = f"refs/agentops/candidate/{uuid.uuid4().hex}"
            try:
                commit = self._fetch_candidate(mirror, ref, candidate, url)
                self._check_refresh(mirror, source, ref, previous, commit, rewrite)
                snapshot = self._ensure_snapshot(source_root, source.id, ref, commit)
                expected_old = accepted_before or ("0" * 40)
                self._git(
                    (
                        "--git-dir",
                        str(mirror),
                        "update-ref",
                        accepted_ref,
                        commit,
                        expected_old,
                    )
                )
                self._write_ref_state(source_root, source.id, ref, commit)
                return snapshot
            finally:
                with suppress(_GitFailure):
                    self._git(("--git-dir", str(mirror), "update-ref", "-d", candidate))

    def snapshot(self, source_id: str, commit: str) -> SourceSnapshot:
        if _WINDOWS_SUPPORTED:
            return _windows_source_store_backend().snapshot(self, source_id, commit)
        _validate_source_id(source_id)
        normalized = _normalize_commit(commit)
        _require_owner_only_directory(self._state_root, "source store root")
        sources = self._state_root / "sources"
        _require_owner_only_directory(sources, "source store sources directory")
        source_root = sources / source_id
        _require_owner_only_directory(source_root, "source directory")
        return self._load_snapshot(source_root, source_id, normalized)

    def _git(
        self,
        args: tuple[str, ...],
        *,
        cwd: Path | None = None,
        accepted_returncodes: frozenset[int] = frozenset({0}),
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return _run_git(
            args,
            cwd=cwd,
            accepted_returncodes=accepted_returncodes,
            timeout=self._git_timeout,
            environment=environment,
        )

    @contextmanager
    def _source_lock(self, sources: Path, source_id: str) -> Iterator[None]:
        locks = sources / ".locks"
        _require_owner_only_directory(locks, "source lock directory", create=True)
        descriptor = os.open(
            locks / f"{source_id}.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("source lock must be a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _cleanup_partial_state(self, source_root: Path) -> None:
        for path in source_root.iterdir():
            if not path.name.startswith(".tmp-"):
                continue
            if path.is_symlink():
                raise RuntimeError("temporary source state must not be a symlink")
            _remove_owned_tree(path)

    def _prepare_mirror(self, source_root: Path, url: str) -> Path:
        persisted_url, _ = _persisted_remote_url_and_transient_auth(url)
        mirror = source_root / "mirror.git"
        if mirror.exists() or mirror.is_symlink():
            _require_directory(mirror, "source mirror")
            try:
                urls = self._git(
                    ("--git-dir", str(mirror), "config", "--get-all", "remote.origin.url")
                ).stdout.splitlines()
            except _GitFailure as error:
                raise RuntimeError("source mirror has no readable origin URL") from error
            if len(urls) != 1 or not _urls_equivalent(persisted_url, urls[0]):
                raise RuntimeError("source mirror origin URL differs from SourceSpec.url")
            return mirror
        staging_parent = Path(tempfile.mkdtemp(prefix=".tmp-mirror-", dir=source_root))
        staged_mirror = staging_parent / "mirror.git"
        try:
            self._git(("init", "--bare", str(staged_mirror)))
            self._git(
                (
                    "--git-dir",
                    str(staged_mirror),
                    "config",
                    "remote.origin.url",
                    persisted_url,
                )
            )
            os.replace(staged_mirror, mirror)
            self._fsync_directory(source_root)
        finally:
            _remove_owned_tree(staging_parent)
        return mirror

    def _fetch_candidate(self, mirror: Path, ref: str, candidate: str, url: str) -> str:
        _, transient_auth = _persisted_remote_url_and_transient_auth(url)
        try:
            self._git(
                (
                    "--git-dir",
                    str(mirror),
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "origin",
                    f"+{ref}:{candidate}",
                ),
                environment=transient_auth,
            )
        except _GitFailure as error:
            raise RuntimeError(f"requested Git ref {ref!r} was not found or fetched") from error
        try:
            commit = self._git(
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

    def _accepted_ref(self, ref: str) -> str:
        return "refs/agentops/accepted/" + hashlib.sha256(ref.encode()).hexdigest()

    def _read_internal_ref(self, mirror: Path, ref: str) -> str | None:
        result = self._git(
            ("--git-dir", str(mirror), "rev-parse", "--verify", f"{ref}^{{commit}}"),
            accepted_returncodes=frozenset({0, 128}),
        )
        return result.stdout.strip().lower() if result.returncode == 0 else None

    def _reconcile_accepted_ref(
        self,
        mirror: Path,
        accepted_ref: str,
        previous: str | None,
        ref: str,
        source: SourceSpec,
    ) -> str | None:
        observed = self._read_internal_ref(mirror, accepted_ref)
        if observed == previous:
            return observed
        if previous is None:
            if observed is not None:
                self._git(("--git-dir", str(mirror), "update-ref", "-d", accepted_ref))
            return None
        try:
            self._git(
                (
                    "--git-dir",
                    str(mirror),
                    "cat-file",
                    "-e",
                    f"{previous}^{{commit}}",
                )
            )
        except _GitFailure as error:
            self._history_error(source, ref, error)
        expected = observed or ("0" * 40)
        self._git(("--git-dir", str(mirror), "update-ref", accepted_ref, previous, expected))
        return previous

    def _history_error(self, source: SourceSpec, ref: str, error: Exception) -> None:
        if ref == source.stable_ref:
            raise RuntimeError("stable ref history is unavailable; refresh is rejected") from error
        raise RuntimeError(
            "development ref history is unavailable; rewrite policy cannot be verified"
        ) from error

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
        try:
            ancestor = self._git(
                (
                    "--git-dir",
                    str(mirror),
                    "merge-base",
                    "--is-ancestor",
                    previous,
                    commit,
                ),
                accepted_returncodes=frozenset({0, 1}),
            )
        except _GitFailure as error:
            self._history_error(source, ref, error)
        if ancestor.returncode == 0:
            return
        if ref == source.stable_ref:
            raise RuntimeError("stable ref rejected a non-fast-forward rewrite")
        if not (
            rewrite is not None and rewrite.old_commit == previous and rewrite.new_commit == commit
        ):
            raise RuntimeError(
                "development ref rewrite acceptance must exactly match the prior and new commits"
            )

    def _ref_path(self, source_root: Path, ref: str) -> tuple[Path, str]:
        return source_root / "refs", f"{hashlib.sha256(ref.encode()).hexdigest()}.json"

    def _read_ref_state(self, source_root: Path, source_id: str, ref: str) -> str | None:
        directory, name = self._ref_path(source_root, ref)
        _require_directory(directory, "source ref state directory", create=True)
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            try:
                value = _read_json_at(
                    directory_fd, name, label="source ref metadata", keys=_REF_KEYS
                )
            except RuntimeError as error:
                if str(error) == "source ref metadata is missing":
                    return None
                raise
        finally:
            os.close(directory_fd)
        if value["source_id"] != source_id or value["ref"] != ref:
            raise RuntimeError("invalid source ref metadata: identity mismatch")
        try:
            normalized = _normalize_commit(value["commit"])
        except ValueError as error:
            raise RuntimeError("invalid source ref metadata: invalid commit") from error
        if normalized != value["commit"]:
            raise RuntimeError("invalid source ref metadata: noncanonical commit")
        return normalized

    def _write_ref_state(self, source_root: Path, source_id: str, ref: str, commit: str) -> None:
        directory, name = self._ref_path(source_root, ref)
        _write_json_atomic(
            directory,
            name,
            {"source_id": source_id, "ref": ref, "commit": commit},
        )

    def _ensure_snapshot(
        self, source_root: Path, source_id: str, ref: str, commit: str
    ) -> SourceSnapshot:
        snapshots = source_root / "snapshots"
        _require_directory(snapshots, "snapshot directory", create=True)
        destination = snapshots / commit
        legacy_root = source_root / "snapshot-metadata"
        if legacy_root.exists() or legacy_root.is_symlink():
            _require_directory(legacy_root, "legacy snapshot metadata directory")
        legacy = legacy_root / f"{commit}.json"
        if destination.exists() or destination.is_symlink():
            try:
                existing = self._load_snapshot(source_root, source_id, commit)
            except _MissingSnapshotMetadata:
                _verify_exact_checkout(destination, commit, timeout=self._git_timeout)
                _remove_owned_tree(destination)
            else:
                # The first materializing ref remains durable provenance; a later
                # fetch returns its requested ref only in the transient result.
                return SourceSnapshot(source_id, ref, commit, existing.root)
        if legacy.exists() or legacy.is_symlink():
            if legacy.is_symlink() or not stat.S_ISREG(legacy.lstat().st_mode):
                raise RuntimeError("legacy snapshot metadata must be a regular file")
            legacy.unlink()
            with suppress(OSError):
                legacy_root.rmdir()

        staging = Path(tempfile.mkdtemp(prefix=".tmp-snapshot-", dir=source_root))
        staged_snapshot = staging / "snapshot"
        try:
            self._git(("init", str(staged_snapshot)))
            self._git(
                (
                    "fetch",
                    "--no-tags",
                    "--no-write-fetch-head",
                    str(source_root / "mirror.git"),
                    commit,
                ),
                cwd=staged_snapshot,
            )
            self._git(("checkout", "--detach", commit), cwd=staged_snapshot)
            _verify_exact_checkout(staged_snapshot, commit, timeout=self._git_timeout)
            git_fd = os.open(
                staged_snapshot / ".git",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                _write_json_at(
                    git_fd,
                    _SNAPSHOT_METADATA,
                    {"source_id": source_id, "ref": ref, "commit": commit},
                )
            finally:
                os.close(git_fd)
            _fsync_tree(staged_snapshot)
            os.replace(staged_snapshot, destination)
            self._fsync_directory(snapshots)
            self._fsync_directory(source_root)
        finally:
            _remove_owned_tree(staging)
        return self._load_snapshot(source_root, source_id, commit)

    def _load_snapshot(self, source_root: Path, source_id: str, commit: str) -> SourceSnapshot:
        root = source_root / "snapshots" / commit
        _require_directory(root, "snapshot directory")
        git_path = root / ".git"
        _require_directory(git_path, "snapshot Git directory")
        git_fd = os.open(git_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            metadata = _read_json_at(
                git_fd,
                _SNAPSHOT_METADATA,
                label="snapshot metadata",
                keys=_SNAPSHOT_KEYS,
                missing_type=_MissingSnapshotMetadata,
            )
        finally:
            os.close(git_fd)
        if metadata["source_id"] != source_id or metadata["commit"] != commit:
            raise RuntimeError("invalid snapshot metadata: identity mismatch")
        _validate_ref(metadata["ref"])
        _verify_exact_checkout(root, commit, timeout=self._git_timeout)
        return SourceSnapshot(source_id, metadata["ref"], commit, root)

    def _fsync_directory(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@dataclass(frozen=True)
class _ClosureEntryInfo:
    relative_path: Path
    kind: str
    mode: int
    size: int


class _PinnedEntry:
    def __init__(
        self,
        closure: _PinnedClosure,
        relative_path: Path,
        descriptor: int,
        chain: tuple[tuple[int, str, tuple[int, int, int, int, int]], ...],
        expected_blob: str | None,
        object_format: str,
    ) -> None:
        self._closure = closure
        self._descriptor = descriptor
        self._chain = chain
        observed = os.fstat(descriptor)
        self._identity = _identity(observed)
        self._expected_blob = expected_blob
        self._object_format = object_format
        self.relative_path = relative_path
        self.kind = "directory" if stat.S_ISDIR(observed.st_mode) else "file"
        self.mode = stat.S_IMODE(observed.st_mode)
        self.size = observed.st_size

    @property
    def info(self) -> _ClosureEntryInfo:
        return _ClosureEntryInfo(self.relative_path, self.kind, self.mode, self.size)

    def _revalidate(self) -> None:
        self._closure._revalidate_root()
        for parent_fd, name, expected in self._chain:
            try:
                observed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise RuntimeError("provider data changed during consumption") from None
            if _identity(observed) != expected:
                raise RuntimeError("provider data changed during consumption")
        if _identity(os.fstat(self._descriptor)) != self._identity:
            raise RuntimeError("provider data changed during consumption")

    def read_bytes(self) -> bytes:
        if self.kind != "file":
            raise RuntimeError("provider data directories cannot be read as bytes")
        self._revalidate()
        data = _read_fd_bytes(self._descriptor)
        self._revalidate()
        if self._expected_blob is None or (
            _blob_oid(data, self._object_format) != self._expected_blob
        ):
            raise RuntimeError("provider data bytes differ from the tracked Git blob")
        return data


class _PinnedClosure:
    def __init__(self, snapshot: SourceSnapshot, declared: tuple[Path, ...]) -> None:
        self._root = Path(snapshot.root)
        self._root_fd = -1
        self._root_identity: tuple[int, int, int, int, int] | None = None
        self._owned_fds: list[int] = []
        self.entries: tuple[_PinnedEntry, ...] = ()
        self.closed = False
        self._open(snapshot, declared)

    def _open(self, snapshot: SourceSnapshot, declared: tuple[Path, ...]) -> None:
        _validate_source_id(snapshot.source_id)
        _validate_ref(snapshot.ref)
        commit = _normalize_commit(snapshot.commit)
        self._root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._root_identity = _identity(os.fstat(self._root_fd))
        try:
            expected = _verify_exact_checkout(self._root, commit, timeout=_DEFAULT_GIT_TIMEOUT)
            if _identity(os.stat(self._root, follow_symlinks=False)) != (self._root_identity):
                raise RuntimeError("snapshot root changed during verification")
            object_format = _run_git(
                ("rev-parse", "--show-object-format"),
                cwd=self._root,
                timeout=_DEFAULT_GIT_TIMEOUT,
            ).stdout.strip()
            raw_paths = [str(path) for path in declared]
            if len(raw_paths) != len(set(raw_paths)):
                raise ValueError("provider data closure contains duplicate paths")
            entries = [self._open_entry(path, expected, object_format) for path in declared]
        except BaseException:
            self.close()
            raise
        self.entries = tuple(entries)

    def _open_entry(
        self,
        path: Path,
        expected: dict[str, _HeadEntry],
        object_format: str,
    ) -> _PinnedEntry:
        if not isinstance(path, Path):
            raise ValueError("provider data closure entries must be Path values")
        raw = str(path)
        if (
            raw in {"", "."}
            or path.is_absolute()
            or "\\" in raw
            or "//" in raw
            or _DRIVE_PATH.match(raw)
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.as_posix() != raw
        ):
            raise ValueError("provider data path must be exact and repository-relative")
        if raw not in expected and not any(item.startswith(f"{raw}/") for item in expected):
            raise RuntimeError(f"provider data path is not tracked at HEAD: {raw}")
        closure_entries = {
            item: head
            for item, head in expected.items()
            if item == raw or item.startswith(f"{raw}/")
        }
        if any(head.mode == "120000" for head in closure_entries.values()):
            raise RuntimeError(f"provider data closure contains a symlink: {raw}")
        parent_fd = os.dup(self._root_fd)
        self._owned_fds.append(parent_fd)
        chain: list[tuple[int, str, tuple[int, int, int, int, int]]] = []
        current_fd = parent_fd
        for index, part in enumerate(path.parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            if index < len(path.parts) - 1 or raw not in expected:
                flags |= os.O_DIRECTORY
            try:
                next_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                raise RuntimeError(f"provider data path is missing: {raw}") from None
            except OSError as error:
                if error.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise RuntimeError(f"provider data path contains a symlink: {raw}") from error
                raise
            self._owned_fds.append(next_fd)
            observed = os.fstat(next_fd)
            chain.append((current_fd, part, _identity(observed)))
            current_fd = next_fd
        expected_blob = expected[raw].object_id if raw in expected else None
        return _PinnedEntry(
            self,
            path,
            current_fd,
            tuple(chain),
            expected_blob,
            object_format,
        )

    def _revalidate_root(self) -> None:
        if self.closed or self._root_identity is None:
            raise RuntimeError("provider data closure is closed")
        observed = os.stat(self._root, follow_symlinks=False)
        if _identity(observed) != self._root_identity:
            raise RuntimeError("provider data changed during consumption")

    def __enter__(self) -> _PinnedClosure:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        for descriptor in reversed(self._owned_fds):
            with suppress(OSError):
                os.close(descriptor)
        if self._root_fd >= 0:
            with suppress(OSError):
                os.close(self._root_fd)
        self.closed = True


def _open_provider_data_closure(
    snapshot: SourceSnapshot, declared: tuple[Path, ...]
) -> _PinnedClosure:
    if not isinstance(declared, tuple):
        raise ValueError("provider data closure must be a tuple of paths")
    if _WINDOWS_SUPPORTED:
        return _windows_source_store_backend().open_provider_data_closure(
            snapshot,
            declared,
        )
    return _PinnedClosure(snapshot, declared)


def _validate_provider_data_closure(
    snapshot: SourceSnapshot, declared: tuple[Path, ...]
) -> tuple[_ClosureEntryInfo, ...]:
    if _WINDOWS_SUPPORTED:
        return _windows_source_store_backend().validate_provider_data_closure(
            snapshot,
            declared,
        )
    with _open_provider_data_closure(snapshot, declared) as closure:
        return tuple(entry.info for entry in closure.entries)
