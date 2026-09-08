"""Exact commit sources preserve snapshot integrity and reject branch substitution."""
from dataclasses import replace
from pathlib import Path

import pytest
from test_deployment_source_store import _git
from test_deployment_source_store import git_remote as _git_remote

import agent_ops.deployment.source_store as module
from agent_ops.deployment.models import SourceSpec
from agent_ops.deployment.source_store import SourceStore, _open_provider_data_closure


@pytest.fixture
def git_remote(tmp_path):
    return _git_remote.__wrapped__(tmp_path)


def test_fetch_pinned_preserves_exact_commit_and_opens_closure(git_remote, tmp_path):
    commit = _git('--git-dir', str(git_remote), 'rev-parse', 'refs/heads/main')
    source = SourceSpec(id='pinned', url=str(git_remote), stable_ref='refs/heads/main')
    store = SourceStore(tmp_path / 'state')
    snapshot = store.fetch_pinned(source, commit)
    assert snapshot.ref == snapshot.commit == commit
    with _open_provider_data_closure(snapshot, (Path('catalog/skill.txt'),)) as closure:
        assert closure.entries[0].read_bytes() == b'one\n'
    assert store.snapshot(source.id, commit) == snapshot
    assert store.fetch_pinned(source, commit) == snapshot
    with pytest.raises(ValueError):
        _open_provider_data_closure(replace(snapshot, ref='0' * 40), (Path('catalog/skill.txt'),))
    (snapshot.root / 'catalog/skill.txt').write_text('changed')
    with pytest.raises(RuntimeError, match='bytes differ'):
        store.fetch_pinned(source, commit)


def test_missing_pinned_commit_never_falls_back_to_branch(git_remote, tmp_path):
    source = SourceSpec(id='pinned', url=str(git_remote), stable_ref='refs/heads/main')
    store = SourceStore(tmp_path / 'state')
    with pytest.raises(RuntimeError):
        store.fetch_pinned(source, '0' * 40)
    assert not list((tmp_path / 'state').glob('sources/*/snapshots/*'))


def test_pinned_fetch_refuses_wrong_fetched_commit(git_remote, tmp_path, monkeypatch):
    source = SourceSpec(id='pinned', url=str(git_remote), stable_ref='refs/heads/main')
    store = SourceStore(tmp_path / 'state')
    monkeypatch.setattr(store, '_fetch_candidate', lambda *args: '1' * 40)
    with pytest.raises(RuntimeError, match='requested commit'):
        store.fetch_pinned(source, '2' * 40)
    assert not list((tmp_path / 'state').glob('sources/*/snapshots/*'))


@pytest.mark.parametrize('commit', ['main', 'refs/heads/main', 'abc123', 'A' * 40])
def test_invalid_pins_refuse_before_state_writes(tmp_path, commit):
    source = SourceSpec(id='pinned', url=str(tmp_path), stable_ref='refs/heads/main')
    with pytest.raises(ValueError):
        SourceStore(tmp_path / 'state').fetch_pinned(source, commit)
    assert not (tmp_path / 'state').exists()


def test_windows_pinned_fetch_refuses_before_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(module, '_WINDOWS_SUPPORTED', True)
    source = SourceSpec(id='pinned', url=str(tmp_path), stable_ref='refs/heads/main')
    with pytest.raises(RuntimeError, match='unsupported'):
        SourceStore(tmp_path / 'state').fetch_pinned(source, '0' * 40)
    assert not (tmp_path / 'state').exists()
