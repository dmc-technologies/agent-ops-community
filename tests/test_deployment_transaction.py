from __future__ import annotations

import base64
import ctypes
import errno
import hashlib
import importlib.util
import json
import multiprocessing
import os
import py_compile
import socket
import stat
import subprocess
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_ops.deployment import transaction as transaction_module
from agent_ops.deployment.models import (
    LegacyLinkTransition,
    PlannedFile,
    PrimeGstackLegacyAdoption,
    ProviderPlan,
    TargetChannelTransition,
    TargetSpec,
)
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


_CPYTHON_CACHE_MAGICS = {
    "cpython-311": bytes.fromhex("a70d0d0a"),
    "cpython-312": bytes.fromhex("cb0d0d0a"),
    "cpython-313": bytes.fromhex("f30d0d0a"),
    "cpython-314": bytes.fromhex("2b0e0d0a"),
}


def _cache_path(source: Path, tag: str, optimization: str | None = None) -> Path:
    suffix = f".{tag}"
    if optimization is not None:
        suffix += f".opt-{optimization}"
    return source.parent / "__pycache__" / f"{source.stem}{suffix}.pyc"


def _foreign_timestamp_cache(source: Path, tag: str, *, payload: bytes = b"code") -> bytes:
    source_stat = source.stat()
    source_bytes = source.read_bytes()
    return b"".join(
        (
            _CPYTHON_CACHE_MAGICS[tag],
            b"\0\0\0\0",
            int(source_stat.st_mtime).to_bytes(4, "little"),
            len(source_bytes).to_bytes(4, "little"),
            payload,
        )
    )


def _legacy_plan(home: Path) -> ProviderPlan:
    return ProviderPlan(
        "fixture",
        "1" * 40,
        TargetSpec("codex-dev", Framework.CODEX, home, "stable"),
        (PlannedFile(Path("AGENTS.md"), b"stable\n", 0o644),),
        legacy_link_transition=LegacyLinkTransition(
            "fixture",
            "codex-dev",
            "stable",
            Path("AGENTS.md"),
            "configs/global/AGENTS.md",
            b"stable\n",
            0o644,
        ),
    )


def _legacy_refresh_plan(home: Path) -> ProviderPlan:
    return ProviderPlan(
        "fixture",
        "2" * 40,
        TargetSpec("codex-dev", Framework.CODEX, home, "stable"),
        (
            PlannedFile(Path("AGENTS.md"), b"stable\n", 0o644),
            PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),
        ),
        legacy_link_transition=LegacyLinkTransition(
            "fixture",
            "codex-dev",
            "stable",
            Path("AGENTS.md"),
            "configs/global/AGENTS.md",
            b"stable\n",
            0o644,
        ),
    )


def _prepare_legacy_refresh(home: Path) -> None:
    install_provider_plans(
        (
            ProviderPlan(
                "fixture",
                "1" * 40,
                TargetSpec("codex-dev", Framework.CODEX, home, "stable"),
                (PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o600),),
            ),
        )
    )
    (home / "AGENTS.md").symlink_to("configs/global/AGENTS.md")


def _crash_after_legacy_link_move(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    original_hook = transaction_module._after_legacy_link_move

    def terminate(*_args: object) -> None:
        os._exit(91)

    monkeypatch.setattr(transaction_module, "_after_legacy_link_move", terminate)
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((_legacy_plan(home),))
    )
    process.start()
    process.join(timeout=5)
    assert not process.is_alive()
    assert process.exitcode == 91
    monkeypatch.setattr(transaction_module, "_after_legacy_link_move", original_hook)
    return next((home / ".agentops/deployment/transactions").glob("*/record.json"))


def _crash_refresh_after_legacy_link_move(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    _prepare_legacy_refresh(home)
    original_hook = transaction_module._after_legacy_link_move

    def terminate(*_args: object) -> None:
        os._exit(92)

    monkeypatch.setattr(transaction_module, "_after_legacy_link_move", terminate)
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((_legacy_refresh_plan(home),))
    )
    process.start()
    process.join(timeout=5)
    assert not process.is_alive()
    assert process.exitcode == 92
    monkeypatch.setattr(transaction_module, "_after_legacy_link_move", original_hook)
    records = sorted(
        (home / ".agentops/deployment/transactions").glob("*/record.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    return records[-1]


def _crash_refresh_at_later_operation(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_name: str,
    exit_code: int,
) -> Path:
    _prepare_legacy_refresh(home)
    original_hook = getattr(transaction_module, hook_name)

    def terminate(_home_fs: object, _record_path: Path, operation: dict[str, object]) -> None:
        if operation["index"] == 1:
            os._exit(exit_code)

    monkeypatch.setattr(transaction_module, hook_name, terminate)
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((_legacy_refresh_plan(home),))
    )
    process.start()
    process.join(timeout=5)
    assert not process.is_alive()
    assert process.exitcode == exit_code
    monkeypatch.setattr(transaction_module, hook_name, original_hook)
    return sorted(
        (home / ".agentops/deployment/transactions").glob("*/record.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )[-1]


def _crash_refresh_before_committed_record_write(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    _prepare_legacy_refresh(home)
    original_hook = transaction_module._before_committed_record_write

    def terminate(*_args: object) -> None:
        os._exit(98)

    monkeypatch.setattr(transaction_module, "_before_committed_record_write", terminate)
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((_legacy_refresh_plan(home),))
    )
    process.start()
    process.join(timeout=5)
    assert not process.is_alive()
    assert process.exitcode == 98
    monkeypatch.setattr(
        transaction_module,
        "_before_committed_record_write",
        original_hook,
    )
    return sorted(
        (home / ".agentops/deployment/transactions").glob("*/record.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )[-1]


class _FakeNativeRename:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


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


def test_legacy_link_transition_record_binds_exact_authority(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").symlink_to("configs/global/AGENTS.md")
    install_provider_plans((_legacy_plan(home),))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    assert record["legacy_link_transition"]["destination"] == "AGENTS.md"
    record["legacy_link_transition"]["expected_link_text"] = "other"
    record_path.write_text(json.dumps(record))
    record_path.chmod(0o600)
    with pytest.raises(ValueError, match="legacy link"):
        recover_transaction(record_path)


def test_recovery_restores_legacy_link_after_power_loss_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    record_path = _crash_after_legacy_link_move(home, monkeypatch)

    assert not destination.exists()
    recovered = recover_transaction(record_path)

    assert recovered.target_id == "codex-dev"
    assert destination.is_symlink()
    assert os.readlink(destination) == "configs/global/AGENTS.md"
    assert json.loads(record_path.read_text())["state"] == "rolled-back"
    assert recover_transaction(record_path) == recovered


def test_recovery_restores_legacy_link_before_unstarted_later_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    record_path = _crash_refresh_after_legacy_link_move(home, monkeypatch)
    record = json.loads(record_path.read_text())
    later = record["operations"][1]

    assert not (home / "AGENTS.md").exists()
    assert later["backup"] is not None
    assert not (home / later["backup"]).exists()
    recover_transaction(record_path)

    assert (home / "AGENTS.md").is_symlink()
    assert os.readlink(home / "AGENTS.md") == "configs/global/AGENTS.md"
    assert (home / "skills/example/SKILL.md").read_bytes() == b"old\n"
    assert stat.S_IMODE((home / "skills/example/SKILL.md").stat().st_mode) == 0o600


@pytest.mark.parametrize(
    ("hook_name", "exit_code", "expected_phase"),
    [
        ("_before_operation_mutation", 93, "applying"),
        ("_after_operation_backup", 94, "backup-created"),
        ("_after_operation_mutation", 95, "backup-created"),
    ],
)
def test_recovery_rolls_back_each_later_backup_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hook_name: str,
    exit_code: int,
    expected_phase: str,
) -> None:
    home = tmp_path / "home"
    record_path = _crash_refresh_at_later_operation(
        home,
        monkeypatch,
        hook_name,
        exit_code,
    )
    record = json.loads(record_path.read_text())
    assert record["legacy_link_transition"]["operation_cursor"] == 1
    assert record["legacy_link_transition"]["operation_phase"] == expected_phase

    recovered = recover_transaction(record_path)

    assert (home / "AGENTS.md").is_symlink()
    assert os.readlink(home / "AGENTS.md") == "configs/global/AGENTS.md"
    assert (home / "skills/example/SKILL.md").read_bytes() == b"old\n"
    assert recover_transaction(record_path) == recovered


@pytest.mark.parametrize("case", ["missing", "changed"])
def test_recovery_restores_link_before_rejecting_later_backup_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    home = tmp_path / "home"
    record_path = _crash_refresh_at_later_operation(
        home,
        monkeypatch,
        "_after_operation_backup",
        96,
    )
    record = json.loads(record_path.read_text())
    backup = home / record["operations"][1]["backup"]
    if case == "missing":
        backup.unlink()
    else:
        backup.write_bytes(b"changed backup\n")

    with pytest.raises((ValueError, IncompleteRollbackError), match="backup"):
        recover_transaction(record_path)

    assert (home / "AGENTS.md").is_symlink()
    assert os.readlink(home / "AGENTS.md") == "configs/global/AGENTS.md"
    assert record_path.is_file()


def test_recovery_restores_link_before_rejecting_changed_unstarted_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    record_path = _crash_refresh_at_later_operation(
        home,
        monkeypatch,
        "_before_operation_mutation",
        97,
    )
    prior_destination = home / "skills/example/SKILL.md"
    prior_destination.write_bytes(b"concurrent prior change\n")

    with pytest.raises(ValueError, match="backup evidence is missing"):
        recover_transaction(record_path)

    assert (home / "AGENTS.md").is_symlink()
    assert os.readlink(home / "AGENTS.md") == "configs/global/AGENTS.md"
    assert prior_destination.read_bytes() == b"concurrent prior change\n"


def test_recovery_commits_forward_when_exact_manifest_was_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    record_path = _crash_refresh_before_committed_record_write(home, monkeypatch)
    record = json.loads(record_path.read_text())
    retained = home / record["legacy_link_transition"]["retained_entry"]
    manifest_path = home / record["manifest_path"]

    assert record["state"] == "prepared"
    assert manifest_path.read_bytes() == base64.b64decode(record["manifest_content"])
    assert (home / "AGENTS.md").read_bytes() == b"stable\n"
    assert (home / "skills/example/SKILL.md").read_bytes() == b"new\n"
    assert retained.is_symlink()

    recovered = recover_transaction(record_path)

    assert json.loads(record_path.read_text())["state"] == "committed"
    assert (home / "AGENTS.md").read_bytes() == b"stable\n"
    assert (home / "skills/example/SKILL.md").read_bytes() == b"new\n"
    assert retained.is_symlink()
    assert recover_transaction(record_path) == recovered


def test_recovery_rolls_back_complete_outputs_when_candidate_manifest_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    record_path = _crash_refresh_before_committed_record_write(home, monkeypatch)
    record = json.loads(record_path.read_text())
    (home / record["manifest_path"]).unlink()

    recovered = recover_transaction(record_path)

    assert (home / "AGENTS.md").is_symlink()
    assert os.readlink(home / "AGENTS.md") == "configs/global/AGENTS.md"
    assert (home / "skills/example/SKILL.md").read_bytes() == b"old\n"
    assert json.loads(record_path.read_text())["state"] == "rolled-back"
    assert recover_transaction(record_path) == recovered


@pytest.mark.parametrize("case", ["changed-output", "unexpected-manifest"])
def test_recovery_rejects_contradictory_published_state_without_restoring_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    home = tmp_path / "home"
    record_path = _crash_refresh_before_committed_record_write(home, monkeypatch)
    record = json.loads(record_path.read_text())
    destination = home / "AGENTS.md"
    retained = home / record["legacy_link_transition"]["retained_entry"]
    manifest_path = home / record["manifest_path"]
    if case == "changed-output":
        destination.write_bytes(b"changed concurrent output\n")
        expected = "final state is invalid"
    else:
        manifest_path.write_bytes(b"{}\n")
        expected = "unexpected manifest"

    with pytest.raises(PublicationIndeterminateError, match=expected):
        recover_transaction(record_path)

    if case == "changed-output":
        assert destination.read_bytes() == b"changed concurrent output\n"
    else:
        assert destination.read_bytes() == b"stable\n"
        assert manifest_path.read_bytes() == b"{}\n"
    assert retained.is_symlink()
    assert os.readlink(retained) == "configs/global/AGENTS.md"
    assert json.loads(record_path.read_text())["state"] == "prepared"


@pytest.mark.parametrize(
    "process_control",
    [KeyboardInterrupt("stop"), SystemExit(71)],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_recovery_preserves_process_control_before_commit_record_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_control: BaseException,
) -> None:
    home = tmp_path / "home"
    record_path = _crash_refresh_before_committed_record_write(home, monkeypatch)
    record = json.loads(record_path.read_text())
    retained = home / record["legacy_link_transition"]["retained_entry"]
    original_hook = transaction_module._before_committed_record_write

    def interrupt(*_args: object) -> None:
        raise process_control

    monkeypatch.setattr(transaction_module, "_before_committed_record_write", interrupt)

    with pytest.raises(type(process_control)) as caught:
        recover_transaction(record_path)

    assert caught.value is process_control
    assert (home / "AGENTS.md").read_bytes() == b"stable\n"
    assert (home / "skills/example/SKILL.md").read_bytes() == b"new\n"
    assert retained.is_symlink()
    assert json.loads(record_path.read_text())["state"] == "prepared"
    monkeypatch.setattr(
        transaction_module,
        "_before_committed_record_write",
        original_hook,
    )
    recover_transaction(record_path)
    assert json.loads(record_path.read_text())["state"] == "committed"


def test_recovery_restores_legacy_link_when_staged_evidence_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    record_path = _crash_after_legacy_link_move(home, monkeypatch)
    record = json.loads(record_path.read_text())
    staged = home / record["operations"][0]["staged"]
    staged.unlink()

    recover_transaction(record_path)

    assert destination.is_symlink()
    assert os.readlink(destination) == "configs/global/AGENTS.md"
    assert json.loads(record_path.read_text())["state"] == "rolled-back"


def test_recovery_restores_legacy_link_and_retains_changed_staged_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    record_path = _crash_after_legacy_link_move(home, monkeypatch)
    record = json.loads(record_path.read_text())
    staged = home / record["operations"][0]["staged"]
    staged.write_bytes(b"changed staged evidence\n")

    with pytest.raises(IncompleteRollbackError, match="staged file changed"):
        recover_transaction(record_path)

    assert destination.is_symlink()
    assert os.readlink(destination) == "configs/global/AGENTS.md"
    assert staged.read_bytes() == b"changed staged evidence\n"
    assert record_path.is_file()


def test_recovery_never_overwrites_concurrent_legacy_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    record_path = _crash_after_legacy_link_move(home, monkeypatch)
    retained = record_path.parent / "prestate/legacy-link.entry"
    original_move = transaction_module._HomeFS.move_new

    def create_destination(home_fs: object, source: Path, target: Path) -> None:
        if source == retained.relative_to(home) and target == Path("AGENTS.md"):
            destination.write_bytes(b"concurrent recovery\n")
        original_move(home_fs, source, target)

    monkeypatch.setattr(transaction_module._HomeFS, "move_new", create_destination)

    with pytest.raises(IncompleteRollbackError, match="rollback incomplete"):
        recover_transaction(record_path)

    assert destination.read_bytes() == b"concurrent recovery\n"
    assert retained.is_symlink()
    assert os.readlink(retained) == "configs/global/AGENTS.md"


def test_recovery_preserves_process_control_after_restoring_legacy_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    record_path = _crash_after_legacy_link_move(home, monkeypatch)
    original_move = transaction_module._HomeFS.move_new
    process_exit = KeyboardInterrupt("stop")

    def interrupt_after_restore(home_fs: object, source: Path, target: Path) -> None:
        original_move(home_fs, source, target)
        if source.name == "legacy-link.entry" and target == Path("AGENTS.md"):
            raise process_exit

    monkeypatch.setattr(transaction_module._HomeFS, "move_new", interrupt_after_restore)

    with pytest.raises(KeyboardInterrupt) as caught:
        recover_transaction(record_path)

    assert caught.value is process_exit
    assert destination.is_symlink()
    assert os.readlink(destination) == "configs/global/AGENTS.md"
    monkeypatch.setattr(transaction_module._HomeFS, "move_new", original_move)
    recover_transaction(record_path)
    assert json.loads(record_path.read_text())["state"] == "rolled-back"


@pytest.mark.parametrize(
    ("platform", "symbol", "flag"),
    [("linux", "renameat2", 1), ("darwin", "renameatx_np", 0x00000004)],
)
def test_atomic_noreplace_selects_native_backend_with_exact_arguments(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    symbol: str,
    flag: int,
) -> None:
    native = _FakeNativeRename(0)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(
        ctypes,
        "CDLL",
        lambda *_args, **_kwargs: SimpleNamespace(**{symbol: native}),
    )

    transaction_module._rename_noreplace_at(
        "source",
        "destination",
        source_dir_fd=11,
        destination_dir_fd=12,
    )

    assert native.calls == [(11, b"source", 12, b"destination", flag)]
    assert native.argtypes is not None
    assert native.restype is ctypes.c_int


@pytest.mark.parametrize(
    ("native_errno", "error_type", "message"),
    [
        (errno.EEXIST, FileExistsError, "destination"),
        (errno.ENOSYS, transaction_module.UnsupportedPlatformError, "unavailable"),
        (errno.EPERM, OSError, "not permitted"),
    ],
)
def test_macos_atomic_noreplace_maps_native_errno_exactly(
    monkeypatch: pytest.MonkeyPatch,
    native_errno: int,
    error_type: type[BaseException],
    message: str,
) -> None:
    native = _FakeNativeRename(-1)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        ctypes,
        "CDLL",
        lambda *_args, **_kwargs: SimpleNamespace(renameatx_np=native),
    )
    monkeypatch.setattr(ctypes, "get_errno", lambda: native_errno)

    with pytest.raises(error_type, match=message):
        transaction_module._rename_noreplace_at(
            "source",
            "destination",
            source_dir_fd=11,
            destination_dir_fd=12,
        )


def test_legacy_link_transition_without_atomic_backend_creates_no_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: SimpleNamespace())

    with pytest.raises(transaction_module.UnsupportedPlatformError, match="unavailable"):
        install_provider_plans((_legacy_plan(home),))

    assert destination.is_symlink()
    assert os.readlink(destination) == "configs/global/AGENTS.md"
    assert not (home / ".agentops").exists()


def test_legacy_link_transition_record_cannot_downgrade_to_prior_schema(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").symlink_to("configs/global/AGENTS.md")
    install_provider_plans((_legacy_plan(home),))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    del record["legacy_link_transition"]
    record_path.write_text(json.dumps(record))
    record_path.chmod(0o600)

    with pytest.raises(ValueError, match="missing transaction record member"):
        recover_transaction(record_path)


def test_legacy_link_transition_schema_downgrade_cannot_rollback_installed_file(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    manifest = install_provider_plans((_legacy_plan(home),))[0]
    record_path = (
        home
        / ".agentops/deployment/transactions"
        / manifest.transaction_id
        / "record.json"
    )
    evidence = record_path.parent / "prestate/legacy-link.json"
    record = json.loads(record_path.read_text())
    record["schema_version"] = 3
    del record["legacy_link_transition"]
    del record["operation_cursor"]
    del record["operation_phase"]
    record_path.write_text(json.dumps(record))
    record_path.chmod(0o600)
    installed = destination.read_bytes()

    with pytest.raises(ValueError, match="prestate evidence"):
        rollback_manifests((manifest,))

    assert destination.read_bytes() == installed
    assert evidence.is_file()
    assert record_path.is_file()


def test_legacy_link_transition_schema_downgrade_recovery_preserves_prior_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    stop = SystemExit(23)
    original_write = transaction_module._HomeFS.write_file

    def stop_after_record(
        home_fs: object,
        relative: Path,
        content: bytes,
        mode: int,
    ) -> None:
        original_write(home_fs, relative, content, mode)
        if relative.name == "record.json":
            raise stop

    monkeypatch.setattr(transaction_module._HomeFS, "write_file", stop_after_record)
    with pytest.raises(SystemExit) as caught:
        install_provider_plans((_legacy_plan(home),))
    assert caught.value is stop
    monkeypatch.setattr(transaction_module._HomeFS, "write_file", original_write)

    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    evidence = record_path.parent / "prestate/legacy-link.json"
    record = json.loads(record_path.read_text())
    record["schema_version"] = 3
    del record["legacy_link_transition"]
    del record["operation_cursor"]
    del record["operation_phase"]
    record_path.write_text(json.dumps(record))
    record_path.chmod(0o600)

    with pytest.raises(ValueError, match="prestate evidence"):
        recover_transaction(record_path)

    assert destination.is_symlink()
    assert os.readlink(destination) == "configs/global/AGENTS.md"
    assert evidence.is_file()
    assert record_path.is_file()


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_id", True),
        ("target_id", True),
        ("expected_channel", True),
        ("destination", "other"),
        ("expected_link_text", True),
        ("replacement_fingerprint", "f" * 64),
        ("replacement_mode", True),
        ("operation_index", True),
        ("prestate_evidence", "other"),
        ("retained_entry", "other"),
        ("operation_cursor", True),
        ("operation_phase", True),
    ],
)
def test_legacy_link_transition_record_rejects_tampered_binding(
    tmp_path: Path, field: str, value: object
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").symlink_to("configs/global/AGENTS.md")
    install_provider_plans((_legacy_plan(home),))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    record["legacy_link_transition"][field] = value
    record_path.write_text(json.dumps(record))
    record_path.chmod(0o600)

    with pytest.raises(ValueError, match="legacy link"):
        recover_transaction(record_path)


def test_legacy_link_transition_migrates_only_exact_existing_link(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").symlink_to("configs/global/AGENTS.md")
    install_provider_plans((_legacy_plan(home),))
    assert not (home / "AGENTS.md").is_symlink()


def test_legacy_link_transition_allows_absent_destination_as_normal_install(tmp_path: Path) -> None:
    home = tmp_path / "home"
    install_provider_plans((_legacy_plan(home),))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    assert record["legacy_link_transition"] is None


def test_legacy_link_transition_rollback_restores_exact_link(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    manifests = install_provider_plans((_legacy_plan(home),))

    rollback_manifests(manifests)

    assert destination.is_symlink()
    assert os.readlink(destination) == "configs/global/AGENTS.md"


def test_legacy_link_transition_failure_restores_exact_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")

    def fail_before_manifest(*_args: object) -> None:
        raise OSError("injected publication failure")

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", fail_before_manifest)

    with pytest.raises(OSError, match="injected publication failure"):
        install_provider_plans((_legacy_plan(home),))

    assert destination.is_symlink()
    assert os.readlink(destination) == "configs/global/AGENTS.md"


def test_legacy_link_transition_wrong_link_remains_unchanged(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("other-policy.md")

    with pytest.raises(ValueError, match="symbolic link"):
        install_provider_plans((_legacy_plan(home),))

    assert destination.is_symlink()
    assert os.readlink(destination) == "other-policy.md"
    assert not (home / ".agentops").exists()


def test_existing_transaction_record_without_legacy_member_remains_recoverable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    manifest = install_provider_plans(
        (_plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644)),)
    )[0]
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    record["schema_version"] = 3
    del record["legacy_link_transition"]
    del record["operation_cursor"]
    del record["operation_phase"]
    record_path.write_text(json.dumps(record))
    record_path.chmod(0o600)

    assert recover_transaction(record_path) == manifest


def test_existing_schema_five_transaction_without_global_cursor_remains_recoverable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    manifest = install_provider_plans(
        (_plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644)),)
    )[0]
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    record["schema_version"] = 5
    del record["operation_cursor"]
    del record["operation_phase"]
    record_path.write_text(json.dumps(record))
    record_path.chmod(0o600)

    assert recover_transaction(record_path) == manifest


def test_legacy_link_transition_later_refresh_uses_normal_managed_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    install_provider_plans((_legacy_plan(home),))

    refreshed = ProviderPlan(
        "fixture",
        "2" * 40,
        TargetSpec("codex-dev", Framework.CODEX, home, "stable"),
        (PlannedFile(Path("AGENTS.md"), b"refreshed\n", 0o600),),
        legacy_link_transition=LegacyLinkTransition(
            "fixture",
            "codex-dev",
            "stable",
            Path("AGENTS.md"),
            "configs/global/AGENTS.md",
            b"refreshed\n",
            0o600,
        ),
    )
    install_provider_plans((refreshed,))
    record_path = sorted(
        (home / ".agentops/deployment/transactions").glob("*/record.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )[-1]

    assert destination.read_bytes() == b"refreshed\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    assert json.loads(record_path.read_text())["legacy_link_transition"] is None


def test_recovery_verifies_completed_legacy_link_rollback(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    manifest = install_provider_plans((_legacy_plan(home),))[0]
    record_path = (
        home
        / ".agentops/deployment/transactions"
        / manifest.transaction_id
        / "record.json"
    )

    rollback_manifests((manifest,))

    assert recover_transaction(record_path) == manifest
    assert destination.is_symlink()
    assert os.readlink(destination) == "configs/global/AGENTS.md"


@pytest.mark.parametrize("process_exit", [KeyboardInterrupt("stop"), SystemExit(19)])
def test_legacy_link_transition_restores_same_process_control_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    process_exit: BaseException,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")

    def interrupt(*_args: object) -> None:
        raise process_exit

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", interrupt)

    with pytest.raises(type(process_exit)) as caught:
        install_provider_plans((_legacy_plan(home),))

    assert caught.value is process_exit
    assert destination.is_symlink()
    assert os.readlink(destination) == "configs/global/AGENTS.md"


def test_legacy_link_transition_preserves_replacement_at_final_move_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    original_move = transaction_module._rename_noreplace_at

    def replace_at_boundary(
        source: str,
        target: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        if target == "legacy-link.entry":
            os.unlink(source, dir_fd=source_dir_fd)
            os.symlink("concurrent-policy.md", source, dir_fd=source_dir_fd)
        original_move(
            source,
            target,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )

    monkeypatch.setattr(transaction_module, "_rename_noreplace_at", replace_at_boundary)

    with pytest.raises(IncompleteRollbackError, match="rollback incomplete"):
        install_provider_plans((_legacy_plan(home),))

    assert destination.is_symlink()
    assert os.readlink(destination) == "concurrent-policy.md"
    assert len(list((home / ".agentops/deployment/transactions").glob("*/record.json"))) == 1


def test_legacy_link_transition_preserves_post_move_destination_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")

    def create_destination(*_args: object) -> None:
        destination.write_bytes(b"concurrent\n")

    monkeypatch.setattr(transaction_module, "_after_legacy_link_move", create_destination)

    with pytest.raises(IncompleteRollbackError, match="rollback incomplete"):
        install_provider_plans((_legacy_plan(home),))

    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    backup = record_path.parent / "prestate/legacy-link.entry"
    assert destination.read_bytes() == b"concurrent\n"
    assert backup.is_symlink()
    assert os.readlink(backup) == "configs/global/AGENTS.md"


def test_legacy_link_transition_rollback_preserves_destination_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    destination = home / "AGENTS.md"
    destination.symlink_to("configs/global/AGENTS.md")
    manifest = install_provider_plans((_legacy_plan(home),))[0]
    record_path = (
        home
        / ".agentops/deployment/transactions"
        / manifest.transaction_id
        / "record.json"
    )
    backup = record_path.parent / "prestate/legacy-link.entry"
    original_move = transaction_module._HomeFS.move_new

    def race_restore(home_fs: object, source: Path, target: Path) -> None:
        if source.name == "legacy-link.entry" and target == Path("AGENTS.md"):
            destination.write_bytes(b"concurrent rollback\n")
        original_move(home_fs, source, target)

    monkeypatch.setattr(transaction_module._HomeFS, "move_new", race_restore)

    with pytest.raises(IncompleteRollbackError, match="rollback incomplete"):
        rollback_manifests((manifest,))

    assert destination.read_bytes() == b"concurrent rollback\n"
    assert backup.is_symlink()
    assert os.readlink(backup) == "configs/global/AGENTS.md"


def test_audit_allows_co_resident_provider_owned_roots_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    private = ProviderPlan(
        "private", "1" * 40, TargetSpec("codex-dev", Framework.CODEX, home, "feature"),
        (PlannedFile(Path("skills/private/SKILL.md"), b"private\n", 0o644),),
        audit_roots=(Path("skills/private"),),
    )
    public = _plan(
        home, PlannedFile(Path("skills/public/SKILL.md"), b"public\n", 0o644), provider="public"
    )
    install_provider_plans((private, public))
    assert audit_provider_plans((private, public)).matches


def test_audit_rejects_cpython_bytecode_and_symlinked_bytecode(tmp_path: Path) -> None:
    home = tmp_path / "home"
    plan = ProviderPlan(
        "private", "1" * 40, TargetSpec("codex-dev", Framework.CODEX, home, "feature"),
        (PlannedFile(Path("skills/private/module.py"), b"x = 1\n", 0o644),),
        audit_roots=(Path("skills/private"),),
    )
    install_provider_plans((plan,))
    cache = home / "skills/private/__pycache__"
    cache.mkdir()
    external = tmp_path / "external.pyc"
    external.write_bytes(b"malicious")
    (cache / "module.cpython-312.pyc").symlink_to(external)
    assert audit_provider_plans((plan,)).unexpected == (
        "skills/private/__pycache__/module.cpython-312.pyc",
    )


def test_public_gstack_provider_may_own_its_reserved_runtime_only(tmp_path: Path) -> None:
    home = tmp_path / "home"
    public = ProviderPlan(
        "public-skill:gstack",
        "1" * 40,
        TargetSpec("prime", Framework.PRIME_AGENT, home, "public"),
        (PlannedFile(Path(".agentops/runtime/gstack/ETHOS.md"), b"ethos\n", 0o644),),
        audit_roots=(Path(".agentops/runtime/gstack"),),
    )
    install_provider_plans((public,))
    assert (home / ".agentops/runtime/gstack/ETHOS.md").read_bytes() == b"ethos\n"
    assert audit_provider_plans((public,)).matches

    private = ProviderPlan(
        "private",
        "1" * 40,
        TargetSpec("other", Framework.PRIME_AGENT, tmp_path / "other", "stable"),
        (PlannedFile(Path(".agentops/runtime/gstack/ETHOS.md"), b"no\n", 0o644),),
    )
    with pytest.raises(ValueError, match="reserved metadata"):
        install_provider_plans((private,))


def _prime_gstack_legacy_plan(
    home: Path,
) -> tuple[ProviderPlan, Path, Path, Path, bytes]:
    source_ref = "74895062fb8a3acbf9f66cd088a83359aaaa56cd"
    runtime_relative = Path(".agentops/runtime/gstack/bin/gstack-global-discover")
    skill_relative = Path("skills/agentops-gstack-review/SKILL.md")
    runtime = home / runtime_relative
    skill = home / skill_relative
    runtime.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True)
    runtime.write_bytes(b"legacy executable\n")
    runtime.chmod(0o755)
    skill.write_bytes(b"legacy skill\n")
    skill.chmod(0o644)
    legacy_manifest = home / ".agentops/gstack-prime-manifest.json"
    legacy_manifest_bytes = (
        json.dumps(
            {
                "schema_version": 1,
                "owner": "agent-ops-community:gstack-prime",
                "upstream_ref": source_ref,
                "files": {
                    runtime_relative.as_posix(): hashlib.sha256(runtime.read_bytes()).hexdigest(),
                    skill_relative.as_posix(): hashlib.sha256(skill.read_bytes()).hexdigest(),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    legacy_manifest.write_bytes(legacy_manifest_bytes)
    legacy_manifest.chmod(0o644)
    target = TargetSpec(
        "public-skills:prime-agent",
        Framework.PRIME_AGENT,
        home,
        "public",
    )
    source_revision = "public-skills:" + json.dumps(
        [
            {
                "id": "gstack",
                "repo": "https://github.com/garrytan/gstack.git",
                "ref": source_ref,
                "install": {
                    "strategy": "prime-gstack",
                    "source": None,
                    "destination": ".",
                },
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    plan = ProviderPlan(
        "public-skill:gstack",
        source_revision,
        target,
        (
            PlannedFile(runtime_relative, b"current executable\n", 0o755),
            PlannedFile(skill_relative, b"current skill\n", 0o644),
        ),
        audit_roots=(Path(".agentops/runtime/gstack"), Path("skills")),
        prime_gstack_legacy_adoption=PrimeGstackLegacyAdoption(
            target.id,
            target.channel,
            source_ref,
            tuple(sorted((runtime_relative, skill_relative), key=lambda path: path.as_posix())),
        ),
    )
    return plan, runtime, skill, legacy_manifest, legacy_manifest_bytes


def test_prime_gstack_legacy_manifest_decoder_rejects_non_gstack_path() -> None:
    source_ref = "74895062fb8a3acbf9f66cd088a83359aaaa56cd"
    foreign = Path("skills/foreign/SKILL.md")
    content = json.dumps(
        {
            "schema_version": 1,
            "owner": "agent-ops-community:gstack-prime",
            "upstream_ref": source_ref,
            "files": {foreign.as_posix(): "0" * 64},
        }
    ).encode()

    with pytest.raises(ValueError, match="legacy manifest file"):
        transaction_module._decode_prime_gstack_legacy_manifest(
            content,
            source_ref=source_ref,
            expected_paths=(foreign,),
        )


def test_prime_gstack_legacy_source_binding_requires_exact_install_descriptor() -> None:
    source_ref = "74895062fb8a3acbf9f66cd088a83359aaaa56cd"
    revision = "public-skills:" + json.dumps(
        [
            {
                "id": "gstack",
                "repo": "https://github.com/garrytan/gstack.git",
                "ref": source_ref,
                "install": {
                    "strategy": "prime-gstack",
                    "source": "skills",
                    "destination": ".",
                },
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    )

    assert not transaction_module._source_revision_binds_prime_gstack_ref(
        revision,
        source_ref,
    )


def test_prime_gstack_legacy_source_binding_rejects_foreign_repository() -> None:
    source_ref = "74895062fb8a3acbf9f66cd088a83359aaaa56cd"
    revision = "public-skills:" + json.dumps(
        [
            {
                "id": "gstack",
                "repo": "https://attacker.invalid/not-public-gstack.git",
                "ref": source_ref,
                "install": {
                    "strategy": "prime-gstack",
                    "source": None,
                    "destination": ".",
                },
            }
        ],
        separators=(",", ":"),
        sort_keys=True,
    )

    assert not transaction_module._source_revision_binds_prime_gstack_ref(
        revision,
        source_ref,
    )


@pytest.mark.parametrize(
    "case",
    (
        "wrong-schema",
        "wrong-owner",
        "wrong-ref",
        "wrong-plan-ref",
        "wrong-plan-repository",
        "extra-path",
        "missing-path",
        "modified-file",
        "missing-file",
        "wrong-file-mode",
        "wrong-manifest-mode",
    ),
)
def test_prime_gstack_legacy_adoption_refuses_inexact_evidence_without_replacement(
    tmp_path: Path,
    case: str,
) -> None:
    home = tmp_path / "home"
    plan, runtime, skill, legacy_manifest, original_manifest = _prime_gstack_legacy_plan(home)
    data = json.loads(original_manifest)
    if case == "wrong-schema":
        data["schema_version"] = 2
    elif case == "wrong-owner":
        data["owner"] = "another-owner"
    elif case == "wrong-ref":
        data["upstream_ref"] = "0" * 40
    elif case == "wrong-plan-ref":
        plan = ProviderPlan(
            plan.provider_id,
            plan.source_revision.replace(
                "74895062fb8a3acbf9f66cd088a83359aaaa56cd",
                "0" * 40,
            ),
            plan.target,
            plan.files,
            audit_roots=plan.audit_roots,
            prime_gstack_legacy_adoption=plan.prime_gstack_legacy_adoption,
        )
    elif case == "wrong-plan-repository":
        plan = ProviderPlan(
            plan.provider_id,
            plan.source_revision.replace(
                "https://github.com/garrytan/gstack.git",
                "https://attacker.invalid/not-public-gstack.git",
            ),
            plan.target,
            plan.files,
            audit_roots=plan.audit_roots,
            prime_gstack_legacy_adoption=plan.prime_gstack_legacy_adoption,
        )
    elif case == "extra-path":
        data["files"]["skills/agentops-gstack-extra/SKILL.md"] = "0" * 64
    elif case == "missing-path":
        del data["files"]["skills/agentops-gstack-review/SKILL.md"]
    elif case == "modified-file":
        runtime.write_bytes(b"locally modified\n")
    elif case == "missing-file":
        runtime.unlink()
    elif case == "wrong-file-mode":
        runtime.chmod(0o744)
    elif case == "wrong-manifest-mode":
        legacy_manifest.chmod(0o600)
    if case in {"wrong-schema", "wrong-owner", "wrong-ref", "extra-path", "missing-path"}:
        legacy_manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        legacy_manifest.chmod(0o644)
    runtime_before = runtime.read_bytes() if runtime.exists() else None
    runtime_mode = stat.S_IMODE(runtime.stat().st_mode) if runtime.exists() else None
    skill_before = skill.read_bytes()
    manifest_before = legacy_manifest.read_bytes()
    manifest_mode = stat.S_IMODE(legacy_manifest.stat().st_mode)

    with pytest.raises((OSError, ValueError)):
        install_provider_plans((plan,))

    assert (runtime.read_bytes() if runtime.exists() else None) == runtime_before
    assert (stat.S_IMODE(runtime.stat().st_mode) if runtime.exists() else None) == runtime_mode
    assert skill.read_bytes() == skill_before
    assert legacy_manifest.read_bytes() == manifest_before
    assert stat.S_IMODE(legacy_manifest.stat().st_mode) == manifest_mode
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))


def test_prime_gstack_legacy_adoption_rolls_back_exact_prestate_after_late_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan, runtime, skill, legacy_manifest, legacy_manifest_bytes = _prime_gstack_legacy_plan(home)

    def fail_before_manifest(*_args: object) -> None:
        raise OSError("injected late adoption failure")

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", fail_before_manifest)

    with pytest.raises(OSError, match="injected late adoption failure"):
        install_provider_plans((plan,))

    assert runtime.read_bytes() == b"legacy executable\n"
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o755
    assert skill.read_bytes() == b"legacy skill\n"
    assert stat.S_IMODE(skill.stat().st_mode) == 0o644
    assert legacy_manifest.read_bytes() == legacy_manifest_bytes
    assert stat.S_IMODE(legacy_manifest.stat().st_mode) == 0o644
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))


@pytest.mark.parametrize("raced_entry", ("runtime", "legacy-manifest"))
@pytest.mark.parametrize("race_kind", ("replacement", "content", "mode"))
def test_prime_gstack_legacy_adoption_preserves_concurrent_entry_before_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raced_entry: str,
    race_kind: str,
) -> None:
    home = tmp_path / "home"
    plan, runtime, skill, legacy_manifest, legacy_manifest_bytes = _prime_gstack_legacy_plan(home)
    raced_path = runtime if raced_entry == "runtime" else legacy_manifest
    raced_relative = raced_path.relative_to(home)
    concurrent_content = f"concurrent {raced_entry}\n".encode()
    runtime_before = runtime.read_bytes()
    skill_before = skill.read_bytes()
    original_content = raced_path.read_bytes()
    original_mode = stat.S_IMODE(raced_path.stat().st_mode)
    concurrent_mode = (
        0o700 if raced_entry == "runtime" else 0o600
    ) if race_kind == "mode" else original_mode
    expected_content = original_content if race_kind == "mode" else concurrent_content
    changed = False

    def replace_before_backup(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        nonlocal changed
        if changed or operation["destination"] != raced_relative.as_posix():
            return
        if race_kind == "replacement":
            concurrent = raced_path.with_name(f"{raced_path.name}.concurrent")
            concurrent.write_bytes(concurrent_content)
            concurrent.chmod(original_mode)
            os.replace(concurrent, raced_path)
        elif race_kind == "content":
            raced_path.write_bytes(concurrent_content)
        else:
            raced_path.chmod(concurrent_mode)
        changed = True

    monkeypatch.setattr(
        transaction_module,
        "_before_operation_mutation",
        replace_before_backup,
    )

    with pytest.raises((OSError, ValueError)):
        install_provider_plans((plan,))

    assert changed
    assert raced_path.read_bytes() == expected_content
    assert stat.S_IMODE(raced_path.stat().st_mode) == concurrent_mode
    if raced_entry == "runtime":
        assert skill.read_bytes() == skill_before
        assert legacy_manifest.read_bytes() == legacy_manifest_bytes
    else:
        assert runtime.read_bytes() == runtime_before
        assert skill.read_bytes() == skill_before
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))
    assert not list((home / ".agentops/deployment/transactions").glob("*/record.json"))


@pytest.mark.parametrize("future_entry", ("skill", "legacy-manifest"))
@pytest.mark.parametrize("race_kind", ("replacement", "missing"))
def test_prime_gstack_legacy_adoption_rolls_back_when_future_entry_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    future_entry: str,
    race_kind: str,
) -> None:
    home = tmp_path / "home"
    plan, runtime, skill, legacy_manifest, legacy_manifest_bytes = _prime_gstack_legacy_plan(home)
    future_path = skill if future_entry == "skill" else legacy_manifest
    concurrent_content = f"concurrent future {future_entry}\n".encode()
    runtime_before = runtime.read_bytes()
    changed = False

    def replace_after_earlier_operation(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        nonlocal changed
        if changed or operation["destination"] != runtime.relative_to(home).as_posix():
            return
        if race_kind == "replacement":
            concurrent = future_path.with_name(f"{future_path.name}.concurrent")
            concurrent.write_bytes(concurrent_content)
            concurrent.chmod(0o644)
            os.replace(concurrent, future_path)
        else:
            future_path.unlink()
        changed = True

    monkeypatch.setattr(
        transaction_module,
        "_after_operation_mutation",
        replace_after_earlier_operation,
    )

    with pytest.raises((OSError, ValueError)):
        install_provider_plans((plan,))

    assert changed
    assert runtime.read_bytes() == runtime_before
    if race_kind == "replacement":
        assert future_path.read_bytes() == concurrent_content
    else:
        assert not future_path.exists()
    if future_entry == "skill":
        assert legacy_manifest.read_bytes() == legacy_manifest_bytes
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))
    assert not list((home / ".agentops/deployment/transactions").glob("*/record.json"))


def test_prime_gstack_legacy_adoption_rolls_back_when_future_unchanged_entry_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan, runtime, skill, legacy_manifest, legacy_manifest_bytes = _prime_gstack_legacy_plan(home)
    plan = ProviderPlan(
        plan.provider_id,
        plan.source_revision,
        plan.target,
        (plan.files[0], PlannedFile(plan.files[1].path, skill.read_bytes(), 0o644)),
        audit_roots=plan.audit_roots,
        prime_gstack_legacy_adoption=plan.prime_gstack_legacy_adoption,
    )
    runtime_before = runtime.read_bytes()
    concurrent_content = b"concurrent unchanged skill\n"
    changed = False

    def replace_after_earlier_operation(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        nonlocal changed
        if changed or operation["destination"] != runtime.relative_to(home).as_posix():
            return
        concurrent = skill.with_name(f"{skill.name}.concurrent")
        concurrent.write_bytes(concurrent_content)
        concurrent.chmod(0o644)
        os.replace(concurrent, skill)
        changed = True

    monkeypatch.setattr(
        transaction_module,
        "_after_operation_mutation",
        replace_after_earlier_operation,
    )

    with pytest.raises((OSError, ValueError)):
        install_provider_plans((plan,))

    assert changed
    assert runtime.read_bytes() == runtime_before
    assert skill.read_bytes() == concurrent_content
    assert legacy_manifest.read_bytes() == legacy_manifest_bytes
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))
    assert not list((home / ".agentops/deployment/transactions").glob("*/record.json"))


@pytest.mark.parametrize("exit_point", ("journaled-backup", "restored-destination"))
@pytest.mark.parametrize("race_kind", ("replacement", "missing"))
def test_prime_gstack_concurrent_entry_recovery_restores_live_path_after_process_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_point: str,
    race_kind: str,
) -> None:
    home = tmp_path / "home"
    plan, runtime, skill, legacy_manifest, legacy_manifest_bytes = _prime_gstack_legacy_plan(home)
    concurrent_content = b"concurrent runtime before recovery\n"
    runtime_relative = runtime.relative_to(home).as_posix()

    def replace_before_backup(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        if operation["destination"] != runtime_relative:
            return
        if race_kind == "replacement":
            concurrent = runtime.with_name(f"{runtime.name}.concurrent")
            concurrent.write_bytes(concurrent_content)
            concurrent.chmod(0o755)
            os.replace(concurrent, runtime)
        else:
            runtime.unlink()

    original_rollback = transaction_module._rollback_record

    def exit_after_journal(*_args: object) -> None:
        if exit_point == "journaled-backup":
            os._exit(84)

    def exit_after_concurrent_retention(
        home_fs: object,
        record: dict[str, object],
        record_path: Path,
        **kwargs: object,
    ) -> None:
        if (
            exit_point == "restored-destination"
            and record.get("prime_gstack_concurrent_mutation") is not None
        ):
            os._exit(84)
        original_rollback(home_fs, record, record_path, **kwargs)

    monkeypatch.setattr(transaction_module, "_before_operation_mutation", replace_before_backup)
    monkeypatch.setattr(
        transaction_module,
        "_after_prime_gstack_concurrent_journal",
        exit_after_journal,
    )
    monkeypatch.setattr(transaction_module, "_rollback_record", exit_after_concurrent_retention)
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((plan,))
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 84

    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    monkeypatch.setattr(transaction_module, "_rollback_record", original_rollback)
    monkeypatch.setattr(transaction_module, "_before_operation_mutation", lambda *_args: None)
    monkeypatch.setattr(
        transaction_module,
        "_after_prime_gstack_concurrent_journal",
        lambda *_args: None,
    )

    recover_transaction(record_path)

    if race_kind == "replacement":
        assert runtime.read_bytes() == concurrent_content
    else:
        assert not runtime.exists()
    assert skill.read_bytes() == b"legacy skill\n"
    assert legacy_manifest.read_bytes() == legacy_manifest_bytes
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))

    recover_transaction(record_path)

    if race_kind == "replacement":
        assert runtime.read_bytes() == concurrent_content
    else:
        assert not runtime.exists()


def test_prime_gstack_concurrent_entry_recovery_restores_after_backup_move_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan, runtime, skill, legacy_manifest, legacy_manifest_bytes = _prime_gstack_legacy_plan(home)
    concurrent_content = b"concurrent future skill before backup exit\n"
    runtime_before = runtime.read_bytes()
    runtime_relative = runtime.relative_to(home).as_posix()
    skill_relative = skill.relative_to(home)
    changed = False

    def replace_skill_after_runtime(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        nonlocal changed
        if changed or operation["destination"] != runtime_relative:
            return
        concurrent = skill.with_name(f"{skill.name}.concurrent")
        concurrent.write_bytes(concurrent_content)
        concurrent.chmod(0o644)
        os.replace(concurrent, skill)
        changed = True

    original_replace = transaction_module._HomeFS.replace

    def exit_after_concurrent_backup_move(
        home_fs: object,
        source: Path,
        destination: Path,
    ) -> None:
        original_replace(home_fs, source, destination)
        if source == skill_relative and destination.parts[-2:] == ("backups", "0001"):
            os._exit(85)

    monkeypatch.setattr(
        transaction_module,
        "_after_operation_mutation",
        replace_skill_after_runtime,
    )
    monkeypatch.setattr(
        transaction_module._HomeFS,
        "replace",
        exit_after_concurrent_backup_move,
    )
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((plan,))
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 85

    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    concurrent = json.loads(record_path.read_text())["prime_gstack_concurrent_mutation"]
    assert concurrent["destination"] == skill_relative.as_posix()
    assert concurrent["kind"] == "regular"
    assert concurrent["identity"]["inode"] > 0
    monkeypatch.setattr(transaction_module._HomeFS, "replace", original_replace)
    monkeypatch.setattr(transaction_module, "_after_operation_mutation", lambda *_args: None)

    recover_transaction(record_path)

    assert runtime.read_bytes() == runtime_before
    assert skill.read_bytes() == concurrent_content
    assert legacy_manifest.read_bytes() == legacy_manifest_bytes
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))

    recover_transaction(record_path)

    assert runtime.read_bytes() == runtime_before
    assert skill.read_bytes() == concurrent_content


def test_prime_gstack_concurrent_entry_recovery_restores_after_classification_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan, runtime, skill, legacy_manifest, legacy_manifest_bytes = _prime_gstack_legacy_plan(home)
    concurrent_content = b"concurrent skill after legacy classification\n"
    runtime_before = runtime.read_bytes()
    skill_relative = skill.relative_to(home)
    original_matches_legacy = transaction_module._PinnedPrimeGstackCurrentFile.matches_legacy
    original_replace = transaction_module._HomeFS.replace

    def replace_after_legacy_classification(
        current: object,
        pinned: object,
    ) -> bool:
        matches = original_matches_legacy(current, pinned)
        if matches and current.path == skill_relative:
            concurrent = skill.with_name(f"{skill.name}.concurrent")
            concurrent.write_bytes(concurrent_content)
            concurrent.chmod(0o644)
            os.replace(concurrent, skill)
        return matches

    def exit_after_concurrent_backup_move(
        home_fs: object,
        source: Path,
        destination: Path,
    ) -> None:
        original_replace(home_fs, source, destination)
        if source == skill_relative and destination.parts[-2:] == ("backups", "0001"):
            os._exit(86)

    monkeypatch.setattr(
        transaction_module._PinnedPrimeGstackCurrentFile,
        "matches_legacy",
        replace_after_legacy_classification,
    )
    monkeypatch.setattr(
        transaction_module._HomeFS,
        "replace",
        exit_after_concurrent_backup_move,
    )
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((plan,))
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 86

    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    monkeypatch.setattr(transaction_module._HomeFS, "replace", original_replace)
    monkeypatch.setattr(
        transaction_module._PinnedPrimeGstackCurrentFile,
        "matches_legacy",
        original_matches_legacy,
    )

    recover_transaction(record_path)

    assert runtime.read_bytes() == runtime_before
    assert skill.read_bytes() == concurrent_content
    assert legacy_manifest.read_bytes() == legacy_manifest_bytes
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))

    recover_transaction(record_path)

    assert runtime.read_bytes() == runtime_before
    assert skill.read_bytes() == concurrent_content


def test_prime_gstack_legacy_adoption_recovery_restores_exact_prestate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan, runtime, skill, legacy_manifest, legacy_manifest_bytes = _prime_gstack_legacy_plan(home)
    control = SystemExit(82)

    def interrupt_after_backup(*_args: object) -> None:
        raise control

    def interrupt_automatic_rollback(*_args: object, **_kwargs: object) -> None:
        raise SystemExit(83)

    original_rollback = transaction_module._rollback_record
    monkeypatch.setattr(transaction_module, "_after_operation_backup", interrupt_after_backup)
    monkeypatch.setattr(transaction_module, "_rollback_record", interrupt_automatic_rollback)

    with pytest.raises(SystemExit):
        install_provider_plans((plan,))

    records = list((home / ".agentops/deployment/transactions").glob("*/record.json"))
    assert len(records) == 1
    monkeypatch.setattr(transaction_module, "_rollback_record", original_rollback)
    monkeypatch.setattr(transaction_module, "_after_operation_backup", lambda *_args: None)

    recover_transaction(records[0])

    assert runtime.read_bytes() == b"legacy executable\n"
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o755
    assert skill.read_bytes() == b"legacy skill\n"
    assert stat.S_IMODE(skill.stat().st_mode) == 0o644
    assert legacy_manifest.read_bytes() == legacy_manifest_bytes
    assert stat.S_IMODE(legacy_manifest.stat().st_mode) == 0o644
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))


def test_audit_accepts_only_opted_in_exact_python_cache(tmp_path: Path) -> None:
    home = tmp_path / "home"
    source = Path("skills/private/src/package/module.py")
    plan = ProviderPlan(
        "private", "1" * 40, TargetSpec("codex", Framework.CODEX, home, "stable"),
        (PlannedFile(source, b"value = 1\n", 0o644),),
        audit_roots=(Path("skills/private"),), runtime_python_sources=(source,),
    )
    install_provider_plans((plan,))
    py_compile.compile(home / source, doraise=True)
    assert audit_provider_plans((plan,)).matches
    cache = next((home / source.parent / "__pycache__").glob("*.pyc"))
    cache.write_bytes(cache.read_bytes() + b"tampered")
    assert not audit_provider_plans((plan,)).matches


def test_refresh_preserves_unchanged_managed_python_source_and_runtime_cache(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("prime", Framework.PRIME_AGENT, home, "stable")
    connector_skill = "d" + "mc-connectors"
    module_name = "d" + "mc_connectors"
    source = Path("skills") / connector_skill / "src" / module_name / "__init__.py"
    initial = ProviderPlan(
        "catalog-skills",
        "1" * 40,
        target,
        (PlannedFile(source, b"VALUE = 'managed'\n", 0o644),),
        audit_roots=(Path("skills") / connector_skill,),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    installed_source = home / source
    fixed_mtime_ns = 1_700_000_000_000_000_000
    os.utime(installed_source, ns=(fixed_mtime_ns, fixed_mtime_ns))
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(installed_source.parents[1])
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    imported = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            f"import {module_name}; print({module_name}.__file__)",
        ],
        check=True,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert Path(imported.stdout.strip()).resolve() == installed_source.resolve()
    cache = installed_source.parent / "__pycache__" / (
        f"__init__.{sys.implementation.cache_tag}.pyc"
    )
    assert cache.is_file()
    source_before = installed_source.stat()
    cache_before = cache.read_bytes()

    refreshed = ProviderPlan(
        "catalog-skills",
        "2" * 40,
        target,
        initial.files,
        audit_roots=initial.audit_roots,
        runtime_python_sources=initial.runtime_python_sources,
    )
    install_provider_plans((refreshed,))

    source_after = installed_source.stat()
    assert (source_after.st_dev, source_after.st_ino) == (
        source_before.st_dev,
        source_before.st_ino,
    )
    assert stat.S_IMODE(source_after.st_mode) == stat.S_IMODE(source_before.st_mode)
    assert source_after.st_mtime_ns == source_before.st_mtime_ns
    assert cache.read_bytes() == cache_before
    assert audit_provider_plans((refreshed,)).matches


def test_refresh_rewrites_changed_managed_python_source_and_rejects_stale_cache(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("prime", Framework.PRIME_AGENT, home, "stable")
    connector_skill = "d" + "mc-connectors"
    module_name = "d" + "mc_connectors"
    source = Path("skills") / connector_skill / "src" / module_name / "__init__.py"
    initial = ProviderPlan(
        "catalog-skills",
        "1" * 40,
        target,
        (PlannedFile(source, b"VALUE = 'old'\n", 0o644),),
        audit_roots=(Path("skills") / connector_skill,),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    installed_source = home / source
    fixed_mtime_ns = 1_700_000_000_000_000_000
    os.utime(installed_source, ns=(fixed_mtime_ns, fixed_mtime_ns))
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(installed_source.parents[1])
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    subprocess.run(
        [sys.executable, "-S", "-c", f"import {module_name}"],
        check=True,
        cwd=tmp_path,
        env=environment,
    )
    cache = installed_source.parent / "__pycache__" / (
        f"__init__.{sys.implementation.cache_tag}.pyc"
    )
    source_before = installed_source.stat()
    assert cache.is_file()

    refreshed = ProviderPlan(
        "catalog-skills",
        "2" * 40,
        target,
        (PlannedFile(source, b"VALUE = 'new'\n", 0o644),),
        audit_roots=initial.audit_roots,
        runtime_python_sources=initial.runtime_python_sources,
    )
    install_provider_plans((refreshed,))

    source_after = installed_source.stat()
    assert (source_after.st_dev, source_after.st_ino) != (
        source_before.st_dev,
        source_before.st_ino,
    )
    audit = audit_provider_plans((refreshed,))
    assert not audit.matches
    assert audit.unexpected == (cache.relative_to(home).as_posix(),)


def test_refresh_removes_exact_runtime_cache_with_its_prior_managed_source(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        audit_roots=(Path("skills/private"),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    cache = Path(importlib.util.cache_from_source(str(source)))
    os.utime(home / source, (1_700_000_000, 1_700_000_000))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    assert audit_provider_plans((initial,)).matches

    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source, cache),
        audit_roots=(Path("skills/private"),),
    )
    install_provider_plans((replacement,))

    assert not (home / source).exists()
    assert not (home / cache).exists()
    assert audit_provider_plans((replacement,)).matches


def test_retired_source_discovers_supported_default_and_optimized_caches(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    source_bytes = b'"""retired"""\nassert True\nvalue = "retired"\n'
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, source_bytes, 0o644),),
        audit_roots=(Path("skills/private"),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    installed_source = home / source
    os.utime(installed_source, (1_700_000_000, 1_700_000_000))
    current_tag = sys.implementation.cache_tag
    assert current_tag in _CPYTHON_CACHE_MAGICS
    caches: list[Path] = []
    for tag in _CPYTHON_CACHE_MAGICS:
        for optimization in (None, "1", "2"):
            cache = _cache_path(source, tag, optimization)
            absolute_cache = home / cache
            absolute_cache.parent.mkdir(parents=True, exist_ok=True)
            if tag == current_tag:
                py_compile.compile(
                    installed_source,
                    cfile=absolute_cache,
                    doraise=True,
                    optimize=-1 if optimization is None else int(optimization),
                )
            else:
                absolute_cache.write_bytes(_foreign_timestamp_cache(installed_source, tag))
                absolute_cache.chmod(0o644)
            caches.append(cache)

    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source,),
        audit_roots=(Path("skills/private"),),
    )
    manifests = install_provider_plans((replacement,))

    assert not installed_source.exists()
    assert all(not (home / cache).exists() for cache in caches)
    record_path = transaction_module._TRANSACTION_PATHS[manifests[0].transaction_id]
    record = json.loads(record_path.read_text())
    recorded = {
        Path(operation["destination"])
        for operation in record["operations"]
        if operation["kind"] == "runtime-cache-removal"
    }
    assert recorded == set(caches)
    assert audit_provider_plans((replacement,)).matches


@pytest.mark.parametrize(
    "case",
    [
        "wrong-stem",
        "wrong-tag",
        "opt-3",
        "wrong-mode",
        "hard-link",
        "hash-based",
        "wrong-timestamp",
        "wrong-size",
        "wrong-magic",
        "short-header",
        "oversized",
        "symlink",
    ],
)
def test_retired_source_preserves_untrusted_cache_candidates(
    tmp_path: Path,
    case: str,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        audit_roots=(Path("skills/private"),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    installed_source = home / source
    os.utime(installed_source, (1_700_000_000, 1_700_000_000))
    foreign_tag = next(tag for tag in _CPYTHON_CACHE_MAGICS if tag != sys.implementation.cache_tag)
    candidate = _cache_path(source, foreign_tag)
    if case == "wrong-stem":
        candidate = candidate.with_name(f"other.{foreign_tag}.pyc")
    elif case == "wrong-tag":
        candidate = candidate.with_name(f"{source.stem}.cpython-399.pyc")
    elif case == "opt-3":
        candidate = _cache_path(source, foreign_tag, "3")
    absolute_candidate = home / candidate
    absolute_candidate.parent.mkdir(parents=True, exist_ok=True)
    content = _foreign_timestamp_cache(installed_source, foreign_tag)
    if case == "hash-based":
        content = content[:4] + b"\x03\0\0\0" + content[8:]
    elif case == "wrong-timestamp":
        content = content[:8] + (1_700_000_001).to_bytes(4, "little") + content[12:]
    elif case == "wrong-size":
        wrong_size = (len(installed_source.read_bytes()) + 1).to_bytes(4, "little")
        content = content[:12] + wrong_size + content[16:]
    elif case == "wrong-magic":
        content = b"BAD!" + content[4:]
    elif case == "short-header":
        content = content[:15]
    elif case == "oversized":
        content += b"x" * (16 * 1024 * 1024)
    if case == "symlink":
        external = tmp_path / "foreign.pyc"
        external.write_bytes(content)
        absolute_candidate.symlink_to(external)
    else:
        absolute_candidate.write_bytes(content)
        absolute_candidate.chmod(0o600 if case == "wrong-mode" else 0o644)
        if case == "hard-link":
            os.link(absolute_candidate, tmp_path / "foreign.pyc")

    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source,),
        audit_roots=(Path("skills/private"),),
    )
    install_provider_plans((replacement,))

    assert not installed_source.exists()
    assert absolute_candidate.exists() or absolute_candidate.is_symlink()
    audit = audit_provider_plans((replacement,))
    assert not audit.matches
    assert candidate.as_posix() in audit.unexpected


def test_discovered_runtime_cache_rollback_restores_exact_prestate(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    installed_source = home / source
    os.utime(installed_source, (1_700_000_000, 1_700_000_000))
    foreign_tag = next(tag for tag in _CPYTHON_CACHE_MAGICS if tag != sys.implementation.cache_tag)
    caches = (_cache_path(source, foreign_tag), _cache_path(source, foreign_tag, "1"))
    for cache in caches:
        absolute_cache = home / cache
        absolute_cache.parent.mkdir(parents=True, exist_ok=True)
        absolute_cache.write_bytes(_foreign_timestamp_cache(installed_source, foreign_tag))
        absolute_cache.chmod(0o644)
    source_prestate = installed_source.read_bytes()
    cache_prestates = {cache: (home / cache).read_bytes() for cache in caches}

    replacement = ProviderPlan("private", "2" * 40, target, (), removals=(source,))
    manifests = install_provider_plans((replacement,))
    assert not installed_source.exists()
    assert all(not (home / cache).exists() for cache in caches)
    rollback_manifests(manifests)

    assert installed_source.read_bytes() == source_prestate
    assert {cache: (home / cache).read_bytes() for cache in caches} == cache_prestates


def test_discovered_runtime_cache_recovery_restores_exact_prestate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    installed_source = home / source
    os.utime(installed_source, (1_700_000_000, 1_700_000_000))
    foreign_tag = next(tag for tag in _CPYTHON_CACHE_MAGICS if tag != sys.implementation.cache_tag)
    caches = (_cache_path(source, foreign_tag), _cache_path(source, foreign_tag, "1"))
    for cache in caches:
        absolute_cache = home / cache
        absolute_cache.parent.mkdir(parents=True, exist_ok=True)
        absolute_cache.write_bytes(_foreign_timestamp_cache(installed_source, foreign_tag))
        absolute_cache.chmod(0o644)
    source_prestate = installed_source.read_bytes()
    cache_prestates = {cache: (home / cache).read_bytes() for cache in caches}
    crash_destination = caches[-1].as_posix()

    def crash_after_second_cache_backup(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        if operation["destination"] == crash_destination:
            os._exit(92)

    monkeypatch.setattr(
        transaction_module,
        "_after_operation_backup",
        crash_after_second_cache_backup,
    )
    replacement = ProviderPlan("private", "2" * 40, target, (), removals=(source,))
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((replacement,))
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 92
    record_path = next(
        candidate
        for candidate in (home / ".agentops/deployment/transactions").glob("*/record.json")
        if any(
            operation["destination"] == crash_destination
            for operation in json.loads(candidate.read_text())["operations"]
        )
    )

    recover_transaction(record_path)

    assert installed_source.read_bytes() == source_prestate
    assert {cache: (home / cache).read_bytes() for cache in caches} == cache_prestates


def test_runtime_cache_removal_rejects_forged_backup_and_record_fingerprint(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    cache = Path(importlib.util.cache_from_source(str(source)))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source, cache),
    )
    manifests = install_provider_plans((replacement,))
    record_path = transaction_module._TRANSACTION_PATHS[manifests[0].transaction_id]
    record = json.loads(record_path.read_text())
    operation = next(
        item for item in record["operations"] if item["kind"] == "runtime-cache-removal"
    )
    forged = b"forged bytecode"
    backup = home / operation["backup"]
    backup.write_bytes(forged)
    backup.chmod(0o644)
    operation["prior_fingerprint"] = hashlib.sha256(forged).hexdigest()
    record_path.write_text(json.dumps(record))

    with pytest.raises(ValueError, match="runtime cache.*evidence"):
        rollback_manifests(manifests)

    assert not (home / source).exists()
    assert not (home / cache).exists()


def test_runtime_cache_removal_rejects_changed_source_backup(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    cache = Path(importlib.util.cache_from_source(str(source)))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source, cache),
    )
    manifests = install_provider_plans((replacement,))
    record_path = transaction_module._TRANSACTION_PATHS[manifests[0].transaction_id]
    record = json.loads(record_path.read_text())
    source_operation = next(
        item for item in record["operations"] if item["destination"] == source.as_posix()
    )
    source_backup = home / source_operation["backup"]
    source_backup.write_bytes(b"changed source\n")
    source_backup.chmod(0o644)

    with pytest.raises(ValueError, match="runtime cache.*evidence"):
        rollback_manifests(manifests)

    assert not (home / source).exists()
    assert not (home / cache).exists()


@pytest.mark.parametrize(
    "case",
    [
        "wrong-tag",
        "wrong-mode",
        "changed-content",
        "symlink",
        "source-not-removed",
        "source-not-prior-managed",
        "other-file",
    ],
)
def test_runtime_cache_removal_rejects_untrusted_candidates(
    tmp_path: Path,
    case: str,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial_source = source
    if case == "source-not-prior-managed":
        initial_source = Path("skills/private/src/managed.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(initial_source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(initial_source,),
    )
    install_provider_plans((initial,))
    if case == "source-not-prior-managed":
        (home / source).write_bytes(b"value = 'unmanaged'\n")
        (home / source).chmod(0o644)
    cache = Path(importlib.util.cache_from_source(str(source)))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    candidate = cache
    removals = (source, cache)
    if case == "wrong-tag":
        candidate = cache.with_name("sitecustomize.cpython-000.pyc")
        (home / cache).rename(home / candidate)
        removals = (source, candidate)
    elif case == "wrong-mode":
        (home / cache).chmod(0o600)
    elif case == "changed-content":
        (home / cache).write_bytes((home / cache).read_bytes() + b"changed")
    elif case == "symlink":
        external = tmp_path / "external.pyc"
        external.write_bytes((home / cache).read_bytes())
        (home / cache).unlink()
        (home / cache).symlink_to(external)
    elif case == "source-not-removed":
        removals = (cache,)
    elif case == "other-file":
        candidate = Path("skills/private/src/unrelated.txt")
        (home / candidate).write_text("unrelated\n")
        removals = (source, candidate)
    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=removals,
    )

    with pytest.raises(ValueError, match="refusing to remove unmanaged destination"):
        install_provider_plans((replacement,))

    assert (home / candidate).exists() or (home / candidate).is_symlink()


def test_runtime_cache_removal_requires_source_removal_in_the_same_provider_plan(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "source-owner",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    cache = Path(importlib.util.cache_from_source(str(source)))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    source_removal = ProviderPlan(
        "source-owner",
        "2" * 40,
        target,
        (),
        removals=(source,),
    )
    cache_removal = ProviderPlan(
        "cache-remover",
        "2" * 40,
        target,
        (),
        removals=(cache,),
    )

    with pytest.raises(ValueError, match="refusing to remove unmanaged destination"):
        install_provider_plans((source_removal, cache_removal))

    assert (home / source).is_file()
    assert (home / cache).is_file()


@pytest.mark.parametrize(
    ("phase", "hook_name", "exit_code"),
    [
        ("before-cache", "_before_operation_mutation", 96),
        ("after-cache-backup", "_after_operation_backup", 97),
        ("after-source-backup", "_after_operation_backup", 98),
    ],
)
def test_runtime_cache_removal_crash_rolls_back_exact_prestate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    hook_name: str,
    exit_code: int,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    cache = Path(importlib.util.cache_from_source(str(source)))
    os.utime(home / source, (1_700_000_000, 1_700_000_000))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    source_before = (home / source).read_bytes()
    cache_before = (home / cache).read_bytes()
    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source, cache),
    )
    original_hook = getattr(transaction_module, hook_name)

    def terminate_at_selected_phase(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        cache_phase = phase in {"before-cache", "after-cache-backup"}
        if (
            cache_phase
            and operation["kind"] == "runtime-cache-removal"
            or phase == "after-source-backup"
            and operation["destination"] == source.as_posix()
        ):
            os._exit(exit_code)

    monkeypatch.setattr(
        transaction_module,
        hook_name,
        terminate_at_selected_phase,
    )
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((replacement,))
    )
    process.start()
    process.join(10)
    assert process.exitcode == exit_code
    monkeypatch.setattr(transaction_module, hook_name, original_hook)
    matching_records = []
    for candidate in (home / ".agentops/deployment/transactions").glob("*/record.json"):
        record = json.loads(candidate.read_text())
        if any(operation["kind"] == "runtime-cache-removal" for operation in record["operations"]):
            matching_records.append(candidate)
    assert len(matching_records) == 1
    record_path = matching_records[0]

    recover_transaction(record_path)

    assert (home / source).read_bytes() == source_before
    assert (home / cache).read_bytes() == cache_before
    assert audit_provider_plans((initial,)).matches


def test_runtime_cache_recovery_rejects_replaced_backup_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    cache = Path(importlib.util.cache_from_source(str(source)))
    os.utime(home / source, (1_700_000_000, 1_700_000_000))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source, cache),
    )

    def crash_after_cache_backup(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        if operation["kind"] == "runtime-cache-removal":
            os._exit(99)

    monkeypatch.setattr(
        transaction_module,
        "_after_operation_backup",
        crash_after_cache_backup,
    )
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((replacement,))
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 99
    record_path = next(
        candidate
        for candidate in (home / ".agentops/deployment/transactions").glob("*/record.json")
        if any(
            operation["kind"] == "runtime-cache-removal"
            for operation in json.loads(candidate.read_text())["operations"]
        )
    )
    record = json.loads(record_path.read_text())
    operation = next(
        item for item in record["operations"] if item["kind"] == "runtime-cache-removal"
    )
    backup = home / operation["backup"]
    backup_bytes = backup.read_bytes()
    original_identity = backup.stat().st_dev, backup.stat().st_ino
    replacement_backup = backup.with_name("replacement-backup")
    replacement_backup.write_bytes(backup_bytes)
    replacement_backup.chmod(0o644)
    os.replace(replacement_backup, backup)
    assert (backup.stat().st_dev, backup.stat().st_ino) != original_identity

    with pytest.raises(IncompleteRollbackError, match="unauthorized runtime cache preserved"):
        recover_transaction(record_path)

    assert not backup.exists()
    assert (home / cache).read_bytes() == backup_bytes
    assert ((home / cache).stat().st_dev, (home / cache).stat().st_ino) != original_identity
    assert record_path.exists()


def test_runtime_cache_removal_preserves_replacement_after_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    cache = Path(importlib.util.cache_from_source(str(source)))
    os.utime(home / source, (1_700_000_000, 1_700_000_000))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source, cache),
    )
    authorized_bytes = (home / cache).read_bytes()
    authorized_identity = (home / cache).stat().st_dev, (home / cache).stat().st_ino
    original_hook = transaction_module._before_operation_mutation

    def replace_authorized_cache(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        if operation["kind"] != "runtime-cache-removal":
            return
        candidate = home / "replacement.pyc"
        candidate.write_bytes(authorized_bytes)
        candidate.chmod(0o644)
        os.replace(candidate, home / cache)

    monkeypatch.setattr(
        transaction_module,
        "_before_operation_mutation",
        replace_authorized_cache,
    )

    with pytest.raises(IncompleteRollbackError, match="runtime cache removal changed"):
        install_provider_plans((replacement,))

    monkeypatch.setattr(transaction_module, "_before_operation_mutation", original_hook)
    assert (home / cache).read_bytes() == authorized_bytes
    assert ((home / cache).stat().st_dev, (home / cache).stat().st_ino) != authorized_identity
    assert (home / source).read_bytes() == b"value = 'retired'\n"


def test_runtime_cache_recovery_restores_raced_cache_after_backup_move_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    cache = Path(importlib.util.cache_from_source(str(source)))
    os.utime(home / source, (1_700_000_000, 1_700_000_000))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source, cache),
    )
    raced_bytes = (home / cache).read_bytes()

    def replace_authorized_cache(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        if operation["kind"] != "runtime-cache-removal":
            return
        candidate = home / "raced.pyc"
        candidate.write_bytes(raced_bytes)
        candidate.chmod(0o644)
        os.replace(candidate, home / cache)

    def crash_after_backup_move(
        _home_fs: object,
        _destination: Path,
        _backup: Path,
        _operation: dict[str, object],
    ) -> None:
        os._exit(95)

    monkeypatch.setattr(
        transaction_module,
        "_before_operation_mutation",
        replace_authorized_cache,
    )
    monkeypatch.setattr(
        transaction_module,
        "_after_runtime_cache_backup_move",
        crash_after_backup_move,
        raising=False,
    )
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((replacement,))
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 95
    record_path = next(
        candidate
        for candidate in (home / ".agentops/deployment/transactions").glob("*/record.json")
        if any(
            operation["kind"] == "runtime-cache-removal"
            for operation in json.loads(candidate.read_text())["operations"]
        )
    )
    record = json.loads(record_path.read_text())
    operation = next(
        item for item in record["operations"] if item["kind"] == "runtime-cache-removal"
    )
    backup = home / operation["backup"]

    assert not (home / cache).exists()
    assert backup.read_bytes() == raced_bytes

    with pytest.raises(IncompleteRollbackError, match="unauthorized runtime cache preserved"):
        recover_transaction(record_path)

    assert (home / cache).read_bytes() == raced_bytes
    assert not backup.exists()


def test_runtime_cache_removal_recovery_uses_recorded_cache_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    cache = Path(importlib.util.cache_from_source(str(source)))
    os.utime(home / source, (1_700_000_000, 1_700_000_000))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    source_before = (home / source).read_bytes()
    cache_before = (home / cache).read_bytes()
    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source, cache),
    )

    def crash_after_cache_backup(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        if operation["kind"] == "runtime-cache-removal":
            os._exit(98)

    monkeypatch.setattr(
        transaction_module,
        "_after_operation_backup",
        crash_after_cache_backup,
    )
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((replacement,))
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 98
    record_path = next(
        candidate
        for candidate in (home / ".agentops/deployment/transactions").glob("*/record.json")
        if any(
            operation["kind"] == "runtime-cache-removal"
            for operation in json.loads(candidate.read_text())["operations"]
        )
    )

    monkeypatch.setattr(sys, "pycache_prefix", str(tmp_path / "alternate-cache-root"))
    monkeypatch.setattr(sys.implementation, "cache_tag", "cpython-alternate")
    monkeypatch.setattr(transaction_module.importlib.util, "MAGIC_NUMBER", b"ALTR")

    assert recover_transaction(record_path).source_revision == "2" * 40
    assert (home / source).read_bytes() == source_before
    assert (home / cache).read_bytes() == cache_before


def test_schema_six_runtime_cache_recovery_keeps_operation_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    cache = Path(importlib.util.cache_from_source(str(source)))
    os.utime(home / source, (1_700_000_000, 1_700_000_000))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source, cache),
    )

    def crash_after_cache_backup(
        _home_fs: object,
        _record_path: Path,
        operation: dict[str, object],
    ) -> None:
        if operation["kind"] == "runtime-cache-removal":
            os._exit(97)

    monkeypatch.setattr(
        transaction_module,
        "_after_operation_backup",
        crash_after_cache_backup,
    )
    process = multiprocessing.get_context("fork").Process(
        target=lambda: install_provider_plans((replacement,))
    )
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 97
    record_path = next(
        candidate
        for candidate in (home / ".agentops/deployment/transactions").glob("*/record.json")
        if any(
            operation["kind"] == "runtime-cache-removal"
            for operation in json.loads(candidate.read_text())["operations"]
        )
    )
    record = json.loads(record_path.read_text())
    record["schema_version"] = 6
    for operation in record["operations"]:
        operation.pop("runtime_cache_provenance", None)
    record_path.write_text(json.dumps(record))

    assert recover_transaction(record_path).source_revision == "2" * 40


def test_schema_seven_runtime_cache_provenance_remains_readable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex", Framework.CODEX, home, "stable")
    source = Path("skills/private/src/sitecustomize.py")
    initial = ProviderPlan(
        "private",
        "1" * 40,
        target,
        (PlannedFile(source, b"value = 'retired'\n", 0o644),),
        runtime_python_sources=(source,),
    )
    install_provider_plans((initial,))
    cache = Path(importlib.util.cache_from_source(str(source)))
    py_compile.compile(home / source, cfile=home / cache, doraise=True)
    replacement = ProviderPlan(
        "private",
        "2" * 40,
        target,
        (),
        removals=(source, cache),
    )
    manifest = install_provider_plans((replacement,))[0]
    record_path = transaction_module._TRANSACTION_PATHS[manifest.transaction_id]
    record = json.loads(record_path.read_text())
    record["schema_version"] = 7
    record_path.write_text(json.dumps(record))

    assert recover_transaction(record_path) == manifest


def test_ordinary_install_rejects_prior_manifest_with_contradictory_channel(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex-dev", Framework.CODEX, home, "stable")
    original = ProviderPlan(
        "fixture",
        "1" * 40,
        target,
        (PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o644),),
    )
    install_provider_plans((original,))
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    manifest = json.loads(manifest_path.read_text())
    manifest["channel"] = "feature"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest_path.chmod(0o600)
    replacement = ProviderPlan(
        "fixture",
        "2" * 40,
        target,
        (PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),),
    )

    with pytest.raises(ValueError, match="channel"):
        install_provider_plans((replacement,))

    assert (home / "skills/example/SKILL.md").read_bytes() == b"old\n"
    assert json.loads(manifest_path.read_text())["channel"] == "feature"


def test_explicit_channel_transition_allows_only_expected_prior_to_candidate(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    stable = TargetSpec("codex-dev", Framework.CODEX, home, "stable")
    install_provider_plans(
        (
            ProviderPlan(
                "fixture",
                "1" * 40,
                stable,
                (PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o644),),
            ),
        )
    )
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    stable_manifest = manifest_path.read_bytes()
    feature = TargetSpec("codex-dev", Framework.CODEX, home, "feature")
    replacement = ProviderPlan(
        "fixture",
        "2" * 40,
        feature,
        (PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),),
    )

    with pytest.raises(ValueError, match="channel"):
        install_provider_plans(
            (replacement,),
            channel_transitions=(
                TargetChannelTransition("codex-dev", "wrong", "feature"),
            ),
        )

    manifests = install_provider_plans(
        (replacement,),
        channel_transitions=(
            TargetChannelTransition("codex-dev", "stable", "feature"),
        ),
    )
    assert manifests[0].channel == "feature"
    assert (home / "skills/example/SKILL.md").read_bytes() == b"new\n"
    rollback_manifests(manifests)
    assert (home / "skills/example/SKILL.md").read_bytes() == b"old\n"
    assert manifest_path.read_bytes() == stable_manifest


def test_channel_transition_process_control_is_preserved_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    plan = _plan(
        home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644)
    )
    control = KeyboardInterrupt("operator stopped channel validation")

    def interrupt(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise control

    monkeypatch.setattr(transaction_module, "_channel_transitions", interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        install_provider_plans((plan,))

    assert caught.value is control
    assert not home.exists()


class _TransitionStringSubclass(str):
    pass


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_id", True),
        ("target_id", 1),
        ("target_id", Path("codex-dev")),
        ("target_id", _TransitionStringSubclass("codex-dev")),
        ("expected_prior_channel", True),
        ("expected_prior_channel", 1),
        ("expected_prior_channel", Path("stable")),
        ("expected_prior_channel", _TransitionStringSubclass("stable")),
        ("candidate_channel", True),
        ("candidate_channel", 1),
        ("candidate_channel", Path("feature")),
        ("candidate_channel", _TransitionStringSubclass("feature")),
    ],
)
def test_invalid_channel_transition_cannot_create_initial_install_state(
    tmp_path: Path, field: str, value: object
) -> None:
    home = tmp_path / "home"
    plan = ProviderPlan(
        "fixture",
        "1" * 40,
        TargetSpec("codex-dev", Framework.CODEX, home, "feature"),
        (PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644),),
    )
    values: dict[str, object] = {
        "target_id": "codex-dev",
        "expected_prior_channel": "stable",
        "candidate_channel": "feature",
    }
    values[field] = value

    with pytest.raises(ValueError, match="nonempty exact string"):
        transition = TargetChannelTransition(**values)  # type: ignore[arg-type]
        install_provider_plans((plan,), channel_transitions=(transition,))

    assert not home.exists()


@pytest.mark.parametrize(
    "expected_prior_channel,candidate_channel",
    [("feature", "feature"), ("stable", "feature")],
)
def test_first_install_channel_transition_round_trips_through_recovery_and_rollback(
    tmp_path: Path, expected_prior_channel: str, candidate_channel: str
) -> None:
    home = tmp_path / "home"
    plan = ProviderPlan(
        "fixture",
        "1" * 40,
        TargetSpec("codex-dev", Framework.CODEX, home, candidate_channel),
        (PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644),),
    )
    transition = TargetChannelTransition(
        "codex-dev", expected_prior_channel, candidate_channel
    )

    manifest = install_provider_plans(
        (plan,), channel_transitions=(transition,)
    )[0]
    record_path = (
        home
        / ".agentops/deployment/transactions"
        / manifest.transaction_id
        / "record.json"
    )
    record = json.loads(record_path.read_text())
    assert record["expected_prior_channel"] == expected_prior_channel
    assert record["candidate_channel"] == candidate_channel
    assert record["prior_manifest_content"] is None
    assert recover_transaction(record_path) == manifest

    rollback_manifests((manifest,))

    assert not (home / "skills/example/SKILL.md").exists()
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))


@pytest.mark.parametrize("field", ["expected_prior_channel", "candidate_channel"])
@pytest.mark.parametrize("operation", ["rollback", "recover"])
def test_rollback_rejects_tampered_channel_transition_record(
    tmp_path: Path, field: str, operation: str
) -> None:
    home = tmp_path / "home"
    original = _plan(
        home, PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o644)
    )
    install_provider_plans((original,))
    replacement = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),
        revision="2" * 40,
    )
    manifests = install_provider_plans((replacement,))
    record_path = (
        home
        / ".agentops/deployment/transactions"
        / manifests[0].transaction_id
        / "record.json"
    )
    record = json.loads(record_path.read_text())
    record[field] = "tampered"
    record_path.write_text(json.dumps(record))

    with pytest.raises(ValueError, match="channel"):
        if operation == "rollback":
            rollback_manifests(manifests)
        else:
            recover_transaction(record_path)

    assert (home / "skills/example/SKILL.md").read_bytes() == b"new\n"


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


def test_later_target_install_failure_rolls_back_every_earlier_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_home = tmp_path / "a-home"
    second_home = tmp_path / "b-home"
    first = ProviderPlan(
        "fixture",
        "1" * 40,
        TargetSpec("first", Framework.CODEX, first_home, "feature"),
        (PlannedFile(Path("skills/example/SKILL.md"), b"first\n", 0o640),),
    )
    second = ProviderPlan(
        "fixture",
        "1" * 40,
        TargetSpec("second", Framework.CODEX, second_home, "feature"),
        (PlannedFile(Path("skills/example/SKILL.md"), b"second\n", 0o644),),
    )
    original_hook = transaction_module._before_manifest_replace

    def fail_second(home_fs: object, *args: object) -> None:
        if home_fs.home == second_home:
            raise OSError("injected second target failure")
        original_hook(home_fs, *args)

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", fail_second)

    with pytest.raises(OSError, match="injected second target failure"):
        install_provider_plans((second, first))

    assert not (first_home / "skills/example/SKILL.md").exists()
    assert not (second_home / "skills/example/SKILL.md").exists()
    assert not list((first_home / ".agentops/deployment/manifests").glob("*.json"))
    assert not list((second_home / ".agentops/deployment/manifests").glob("*.json"))


def test_grouped_recovery_process_control_is_preserved_after_ordinary_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_home = tmp_path / "a-home"
    second_home = tmp_path / "b-home"
    first = ProviderPlan(
        "fixture",
        "1" * 40,
        TargetSpec("first", Framework.CODEX, first_home, "feature"),
        (PlannedFile(Path("skills/example/SKILL.md"), b"first\n", 0o640),),
    )
    second = ProviderPlan(
        "fixture",
        "1" * 40,
        TargetSpec("second", Framework.CODEX, second_home, "feature"),
        (PlannedFile(Path("skills/example/SKILL.md"), b"second\n", 0o644),),
    )
    original_hook = transaction_module._before_manifest_replace
    control = KeyboardInterrupt("operator stopped recovery")

    def fail_second(home_fs: object, *args: object) -> None:
        if home_fs.home == second_home:
            raise OSError("ordinary install failure")
        original_hook(home_fs, *args)

    def interrupt_recovery(_manifests: tuple[object, ...]) -> None:
        raise control

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", fail_second)
    monkeypatch.setattr(transaction_module, "rollback_manifests", interrupt_recovery)

    with pytest.raises(KeyboardInterrupt) as caught:
        install_provider_plans((first, second))

    assert caught.value is control
    assert any("recovery incomplete" in note for note in control.__notes__)
    assert (first_home / "skills/example/SKILL.md").read_bytes() == b"first\n"
    assert list((first_home / ".agentops/deployment/transactions").glob("*/record.json"))


def test_grouped_rollback_failure_retains_evidence_for_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_home = tmp_path / "a-home"
    second_home = tmp_path / "b-home"
    original = ProviderPlan(
        "fixture",
        "1" * 40,
        TargetSpec("first", Framework.CODEX, first_home, "feature"),
        (PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o640),),
    )
    install_provider_plans((original,))
    replacement = ProviderPlan(
        "fixture",
        "2" * 40,
        original.target,
        (PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),),
    )
    second = ProviderPlan(
        "fixture",
        "2" * 40,
        TargetSpec("second", Framework.CODEX, second_home, "feature"),
        (PlannedFile(Path("skills/example/SKILL.md"), b"second\n", 0o644),),
    )
    original_hook = transaction_module._before_manifest_replace
    original_write_atomic = transaction_module._HomeFS.write_atomic

    def fail_second(home_fs: object, *args: object) -> None:
        if home_fs.home == second_home:
            raise OSError("injected second target failure")
        original_hook(home_fs, *args)

    def fail_rollback_marker(
        home_fs: object,
        relative: Path,
        content: bytes,
        mode: int,
    ) -> None:
        if (
            home_fs.home == first_home
            and relative.name == "record.json"
            and json.loads(content).get("state") == "rolled-back"
        ):
            raise OSError("injected grouped rollback marker failure")
        original_write_atomic(home_fs, relative, content, mode)

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", fail_second)
    monkeypatch.setattr(transaction_module._HomeFS, "write_atomic", fail_rollback_marker)

    with pytest.raises(IncompleteRollbackError, match="evidence retained"):
        install_provider_plans((replacement, second))

    records = [
        path
        for path in (first_home / ".agentops/deployment/transactions").glob("*/record.json")
        if json.loads(path.read_text())["manifest"]["source_revision"] == "2" * 40
    ]
    assert len(records) == 1
    assert json.loads(records[0].read_text())["state"] == "committed"
    assert stat.S_IMODE(records[0].stat().st_mode) == 0o600

    monkeypatch.setattr(transaction_module._HomeFS, "write_atomic", original_write_atomic)
    _home, _relative, _record, manifest = transaction_module._read_validated_record(
        records[0]
    )
    rollback_manifests((manifest,))

    assert audit_provider_plans((original,)).matches


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
    record_path = (
        home
        / ".agentops/deployment/transactions"
        / manifests[0].transaction_id
        / "record.json"
    )
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
        record["operation_cursor"] = 0
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
    record_path = (
        home
        / ".agentops/deployment/transactions"
        / manifests[0].transaction_id
        / "record.json"
    )
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
    record_path = (
        home
        / ".agentops/deployment/transactions"
        / manifests[0].transaction_id
        / "record.json"
    )
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
    record_path = (
        home
        / ".agentops/deployment/transactions"
        / manifests[0].transaction_id
        / "record.json"
    )
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
        "reserved-lock",
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
        conflicting = PlannedFile(
            Path("skills/foo/SKILL.md"),
            b"different\n",
            0o644,
        )
        plans = (
            ProviderPlan("one", "1" * 40, target, (duplicate,)),
            ProviderPlan("two", "1" * 40, target, (conflicting,)),
        )
    elif case == "reserved-metadata":
        plans = (
            ProviderPlan(
                "fixture",
                "1" * 40,
                target,
                (PlannedFile(Path(".agentops/deployments/owned"), b"bad\n", 0o644),),
            ),
        )
    else:
        plans = (
            ProviderPlan(
                "fixture",
                "1" * 40,
                target,
                (PlannedFile(Path(".agentops-deployment.lock"), b"bad\n", 0o600),),
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
            lock_path = home / ".agentops-deployment.lock"
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
            '"schema_version": 8,',
            '"schema_version": 8,\n  "schema_version": 8,',
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


def _install_indeterminate_transaction(
    home: Path,
    plan: ProviderPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    def retain_unpublished_manifest(
        home_fs: object,
        _record_path: Path,
        manifest_temp: Path,
        _manifest_path: Path,
    ) -> None:
        lost_manifest = Path(".agentops/deployment/lost-manifest")
        home_fs.replace(manifest_temp, lost_manifest)
        raise OSError("publication result unavailable")

    monkeypatch.setattr(
        transaction_module,
        "_before_manifest_replace",
        retain_unpublished_manifest,
    )
    with pytest.raises(PublicationIndeterminateError):
        install_provider_plans((plan,))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    return record_path, home / ".agentops/deployment/lost-manifest"


def test_recovery_verifies_complete_state_before_accepting_expected_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    record_path, lost_manifest = _install_indeterminate_transaction(
        home,
        plan,
        monkeypatch,
    )
    record_before = record_path.read_bytes()
    record = json.loads(record_before)
    manifest_path = home / record["manifest_path"]
    manifest_path.parent.mkdir()
    os.replace(lost_manifest, manifest_path)
    manifest_path.chmod(0o600)
    destination = home / "skills/example/SKILL.md"
    destination.write_bytes(b"changed\n")

    with pytest.raises(PublicationIndeterminateError, match="state|output"):
        recover_transaction(record_path)

    assert record_path.read_bytes() == record_before
    assert json.loads(record_path.read_text())["state"] == "indeterminate"
    assert manifest_path.exists()
    assert destination.read_bytes() == b"changed\n"


def test_recovery_verifies_owned_directories_before_publishing_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    record_path, _lost_manifest = _install_indeterminate_transaction(
        home,
        plan,
        monkeypatch,
    )
    record_before = record_path.read_bytes()
    record = json.loads(record_before)
    manifest_path = home / record["manifest_path"]
    directory = home / "skills/example"
    directory.chmod(0o700)

    with pytest.raises(PublicationIndeterminateError, match="directory|state"):
        recover_transaction(record_path)

    assert record_path.read_bytes() == record_before
    assert json.loads(record_path.read_text())["state"] == "indeterminate"
    assert not manifest_path.exists()
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    directory.chmod(0o755)
    recovered = recover_transaction(record_path)

    assert recovered.source_revision == "1" * 40
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600


def test_rollback_marker_failure_preserves_evidence_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    original = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o600))
    install_provider_plans((original,))
    replacement = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),
        revision="2" * 40,
    )
    manifests = install_provider_plans((replacement,))
    record_path = transaction_module._TRANSACTION_PATHS[manifests[0].transaction_id]
    record = json.loads(record_path.read_text())
    backup = home / record["operations"][0]["backup"]
    record_relative = record_path.relative_to(home)
    original_write_atomic = transaction_module._HomeFS.write_atomic

    def fail_rolled_back_marker(
        home_fs: object,
        relative: Path,
        content: bytes,
        mode: int,
    ) -> None:
        if relative == record_relative and json.loads(content).get("state") == "rolled-back":
            raise OSError("injected rollback marker failure")
        original_write_atomic(home_fs, relative, content, mode)

    monkeypatch.setattr(
        transaction_module._HomeFS,
        "write_atomic",
        fail_rolled_back_marker,
    )

    with pytest.raises(IncompleteRollbackError, match="evidence retained"):
        rollback_manifests(manifests)

    assert json.loads(record_path.read_text())["state"] == "committed"
    assert backup.exists()
    restored = home / "skills/example/SKILL.md"
    assert backup.stat().st_ino != restored.stat().st_ino
    monkeypatch.setattr(
        transaction_module._HomeFS,
        "write_atomic",
        original_write_atomic,
    )

    rollback_manifests(manifests)

    assert (home / "skills/example/SKILL.md").read_bytes() == b"old\n"
    assert json.loads(record_path.read_text())["state"] == "rolled-back"
    assert not backup.exists()


def test_rollback_cleanup_failure_is_retryable_after_durable_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    original = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o600))
    install_provider_plans((original,))
    replacement = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),
        revision="2" * 40,
    )
    manifests = install_provider_plans((replacement,))
    record_path = transaction_module._TRANSACTION_PATHS[manifests[0].transaction_id]
    record = json.loads(record_path.read_text())
    backup_relative = Path(record["operations"][0]["backup"])
    backup = home / backup_relative
    original_unlink = transaction_module._HomeFS.unlink
    failed = False

    def fail_first_backup_cleanup(home_fs: object, relative: Path) -> None:
        nonlocal failed
        if relative == backup_relative and not failed:
            failed = True
            raise OSError("injected cleanup failure")
        original_unlink(home_fs, relative)

    monkeypatch.setattr(transaction_module._HomeFS, "unlink", fail_first_backup_cleanup)

    with pytest.raises(IncompleteRollbackError, match="evidence retained"):
        rollback_manifests(manifests)

    assert json.loads(record_path.read_text())["state"] == "rolled-back"
    assert backup.exists()
    monkeypatch.setattr(transaction_module._HomeFS, "unlink", original_unlink)

    rollback_manifests(manifests)
    assert recover_transaction(record_path) == manifests[0]

    assert (home / "skills/example/SKILL.md").read_bytes() == b"old\n"
    assert not backup.exists()


def test_recovery_rejects_noncanonical_transaction_path_text_before_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    record_path = next((home / ".agentops/deployment/transactions").glob("*/record.json"))
    record = json.loads(record_path.read_text())
    record["operations"][0]["destination"] = "skills//example/SKILL.md"
    record_path.write_text(json.dumps(record))
    record_path.chmod(0o600)
    before = record_path.read_bytes()
    destination = home / "skills/example/SKILL.md"
    destination_before = destination.read_bytes()

    with pytest.raises(ValueError, match="canonical|normalized"):
        recover_transaction(record_path)

    assert record_path.read_bytes() == before
    assert destination.read_bytes() == destination_before


def test_different_sibling_homes_reach_publication_barrier_concurrently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    barrier = threading.Barrier(2, timeout=2)
    failures: list[BaseException] = []

    def coordinate(*_args: object) -> None:
        barrier.wait()

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", coordinate)

    def install(home: Path) -> None:
        plan = _plan(
            home,
            PlannedFile(Path("skills/example/SKILL.md"), home.name.encode(), 0o644),
        )
        try:
            install_provider_plans((plan,))
        except BaseException as exc:  # pragma: no cover - asserted after join
            failures.append(exc)

    workers = (
        threading.Thread(target=install, args=(tmp_path / "one",)),
        threading.Thread(target=install, args=(tmp_path / "two",)),
    )
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert failures == []


def test_replaced_home_identity_fails_closed_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    displaced = tmp_path / "displaced-home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    def replace_home(*_args: object) -> None:
        os.replace(home, displaced)
        home.mkdir()

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", replace_home)

    with pytest.raises(PublicationIndeterminateError, match="evidence retained"):
        install_provider_plans((plan,))

    assert not list(home.rglob("*.json"))
    assert (displaced / "skills/example/SKILL.md").read_bytes() == b"body\n"


def test_grouped_plans_deduplicate_identical_files_and_removals(tmp_path: Path) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex-dev", Framework.CODEX, home, "feature")
    shared = PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644)
    removal = Path("skills/obsolete/SKILL.md")
    plans = (
        ProviderPlan("one", "1" * 40, target, (shared,), (removal,)),
        ProviderPlan("two", "1" * 40, target, (shared,), (removal,)),
    )

    manifests = install_provider_plans(plans)

    assert manifests[0].provider_ids == ("one", "two")
    assert len(manifests[0].files) == 1
    assert manifests[0].files[0].path == shared.path
    assert manifests[0].files[0].fingerprint == shared.fingerprint
    assert manifests[0].files[0].mode == shared.mode
    assert audit_provider_plans(plans).matches


def test_grouped_plans_reject_conflicting_duplicate_files_before_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    target = TargetSpec("codex-dev", Framework.CODEX, home, "feature")
    plans = (
        ProviderPlan(
            "one",
            "1" * 40,
            target,
            (PlannedFile(Path("skills/example/SKILL.md"), b"one\n", 0o644),),
        ),
        ProviderPlan(
            "two",
            "1" * 40,
            target,
            (PlannedFile(Path("skills/example/SKILL.md"), b"two\n", 0o644),),
        ),
    )

    with pytest.raises(ValueError, match="conflicting|duplicate"):
        install_provider_plans(plans)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("missing_ancestor", [False, True])
def test_concurrent_first_home_creation_serializes_without_file_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_ancestor: bool,
) -> None:
    context = multiprocessing.get_context("fork")
    race_name = "missing" if missing_ancestor else "home"
    home = tmp_path / "missing" / "home" if missing_ancestor else tmp_path / "home"
    barrier = context.Barrier(2)
    results = context.Queue()
    original_mkdir = os.mkdir

    def race_mkdir(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == race_name:
            barrier.wait(timeout=3)
        original_mkdir(path, mode, dir_fd=dir_fd)

    monkeypatch.setattr(transaction_module.os, "mkdir", race_mkdir)
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    def install() -> None:
        try:
            manifests = install_provider_plans((plan,))
        except BaseException as exc:
            results.put((type(exc).__name__, str(exc)))
        else:
            results.put(("returned", manifests[0].target_id))

    workers = (context.Process(target=install), context.Process(target=install))
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=2)

    assert all(not worker.is_alive() for worker in workers)
    assert all(worker.exitcode == 0 for worker in workers)
    observed = sorted(results.get(timeout=2) for _worker in workers)
    assert observed == [("returned", "codex-dev"), ("returned", "codex-dev")]
    assert audit_provider_plans((plan,)).matches


@pytest.mark.parametrize("replacement", ["symlink", "file"])
def test_first_home_creation_rejects_unsafe_race_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_mkdir = os.mkdir

    def replace_instead_of_mkdir(
        path: str | bytes,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path != "home":
            original_mkdir(path, mode, dir_fd=dir_fd)
            return
        if replacement == "symlink":
            os.symlink(outside, path, dir_fd=dir_fd)
        else:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dir_fd,
            )
            os.close(descriptor)
        raise FileExistsError("injected unsafe creation race")

    monkeypatch.setattr(transaction_module.os, "mkdir", replace_instead_of_mkdir)
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))

    with pytest.raises(OSError):
        install_provider_plans((plan,))

    assert list(outside.iterdir()) == []
    assert not list(tmp_path.rglob("record.json"))


@pytest.mark.parametrize("ancestor", ["parent", "grandparent"])
def test_install_rejects_replaced_canonical_home_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestor: str,
) -> None:
    root = tmp_path / "root"
    parent = root / "parent"
    home = parent / "home"
    parent.mkdir(parents=True)
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    replaced = parent if ancestor == "parent" else root
    displaced = tmp_path / f"displaced-{ancestor}"
    displaced_home = displaced / "home" if ancestor == "parent" else displaced / "parent/home"

    def replace_ancestor(*_args: object) -> None:
        os.replace(replaced, displaced)
        home.mkdir(parents=True)

    monkeypatch.setattr(transaction_module, "_before_manifest_replace", replace_ancestor)

    with pytest.raises(PublicationIndeterminateError, match="evidence retained"):
        install_provider_plans((plan,))

    assert not list(home.rglob("*.json"))
    assert not list((displaced_home / ".agentops/deployment/manifests").glob("*.json"))
    records = list(
        (displaced_home / ".agentops/deployment/transactions").glob("*/record.json")
    )
    assert len(records) == 1
    assert json.loads(records[0].read_text())["state"] == "prepared"


def test_rollback_rejects_replaced_canonical_home_ancestor_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "root/parent"
    home = parent / "home"
    original = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"old\n", 0o600))
    install_provider_plans((original,))
    replacement = _plan(
        home,
        PlannedFile(Path("skills/example/SKILL.md"), b"new\n", 0o644),
        revision="2" * 40,
    )
    manifests = install_provider_plans((replacement,))
    manifest_path = next((home / ".agentops/deployment/manifests").glob("*.json"))
    replacement_manifest = manifest_path.read_bytes()
    record_path = transaction_module._TRANSACTION_PATHS[manifests[0].transaction_id]
    record_relative = record_path.relative_to(home)
    displaced_parent = tmp_path / "displaced-parent"
    original_verify = transaction_module._HomeFS.verify_lock_identity
    replaced = False

    def replace_before_identity_check(home_fs: object) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            os.replace(parent, displaced_parent)
            home.mkdir(parents=True)
        original_verify(home_fs)

    monkeypatch.setattr(
        transaction_module._HomeFS,
        "verify_lock_identity",
        replace_before_identity_check,
    )

    with pytest.raises(ValueError, match="identity"):
        rollback_manifests(manifests)

    displaced_home = displaced_parent / "home"
    assert (displaced_home / "skills/example/SKILL.md").read_bytes() == b"new\n"
    assert (displaced_home / manifest_path.relative_to(home)).read_bytes() == replacement_manifest
    assert (displaced_home / record_relative).exists()
    assert not list(home.rglob("*.json"))


def test_recovery_rejects_replaced_canonical_home_ancestor_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "root/parent"
    home = parent / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    record_path, _lost_manifest = _install_indeterminate_transaction(
        home,
        plan,
        monkeypatch,
    )
    record_relative = record_path.relative_to(home)
    record_before = record_path.read_bytes()
    record = json.loads(record_before)
    manifest_relative = Path(record["manifest_path"])
    displaced_parent = tmp_path / "displaced-parent"
    original_verify = transaction_module._HomeFS.verify_lock_identity
    replaced = False

    def replace_before_identity_check(home_fs: object) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            os.replace(parent, displaced_parent)
            home.mkdir(parents=True)
        original_verify(home_fs)

    monkeypatch.setattr(
        transaction_module._HomeFS,
        "verify_lock_identity",
        replace_before_identity_check,
    )

    with pytest.raises(ValueError, match="identity"):
        recover_transaction(record_path)

    displaced_home = displaced_parent / "home"
    assert not (displaced_home / manifest_relative).exists()
    assert (displaced_home / record_relative).read_bytes() == record_before
    assert not list(home.rglob("*.json"))


def test_relative_home_is_bound_before_working_directory_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starting_directory = tmp_path / "starting"
    other_directory = tmp_path / "other"
    starting_directory.mkdir()
    other_directory.mkdir()
    monkeypatch.chdir(starting_directory)
    relative_home = Path("relative-home")
    plan = _plan(
        relative_home,
        PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644),
    )

    def change_working_directory(*_args: object) -> None:
        os.chdir(other_directory)

    monkeypatch.setattr(
        transaction_module,
        "_before_manifest_replace",
        change_working_directory,
    )

    install_provider_plans((plan,))

    assert (starting_directory / "relative-home/skills/example/SKILL.md").read_bytes() == b"body\n"
    assert not (other_directory / "relative-home").exists()


@pytest.mark.parametrize("ancestor", ["parent", "grandparent"])
def test_audit_rejects_replaced_canonical_home_ancestor_without_reporting_displaced_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestor: str,
) -> None:
    root = tmp_path / "root"
    parent = root / "parent"
    home = parent / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    (home / "skills/example/SKILL.md").write_bytes(b"displaced data\n")
    replaced = parent if ancestor == "parent" else root
    displaced = tmp_path / f"displaced-audit-{ancestor}"
    original_read = transaction_module._read_ownership_manifest_for_audit
    replaced_once = False

    def replace_ancestor_before_manifest_read(
        home_fs: object,
        manifest_path: Path,
    ) -> tuple[bytes | None, str | None]:
        nonlocal replaced_once
        if not replaced_once:
            replaced_once = True
            os.replace(replaced, displaced)
            home.mkdir(parents=True)
        return original_read(home_fs, manifest_path)

    monkeypatch.setattr(
        transaction_module,
        "_read_ownership_manifest_for_audit",
        replace_ancestor_before_manifest_read,
    )

    def audit_result() -> tuple[
        bool,
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
        tuple[str, ...],
    ]:
        audit = audit_provider_plans((plan,))
        return (
            audit.matches,
            audit.missing,
            audit.changed,
            audit.unexpected,
            audit.duplicates,
            audit.validation_errors,
        )

    result = _run_bounded(audit_result)

    assert result == (
        "returned",
        (
            False,
            (),
            (),
            (),
            (),
            ("deployment canonical home identity changed",),
        ),
    )


def test_audit_reports_opened_home_descriptor_race_as_identity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    plan = _plan(home, PlannedFile(Path("skills/example/SKILL.md"), b"body\n", 0o644))
    install_provider_plans((plan,))
    original_fstat = transaction_module.os.fstat
    failed = False

    def fail_first_fstat(descriptor: int) -> os.stat_result:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("descriptor identity unavailable")
        return original_fstat(descriptor)

    monkeypatch.setattr(transaction_module.os, "fstat", fail_first_fstat)

    audit = audit_provider_plans((plan,))

    assert not audit.matches
    assert audit.validation_errors == ("deployment canonical home identity changed",)
