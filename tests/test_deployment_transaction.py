from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import socket
import stat
import threading
from collections.abc import Callable
from contextlib import suppress
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


def _run_bounded(function: Callable[[], object]) -> tuple[str, object]:
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)

    def invoke() -> None:
        receiver.close()
        try:
            function_result = function()
        except BaseException as exc:
            sender.send((type(exc).__name__, str(exc)))
        else:
            sender.send(("returned", function_result))
        finally:
            sender.close()

    process = context.Process(target=invoke)
    process.start()
    sender.close()
    process.join(timeout=2)
    if process.is_alive():
        process.terminate()
        process.join(timeout=2)
        pytest.fail("filesystem operation exceeded bounded deadline")
    assert process.exitcode == 0
    assert receiver.poll()
    result = receiver.recv()
    receiver.close()
    return result


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


def test_rollback_requires_every_recorded_backup_before_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    original = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o600),
    )
    install_provider_plans((original,))
    replacement = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),
        revision="2" * 40,
    )
    manifests = install_provider_plans((replacement,))
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    backup_path = home / record["operations"][0]["backup"]
    backup_path.unlink()
    installed_before = (home / "skills/example/SKILL.md").read_bytes()
    manifest_before = manifest_path.read_bytes()
    record_before = record_path.read_bytes()

    with pytest.raises(IncompleteRollbackError, match="backup.*missing"):
        rollback_manifests(manifests)

    assert (home / "skills/example/SKILL.md").read_bytes() == installed_before
    assert manifest_path.read_bytes() == manifest_before
    assert record_path.read_bytes() == record_before


@pytest.mark.parametrize(
    "case",
    [
        "forged-destination",
        "missing-operation",
        "extra-operation",
        "wrong-fingerprint",
        "wrong-mode",
        "wrong-kind",
    ],
)
def test_record_operations_are_bound_to_recorded_manifests(
    tmp_path: Path,
    case: str,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    manifests = install_provider_plans((plan,))
    managed = home / "skills/example/SKILL.md"
    unmanaged = home / "unmanaged.txt"
    unmanaged.write_bytes(b"body\n")
    unmanaged.chmod(0o644)
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    operation = record["operations"][0]
    transaction = Path(".agentops/deployment/transactions") / manifests[0].transaction_id
    if case == "forged-destination":
        operation["destination"] = "unmanaged.txt"
        operation["staged"] = (transaction / "rendered/unmanaged.txt").as_posix()
    elif case == "missing-operation":
        record["operations"] = []
    elif case == "extra-operation":
        extra = dict(operation)
        extra["destination"] = "unmanaged.txt"
        extra["staged"] = (transaction / "rendered/unmanaged.txt").as_posix()
        record["operations"].append(extra)
    elif case == "wrong-fingerprint":
        operation["expected_fingerprint"] = "0" * 64
    elif case == "wrong-mode":
        operation["expected_mode"] = 0o600
    else:
        operation.update(
            kind="removal",
            staged=None,
            expected_fingerprint=None,
            expected_mode=None,
        )
    record_path.write_text(json.dumps(record))

    with pytest.raises(ValueError, match="operations.*manifest"):
        rollback_manifests(manifests)

    assert managed.read_bytes() == b"body\n"
    assert unmanaged.read_bytes() == b"body\n"
    assert record_path.exists()


def test_record_rejects_reused_backup_path_before_rollback(tmp_path: Path) -> None:
    home = tmp_path / "home"
    original = _plan(
        home,
        PlannedFile(Path("skills/one/SKILL.md"), b"old one\n", 0o600),
        PlannedFile(Path("skills/two/SKILL.md"), b"old two\n", 0o600),
    )
    install_provider_plans((original,))
    replacement = _plan(
        home,
        PlannedFile(Path("skills/one/SKILL.md"), b"new one\n", 0o644),
        PlannedFile(Path("skills/two/SKILL.md"), b"new two\n", 0o644),
        revision="2" * 40,
    )
    manifests = install_provider_plans((replacement,))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    record["operations"][1]["backup"] = record["operations"][0]["backup"]
    record_path.write_text(json.dumps(record))

    with pytest.raises(ValueError, match="duplicate transaction backup"):
        rollback_manifests(manifests)

    assert (home / "skills/one/SKILL.md").read_bytes() == b"new one\n"
    assert (home / "skills/two/SKILL.md").read_bytes() == b"new two\n"
    assert record_path.exists()


def test_record_rejects_removed_required_backup_reference(tmp_path: Path) -> None:
    home = tmp_path / "home"
    original = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o600),
    )
    install_provider_plans((original,))
    replacement = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),
        revision="2" * 40,
    )
    manifests = install_provider_plans((replacement,))
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    record["operations"][0]["backup"] = None
    record_path.write_text(json.dumps(record))
    installed_before = (home / "skills/example/SKILL.md").read_bytes()
    manifest_before = manifest_path.read_bytes()

    with pytest.raises(ValueError, match="operations.*prior manifest"):
        rollback_manifests(manifests)

    assert (home / "skills/example/SKILL.md").read_bytes() == installed_before
    assert manifest_path.read_bytes() == manifest_before
    assert record_path.exists()


@pytest.mark.parametrize("case", ["missing", "extra", "duplicate", "wrong-mode"])
def test_record_directories_are_bound_to_manifest(
    tmp_path: Path,
    case: str,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    if case == "missing":
        record["directories"].pop()
    elif case == "extra":
        record["directories"].append(
            {"path": "unmanaged", "created": True, "mode": 0o755}
        )
    elif case == "duplicate":
        record["directories"].append(dict(record["directories"][0]))
    else:
        record["directories"][0]["mode"] = 0o700
    record_path.write_text(json.dumps(record))

    with pytest.raises(ValueError, match="directories.*manifest"):
        recover_transaction(record_path)

    assert (home / "skills/example/SKILL.md").read_bytes() == b"body\n"
    assert record_path.exists()


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("target", "deployment manifest target does not match plan"),
        ("framework", "deployment manifest target does not match plan"),
        ("source", "deployment manifest source revision does not match plan"),
        ("providers", "deployment manifest providers do not match plan"),
        ("files", "deployment manifest files do not match plan"),
        (
            "directories",
            "deployment manifest directories do not match installed directories",
        ),
    ],
)
def test_audit_rejects_validly_typed_manifest_provenance_drift(
    tmp_path: Path,
    case: str,
    expected_error: str,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    manifest = json.loads(manifest_path.read_text())
    if case == "target":
        manifest["target_id"] = "other-target"
    elif case == "framework":
        manifest["framework"] = Framework.CLAUDE_CODE.value
    elif case == "source":
        manifest["source_revision"] = "2" * 40
    elif case == "providers":
        manifest["provider_ids"] = ["other-provider"]
    elif case == "files":
        manifest["files"][0]["fingerprint"] = "0" * 64
    else:
        manifest["directories"][0]["mode"] = 0o700
    manifest_path.write_text(json.dumps(manifest))

    audit = audit_provider_plans((plan,))

    assert audit.matches is False
    assert audit.validation_errors == (expected_error,)


@pytest.mark.parametrize("case", ["keyboard", "system-exit"])
def test_failed_install_rollback_preserves_original_process_control_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    control: BaseException = (
        KeyboardInterrupt("operator stop") if case == "keyboard" else SystemExit(17)
    )

    def interrupt_after_changing_output(*_args: object) -> None:
        (home / "skills/example/SKILL.md").write_bytes(b"changed during failure\n")
        raise control

    monkeypatch.setattr(
        transaction_module,
        "_before_manifest_replace",
        interrupt_after_changing_output,
    )

    with pytest.raises(type(control)) as caught:
        install_provider_plans((plan,))

    assert caught.value is control
    assert caught.value.args == control.args
    assert isinstance(caught.value.__cause__, IncompleteRollbackError)
    assert any("recovery evidence retained" in note for note in caught.value.__notes__)
    assert (home / "skills/example/SKILL.md").read_bytes() == b"changed during failure\n"
    assert len(list((home / ".agentops/deployment/transactions").glob("*/record.json"))) == 1


@pytest.mark.parametrize(
    "case",
    [
        "boolean-schema",
        "float-schema",
        "boolean-file-mode",
        "boolean-directory-mode",
        "negative-mode",
        "special-mode",
        "duplicate-provider",
        "duplicate-file",
        "duplicate-directory",
        "invalid-transaction",
        "non-normalized-path",
    ],
)
def test_install_strictly_validates_existing_manifest_before_mutation(
    tmp_path: Path,
    case: str,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    destination = home / "skills/example/SKILL.md"
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    manifest = json.loads(manifest_path.read_text())
    if case == "boolean-schema":
        manifest["schema_version"] = True
    elif case == "float-schema":
        manifest["schema_version"] = 1.0
    elif case == "boolean-file-mode":
        manifest["files"][0]["mode"] = True
    elif case == "boolean-directory-mode":
        manifest["directories"][0]["mode"] = True
    elif case == "negative-mode":
        manifest["directories"][0]["mode"] = -1
    elif case == "special-mode":
        manifest["files"][0]["mode"] = 0o1000
    elif case == "duplicate-provider":
        manifest["provider_ids"].append(manifest["provider_ids"][0])
    elif case == "duplicate-file":
        manifest["files"].append(dict(manifest["files"][0]))
    elif case == "duplicate-directory":
        manifest["directories"].append(dict(manifest["directories"][0]))
    elif case == "invalid-transaction":
        manifest["transaction_id"] = "not-a-transaction"
    else:
        manifest["files"][0]["path"] = "skills//example/SKILL.md"
    manifest_path.write_text(json.dumps(manifest))
    tampered_manifest = manifest_path.read_bytes()
    destination_stat = destination.stat()
    record_paths = set((home / ".agentops/deployment/transactions").glob("*/record.json"))

    with pytest.raises(ValueError, match="invalid deployment manifest"):
        install_provider_plans((plan,))

    assert destination.read_bytes() == b"body\n"
    assert destination.stat().st_ino == destination_stat.st_ino
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644
    assert manifest_path.read_bytes() == tampered_manifest
    assert set((home / ".agentops/deployment/transactions").glob("*/record.json")) == record_paths


def test_record_rejects_erased_existing_file_prestate(tmp_path: Path) -> None:
    home = tmp_path / "home"
    original = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o600),
    )
    install_provider_plans((original,))
    replacement = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),
        revision="2" * 40,
    )
    manifests = install_provider_plans((replacement,))
    destination = home / "skills/example/SKILL.md"
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    operation = record["operations"][0]
    operation["backup"] = None
    operation["prior_fingerprint"] = None
    operation["prior_mode"] = None
    record_path.write_text(json.dumps(record))
    installed_before = destination.read_bytes()
    manifest_before = manifest_path.read_bytes()

    with pytest.raises(ValueError, match="prior existence"):
        rollback_manifests(manifests)

    assert destination.read_bytes() == installed_before
    assert manifest_path.read_bytes() == manifest_before
    assert record_path.exists()


def test_record_rejects_flipped_preexisting_directory_state(tmp_path: Path) -> None:
    home = tmp_path / "home"
    directory = home / "skills/example"
    directory.mkdir(parents=True)
    directory.chmod(0o755)
    directory_inode = directory.stat().st_ino
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    manifests = install_provider_plans((plan,))
    destination = directory / "SKILL.md"
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    directory_record = next(
        item for item in record["directories"] if item["path"] == "skills/example"
    )
    assert directory_record["created"] is False
    directory_record["created"] = True
    record_path.write_text(json.dumps(record))
    manifest_before = manifest_path.read_bytes()

    with pytest.raises(ValueError, match="directory pre-state"):
        rollback_manifests(manifests)

    assert directory.exists()
    assert directory.stat().st_ino == directory_inode
    assert destination.read_bytes() == b"body\n"
    assert manifest_path.read_bytes() == manifest_before
    assert record_path.exists()


@pytest.mark.parametrize("case", ["missing", "orphan", "duplicate"])
def test_directory_prestate_evidence_is_one_to_one(
    tmp_path: Path,
    case: str,
) -> None:
    home = tmp_path / "home"
    directory = home / "skills/example"
    directory.mkdir(parents=True)
    directory.chmod(0o755)
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    manifests = install_provider_plans((plan,))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    evidence_directory = record_path.parent / "prestate/directories"
    markers = sorted(evidence_directory.glob("*.json"))
    assert markers, "install must retain independent directory pre-state evidence"
    if case == "missing":
        markers[0].unlink()
    elif case == "orphan":
        (evidence_directory / "orphan.json").write_text("{}\n")
    else:
        (evidence_directory / "duplicate.json").write_bytes(markers[0].read_bytes())

    with pytest.raises(ValueError, match="directory pre-state evidence"):
        rollback_manifests(manifests)

    assert (home / "skills/example/SKILL.md").read_bytes() == b"body\n"
    assert record_path.exists()


def test_record_rejects_orphan_backup_evidence(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    manifests = install_provider_plans((plan,))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    backup_directory = record_path.parent / "backups"
    (backup_directory / "orphan").write_bytes(b"orphan\n")

    with pytest.raises(ValueError, match="backup evidence"):
        rollback_manifests(manifests)

    assert (home / "skills/example/SKILL.md").read_bytes() == b"body\n"
    assert record_path.exists()


def test_recovery_rejects_boolean_transaction_schema_before_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    record["schema_version"] = True
    record_path.write_text(json.dumps(record))

    with pytest.raises(ValueError, match="transaction record schema"):
        recover_transaction(record_path)

    assert (home / "skills/example/SKILL.md").read_bytes() == b"body\n"
    assert record_path.exists()


def test_mode_0777_install_and_rollback_round_trip(tmp_path: Path) -> None:
    home = tmp_path / "home"
    destination = home / "skills/example/run"
    plan = _plan(home, PlannedFile(Path("skills/example/run"), b"#!/bin/sh\n", 0o777))

    manifests = install_provider_plans((plan,))

    assert destination.read_bytes() == b"#!/bin/sh\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o777
    rollback_manifests(manifests)
    assert not destination.exists()


@pytest.mark.parametrize("mode", [True, 0o1000])
def test_planned_file_rejects_boolean_and_special_modes_before_mutation(
    tmp_path: Path,
    mode: int,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", mode))

    with pytest.raises(ValueError, match="invalid planned file"):
        install_provider_plans((plan,))

    assert not home.exists()


@pytest.mark.parametrize("mode", [True, 0o1000])
def test_recovery_rejects_boolean_and_special_directory_modes(
    tmp_path: Path,
    mode: int,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    record["directories"][0]["mode"] = mode
    record_path.write_text(json.dumps(record))

    with pytest.raises(ValueError, match="invalid transaction record directory"):
        recover_transaction(record_path)

    assert (home / "skills/example/SKILL.md").read_bytes() == b"body\n"


@pytest.mark.parametrize("mode", [0o000, 0o111, 0o222, 0o333])
def test_file_modes_without_owner_read_are_rejected_before_mutation(
    tmp_path: Path,
    mode: int,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/file"), b"body\n", mode))

    with pytest.raises(ValueError, match="invalid planned file"):
        install_provider_plans((plan,))

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("mode", [0o400, 0o600, 0o777])
def test_owner_readable_file_modes_install_audit_and_rollback(
    tmp_path: Path,
    mode: int,
) -> None:
    home = tmp_path / "home"
    destination = home / "skills/example/SKILL.md"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", mode))

    manifests = install_provider_plans((plan,))
    installed = destination.stat()
    audit = audit_provider_plans((plan,))
    audited = destination.stat()

    assert audit.matches
    assert stat.S_IMODE(installed.st_mode) == mode
    assert (audited.st_ino, audited.st_mode, audited.st_mtime_ns) == (
        installed.st_ino,
        installed.st_mode,
        installed.st_mtime_ns,
    )
    rollback_manifests(manifests)
    assert not destination.exists()


@pytest.mark.parametrize("operation", ["rollback", "recover"])
@pytest.mark.parametrize("tree", ["backups", "prestate/directories"])
def test_unexpected_empty_evidence_directory_is_rejected_before_mutation(
    tmp_path: Path,
    operation: str,
    tree: str,
) -> None:
    home = tmp_path / "home"
    destination = home / "skills/example/SKILL.md"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    manifests = install_provider_plans((plan,))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    unexpected = record_path.parent / tree / "unexpected"
    unexpected.mkdir()
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    before = (destination.read_bytes(), manifest_path.read_bytes(), record_path.read_bytes())

    with pytest.raises(ValueError, match="evidence"):
        if operation == "rollback":
            rollback_manifests(manifests)
        else:
            recover_transaction(record_path)

    assert unexpected.is_dir()
    after = (destination.read_bytes(), manifest_path.read_bytes(), record_path.read_bytes())
    assert after == before


@pytest.mark.parametrize("entry_kind", ["symlink", "fifo", "socket"])
def test_nonregular_backup_evidence_is_rejected_before_recovery(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    home = tmp_path / "home"
    destination = home / "skills/example/SKILL.md"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    evidence = record_path.parent / "backups" / "unexpected"
    open_socket: socket.socket | None = None
    if entry_kind == "symlink":
        evidence.symlink_to(tmp_path / "outside")
    elif entry_kind == "fifo":
        os.mkfifo(evidence)
    else:
        open_socket = socket.socket(socket.AF_UNIX)
        evidence_directory = os.open(
            evidence.parent,
            os.O_RDONLY | os.O_DIRECTORY,
        )
        try:
            open_socket.bind(f"/proc/self/fd/{evidence_directory}/{evidence.name}")
        finally:
            os.close(evidence_directory)
    try:
        with pytest.raises(ValueError, match="evidence"):
            recover_transaction(record_path)
    finally:
        if open_socket is not None:
            open_socket.close()

    assert destination.read_bytes() == b"body\n"
    assert record_path.exists()


def test_managed_directory_without_owner_read_is_rejected_before_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    directory = home / "skills/example"
    directory.mkdir(parents=True)
    directory.chmod(0o300)
    marker = directory / "unmanaged.txt"
    marker.write_bytes(b"keep\n")
    before = (directory.stat().st_ino, stat.S_IMODE(directory.stat().st_mode))
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    try:
        with pytest.raises(ValueError, match="invalid managed directory mode"):
            install_provider_plans((plan,))

        assert (directory.stat().st_ino, stat.S_IMODE(directory.stat().st_mode)) == before
        assert marker.read_bytes() == b"keep\n"
        assert not (home / ".agentops").exists()
    finally:
        directory.chmod(0o700)


@pytest.mark.parametrize("mode", [0o700, 0o777])
def test_owner_auditable_managed_directory_modes_round_trip(
    tmp_path: Path,
    mode: int,
) -> None:
    home = tmp_path / "home"
    directory = home / "skills/example"
    directory.mkdir(parents=True)
    directory.chmod(mode)
    original_inode = directory.stat().st_ino
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    manifests = install_provider_plans((plan,))

    recorded = next(
        item for item in manifests[0].directories if item.path == Path("skills/example")
    )
    assert recorded.mode == mode
    assert audit_provider_plans((plan,)).matches
    rollback_manifests(manifests)
    assert directory.stat().st_ino == original_inode
    assert stat.S_IMODE(directory.stat().st_mode) == mode


def test_wrong_ownership_manifest_mode_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    destination = home / "skills/example/SKILL.md"
    initial = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"before\n", 0o644),
    )
    install_provider_plans((initial,))
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    manifest_path.chmod(0o644)
    transaction_root = home / ".agentops/deployment/transactions"

    def transaction_snapshot() -> dict[Path, tuple[bool, int, bytes | None]]:
        return {
            path.relative_to(transaction_root): (
                path.is_dir(),
                stat.S_IMODE(path.stat().st_mode),
                None if path.is_dir() else path.read_bytes(),
            )
            for path in transaction_root.rglob("*")
        }

    destination_before = (
        destination.read_bytes(),
        destination.stat().st_ino,
        stat.S_IMODE(destination.stat().st_mode),
        destination.stat().st_mtime_ns,
    )
    manifest_before = (
        manifest_path.read_bytes(),
        manifest_path.stat().st_ino,
        stat.S_IMODE(manifest_path.stat().st_mode),
        manifest_path.stat().st_mtime_ns,
    )
    transactions_before = transaction_snapshot()
    replacement = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"after\n", 0o600),
        revision="2" * 40,
    )

    with pytest.raises(ValueError, match="ownership manifest mode"):
        install_provider_plans((replacement,))

    audit = audit_provider_plans((initial,))
    assert not audit.matches
    assert audit.validation_errors == ("ownership manifest mode must be 0o600",)
    assert (
        destination.read_bytes(),
        destination.stat().st_ino,
        stat.S_IMODE(destination.stat().st_mode),
        destination.stat().st_mtime_ns,
    ) == destination_before
    assert (
        manifest_path.read_bytes(),
        manifest_path.stat().st_ino,
        stat.S_IMODE(manifest_path.stat().st_mode),
        manifest_path.stat().st_mtime_ns,
    ) == manifest_before
    assert transaction_snapshot() == transactions_before


def test_wrong_mode_manifest_publication_remains_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    def publish_wrong_mode_then_fail(
        home_fs: object,
        _record_path: Path,
        manifest_temp: Path,
        manifest_path: Path,
    ) -> None:
        home_fs.replace(manifest_temp, manifest_path)
        (home / manifest_path).chmod(0o644)
        raise OSError("publication result unavailable")

    monkeypatch.setattr(
        transaction_module,
        "_before_manifest_replace",
        publish_wrong_mode_then_fail,
    )

    with pytest.raises(PublicationIndeterminateError, match="indeterminate"):
        install_provider_plans((plan,))

    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    manifest_path = home / record["manifest_path"]
    assert record["state"] == "indeterminate"
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o644
    assert (home / "skills/example/SKILL.md").read_bytes() == b"body\n"
    assert record_path.exists()


def test_recovery_does_not_commit_expected_manifest_bytes_at_wrong_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    def retain_unpublished_manifest(
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
        retain_unpublished_manifest,
    )
    with pytest.raises(PublicationIndeterminateError):
        install_provider_plans((plan,))

    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record_before = record_path.read_bytes()
    record = json.loads(record_before)
    manifest_path = home / record["manifest_path"]
    lost_manifest = home / ".agentops/deployment/lost-manifest"
    manifest_path.parent.mkdir()
    os.replace(lost_manifest, manifest_path)
    manifest_path.chmod(0o644)
    manifest_before = (
        manifest_path.read_bytes(),
        manifest_path.stat().st_ino,
        stat.S_IMODE(manifest_path.stat().st_mode),
        manifest_path.stat().st_mtime_ns,
    )

    with pytest.raises(PublicationIndeterminateError, match="indeterminate"):
        recover_transaction(record_path)

    assert record_path.read_bytes() == record_before
    assert json.loads(record_path.read_text())["state"] == "indeterminate"
    assert (
        manifest_path.read_bytes(),
        manifest_path.stat().st_ino,
        stat.S_IMODE(manifest_path.stat().st_mode),
        manifest_path.stat().st_mtime_ns,
    ) == manifest_before


def test_fifo_manifest_publication_classification_does_not_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    def publish_fifo_then_fail(
        home_fs: object,
        _record_path: Path,
        manifest_temp: Path,
        manifest_path: Path,
    ) -> None:
        home_fs.replace(manifest_temp, Path(".agentops/deployment/lost-manifest"))
        destination = home / manifest_path
        destination.parent.mkdir()
        os.mkfifo(destination, 0o600)
        destination.chmod(0o600)
        raise OSError("publication result unavailable")

    monkeypatch.setattr(
        transaction_module,
        "_before_manifest_replace",
        publish_fifo_then_fail,
    )

    result = _run_bounded(lambda: install_provider_plans((plan,)))

    assert result[0] == "PublicationIndeterminateError"
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    manifest_path = home / record["manifest_path"]
    assert record["state"] == "indeterminate"
    assert stat.S_ISFIFO(manifest_path.stat().st_mode)
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert (home / "skills/example/SKILL.md").read_bytes() == b"body\n"


def test_recovery_rejects_fifo_manifest_without_blocking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    def retain_unpublished_manifest(
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
        retain_unpublished_manifest,
    )
    with pytest.raises(PublicationIndeterminateError):
        install_provider_plans((plan,))

    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record_before = record_path.read_bytes()
    record = json.loads(record_before)
    manifest_path = home / record["manifest_path"]
    manifest_path.parent.mkdir()
    os.mkfifo(manifest_path, 0o600)
    manifest_path.chmod(0o600)

    result = _run_bounded(lambda: recover_transaction(record_path))

    assert result[0] == "PublicationIndeterminateError"
    assert record_path.read_bytes() == record_before
    assert json.loads(record_path.read_text())["state"] == "indeterminate"
    assert stat.S_ISFIFO(manifest_path.stat().st_mode)
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("kind", "expected_error"),
    [
        ("fifo", "ownership manifest is not a regular file"),
        ("symlink", "ownership manifest is a symbolic link"),
    ],
)
def test_audit_reports_invalid_manifest_entries_without_blocking(
    tmp_path: Path,
    kind: str,
    expected_error: str,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    manifest_content = manifest_path.read_bytes()
    manifest_path.unlink()
    if kind == "fifo":
        os.mkfifo(manifest_path, 0o600)
        manifest_path.chmod(0o600)
    else:
        outside = tmp_path / "outside-manifest.json"
        outside.write_bytes(manifest_content)
        outside.chmod(0o600)
        manifest_path.symlink_to(outside)

    def audit_result() -> tuple[bool, tuple[str, ...]]:
        audit = audit_provider_plans((plan,))
        return audit.matches, audit.validation_errors

    result = _run_bounded(audit_result)

    assert result == ("returned", (False, (expected_error,)))
    if kind == "fifo":
        assert stat.S_ISFIFO(manifest_path.stat().st_mode)
        assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    else:
        assert manifest_path.is_symlink()
        assert manifest_path.readlink() == outside


def test_install_verifies_promoted_files_before_manifest_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    destination = home / "skills/example/SKILL.md"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    def change_promoted_file(*_args: object) -> None:
        destination.write_bytes(b"changed\n")

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", change_promoted_file)

    with pytest.raises(IncompleteRollbackError, match="evidence retained"):
        install_provider_plans((plan,))

    assert destination.read_bytes() == b"changed\n"
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))
    records = list((home / ".agentops/deployment/transactions").glob("*/record.json"))
    assert len(records) == 1
    assert json.loads(records[0].read_text())["state"] == "prepared"


def test_rollback_verifies_untouched_prior_files_before_restoring_manifest(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    original = _plan(
        home,
        PlannedFile(Path("skills/changed/SKILL.md"), b"old\n", 0o644),
        PlannedFile(Path("skills/untouched/SKILL.md"), b"untouched\n", 0o644),
    )
    install_provider_plans((original,))
    prior_manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    prior_manifest = prior_manifest_path.read_bytes()
    replacement = _plan(
        home,
        PlannedFile(Path("skills/changed/SKILL.md"), b"new\n", 0o644),
        revision="2" * 40,
    )
    manifests = install_provider_plans((replacement,))
    replacement_manifest = prior_manifest_path.read_bytes()
    untouched = home / "skills/untouched/SKILL.md"
    untouched.write_bytes(b"changed outside transaction\n")

    with pytest.raises(IncompleteRollbackError, match="prior managed file"):
        rollback_manifests(manifests)

    assert prior_manifest_path.read_bytes() == replacement_manifest
    assert prior_manifest_path.read_bytes() != prior_manifest
    record_path = transaction_module._TRANSACTION_PATHS[manifests[0].transaction_id]
    assert record_path.exists()


@pytest.mark.parametrize(
    "case",
    [
        "file-descendant",
        "file-removal-ancestor",
        "removal-ancestor",
        "duplicate-destination",
        "reserved-metadata",
    ],
)
def test_plan_topology_is_rejected_before_target_mutation(
    tmp_path: Path,
    case: str,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex-dev", Framework.CODEX, home, "feature")
    if case == "file-descendant":
        plans = (
            ProviderPlan(
                "fixture",
                "1" * 40,
                target,
                (
                    PlannedFile(Path("skills/foo"), b"file\n", 0o644),
                    PlannedFile(Path("skills/foo/bar"), b"child\n", 0o644),
                ),
            ),
        )
    elif case == "file-removal-ancestor":
        plans = (
            ProviderPlan(
                "fixture",
                "1" * 40,
                target,
                (PlannedFile(Path("skills/foo/bar"), b"child\n", 0o644),),
                (Path("skills/foo"),),
            ),
        )
    elif case == "removal-ancestor":
        plans = (
            ProviderPlan(
                "fixture",
                "1" * 40,
                target,
                (),
                (Path("skills/foo"), Path("skills/foo/bar")),
            ),
        )
    elif case == "duplicate-destination":
        duplicate = PlannedFile(Path("skills/foo/SKILL.md"), b"body\n", 0o644)
        plans = (
            ProviderPlan("one", "1" * 40, target, (duplicate,)),
            ProviderPlan("two", "1" * 40, target, (duplicate,)),
        )
    else:
        plans = (
            ProviderPlan(
                "fixture",
                "1" * 40,
                target,
                (PlannedFile(Path(".agentops/deployments/owned"), b"bad\n", 0o644),),
            ),
        )

    with pytest.raises(ValueError, match="plan topology|reserved|duplicate"):
        install_provider_plans(plans)

    assert list(tmp_path.iterdir()) == []


def test_different_targets_cannot_share_one_home(tmp_path: Path) -> None:
    home = tmp_path / "home"
    first = _plan(home, PlannedFile(Path("skills/one/SKILL.md"), b"one\n", 0o644))
    second = ProviderPlan(
        "second",
        "1" * 40,
        TargetSpec("other-target", Framework.CODEX, home, "feature"),
        (PlannedFile(Path("skills/two/SKILL.md"), b"two\n", 0o644),),
    )

    with pytest.raises(ValueError, match="same target home"):
        install_provider_plans((first, second))

    assert list(tmp_path.iterdir()) == []


def test_prior_owned_directory_drift_rejects_before_transaction_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    original = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o644))
    install_provider_plans((original,))
    directory = home / "skills/example"
    directory.chmod(0o700)
    transaction_root = home / ".agentops/deployment/transactions"
    transactions_before = sorted(path.relative_to(home) for path in transaction_root.rglob("*"))
    replacement = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),
        revision="2" * 40,
    )

    with pytest.raises(ValueError, match="prior owned directory changed"):
        install_provider_plans((replacement,))

    assert (home / "skills/example/SKILL.md").read_bytes() == b"old\n"
    transactions_after = sorted(
        path.relative_to(home) for path in transaction_root.rglob("*")
    )
    assert transactions_after == transactions_before
    assert not audit_provider_plans((original,)).matches


def test_replaced_lock_entry_cannot_create_concurrent_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = multiprocessing.get_context("fork")
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    first_entered = context.Event()
    release_first = context.Event()
    second_entered = context.Event()
    calls = context.Value("i", 0)

    def replace_lock_then_coordinate(*_args: object) -> None:
        with calls.get_lock():
            calls.value += 1
            current = calls.value
        if current == 1:
            lock_path = tmp_path / ".home.agentops-deployment.lock"
            os.replace(lock_path, tmp_path / "displaced-lock")
            lock_path.write_bytes(b"")
            lock_path.chmod(0o600)
            first_entered.set()
            assert release_first.wait(timeout=3)
        else:
            second_entered.set()

    monkeypatch.setattr(
        transaction_module,
        "_before_manifest_replace",
        replace_lock_then_coordinate,
    )

    def install() -> None:
        with suppress(BaseException):
            install_provider_plans((plan,))

    first = context.Process(target=install)
    second = context.Process(target=install)
    first.start()
    assert first_entered.wait(timeout=2)
    second.start()
    entered_concurrently = second_entered.wait(timeout=0.3)
    release_first.set()
    first.join(timeout=3)
    second.join(timeout=3)
    for process in (first, second):
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)

    assert not entered_concurrently
    assert not first.is_alive()
    assert not second.is_alive()


@pytest.mark.parametrize(
    "case",
    ["duplicate-manifest-key", "unknown-manifest-member", "noncanonical-files"],
)
def test_strict_manifest_json_rejects_ambiguity_before_mutation(
    tmp_path: Path,
    case: str,
) -> None:
    home = tmp_path / "home"
    initial = _plan(
        home,
        PlannedFile(Path("skills/a/SKILL.md"), b"a\n", 0o644),
        PlannedFile(Path("skills/b/SKILL.md"), b"b\n", 0o644),
    )
    install_provider_plans((initial,))
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    if case == "duplicate-manifest-key":
        content = manifest_path.read_text().replace(
            '"schema_version": 1,',
            '"schema_version": 1,\n  "schema_version": 1,',
        )
        manifest_path.write_text(content)
    else:
        manifest = json.loads(manifest_path.read_text())
        if case == "unknown-manifest-member":
            manifest["files"][0]["unknown"] = "value"
        else:
            manifest["files"].reverse()
        manifest_path.write_text(json.dumps(manifest))
        manifest_path.chmod(0o600)
    before = manifest_path.read_bytes()

    with pytest.raises(ValueError, match="duplicate JSON key|unknown|canonical"):
        install_provider_plans((initial,))

    assert manifest_path.read_bytes() == before
    assert len(list((home / ".agentops/deployment/transactions").glob("*/record.json"))) == 1


@pytest.mark.parametrize("case", ["duplicate-record-key", "unknown-operation-member"])
def test_strict_transaction_json_rejects_ambiguity(
    tmp_path: Path,
    case: str,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    if case == "duplicate-record-key":
        content = record_path.read_text().replace(
            '"schema_version": 3,',
            '"schema_version": 3,\n  "schema_version": 3,',
        )
        record_path.write_text(content)
    else:
        record = json.loads(record_path.read_text())
        record["operations"][0]["unknown"] = "value"
        record_path.write_text(json.dumps(record))

    with pytest.raises(ValueError, match="duplicate JSON key|unknown"):
        recover_transaction(record_path)

    assert record_path.exists()


@pytest.mark.parametrize("name", ["on", "off", "yes", "no"])
def test_frontmatter_names_preserve_yaml_strings_for_duplicates(
    tmp_path: Path,
    name: str,
) -> None:
    home = tmp_path / "home"
    plan = _plan(
        home,
        PlannedFile(
            Path("skills/one/SKILL.md"),
            f"---\nname: {name}\n---\n".encode(),
            0o644,
        ),
        PlannedFile(
            Path("skills/two/SKILL.md"),
            f'---\nname: "{name}"\n---\n'.encode(),
            0o644,
        ),
    )
    install_provider_plans((plan,))

    audit = audit_provider_plans((plan,))

    assert audit.duplicates == (
        f"{name}: skills/one/SKILL.md, skills/two/SKILL.md",
    )


def test_public_api_fails_closed_on_non_posix_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    monkeypatch.setattr(transaction_module, "_POSIX_SUPPORTED", False, raising=False)

    calls = (
        lambda: install_provider_plans((plan,)),
        lambda: rollback_manifests(()),
        lambda: audit_provider_plans((plan,)),
        lambda: recover_transaction(tmp_path / "record.json"),
    )
    for call in calls:
        with pytest.raises(transaction_module.UnsupportedPlatformError, match="POSIX"):
            call()

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("replacement", [False, True])
def test_successful_rollback_is_idempotent_and_durable(
    tmp_path: Path,
    replacement: bool,
) -> None:
    home = tmp_path / "home"
    original = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o644))
    manifests = install_provider_plans((original,))
    if replacement:
        updated = _plan(
            home,
            PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o600),
            revision="2" * 40,
        )
        manifests = install_provider_plans((updated,))
    manifest = manifests[0]
    record_path = transaction_module._TRANSACTION_PATHS[manifest.transaction_id]

    rollback_manifests(manifests)
    rollback_manifests(manifests)

    assert record_path.exists()
    assert json.loads(record_path.read_text())["state"] == "rolled-back"
    transaction_module._TRANSACTION_PATHS.pop(manifest.transaction_id)
    assert recover_transaction(record_path) == manifest
    rollback_manifests(manifests)
    if replacement:
        assert (home / "skills/example/SKILL.md").read_bytes() == b"old\n"
    else:
        assert not (home / "skills/example/SKILL.md").exists()


def test_repeat_rollback_rejects_recreated_owned_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    manifests = install_provider_plans((plan,))

    rollback_manifests(manifests)
    recreated = home / "skills/example"
    recreated.mkdir(parents=True)

    with pytest.raises(IncompleteRollbackError, match="created owned directory"):
        rollback_manifests(manifests)

    assert recreated.is_dir()


def test_noop_removal_does_not_break_canonical_transaction_order(tmp_path: Path) -> None:
    home = tmp_path / "home"
    original = _plan(home, PlannedFile(Path("skills/z/SKILL.md"), b"old\n", 0o644))
    install_provider_plans((original,))
    replacement = ProviderPlan(
        "fixture",
        "2" * 40,
        TargetSpec("codex-dev", Framework.CODEX, home, "feature"),
        (),
        (Path("skills/a/SKILL.md"), Path("skills/z/SKILL.md")),
    )
    manifests = install_provider_plans((replacement,))

    rollback_manifests(manifests)

    assert (home / "skills/z/SKILL.md").read_bytes() == b"old\n"
