from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import agent_ops.deployment.source_store as source_store_module
from agent_ops.deployment.models import RewriteAcceptance, SourceSnapshot, SourceSpec
from agent_ops.deployment.source_store import SourceStore, _validate_provider_data_closure


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
    assert refs <= {"refs/agentops/fetched"}


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
    metadata = tmp_path / f"state/sources/example/snapshot-metadata/{snapshot.commit}.json"
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
    metadata = tmp_path / f"state/sources/example/snapshot-metadata/{snapshot.commit}.json"
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
    mirror.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(RuntimeError, match="mirror"):
        SourceStore(state).fetch(SourceSpec("example", "/missing"), "refs/heads/main")
    assert mirror.is_symlink()
    assert not list((state / "sources/example").glob(".tmp-*"))


def test_git_process_control_exception_propagates_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StopGit(BaseException):
        pass

    stop = StopGit()

    def stop_git(*args: object, **kwargs: object) -> object:
        raise stop

    monkeypatch.setattr(source_store_module, "_run_git", stop_git)
    with pytest.raises(StopGit) as caught:
        SourceStore(tmp_path / "state").fetch(
            SourceSpec("example", "/missing"), "refs/heads/main"
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
    code = snapshot.root / "catalog" / "inert.py"
    code.write_text("raise AssertionError('must not execute')\n")
    validated = _validate_provider_data_closure(
        snapshot, (Path("catalog/skill.txt"), Path("catalog/inert.py"), Path("catalog"))
    )
    assert validated == (
        snapshot.root / "catalog/skill.txt",
        snapshot.root / "catalog/inert.py",
        snapshot.root / "catalog",
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
    with pytest.raises(RuntimeError, match="symlink"):
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
    with pytest.raises(RuntimeError, match="symlink"):
        _validate_provider_data_closure(snapshot, (Path("catalog"),))


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
