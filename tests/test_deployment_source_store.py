from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import os
import stat
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import agent_ops.deployment.source_store as source_store_module
from agent_ops.deployment.models import RewriteAcceptance, SourceSnapshot, SourceSpec
from agent_ops.deployment.source_store import (
    SourceStore,
    _GitTimeout,
    _normalize_source_url,
    _open_provider_data_closure,
    _run_git,
    _validate_provider_data_closure,
)


def _git(*args: str, cwd: Path | None = None) -> str:
    environment = {
        "PATH": os.environ["PATH"],
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Agent Ops Tests",
        "GIT_AUTHOR_EMAIL": "agent-ops@example.invalid",
        "GIT_COMMITTER_NAME": "Agent Ops Tests",
        "GIT_COMMITTER_EMAIL": "agent-ops@example.invalid",
    }
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()


@pytest.fixture
def git_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    _git("init", "--bare", str(remote))
    _git("init", "-b", "main", str(work))
    (work / "catalog").mkdir()
    (work / "catalog" / "skill.txt").write_text("one\n")
    (work / "catalog" / "inert.py").write_text(
        "raise AssertionError('must remain inert')\n"
    )
    (work / ".gitignore").write_text("ignored-extra\n")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "initial", cwd=work)
    _git("remote", "add", "origin", str(remote), cwd=work)
    _git("push", "origin", "HEAD:refs/heads/main", cwd=work)
    _git("checkout", "-b", "feat/example", cwd=work)
    (work / "catalog" / "branch.txt").write_text("branch\n")
    _git("add", ".", cwd=work)
    _git("commit", "-m", "branch", cwd=work)
    _git("push", "origin", "HEAD:refs/heads/feat/example", cwd=work)
    return remote


def _source(remote: Path, *, source_id: str = "example") -> SourceSpec:
    return SourceSpec(id=source_id, url=str(remote), stable_ref="refs/heads/main")


def _remote_work(remote: Path) -> Path:
    return remote.parent / "work"


def _commit_and_push(remote: Path, ref: str, content: str) -> str:
    work = _remote_work(remote)
    _git("checkout", ref.removeprefix("refs/heads/"), cwd=work)
    (work / "catalog" / "skill.txt").write_text(content)
    _git("add", ".", cwd=work)
    _git("commit", "-m", content.strip(), cwd=work)
    _git("push", "origin", f"HEAD:{ref}", cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


def _force_rewrite(remote: Path, ref: str, content: str) -> str:
    work = _remote_work(remote)
    _git("checkout", ref.removeprefix("refs/heads/"), cwd=work)
    _git("checkout", "--orphan", f"rewrite-{uuid.uuid4().hex}", cwd=work)
    _git("rm", "-rf", ".", cwd=work)
    (work / "catalog").mkdir()
    (work / "catalog" / "skill.txt").write_text(content)
    _git("add", ".", cwd=work)
    _git("commit", "-m", content.strip(), cwd=work)
    _git("push", "--force", "origin", f"HEAD:{ref}", cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


def _process_fetch(
    state: str,
    remote: str,
    barrier: multiprocessing.synchronize.Barrier,
    results: multiprocessing.queues.Queue,
) -> None:
    try:
        barrier.wait(timeout=10)
        snapshot = SourceStore(Path(state)).fetch(
            SourceSpec("example", remote), "refs/heads/main"
        )
        results.put(("ok", snapshot.commit))
    except BaseException as error:
        results.put(("error", repr(error)))


def test_branch_refresh_resolves_one_immutable_commit(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    source = SourceSpec(
        id="example", url=str(git_remote), stable_ref="refs/heads/main"
    )
    snapshot = store.fetch(source, "refs/heads/feat/example")
    assert len(snapshot.commit) == 40
    assert snapshot.root.is_dir()
    observed = subprocess.run(
        ["git", "-C", str(snapshot.root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert observed == snapshot.commit


def test_source_store_creates_owner_only_state_for_fetched_private_content(
    git_remote: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    previous_umask = os.umask(0o022)
    try:
        snapshot = SourceStore(state).fetch(_source(git_remote), "refs/heads/main")
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "sources").stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "sources/example").stat().st_mode) == 0o700
    assert (snapshot.root / "catalog/skill.txt").read_text() == "one\n"


def test_source_store_never_persists_https_credentials_in_mirror_config(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    source_root = state / "sources" / "private"
    source_root.mkdir(parents=True)
    store = SourceStore(state)

    mirror = store._prepare_mirror(
        source_root,
        "https://username:password@example.invalid/private/repository.git",
    )

    persisted = (mirror / "config").read_text(encoding="utf-8")
    assert "username" not in persisted
    assert "password" not in persisted
    assert "https://example.invalid/private/repository.git" in persisted


def test_source_store_passes_https_credentials_only_in_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_ops.deployment.source_store as source_store_module

    observed: dict[str, object] = {}

    def run_git(args, **kwargs):
        if "fetch" in args:
            observed["args"] = args
            observed["environment"] = kwargs["environment"]
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")

    monkeypatch.setattr(source_store_module, "_run_git", run_git)
    SourceStore(tmp_path / "state")._fetch_candidate(
        tmp_path / "mirror.git",
        "refs/heads/main",
        "refs/agentops/candidate/test",
        "https://username:password@example.invalid/private/repository.git",
    )

    assert "username" not in " ".join(observed["args"])
    assert "password" not in " ".join(observed["args"])
    environment = observed["environment"]
    assert environment["GIT_CONFIG_COUNT"] == "1"
    assert environment["GIT_CONFIG_KEY_0"] == (
        "http.https://example.invalid/private/repository.git.extraHeader"
    )
    assert "password" in base64.b64decode(
        environment["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: Basic ")
    ).decode()


def test_layout_detached_head_and_old_snapshot_remains_unchanged(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    first = store.fetch(source, "refs/heads/main")
    old_bytes = (first.root / "catalog" / "skill.txt").read_bytes()
    new_commit = _commit_and_push(git_remote, "refs/heads/main", "two\n")
    second = store.fetch(source, "refs/heads/main")

    source_root = tmp_path / "state" / "sources" / "example"
    assert (source_root / "mirror.git").is_dir()
    assert first.root == source_root / "snapshots" / first.commit
    assert second.root == source_root / "snapshots" / new_commit
    symbolic = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=second.root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert symbolic.returncode == 1
    assert _git("rev-parse", "HEAD", cwd=first.root) == first.commit
    assert (first.root / "catalog" / "skill.txt").read_bytes() == old_bytes


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "main",
        "heads/main",
        "refs/heads/main:refs/heads/other",
        "+refs/heads/main",
        "refs/heads/main^",
        "refs/heads/main~1",
        "refs/heads/../main",
        "refs/heads/a..b",
        "refs/heads/a\\b",
        "refs/heads/a b",
    ],
)
def test_invalid_refs_reject_before_mutation(tmp_path: Path, ref: str) -> None:
    state = tmp_path / "state"
    with pytest.raises(ValueError, match="fully qualified Git ref"):
        SourceStore(state).fetch(SourceSpec("example", "/missing"), ref)
    assert not state.exists()


@pytest.mark.parametrize(
    "source_id", ["", ".", "..", "../escape", "a/b", "a\\b", "a b", "C:drive"]
)
def test_unsafe_source_ids_reject_before_mutation(
    tmp_path: Path, source_id: str
) -> None:
    state = tmp_path / "state"
    if source_id:
        source = SourceSpec(source_id, "/missing")
    else:
        with pytest.raises(ValueError, match="source id"):
            SourceSpec(source_id, "/missing")
        return
    with pytest.raises(ValueError, match="source id"):
        SourceStore(state).fetch(source, "refs/heads/main")
    assert not state.exists()


def test_mirror_fetches_only_requested_ref(git_remote: Path, tmp_path: Path) -> None:
    store = SourceStore(tmp_path / "state")
    store.fetch(_source(git_remote), "refs/heads/feat/example")
    mirror = tmp_path / "state/sources/example/mirror.git"
    refs = set(_git("for-each-ref", "--format=%(refname)", cwd=mirror).splitlines())
    assert not any(ref.startswith("refs/remotes/") for ref in refs)
    assert not any(ref.startswith("refs/heads/") for ref in refs)
    expected = "refs/agentops/accepted/" + hashlib.sha256(
        b"refs/heads/feat/example"
    ).hexdigest()
    assert refs == {expected}


def test_fast_forward_refresh_records_new_snapshot(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    first = store.fetch(source, "refs/heads/main")
    new_commit = _commit_and_push(git_remote, "refs/heads/main", "forward\n")
    second = store.fetch(source, "refs/heads/main")
    assert second.commit == new_commit
    assert first.root.is_dir()


def test_missing_ref_preserves_prior_state_and_snapshot(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    prior = store.fetch(source, "refs/heads/main")
    source_root = tmp_path / "state/sources/example"
    before = {
        path.relative_to(source_root): path.read_bytes()
        for path in source_root.rglob("*.json")
    }
    with pytest.raises(RuntimeError, match="requested Git ref.*not found"):
        store.fetch(source, "refs/heads/missing")
    after = {
        path.relative_to(source_root): path.read_bytes()
        for path in source_root.rglob("*.json")
    }
    assert after == before
    assert not list(source_root.glob(".tmp-*"))
    assert store.snapshot("example", prior.commit) == prior


def test_origin_mismatch_rejects_without_mutation(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    prior = store.fetch(source, "refs/heads/main")
    mirror = tmp_path / "state/sources/example/mirror.git"
    before = _git("show-ref", cwd=mirror)
    other = git_remote.parent / "other.git"
    _git("init", "--bare", str(other))
    with pytest.raises(RuntimeError, match="origin URL"):
        store.fetch(_source(other), "refs/heads/main")
    assert _git("show-ref", cwd=mirror) == before
    assert store.snapshot("example", prior.commit) == prior


def test_equivalent_local_file_url_is_accepted(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    store.fetch(_source(git_remote), "refs/heads/main")
    again = store.fetch(
        SourceSpec("example", git_remote.resolve().as_uri()), "refs/heads/main"
    )
    assert len(again.commit) == 40


def test_stable_ref_never_accepts_rewrite(git_remote: Path, tmp_path: Path) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    prior = store.fetch(source, source.stable_ref)
    rewritten = _force_rewrite(git_remote, source.stable_ref, "rewritten stable\n")
    with pytest.raises(RuntimeError, match="stable ref.*non-fast-forward"):
        store.fetch(
            source,
            source.stable_ref,
            rewrite=RewriteAcceptance(prior.commit, rewritten),
        )
    assert store.snapshot("example", prior.commit).commit == prior.commit


def test_development_rewrite_requires_exact_ref_scoped_acceptance(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    ref = "refs/heads/feat/example"
    prior = store.fetch(source, ref)
    rewritten = _force_rewrite(git_remote, ref, "rewritten development\n")

    with pytest.raises(RuntimeError, match="rewrite acceptance"):
        store.fetch(source, ref)
    with pytest.raises((ValueError, RuntimeError)):
        store.fetch(source, ref, rewrite=RewriteAcceptance(prior.commit[:12], rewritten))
    with pytest.raises((ValueError, RuntimeError)):
        store.fetch(source, ref, rewrite=RewriteAcceptance(prior.commit, rewritten[:12]))
    with pytest.raises((ValueError, RuntimeError)):
        store.fetch(
            source,
            ref,
            rewrite=RewriteAcceptance(prior.commit.upper(), rewritten.upper()),
        )
    accepted = store.fetch(
        source, ref, rewrite=RewriteAcceptance(prior.commit, rewritten)
    )
    assert accepted.commit == rewritten


def test_rewrite_acceptance_does_not_authorize_another_ref(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    feature = store.fetch(source, "refs/heads/feat/example")
    main = store.fetch(source, "refs/heads/main")
    rewritten_main = _force_rewrite(git_remote, "refs/heads/main", "main rewrite\n")
    with pytest.raises(RuntimeError, match="stable ref"):
        store.fetch(
            source,
            "refs/heads/main",
            rewrite=RewriteAcceptance(feature.commit, rewritten_main),
        )
    assert store.snapshot("example", main.commit).commit == main.commit


def test_concurrent_same_ref_fetches_are_coherent(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    barrier = threading.Barrier(3)

    def fetch() -> SourceSnapshot:
        barrier.wait()
        return store.fetch(source, "refs/heads/main")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(fetch) for _ in range(2)]
        barrier.wait()
        snapshots = [future.result(timeout=20) for future in futures]
    assert snapshots[0].commit == snapshots[1].commit
    assert store.snapshot("example", snapshots[0].commit).root.is_dir()


def test_different_sources_do_not_share_a_lock(
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_remote = tmp_path / "other.git"
    _git("clone", "--bare", str(git_remote), str(other_remote))
    store = SourceStore(tmp_path / "state")
    barrier = threading.Barrier(2)
    original = source_store_module._run_git

    def observe_fetch(*args: object, **kwargs: object) -> object:
        git_args = args[0]
        if "fetch" in git_args and "origin" in git_args:
            barrier.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(source_store_module, "_run_git", observe_fetch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(store.fetch, _source(git_remote), "refs/heads/main"),
            executor.submit(
                store.fetch,
                _source(other_remote, source_id="other"),
                "refs/heads/main",
            ),
        )
        snapshots = [future.result(timeout=20) for future in futures]
    assert {snapshot.source_id for snapshot in snapshots} == {"example", "other"}


def test_same_commit_fetched_from_another_ref_reports_requested_ref(
    git_remote: Path, tmp_path: Path
) -> None:
    _git(
        "push",
        "origin",
        "refs/heads/main:refs/heads/alias",
        cwd=_remote_work(git_remote),
    )
    store = SourceStore(tmp_path / "state")
    store.fetch(_source(git_remote), "refs/heads/main")
    alias = store.fetch(_source(git_remote), "refs/heads/alias")
    assert alias.ref == "refs/heads/alias"
    assert store.snapshot("example", alias.commit).ref == "refs/heads/main"


@pytest.mark.parametrize("commit", ["abc", "g" * 40, "../" + "a" * 40])
def test_snapshot_lookup_rejects_invalid_commit(
    tmp_path: Path, commit: str
) -> None:
    with pytest.raises(ValueError, match="40-hex"):
        SourceStore(tmp_path / "state").snapshot("example", commit)
    assert not (tmp_path / "state").exists()


def test_snapshot_lookup_normalizes_uppercase_and_rejects_tampering(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    snapshot = store.fetch(_source(git_remote), "refs/heads/main")
    assert store.snapshot("example", snapshot.commit.upper()).commit == snapshot.commit
    metadata = snapshot.root / ".git/agentops-snapshot.json"
    metadata.write_text(
        json.dumps(
            {"source_id": "example", "ref": "refs/heads/main", "commit": "0" * 40}
        )
    )
    with pytest.raises(RuntimeError, match="snapshot metadata"):
        store.snapshot("example", snapshot.commit)


def test_snapshot_lookup_rejects_symlinked_or_non_directory_state(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    snapshot = store.fetch(_source(git_remote), "refs/heads/main")
    moved = snapshot.root.with_name("moved")
    snapshot.root.rename(moved)
    snapshot.root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(RuntimeError, match="snapshot directory"):
        store.snapshot("example", snapshot.commit)


def test_snapshot_lookup_rejects_symlinked_git_state(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    snapshot = store.fetch(_source(git_remote), "refs/heads/main")
    git_directory = snapshot.root / ".git"
    moved = snapshot.root / ".git-moved"
    git_directory.rename(moved)
    git_directory.symlink_to(moved.name, target_is_directory=True)
    with pytest.raises(RuntimeError, match="snapshot Git directory"):
        store.snapshot("example", snapshot.commit)


def test_snapshot_lookup_rejects_symlinked_store_root(
    git_remote: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    store = SourceStore(state)
    snapshot = store.fetch(_source(git_remote), "refs/heads/main")
    moved = tmp_path / "moved-state"
    state.rename(moved)
    state.symlink_to(moved, target_is_directory=True)
    with pytest.raises(RuntimeError, match="source store root"):
        store.snapshot("example", snapshot.commit)


@pytest.mark.parametrize("replacement", ["missing", "file", "metadata-link"])
def test_snapshot_lookup_rejects_missing_or_nonregular_state(
    git_remote: Path, tmp_path: Path, replacement: str
) -> None:
    store = SourceStore(tmp_path / "state")
    snapshot = store.fetch(_source(git_remote), "refs/heads/main")
    metadata = snapshot.root / ".git/agentops-snapshot.json"
    if replacement == "metadata-link":
        moved_metadata = metadata.with_suffix(".moved")
        metadata.rename(moved_metadata)
        metadata.symlink_to(moved_metadata.name)
        match = "snapshot metadata"
    else:
        moved_snapshot = snapshot.root.with_name("moved-snapshot")
        snapshot.root.rename(moved_snapshot)
        if replacement == "file":
            snapshot.root.write_text("not a directory\n")
        match = "snapshot directory"
    with pytest.raises(RuntimeError, match=match):
        store.snapshot("example", snapshot.commit)


def test_git_environment_ignores_ambient_trace_config_and_hooks(
    git_remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / "external-sentinel"
    sentinel.write_text("unchanged\n")
    malicious_config = tmp_path / "malicious.gitconfig"
    malicious_config.write_text(
        f"[core]\n\thooksPath = {tmp_path / 'hooks'}\n[alias]\n\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(malicious_config))
    monkeypatch.setenv("GIT_TRACE", str(sentinel))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "outside.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "outside-work"))
    SourceStore(tmp_path / "state").fetch(_source(git_remote), "refs/heads/main")
    assert sentinel.read_text() == "unchanged\n"


def test_unexpected_source_paths_and_failed_fetch_leave_no_temporaries(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    mirror = state / "sources/example/mirror.git"
    mirror.parent.mkdir(parents=True)
    state.chmod(0o700)
    (state / "sources").chmod(0o700)
    mirror.parent.chmod(0o700)
    mirror.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError, match="mirror"):
        SourceStore(state).fetch(
            SourceSpec("example", str(tmp_path)), "refs/heads/main"
        )
    assert mirror.is_symlink()
    assert not list((state / "sources/example").glob(".tmp-*"))


def test_git_process_control_exception_propagates_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StopGit(KeyboardInterrupt):
        pass

    stop = StopGit()

    def stop_git(*args: object, **kwargs: object) -> object:
        raise stop

    monkeypatch.setattr(source_store_module, "_run_git", stop_git)
    with pytest.raises(StopGit) as caught:
        SourceStore(tmp_path / "state").fetch(
            SourceSpec("example", str(tmp_path)), "refs/heads/main"
        )
    assert caught.value is stop
    assert not list((tmp_path / "state/sources/example").glob(".tmp-*"))


def test_snapshot_promotion_exception_preserves_prior_state(
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    prior = store.fetch(source, "refs/heads/main")
    new_commit = _commit_and_push(git_remote, "refs/heads/main", "promotion\n")
    promotion_error = OSError("promotion interrupted")
    original_replace = source_store_module.os.replace

    def fail_snapshot_promotion(source_path: object, destination: object) -> None:
        if Path(source_path).name == "snapshot" and Path(destination).name == new_commit:
            raise promotion_error
        original_replace(source_path, destination)

    monkeypatch.setattr(source_store_module.os, "replace", fail_snapshot_promotion)
    with pytest.raises(OSError) as caught:
        store.fetch(source, "refs/heads/main")
    assert caught.value is promotion_error
    assert store.snapshot("example", prior.commit).commit == prior.commit
    source_root = tmp_path / "state/sources/example"
    assert not (source_root / "snapshots" / new_commit).exists()
    assert not list(source_root.glob(".tmp-*"))


def test_provider_data_closure_accepts_confined_inert_data(
    git_remote: Path, tmp_path: Path
) -> None:
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    validated = _validate_provider_data_closure(
        snapshot, (Path("catalog/skill.txt"), Path("catalog/inert.py"), Path("catalog"))
    )
    assert tuple(entry.relative_path for entry in validated) == (
        Path("catalog/skill.txt"),
        Path("catalog/inert.py"),
        Path("catalog"),
    )


@pytest.mark.parametrize(
    "declared",
    [
        Path(""),
        Path("."),
        Path("/absolute"),
        Path("../parent"),
        Path("a/../parent"),
        Path("a\\b"),
        Path("C:drive"),
        Path("missing"),
    ],
)
def test_provider_data_closure_rejects_invalid_or_missing_paths(
    git_remote: Path, tmp_path: Path, declared: Path
) -> None:
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    with pytest.raises((ValueError, RuntimeError)):
        _validate_provider_data_closure(snapshot, (declared,))


def test_provider_data_closure_rejects_symlink_escape(
    git_remote: Path, tmp_path: Path
) -> None:
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    outside = tmp_path / "outside"
    outside.write_text("outside\n")
    (snapshot.root / "catalog/escape").symlink_to(outside)
    with pytest.raises(RuntimeError, match="snapshot worktree|symlink"):
        _validate_provider_data_closure(snapshot, (Path("catalog/escape"),))


def test_provider_data_directory_rejects_nested_symlink_escape(
    git_remote: Path, tmp_path: Path
) -> None:
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    outside = tmp_path / "outside-directory"
    outside.mkdir()
    (snapshot.root / "catalog/nested-escape").symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(RuntimeError, match="snapshot worktree|symlink"):
        _validate_provider_data_closure(snapshot, (Path("catalog"),))


def test_provider_data_directory_rejects_tracked_symlink(
    git_remote: Path, tmp_path: Path
) -> None:
    work = _remote_work(git_remote)
    _git("checkout", "main", cwd=work)
    (work / "catalog/tracked-link").symlink_to("../../outside")
    _git("add", "catalog/tracked-link", cwd=work)
    _git("commit", "-m", "tracked symlink", cwd=work)
    _git("push", "origin", "HEAD:refs/heads/main", cwd=work)
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    with pytest.raises(RuntimeError, match="symlink"):
        _validate_provider_data_closure(snapshot, (Path("catalog"),))


def test_provider_data_closure_rejects_duplicate_paths(
    git_remote: Path, tmp_path: Path
) -> None:
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    with pytest.raises(ValueError, match="duplicate"):
        _validate_provider_data_closure(
            snapshot, (Path("catalog/skill.txt"), Path("catalog/skill.txt"))
        )


def test_ref_state_uses_canonical_strict_json(git_remote: Path, tmp_path: Path) -> None:
    store = SourceStore(tmp_path / "state")
    snapshot = store.fetch(_source(git_remote), "refs/heads/main")
    ref_key = hashlib.sha256(b"refs/heads/main").hexdigest()
    metadata = tmp_path / f"state/sources/example/refs/{ref_key}.json"
    assert metadata.read_bytes().endswith(b"\n")
    metadata.write_text(
        f'{{"commit":"{snapshot.commit}","commit":"{snapshot.commit}",'
        '"ref":"refs/heads/main","source_id":"example"}\n'
    )
    with pytest.raises(RuntimeError, match="duplicate"):
        store.fetch(_source(git_remote), "refs/heads/main")


@pytest.mark.parametrize(
    "url",
    [
        "ext::touch /tmp/sentinel",
        "evil::payload",
        "ftp://example.invalid/repo.git",
        "git://example.invalid/repo.git",
        "-u./repo",
        "https://example.invalid/repo.git\n--upload-pack=evil",
        "ssh://example.invalid/repo.git\x00evil",
        "nosuchscheme://example.invalid/repo.git",
        "example.invalid:-upload-pack=evil",
    ],
)
def test_source_url_allowlist_rejects_before_mutation(tmp_path: Path, url: str) -> None:
    state = tmp_path / "state"
    with pytest.raises(ValueError, match="source URL"):
        SourceStore(state).fetch(SourceSpec("example", url), "refs/heads/main")
    assert not state.exists()


def test_remote_helper_cannot_execute_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / "sentinel"
    binary = tmp_path / "bin"
    binary.mkdir()
    helper = binary / "git-remote-evil"
    helper.write_text(f"#!/bin/sh\nprintf executed > {sentinel}\n")
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    state = tmp_path / "state"
    with pytest.raises(ValueError, match="source URL"):
        SourceStore(state).fetch(
            SourceSpec("example", "evil::payload"), "refs/heads/main"
        )
    assert not state.exists()
    assert not sentinel.exists()


def test_non_string_source_url_rejects_before_mutation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    source = SourceSpec("example", 123)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="source URL"):
        SourceStore(state).fetch(source, "refs/heads/main")
    assert not state.exists()


def test_source_url_allowlist_canonicalizes_supported_forms(git_remote: Path) -> None:
    assert _normalize_source_url(str(git_remote)) == str(git_remote.resolve())
    assert _normalize_source_url(git_remote.as_uri()) == git_remote.resolve().as_uri()
    assert _normalize_source_url("https://example.invalid/repo.git") == (
        "https://example.invalid/repo.git"
    )
    assert _normalize_source_url("ssh://user@example.invalid/repo.git") == (
        "ssh://user@example.invalid/repo.git"
    )
    assert _normalize_source_url("user@example.invalid:repo.git") == (
        "user@example.invalid:repo.git"
    )
    assert _normalize_source_url("git-host:repo.git") == "git-host:repo.git"


@pytest.mark.parametrize(
    "mutation",
    ["untracked", "ignored", "assume-unchanged", "skip-worktree", "mode", "type"],
)
def test_snapshot_lookup_compares_exact_head_tree(
    git_remote: Path, tmp_path: Path, mutation: str
) -> None:
    store = SourceStore(tmp_path / "state")
    snapshot = store.fetch(_source(git_remote), "refs/heads/main")
    tracked = snapshot.root / "catalog/skill.txt"
    if mutation == "untracked":
        (snapshot.root / "extra").write_text("extra\n")
    elif mutation == "ignored":
        (snapshot.root / "ignored-extra").write_text("ignored\n")
    elif mutation == "assume-unchanged":
        _git("update-index", "--assume-unchanged", "catalog/skill.txt", cwd=snapshot.root)
        tracked.write_text("changed\n")
    elif mutation == "skip-worktree":
        _git("update-index", "--skip-worktree", "catalog/skill.txt", cwd=snapshot.root)
        tracked.write_text("changed\n")
    elif mutation == "mode":
        tracked.chmod(0o755)
    else:
        tracked.unlink()
        tracked.mkdir()
    with pytest.raises(RuntimeError, match="snapshot worktree"):
        store.snapshot("example", snapshot.commit)


def test_closure_requires_exact_tracked_head_entry(
    git_remote: Path, tmp_path: Path
) -> None:
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    (snapshot.root / "untracked-data").write_text("outside identity\n")
    with pytest.raises(RuntimeError, match="snapshot worktree"):
        _validate_provider_data_closure(snapshot, (Path("untracked-data"),))


def test_partial_snapshot_publication_self_heals(git_remote: Path, tmp_path: Path) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    snapshot = store.fetch(source, "refs/heads/main")
    (snapshot.root / ".git/agentops-snapshot.json").unlink()
    stale = tmp_path / "state/sources/example/.tmp-snapshot-dead"
    stale.mkdir()
    (stale / "partial").write_text("partial\n")
    repaired = store.fetch(source, "refs/heads/main")
    assert repaired.commit == snapshot.commit
    assert (repaired.root / ".git/agentops-snapshot.json").is_file()
    assert not stale.exists()


def test_legacy_metadata_only_state_self_heals(git_remote: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    source_root = state / "sources/example"
    legacy = source_root / "snapshot-metadata"
    legacy.mkdir(parents=True)
    state.chmod(0o700)
    (state / "sources").chmod(0o700)
    source_root.chmod(0o700)
    commit = _git("rev-parse", "refs/heads/main", cwd=git_remote)
    (legacy / f"{commit}.json").write_text("{}\n")
    snapshot = SourceStore(state).fetch(_source(git_remote), "refs/heads/main")
    assert snapshot.commit == commit
    assert not (legacy / f"{commit}.json").exists()


def test_legacy_metadata_parent_symlink_never_unlinks_outside(
    git_remote: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    source_root = state / "sources/example"
    source_root.mkdir(parents=True)
    state.chmod(0o700)
    (state / "sources").chmod(0o700)
    source_root.chmod(0o700)
    outside = tmp_path / "outside-metadata"
    outside.mkdir()
    commit = _git("rev-parse", "refs/heads/main", cwd=git_remote)
    sentinel = outside / f"{commit}.json"
    sentinel.write_text("outside\n")
    (source_root / "snapshot-metadata").symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(RuntimeError, match="legacy snapshot metadata directory"):
        SourceStore(state).fetch(_source(git_remote), "refs/heads/main")
    assert sentinel.read_text() == "outside\n"


def test_snapshot_metadata_publishes_inside_atomic_directory(
    git_remote: Path, tmp_path: Path
) -> None:
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    source_root = tmp_path / "state/sources/example"
    assert (snapshot.root / ".git/agentops-snapshot.json").is_file()
    assert not (source_root / "snapshot-metadata").exists()


def test_per_ref_history_survives_gc_for_exact_rewrite(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    ref = "refs/heads/feat/example"
    previous = store.fetch(source, ref)
    rewritten = _force_rewrite(git_remote, ref, "after gc rewrite\n")
    mirror = tmp_path / "state/sources/example/mirror.git"
    _git("gc", "--prune=now", cwd=mirror)
    accepted = store.fetch(
        source, ref, rewrite=RewriteAcceptance(previous.commit, rewritten)
    )
    assert accepted.commit == rewritten


def test_two_refs_retain_independent_accepted_history(
    git_remote: Path, tmp_path: Path
) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    main = store.fetch(source, "refs/heads/main")
    feature = store.fetch(source, "refs/heads/feat/example")
    mirror = tmp_path / "state/sources/example/mirror.git"
    refs = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=mirror)
    assert hashlib.sha256(b"refs/heads/main").hexdigest() in refs
    assert hashlib.sha256(b"refs/heads/feat/example").hexdigest() in refs
    assert main.commit in refs
    assert feature.commit in refs


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("refs/heads/main", "stable ref history"),
        ("refs/heads/feat/example", "development ref history"),
    ],
)
def test_missing_retained_history_is_deterministic_policy_error(
    git_remote: Path, tmp_path: Path, ref: str, message: str
) -> None:
    store = SourceStore(tmp_path / "state")
    source = _source(git_remote)
    store.fetch(source, ref)
    mirror = tmp_path / "state/sources/example/mirror.git"
    accepted_ref = "refs/agentops/accepted/" + hashlib.sha256(ref.encode()).hexdigest()
    _git("update-ref", "-d", accepted_ref, cwd=mirror)
    _git("reflog", "expire", "--expire=now", "--all", cwd=mirror)
    _git("gc", "--prune=now", cwd=mirror)
    with pytest.raises(RuntimeError, match=message):
        store.fetch(source, ref)


def test_descriptor_closure_pins_entry_and_rejects_replacement(
    git_remote: Path, tmp_path: Path
) -> None:
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    target = snapshot.root / "catalog/skill.txt"
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside secret\n")
    with _open_provider_data_closure(
        snapshot, (Path("catalog/skill.txt"),)
    ) as closure:
        entry = closure.entries[0]
        assert entry.read_bytes() == b"one\n"
        moved = target.with_name("skill.moved")
        target.rename(moved)
        target.symlink_to(outside)
        with pytest.raises(RuntimeError, match="changed during consumption"):
            entry.read_bytes()
    assert closure.closed


def test_descriptor_closure_rejects_in_place_equal_length_byte_change(
    git_remote: Path, tmp_path: Path
) -> None:
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    target = snapshot.root / "catalog/skill.txt"
    with _open_provider_data_closure(
        snapshot, (Path("catalog/skill.txt"),)
    ) as closure:
        entry = closure.entries[0]
        before = target.stat()
        target.write_bytes(b"evil")
        os.utime(
            target,
            ns=(before.st_atime_ns, before.st_mtime_ns),
            follow_symlinks=False,
        )
        with pytest.raises(RuntimeError, match="tracked Git blob"):
            entry.read_bytes()


def test_descriptor_closure_binds_root_before_verification_returns(
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    original = source_store_module._verify_exact_checkout
    moved = snapshot.root.with_name("verified-root")

    def replace_after_verify(*args: object, **kwargs: object) -> object:
        expected = original(*args, **kwargs)
        snapshot.root.rename(moved)
        snapshot.root.mkdir()
        (snapshot.root / "catalog").mkdir()
        (snapshot.root / "catalog/skill.txt").write_bytes(b"outside secret\n")
        return expected

    monkeypatch.setattr(
        source_store_module, "_verify_exact_checkout", replace_after_verify
    )
    with pytest.raises(RuntimeError, match="changed during verification"):
        _open_provider_data_closure(snapshot, (Path("catalog/skill.txt"),))


def test_descriptor_closure_closes_on_process_control(
    git_remote: Path, tmp_path: Path
) -> None:
    class StopConsumption(BaseException):
        pass

    stop = StopConsumption()
    snapshot = SourceStore(tmp_path / "state").fetch(
        _source(git_remote), "refs/heads/main"
    )
    closure = _open_provider_data_closure(
        snapshot, (Path("catalog/skill.txt"),)
    )
    with pytest.raises(StopConsumption) as caught, closure:
        raise stop
    assert caught.value is stop
    assert closure.closed


def test_metadata_replacement_race_rejects_pinned_old_bytes(
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SourceStore(tmp_path / "state")
    snapshot = store.fetch(_source(git_remote), "refs/heads/main")
    metadata = snapshot.root / ".git/agentops-snapshot.json"
    outside = tmp_path / "outside-metadata"
    outside.write_text(metadata.read_text())
    original = source_store_module._read_fd_bytes
    replaced = False

    def replace_after_open(descriptor: int) -> bytes:
        nonlocal replaced
        if not replaced:
            replaced = True
            moved = metadata.with_name("agentops-snapshot.moved")
            metadata.rename(moved)
            metadata.symlink_to(outside)
        return original(descriptor)

    monkeypatch.setattr(source_store_module, "_read_fd_bytes", replace_after_open)
    with pytest.raises(RuntimeError, match="metadata changed during read"):
        store.snapshot("example", snapshot.commit)


def test_git_timeout_kills_complete_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    marker = tmp_path / "child-pid"
    fake_git = binary / "git"
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal; signal.pause()'])\n"
        f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
        "signal.pause()\n"
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(_GitTimeout, match="timed out"):
        _run_git(("version",), timeout=0.2)
    child_pid = int(marker.read_text())
    child_state = Path(f"/proc/{child_pid}/stat")
    try:
        state = child_state.read_text().split()[2]
    except (FileNotFoundError, ProcessLookupError):
        state = "gone"
    assert state in {"gone", "Z"}


def test_git_timeout_kills_descendant_after_leader_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    marker = tmp_path / "descendant-pid"
    fake_git = binary / "git"
    descendant = (
        "import os,signal;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"open({str(marker)!r},'w').write(str(os.getpid()));"
        "os.write(4,b'x');"
        "signal.pause()"
    )
    fake_git.write_text(
        "#!/usr/bin/env python3\n"
        "import os,subprocess,sys\n"
        "read_fd,write_fd=os.pipe()\n"
        f"subprocess.Popen([sys.executable,'-c',{descendant!r}],pass_fds=(write_fd,))\n"
        "os.close(write_fd)\n"
        "os.read(read_fd,1)\n"
        "os._exit(0)\n"
    )
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
    started = time.monotonic()
    with pytest.raises(_GitTimeout, match="timed out"):
        _run_git(("version",), timeout=0.2)
    assert time.monotonic() - started < 2.0
    descendant_pid = int(marker.read_text())
    process_state = Path(f"/proc/{descendant_pid}/stat")
    try:
        state = process_state.read_text().split()[2]
    except (FileNotFoundError, ProcessLookupError):
        state = "gone"
    assert state in {"gone", "Z"}


def test_git_runner_preserves_process_control_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StopGit(KeyboardInterrupt):
        pass

    payload = {"operation": "git version"}
    stop = StopGit("stop requested", payload)
    original_args = stop.args
    communicate_calls = 0
    cleanup_operations: list[str] = []
    cleanup_failure = RuntimeError("cleanup failed")

    class FailingProcess:
        pid = 987654321
        returncode = None
        stdout = None
        stderr = None

        def communicate(self, timeout: float) -> tuple[str, str]:
            nonlocal communicate_calls
            communicate_calls += 1
            if communicate_calls == 1:
                raise stop
            cleanup_operations.append("communicate")
            raise cleanup_failure

        def wait(self, timeout: float) -> int:
            cleanup_operations.append("wait")
            raise cleanup_failure

    monkeypatch.setattr(source_store_module.subprocess, "Popen", lambda *a, **k: FailingProcess())
    with pytest.raises(StopGit) as caught:
        _run_git(("version",), timeout=0.2)
    assert caught.value is stop
    assert caught.value.args == original_args
    assert caught.value.args[1] is payload
    assert communicate_calls > 1
    assert "communicate" in cleanup_operations
    assert "wait" in cleanup_operations


def test_source_store_passes_configured_git_timeout(
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[float] = []
    original = source_store_module._run_git

    def observe_timeout(*args: object, **kwargs: object) -> object:
        observed.append(kwargs["timeout"])
        return original(*args, **kwargs)

    monkeypatch.setattr(source_store_module, "_run_git", observe_timeout)
    SourceStore(tmp_path / "state", git_timeout=3.5).fetch(
        _source(git_remote), "refs/heads/main"
    )
    assert observed and set(observed) == {3.5}


def test_cross_process_same_source_creation_is_coherent(
    git_remote: Path, tmp_path: Path
) -> None:
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    results = context.Queue()
    processes = [
        context.Process(
            target=_process_fetch,
            args=(str(tmp_path / "state"), str(git_remote), barrier, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert not process.is_alive()
        assert process.exitcode == 0
    observed = [results.get(timeout=5) for _ in processes]
    assert {status for status, _ in observed} == {"ok"}
    assert len({commit for _, commit in observed}) == 1


def test_relative_state_root_is_stable_after_chdir(
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starting = tmp_path / "starting"
    later = tmp_path / "later"
    starting.mkdir()
    later.mkdir()
    monkeypatch.chdir(starting)
    observed_paths: list[Path] = []
    original = source_store_module._run_git

    def observe_paths(*args: object, **kwargs: object) -> object:
        git_args = args[0]
        for argument in git_args:
            if "relative-state" in str(argument):
                observed_paths.append(Path(argument))
        if kwargs.get("cwd") is not None:
            observed_paths.append(Path(kwargs["cwd"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(source_store_module, "_run_git", observe_paths)
    store = SourceStore(Path("relative-state"))
    snapshot = store.fetch(_source(git_remote), "refs/heads/main")
    assert snapshot.root.is_absolute()
    monkeypatch.chdir(later)
    loaded = store.snapshot("example", snapshot.commit)
    assert loaded.root == snapshot.root
    assert observed_paths and all(path.is_absolute() for path in observed_paths)
