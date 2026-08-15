from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from pathlib import Path

import pytest

from agent_ops.deployment import transaction as transaction_module
from agent_ops.deployment.models import PlannedFile, ProviderPlan, TargetSpec
from agent_ops.deployment.transaction import (
    IncompleteRollbackError,
    PublicationIndeterminateError,
    audit_provider_plans,
    install_provider_plans,
    recover_transaction,
    rollback_manifests,
)
from agent_ops.registries.models import Framework


def _plan(
    home: Path,
    *files: PlannedFile,
    provider: str = "fixture",
    revision: str = "1" * 40,
) -> ProviderPlan:
    return ProviderPlan(
        provider_id=provider,
        source_revision=revision,
        target=TargetSpec("codex-dev", Framework.CODEX, home, "feature"),
        files=tuple(files),
    )


def test_transaction_installs_and_audits_exact_plan(tmp_path: Path) -> None:
    target = TargetSpec("codex-dev", Framework.CODEX, tmp_path / "home", "feature")
    plan = ProviderPlan(
        provider_id="fixture",
        source_revision="1" * 40,
        target=target,
        files=(PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644),),
    )

    manifests = install_provider_plans((plan,))

    assert manifests[0].source_revision == "1" * 40
    assert manifests[0].provider_ids == ("fixture",)
    assert manifests[0].files[0].fingerprint == hashlib.sha256(b"body\n").hexdigest()
    assert manifests[0].files[0].mode == 0o644
    assert [directory.path for directory in manifests[0].directories] == [
        Path("skills"),
        Path("skills/example"),
    ]
    assert stat.S_IMODE((target.home / "skills/example/SKILL.md").stat().st_mode) == 0o644
    assert audit_provider_plans((plan,)).matches


def test_transaction_preserves_unknown_files_and_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    unknown = home / "skills/example/notes/private.txt"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b"keep\n")
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    install_provider_plans((plan,))

    assert unknown.read_bytes() == b"keep\n"


def test_transaction_adopts_only_an_exact_unmanaged_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    destination = home / "skills/example/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"body\n")
    destination.chmod(0o644)
    original_inode = destination.stat().st_ino
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    install_provider_plans((plan,))

    assert destination.stat().st_ino == original_inode


@pytest.mark.parametrize("content,mode", [(b"other\n", 0o644), (b"body\n", 0o600)])
def test_transaction_rejects_conflicting_unmanaged_file(
    tmp_path: Path, content: bytes, mode: int
) -> None:
    home = tmp_path / "home"
    destination = home / "skills/example/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(content)
    destination.chmod(mode)
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    with pytest.raises(ValueError, match="unmanaged destination conflicts"):
        install_provider_plans((plan,))

    assert destination.read_bytes() == content
    assert stat.S_IMODE(destination.stat().st_mode) == mode


def test_transaction_rejects_symbolic_link_ancestor(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    home.mkdir()
    (home / "skills").symlink_to(outside, target_is_directory=True)
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    with pytest.raises(OSError):
        install_provider_plans((plan,))

    assert list(outside.iterdir()) == []


def test_transaction_rejects_symbolic_link_destination(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside\n")
    destination = home / "skills/example/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.symlink_to(outside)
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    with pytest.raises(ValueError, match="symbolic link"):
        install_provider_plans((plan,))

    assert destination.is_symlink()
    assert outside.read_bytes() == b"outside\n"


def test_transaction_groups_compatible_provider_plans_deterministically(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    first = _plan(
        home,
        PlannedFile(Path("skills/one/SKILL.md"), b"one\n", 0o644),
        provider="z-provider",
    )
    second = _plan(
        home,
        PlannedFile(Path("skills/two/SKILL.md"), b"two\n", 0o644),
        provider="a-provider",
    )

    manifests = install_provider_plans((first, second))

    assert len(manifests) == 1
    assert manifests[0].provider_ids == ("a-provider", "z-provider")


def test_transaction_rejects_incompatible_plans_for_one_target(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = _plan(home, PlannedFile(Path("skills/one/SKILL.md"), b"one\n", 0o644))
    second = ProviderPlan(
        provider_id="second",
        source_revision="2" * 40,
        target=first.target,
        files=(PlannedFile(Path("skills/two/SKILL.md"), b"two\n", 0o644),),
    )

    with pytest.raises(ValueError, match="incompatible plans"):
        install_provider_plans((first, second))

    assert not home.exists()


def test_transaction_revalidates_forged_paths_before_opening_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    forged = object.__new__(PlannedFile)
    object.__setattr__(forged, "path", Path("../escape"))
    object.__setattr__(forged, "content", b"bad\n")
    object.__setattr__(forged, "mode", 0o644)
    plan = _plan(home, forged)

    with pytest.raises(ValueError, match="unsafe managed path"):
        install_provider_plans((plan,))

    assert not home.exists()
    assert not (tmp_path / "escape").exists()


def test_empty_plans_do_not_touch_any_home(tmp_path: Path) -> None:
    before = set(os.listdir(tmp_path))

    assert install_provider_plans(()) == ()
    assert rollback_manifests(()) is None
    assert audit_provider_plans(()).matches is True
    assert set(os.listdir(tmp_path)) == before


def test_transaction_module_has_a_minimal_public_api() -> None:
    assert transaction_module.__all__ == (
        "install_provider_plans",
        "rollback_manifests",
        "audit_provider_plans",
        "recover_transaction",
    )


def test_audit_reports_all_deterministic_difference_categories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = _plan(
        home,
        PlannedFile(
            Path("skills/one/SKILL.md"),
            b"---\nname: duplicate\n---\none\n",
            0o644,
        ),
        PlannedFile(
            Path("skills/two/SKILL.md"),
            b"---\nname: duplicate\n---\ntwo\n",
            0o644,
        ),
        PlannedFile(Path("skills/three/SKILL.md"), b"three\n", 0o644),
        PlannedFile(Path("skills/four/SKILL.md"), b"four\n", 0o644),
        PlannedFile(Path("skills/five/SKILL.md"), b"five\n", 0o644),
    )
    install_provider_plans((plan,))
    (home / "skills/three/SKILL.md").unlink()
    (home / "skills/four/SKILL.md").write_bytes(b"changed\n")
    (home / "skills/five/extra.txt").write_bytes(b"unexpected\n")
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    manifest_path.write_bytes(b"{}\n")

    audit = audit_provider_plans((plan,))

    assert audit.matches is False
    assert audit.missing == ("skills/three/SKILL.md",)
    assert audit.changed == ("skills/four/SKILL.md",)
    assert audit.unexpected == ("skills/five/extra.txt",)
    assert audit.duplicates == (
        "duplicate: skills/one/SKILL.md, skills/two/SKILL.md",
    )
    assert len(audit.validation_errors) == 1
    assert "manifest" in audit.validation_errors[0]


def test_manifest_is_published_last_and_known_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    observed_record: dict[str, object] = {}

    def fail_before_manifest(
        home_fs: object,
        record_path: Path,
        _manifest_temp: Path,
        manifest_path: Path,
    ) -> None:
        assert (home / "skills/example/SKILL.md").read_bytes() == b"body\n"
        assert not (home / manifest_path).exists()
        observed_record.update(json.loads((home / record_path).read_text()))
        raise OSError("injected before manifest publication")

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", fail_before_manifest)

    with pytest.raises(OSError, match="injected before manifest publication"):
        install_provider_plans((plan,))

    assert observed_record["state"] == "prepared"
    assert not (home / "skills/example/SKILL.md").exists()
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))
    assert not list((home / ".agentops/deployment/transactions").glob("*/record.json"))


@pytest.mark.parametrize("control", [KeyboardInterrupt(), SystemExit(4)])
def test_process_control_exceptions_propagate_after_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: BaseException,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    def interrupt(*_args: object) -> None:
        raise control

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", interrupt)

    with pytest.raises(type(control)):
        install_provider_plans((plan,))

    assert not (home / "skills/example/SKILL.md").exists()


def test_same_home_operations_are_serialized_without_sleep_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    call_lock = threading.Lock()
    calls = 0

    def coordinate(*_args: object) -> None:
        nonlocal calls
        with call_lock:
            calls += 1
            current = calls
        if current == 1:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", coordinate)
    failures: list[BaseException] = []

    def install() -> None:
        try:
            install_provider_plans((plan,))
        except BaseException as exc:  # pragma: no cover - asserted after join
            failures.append(exc)

    first = threading.Thread(target=install)
    second = threading.Thread(target=install)
    first.start()
    assert first_entered.wait(timeout=5)
    second.start()
    assert not second_entered.wait(timeout=0.2)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert second_entered.is_set()


def test_rollback_restores_exact_prior_files_modes_manifest_and_directories(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    original = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o600),
    )
    install_provider_plans((original,))
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    prior_manifest = manifest_path.read_bytes()
    replacement = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),
        PlannedFile(Path("skills/new/nested/data.bin"), b"new data\n", 0o600),
        revision="2" * 40,
    )
    manifests = install_provider_plans((replacement,))

    rollback_manifests(manifests)

    restored = home / "skills/example/SKILL.md"
    assert restored.read_bytes() == b"old\n"
    assert stat.S_IMODE(restored.stat().st_mode) == 0o600
    assert manifest_path.read_bytes() == prior_manifest
    assert not (home / "skills/new").exists()


def test_indeterminate_publication_retains_evidence_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    def lose_publication_identity(
        home_fs: object,
        _record_path: Path,
        manifest_temp: Path,
        _manifest_path: Path,
    ) -> None:
        home_fs.replace(manifest_temp, Path(".agentops/deployment/lost-manifest"))
        raise OSError("publication result unavailable")

    monkeypatch.setattr(
        transaction_module,
        "_before_manifest_replace",
        lose_publication_identity,
    )

    with pytest.raises(PublicationIndeterminateError, match="indeterminate"):
        install_provider_plans((plan,))

    records = list((home / ".agentops/deployment/transactions").glob("*/record.json"))
    assert len(records) == 1
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))
    assert (home / "skills/example/SKILL.md").read_bytes() == b"body\n"

    recovered = recover_transaction(records[0])

    assert recovered.source_revision == "1" * 40
    assert audit_provider_plans((plan,)).matches


def test_incomplete_rollback_preserves_changed_content_and_evidence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    manifests = install_provider_plans((plan,))
    destination = home / "skills/example/SKILL.md"
    destination.write_bytes(b"person changed this\n")

    with pytest.raises(IncompleteRollbackError, match="rollback incomplete"):
        rollback_manifests(manifests)

    assert destination.read_bytes() == b"person changed this\n"
    records = list((home / ".agentops/deployment/transactions").glob("*/record.json"))
    assert len(records) == 1


def test_recover_transaction_validates_record_before_filesystem_actions(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    manifest = install_provider_plans((plan,))[0]
    record = next((home / ".agentops/deployment/transactions").glob("*/record.json"))

    assert recover_transaction(record) == manifest

    data = json.loads(record.read_text())
    data["operations"][0]["destination"] = "../escape"
    record.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="unsafe"):
        recover_transaction(record)

    assert (home / "skills/example/SKILL.md").read_bytes() == b"body\n"
    assert not (tmp_path / "escape").exists()


def test_recover_transaction_rejects_malformed_record(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    record = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record.write_text("{}\n")

    with pytest.raises(ValueError, match="transaction record"):
        recover_transaction(record)

    assert (home / "skills/example/SKILL.md").read_bytes() == b"body\n"
