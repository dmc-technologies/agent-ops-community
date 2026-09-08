from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_ops.deployment import models
from agent_ops.deployment import transaction as tx


def plan(home: Path, generation: str, *, retained=()):
    assert hasattr(models, "SharedSelectionActivation"), "shared activation model is missing"
    root = Path("snapshots") / (generation * 64)
    return models.ProviderPlan(
        provider_id="shared-skills",
        source_revision=generation * 64,
        target=models.SharedTargetSpec("shared-skills", home),
        files=(
            *retained,
            models.PlannedFile(root / "skills/example/SKILL.md", generation.encode(), 0o644),
        ),
        audit_roots=(root,),
        selection_activation=models.SharedSelectionActivation(root),
    )


def test_activate_and_rollback_retains_previous_snapshot(tmp_path):
    first = plan(tmp_path / "home", "a")
    tx.install_provider_plans((first,))
    second = plan(first.target.home, "b", retained=first.files)
    manifests = tx.install_provider_plans((second,))
    assert (first.target.home / "current/skills/example/SKILL.md").read_bytes() == b"b"
    assert (first.target.home / first.files[0].path).read_bytes() == b"a"
    assert tx.audit_provider_plans((second,)).matches
    tx.rollback_manifests(manifests)
    assert (first.target.home / "current/skills/example/SKILL.md").read_bytes() == b"a"
    assert tx.audit_provider_plans((first,)).matches


def test_unknown_selector_refused_without_replacing_it(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "current").symlink_to("foreign")
    candidate = plan(home, "a")
    with pytest.raises(ValueError, match="selector"):
        tx.install_provider_plans((candidate,))
    assert os.readlink(home / "current") == "foreign"
    assert not (home / "snapshots").exists()


def test_failure_after_selector_activation_restores_previous(tmp_path, monkeypatch):
    first = plan(tmp_path / "home", "a")
    tx.install_provider_plans((first,))
    second = plan(first.target.home, "b", retained=first.files)

    def fail(*args):
        assert os.readlink(first.target.home / "current") == "snapshots/" + "b" * 64
        raise OSError("injected after activation")

    monkeypatch.setattr(tx, "_before_manifest_replace", fail)
    with pytest.raises(OSError, match="injected"):
        tx.install_provider_plans((second,))
    assert os.readlink(first.target.home / "current") == "snapshots/" + "a" * 64
    assert tx.audit_provider_plans((first,)).matches


def test_shared_activation_refuses_windows_before_writes(tmp_path, monkeypatch):
    candidate = plan(tmp_path / "absent", "a")
    monkeypatch.setattr(tx, "_POSIX_SUPPORTED", False)
    monkeypatch.setattr(tx, "_WINDOWS_SUPPORTED", True)
    with pytest.raises(tx.UnsupportedPlatformError, match="shared"):
        tx.install_provider_plans((candidate,))
    assert not candidate.target.home.exists()


def test_durable_committed_recovery_with_empty_process_cache(tmp_path):
    candidate = plan(tmp_path / "home", "a")
    (manifest,) = tx.install_provider_plans((candidate,))
    journal = tx._TRANSACTION_PATHS[manifest.transaction_id]
    tx._TRANSACTION_PATHS.clear()
    assert tx.recover_transaction(journal) == manifest
    tx.rollback_manifests((manifest,))
    assert not (candidate.target.home / "current").is_symlink()


def test_changed_selector_is_reported_by_audit(tmp_path):
    candidate = plan(tmp_path / "home", "a")
    tx.install_provider_plans((candidate,))
    selector = candidate.target.home / "current"
    selector.unlink()
    selector.symlink_to("foreign")
    audit = tx.audit_provider_plans((candidate,))
    assert not audit.matches
    assert any("selector" in error for error in audit.validation_errors)


def test_selector_substitution_before_publish_is_preserved(tmp_path, monkeypatch):
    first = plan(tmp_path / "home", "a")
    tx.install_provider_plans((first,))
    second = plan(first.target.home, "b", retained=first.files)

    def replace(home_fs, record):
        selector = home_fs.home / "current"
        selector.unlink()
        selector.symlink_to("foreign")

    monkeypatch.setattr(tx, "_before_shared_selector_replace", replace)
    with pytest.raises(tx.IncompleteRollbackError, match="rollback incomplete"):
        tx.install_provider_plans((second,))
    assert os.readlink(first.target.home / "current") == "foreign"
    assert (first.target.home / first.files[0].path).read_bytes() == b"a"


def test_selector_parent_symlink_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.symlink_to(outside, target_is_directory=True)
    candidate = plan(home, "a")
    with pytest.raises((OSError, ValueError)):
        tx.install_provider_plans((candidate,))
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "boundary",
    [
        "_before_shared_selector_replace",
        "_after_shared_selector_replace",
        "_before_committed_record_write",
    ],
)
def test_process_exit_recovery(tmp_path, boundary):
    import subprocess
    import sys

    home = tmp_path / "home"
    first = plan(home, "a")
    tx.install_provider_plans((first,))
    script = """
import os, sys
from pathlib import Path
from agent_ops.deployment.models import (
    SharedTargetSpec, SharedSelectionActivation, PlannedFile, ProviderPlan
)
from agent_ops.deployment import transaction as tx
home=Path(sys.argv[1])
files=tuple(PlannedFile(Path('snapshots')/(g*64)/'skills/example/SKILL.md',
                      g.encode(),0o644) for g in ('a','b'))
plan=ProviderPlan('shared-skills','b'*64,SharedTargetSpec('shared-skills',home),files,audit_roots=(Path('snapshots')/('b'*64),),selection_activation=SharedSelectionActivation(Path('snapshots')/('b'*64)))
setattr(tx,sys.argv[2],lambda *args: os._exit(71))
tx.install_provider_plans((plan,))
"""
    result = subprocess.run([sys.executable, "-c", script, str(home), boundary], check=False)
    assert result.returncode == 71
    import json

    records = list((home / ".agentops/deployment/transactions").glob("*/record.json"))
    # The two generations identify the interrupted transaction independently of ordering.
    candidate_records = [
        path
        for path in records
        if json.loads(path.read_text())["manifest"]["source_revision"] == "b" * 64
    ]
    assert len(candidate_records) == 1
    tx._TRANSACTION_PATHS.clear()
    recovered = tx.recover_transaction(candidate_records[0])
    expected = "b" if boundary == "_before_committed_record_write" else "a"
    assert (home / "current/skills/example/SKILL.md").read_bytes() == expected.encode()
    assert tx.recover_transaction(candidate_records[0]) == recovered
    if expected == "b":
        tx.rollback_manifests((recovered,))
        assert (home / "current/skills/example/SKILL.md").read_bytes() == b"a"


def test_shared_manifest_rejects_unconfined_owned_files(tmp_path):
    import json

    candidate = plan(tmp_path / "home", "a")
    (manifest,) = tx.install_provider_plans((candidate,))
    data = tx._manifest_to_dict(manifest)
    data["files"][0]["path"] = "outside/SKILL.md"
    data["directories"] = [{"path": "outside", "mode": 0o755}]
    with pytest.raises(ValueError, match="snapshot"):
        tx._validated_manifest_data(json.dumps(data).encode(), target=candidate.target)


def test_substitution_at_atomic_exchange_preserves_foreign_selector(tmp_path, monkeypatch):
    assert hasattr(tx, "_exchange_shared_selector"), "atomic shared selector exchange missing"
    first = plan(tmp_path / "home", "a")
    tx.install_provider_plans((first,))
    second = plan(first.target.home, "b", retained=first.files)
    original = tx._exchange_shared_selector
    calls = []

    def substitute(home_fs, staged):
        if not calls:
            calls.append(True)
            (home_fs.home / "current").unlink()
            (home_fs.home / "current").symlink_to("foreign")
        return original(home_fs, staged)

    monkeypatch.setattr(tx, "_exchange_shared_selector", substitute)
    with pytest.raises(tx.IncompleteRollbackError):
        tx.install_provider_plans((second,))
    assert calls == [True]
    assert os.readlink(first.target.home / "current") == "foreign"


def test_candidate_drift_before_activation_keeps_previous_selection(tmp_path, monkeypatch):
    first = plan(tmp_path / "home", "a")
    tx.install_provider_plans((first,))
    second = plan(first.target.home, "b", retained=first.files)

    def mutate(home_fs, record):
        (home_fs.home / second.files[-1].path).write_bytes(b"changed")

    monkeypatch.setattr(tx, "_before_shared_selector_replace", mutate)
    observations = []
    monkeypatch.setattr(
        tx, "_after_shared_selector_replace", lambda *args: observations.append("activated")
    )
    with pytest.raises(tx.IncompleteRollbackError):
        tx.install_provider_plans((second,))
    assert observations == []
    assert os.readlink(first.target.home / "current") == "snapshots/" + "a" * 64


def test_shared_manifest_revision_must_match_active_snapshot(tmp_path):
    import json

    candidate = plan(tmp_path / "home", "a")
    (manifest,) = tx.install_provider_plans((candidate,))
    data = tx._manifest_to_dict(manifest)
    data["source_revision"] = "b" * 64
    with pytest.raises(ValueError, match="snapshot"):
        tx._validated_manifest_data(json.dumps(data).encode(), target=candidate.target)


@pytest.mark.skipif(os.name != 'posix', reason='POSIX descriptor limits')
@pytest.mark.parametrize('operation', ['read', 'retain'])
def test_shared_evidence_bounds_descriptors_for_large_snapshots(tmp_path, operation):
    import dataclasses
    import subprocess
    import sys

    initial = plan(tmp_path / 'home', 'a')
    prefix = Path('snapshots') / ('a' * 64)
    candidate = dataclasses.replace(initial, files=initial.files + tuple(
        models.PlannedFile(prefix / f'resource-{i}.txt', b'resource', 0o644)
        for i in range(160)
    ))
    tx.install_provider_plans((candidate,))
    script = '''
import resource, sys
from pathlib import Path
from agent_ops.deployment import models, transaction as tx
home = Path(sys.argv[1])
prefix = Path('snapshots') / ('a' * 64)
target = models.SharedTargetSpec('shared-skills', home)
files = (models.PlannedFile(prefix / 'skills/example/SKILL.md', b'a', 0o644),) + tuple(
    models.PlannedFile(prefix / f'resource-{i}.txt', b'resource', 0o644) for i in range(160)
)
plan = models.ProviderPlan('shared-skills', 'a' * 64, target, files,
    audit_roots=(prefix,), selection_activation=models.SharedSelectionActivation(prefix))
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (64, hard))
if sys.argv[2] == 'read':
    manifest, audit, observed = tx.read_shared_target_evidence(target)
    assert manifest is not None and audit.matches and len(observed) == 161
else:
    with tx._locked_provider_plan_targets((plan,)):
        with tx.retain_provider_plan_evidence((plan,)) as evidence:
            evidence.verify()
print('verified 161 files with a 64-descriptor limit')
'''
    observed = subprocess.run([sys.executable, '-c', script, str(candidate.target.home), operation],
        capture_output=True, text=True, timeout=30)
    assert observed.returncode == 0, observed.stderr
    assert 'verified 161 files' in observed.stdout


@pytest.mark.parametrize('change', ['replacement', 'restore-bytes-and-mtime'])
def test_bounded_evidence_rejects_changed_identity_even_when_bytes_match(tmp_path, change):
    candidate = plan(tmp_path / 'home', 'a')
    tx.install_provider_plans((candidate,))
    path = candidate.target.home / candidate.files[0].path
    with (
        tx._locked_provider_plan_targets((candidate,)),
        tx.retain_provider_plan_evidence((candidate,)) as evidence,
    ):
        before = path.stat()
        if change == 'replacement':
            replacement = path.with_name('replacement')
            replacement.write_bytes(path.read_bytes())
            replacement.chmod(0o644)
            os.replace(replacement, path)
        else:
            path.write_bytes(b'changed')
            path.write_bytes(b'a')
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.read_bytes() == b'a'
        with pytest.raises(ValueError, match='retained audit evidence changed'):
            evidence.verify()
