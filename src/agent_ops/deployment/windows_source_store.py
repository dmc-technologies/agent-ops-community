"""Native Windows managed Git source store and provider-data closure."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_ops.deployment.models import RewriteAcceptance, SourceSnapshot, SourceSpec
from agent_ops.deployment.windows_fs import (
    FILE_READ_ATTRIBUTES,
    GENERIC_READ,
    WindowsHandle,
    WindowsPathPins,
    WindowsTargetLock,
    _open,
    safe_relative,
)


def _source_module() -> Any:
    from agent_ops.deployment import source_store

    return source_store


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _lock(store: Any, source_id: str) -> WindowsTargetLock:
    return WindowsTargetLock(
        store._state_root,
        lock_name=f".agentops-source-{source_id}.lock",
    )


def _ensure_directory(lock: WindowsTargetLock, relative: Path) -> None:
    marker = relative / ".agentops-create-marker"
    with lock.pin_parent(marker, create=True):
        pass
    lock.identity(relative, directory=True)


def _ref_path(source_id: str, ref: str) -> Path:
    return Path("sources") / source_id / "refs" / f"{hashlib.sha256(ref.encode()).hexdigest()}.json"


def _read_ref(lock: WindowsTargetLock, source_id: str, ref: str) -> str | None:
    module = _source_module()
    content = lock.read_optional(_ref_path(source_id, ref))
    if content is None:
        return None
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("source ref metadata is invalid") from error
    if (
        not isinstance(value, dict)
        or set(value) != module._REF_KEYS
        or value.get("source_id") != source_id
        or value.get("ref") != ref
    ):
        raise RuntimeError("source ref metadata identity mismatch")
    commit = module._normalize_commit(value.get("commit"))
    if commit != value["commit"]:
        raise RuntimeError("source ref metadata commit is noncanonical")
    return commit


def _write_ref(lock: WindowsTargetLock, source_id: str, ref: str, commit: str) -> None:
    lock.write_atomic(
        _ref_path(source_id, ref),
        (
            json.dumps(
                {"source_id": source_id, "ref": ref, "commit": commit},
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )


def _prepare_mirror(
    store: Any,
    lock: WindowsTargetLock,
    source_id: str,
    url: str,
) -> Path:
    module = _source_module()
    relative = Path("sources") / source_id
    _ensure_directory(lock, relative)
    mirror_relative = relative / "mirror.git"
    mirror = store._state_root / mirror_relative
    persisted_url, _ = module._persisted_remote_url_and_transient_auth(url)
    if lock.exists(mirror_relative):
        lock.identity(mirror_relative, directory=True)
        urls = store._git(
            ("--git-dir", str(mirror), "config", "--get-all", "remote.origin.url")
        ).stdout.splitlines()
        if len(urls) != 1 or not module._urls_equivalent(persisted_url, urls[0]):
            raise RuntimeError("source mirror origin URL differs from SourceSpec.url")
        return mirror
    staging_relative = relative / f".tmp-mirror-{uuid.uuid4().hex}"
    _ensure_directory(lock, staging_relative)
    staged_mirror_relative = staging_relative / "mirror.git"
    staged_mirror = store._state_root / staged_mirror_relative
    try:
        store._git(("init", "--bare", str(staged_mirror)))
        store._git(
            (
                "--git-dir",
                str(staged_mirror),
                "config",
                "remote.origin.url",
                persisted_url,
            )
        )
        lock.scan(staged_mirror_relative)
        lock.move_directory(staged_mirror_relative, mirror_relative)
    finally:
        if lock.exists(staging_relative):
            lock.remove_tree(staging_relative)
    return mirror


def fetch(
    store: Any,
    source: SourceSpec,
    ref: str,
    *,
    rewrite: RewriteAcceptance | None = None,
) -> SourceSnapshot:
    module = _source_module()
    module._validate_source_id(source.id)
    module._validate_ref(source.stable_ref)
    module._validate_ref(ref)
    url = module._normalize_source_url(source.url)
    if rewrite is not None:
        old = module._normalize_commit(rewrite.old_commit)
        new = module._normalize_commit(rewrite.new_commit)
        if old != rewrite.old_commit or new != rewrite.new_commit:
            raise ValueError("rewrite acceptance commits must be exact lowercase object ids")
    with _lock(store, source.id) as lock:
        source_relative = Path("sources") / source.id
        _ensure_directory(lock, source_relative)
        for path, kind in lock.scan(source_relative).items():
            if path.name.startswith(".tmp-") and kind == "directory":
                lock.remove_tree(source_relative / path)
        mirror = _prepare_mirror(store, lock, source.id, url)
        previous = _read_ref(lock, source.id, ref)
        accepted_ref = f"refs/agentops/accepted/{hashlib.sha256(ref.encode()).hexdigest()}"
        accepted_before = _read_git_ref(store, mirror, accepted_ref)
        if previous is not None and accepted_before not in {None, previous}:
            raise RuntimeError("accepted Git ref differs from durable source ref metadata")
        candidate = f"refs/agentops/candidate/{uuid.uuid4().hex}"
        _, transient_auth = module._persisted_remote_url_and_transient_auth(url)
        try:
            try:
                store._git(
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
            except module._GitFailure as error:
                raise RuntimeError(f"requested Git ref {ref!r} was not found or fetched") from error
            commit = module._normalize_commit(
                store._git(
                    ("--git-dir", str(mirror), "rev-parse", "--verify", f"{candidate}^{{commit}}")
                ).stdout.strip()
            )
            _check_refresh(store, mirror, source, ref, previous, commit, rewrite)
            snapshot = _ensure_snapshot(store, lock, source.id, ref, commit, mirror)
            store._git(
                (
                    "--git-dir",
                    str(mirror),
                    "update-ref",
                    accepted_ref,
                    commit,
                    accepted_before or "0" * 40,
                )
            )
            _write_ref(lock, source.id, ref, commit)
            lock.verify()
            return snapshot
        finally:
            with suppress(module._GitFailure):
                store._git(("--git-dir", str(mirror), "update-ref", "-d", candidate))


def _read_git_ref(store: Any, mirror: Path, ref: str) -> str | None:
    module = _source_module()
    result = store._git(
        ("--git-dir", str(mirror), "rev-parse", "--verify", f"{ref}^{{commit}}"),
        accepted_returncodes=frozenset({0, 128}),
    )
    if result.returncode == 128:
        return None
    return module._normalize_commit(result.stdout.strip())


def _check_refresh(
    store: Any,
    mirror: Path,
    source: SourceSpec,
    ref: str,
    previous: str | None,
    commit: str,
    rewrite: RewriteAcceptance | None,
) -> None:
    if previous is None or previous == commit:
        return
    ancestor = store._git(
        ("--git-dir", str(mirror), "merge-base", "--is-ancestor", previous, commit),
        accepted_returncodes=frozenset({0, 1}),
    )
    if ancestor.returncode == 0:
        return
    if ref == source.stable_ref:
        raise RuntimeError("stable ref rejected a non-fast-forward rewrite")
    if rewrite is None or rewrite.old_commit != previous or rewrite.new_commit != commit:
        raise RuntimeError(
            "development ref rewrite acceptance must exactly match the prior and new commits"
        )


def _ensure_snapshot(
    store: Any,
    lock: WindowsTargetLock,
    source_id: str,
    ref: str,
    commit: str,
    mirror: Path,
) -> SourceSnapshot:
    snapshot_relative = Path("sources") / source_id / "snapshots" / commit
    if lock.exists(snapshot_relative):
        return _load_snapshot(store, lock, source_id, commit, requested_ref=ref)
    staging_relative = Path("sources") / source_id / f".tmp-snapshot-{uuid.uuid4().hex}"
    _ensure_directory(lock, staging_relative)
    staged_relative = staging_relative / "snapshot"
    staged = store._state_root / staged_relative
    try:
        store._git(("init", str(staged)))
        store._git(
            ("fetch", "--no-tags", "--no-write-fetch-head", str(mirror), commit),
            cwd=staged,
        )
        store._git(("checkout", "--detach", commit), cwd=staged)
        _verify_exact_checkout(store, staged, commit)
        metadata = staged_relative / ".git" / _source_module()._SNAPSHOT_METADATA
        lock.write_atomic(
            metadata,
            (
                json.dumps(
                    {"source_id": source_id, "ref": ref, "commit": commit},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8"),
        )
        lock.move_directory(staged_relative, snapshot_relative)
    finally:
        if lock.exists(staging_relative):
            lock.remove_tree(staging_relative)
    return _load_snapshot(store, lock, source_id, commit, requested_ref=ref)


def _verify_exact_checkout(store: Any, root: Path, commit: str) -> dict[str, Any]:
    module = _source_module()
    observed = store._git(("rev-parse", "--verify", "HEAD^{commit}"), cwd=root).stdout.strip()
    if observed.lower() != commit:
        raise RuntimeError("snapshot HEAD does not match requested commit")
    if (
        store._git(
            ("symbolic-ref", "-q", "HEAD"),
            cwd=root,
            accepted_returncodes=frozenset({0, 1}),
        ).returncode
        == 0
    ):
        raise RuntimeError("snapshot HEAD is not detached")
    status = store._git(("status", "--porcelain=v1", "-z", "--untracked-files=all"), cwd=root)
    if status.stdout:
        raise RuntimeError("snapshot worktree differs from HEAD")
    expected = module._head_entries(root, timeout=store._git_timeout)
    if any(item.object_type != "blob" or item.mode == "120000" for item in expected.values()):
        raise RuntimeError("Windows snapshot contains unsupported Git entry type")
    return expected


def snapshot(store: Any, source_id: str, commit: str) -> SourceSnapshot:
    module = _source_module()
    module._validate_source_id(source_id)
    normalized = module._normalize_commit(commit)
    with _lock(store, source_id) as lock:
        return _load_snapshot(store, lock, source_id, normalized)


def _load_snapshot(
    store: Any,
    lock: WindowsTargetLock,
    source_id: str,
    commit: str,
    *,
    requested_ref: str | None = None,
) -> SourceSnapshot:
    module = _source_module()
    relative = Path("sources") / source_id / "snapshots" / commit
    root = store._state_root / relative
    lock.identity(relative, directory=True)
    metadata_relative = relative / ".git" / module._SNAPSHOT_METADATA
    content = lock.read_file(metadata_relative, maximum=1024 * 1024)
    try:
        metadata = json.loads(content.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("snapshot metadata is invalid") from error
    if (
        not isinstance(metadata, dict)
        or set(metadata) != module._SNAPSHOT_KEYS
        or metadata.get("source_id") != source_id
        or metadata.get("commit") != commit
    ):
        raise RuntimeError("snapshot metadata identity mismatch")
    module._validate_ref(metadata.get("ref"))
    _verify_exact_checkout(store, root, commit)
    lock.scan(relative)
    lock.verify()
    return SourceSnapshot(source_id, requested_ref or metadata["ref"], commit, root)


@dataclass(frozen=True)
class _EntryInfo:
    relative_path: Path
    kind: str
    mode: int
    size: int


class _Entry:
    def __init__(
        self,
        root: Path,
        relative: Path,
        handle: WindowsHandle,
        *,
        kind: str,
        mode: int,
        expected_blob: str | None,
        object_format: str,
    ) -> None:
        self.root = root
        self.relative_path = relative
        self.handle = handle
        self.identity = handle.identity()
        self.kind = kind
        self.mode = mode
        self.size = self.identity.size
        self.expected_blob = expected_blob
        self.object_format = object_format

    @property
    def info(self) -> _EntryInfo:
        return _EntryInfo(self.relative_path, self.kind, self.mode, self.size)

    def read_bytes(self) -> bytes:
        if self.kind != "file":
            raise RuntimeError("provider data directories cannot be read as bytes")
        remaining = self.identity.size
        chunks: list[bytes] = []
        kernel = __import__(
            "agent_ops.deployment.windows_fs",
            fromlist=["_kernel32"],
        )._kernel32()
        import ctypes

        while remaining:
            size = min(remaining, 1024 * 1024)
            buffer = ctypes.create_string_buffer(size)
            read = ctypes.c_uint32()
            if not kernel.ReadFile(
                self.handle.value,
                buffer,
                size,
                ctypes.byref(read),
                None,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            chunks.append(buffer.raw[: read.value])
            remaining -= read.value
        content = b"".join(chunks)
        if self.handle.identity() != self.identity:
            raise RuntimeError("provider data changed during consumption")
        if (
            self.expected_blob is None
            or _source_module()._blob_oid(content, self.object_format) != self.expected_blob
        ):
            raise RuntimeError("provider data bytes differ from tracked Git blob")
        return content


class WindowsClosure:
    def __init__(self, snapshot: SourceSnapshot, declared: tuple[Path, ...]) -> None:
        module = _source_module()
        module._validate_source_id(snapshot.source_id)
        module._validate_ref(snapshot.ref)
        commit = module._normalize_commit(snapshot.commit)
        self.root = Path(snapshot.root)
        self.pins = WindowsPathPins(self.root, create=False)
        self.closed = False
        expected = _verify_exact_checkout(
            type("Store", (), {"_git": staticmethod(module._run_git), "_git_timeout": 30.0})(),
            self.root,
            commit,
        )
        object_format = module._run_git(
            ("rev-parse", "--show-object-format"), cwd=self.root
        ).stdout.strip()
        entries = []
        entry_pins: list[WindowsPathPins] = []
        try:
            for relative in declared:
                safe_relative(relative)
                raw = relative.as_posix()
                head = expected.get(raw)
                is_directory = head is None and any(path.startswith(f"{raw}/") for path in expected)
                if head is None and not is_directory:
                    raise RuntimeError(f"provider data path is not tracked at HEAD: {raw}")
                pins = WindowsPathPins(
                    self.root / (relative if is_directory else relative.parent),
                    create=False,
                )
                entry_pins.append(pins)
                handle = _open(
                    self.root / relative,
                    access=(
                        FILE_READ_ATTRIBUTES
                        if is_directory
                        else GENERIC_READ | FILE_READ_ATTRIBUTES
                    ),
                    directory=is_directory,
                )
                identity = handle.identity()
                if identity.is_reparse_point:
                    handle.close()
                    raise RuntimeError(f"provider data closure contains a reparse point: {raw}")
                entries.append(
                    _Entry(
                        self.root,
                        relative,
                        handle,
                        kind="directory" if is_directory else "file",
                        mode=(0o755 if is_directory or head.mode == "100755" else 0o644),
                        expected_blob=None if is_directory else head.object_id,
                        object_format=object_format,
                    )
                )
        except BaseException:
            for entry in entries:
                entry.handle.close()
            for pins in reversed(entry_pins):
                pins.__exit__()
            self.pins.__exit__()
            raise
        self.entries = tuple(entries)
        self.entry_pins = tuple(entry_pins)

    def __enter__(self) -> WindowsClosure:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        for entry in reversed(self.entries):
            entry.handle.close()
        for pins in reversed(self.entry_pins):
            pins.__exit__()
        self.pins.__exit__()
        self.closed = True


def open_provider_data_closure(
    snapshot: SourceSnapshot,
    declared: tuple[Path, ...],
) -> WindowsClosure:
    if not isinstance(declared, tuple):
        raise ValueError("provider data closure must be a tuple of paths")
    return WindowsClosure(snapshot, declared)


def validate_provider_data_closure(
    snapshot: SourceSnapshot,
    declared: tuple[Path, ...],
) -> tuple[_EntryInfo, ...]:
    with open_provider_data_closure(snapshot, declared) as closure:
        return tuple(entry.info for entry in closure.entries)
