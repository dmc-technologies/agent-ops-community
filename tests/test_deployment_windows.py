from __future__ import annotations

import json
import multiprocessing
import os
import queue
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_ops.deployment import transaction as transaction_module
from agent_ops.deployment.engine import DeploymentEngine
from agent_ops.deployment.models import (
    DeploymentManifest,
    ManifestFile,
    PlannedFile,
    ProviderPlan,
    SourceSpec,
    TargetSpec,
)
from agent_ops.deployment.registry import (
    ChannelSpec,
    DeploymentRegistry,
    RegistryConfig,
)
from agent_ops.deployment.source_store import SourceStore
from agent_ops.deployment.transaction import (
    audit_provider_plans,
    install_provider_plans,
    recover_transaction,
    rollback_manifests,
)
from agent_ops.registries.models import Framework

_WINDOWS_RESERVED_ALIASES = ("CONIN$", "CONOUT$", "COM¹.txt", "LPT².log", "COM³")


@pytest.mark.parametrize(
    "authored",
    (
        "skills/example/SKILL.md:payload",
        "skills/example/CON.txt",
        *(f"skills/example/{name}" for name in _WINDOWS_RESERVED_ALIASES),
        "skills/example/name.",
        "skills/example/name ",
    ),
)
def test_windows_managed_paths_reject_ntfs_aliases(authored: str) -> None:
    from agent_ops.deployment.windows_fs import safe_relative

    with pytest.raises(ValueError, match="unsafe managed path"):
        safe_relative(Path(authored))


@pytest.mark.parametrize("name", _WINDOWS_RESERVED_ALIASES)
def test_windows_preflight_rejects_reserved_device_before_target_creation(
    tmp_path: Path,
    name: str,
) -> None:
    from agent_ops.deployment import windows_transaction

    home = tmp_path / "codex"
    plan = ProviderPlan(
        "fixture",
        "1" * 40,
        TargetSpec("codex-windows", Framework.CODEX, home, "stable"),
        (PlannedFile(Path("skills/example") / name, b"payload", 0o644),),
    )

    with pytest.raises(ValueError, match="unsafe managed path"):
        windows_transaction.preflight_provider_plans_read_only((plan,))

    assert not home.exists()


@pytest.mark.parametrize("name", _WINDOWS_RESERVED_ALIASES)
def test_windows_transaction_record_rejects_reserved_device(name: str) -> None:
    from agent_ops.deployment import windows_transaction

    transaction_id = "a" * 32
    path = Path("skills/example") / name
    manifest = DeploymentManifest(
        1,
        "codex-windows",
        Framework.CODEX,
        "stable",
        "1" * 40,
        ("fixture",),
        (ManifestFile(path, "2" * 64, 0o644),),
        (),
        transaction_id,
    )
    record = {
        "schema_version": 1,
        "state": "prepared",
        "home": "C:/managed",
        "manifest": windows_transaction._manifest_data(manifest),
        "manifest_path": ".agentops/deployment/manifests/codex-windows.json",
        "manifest_content": "",
        "prior_manifest_content": None,
        "created_directories": [],
        "operations": [],
    }

    with pytest.raises(ValueError, match="unsafe managed path"):
        windows_transaction._validate_record(record, transaction_id=transaction_id)


def test_windows_recovery_rejects_noncanonical_record_path_before_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_ops.deployment import windows_transaction

    class UnexpectedLock:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("recovery acquired a lock for an invalid path")

    monkeypatch.setattr(windows_transaction, "WindowsTargetLock", UnexpectedLock)

    invalid = tmp_path / "not-agentops/deployment/transactions" / ("a" * 32) / "record.json"
    with pytest.raises(ValueError, match="recovery path"):
        windows_transaction.recover_transaction(invalid)


def test_windows_rollback_accepts_interruption_before_backup_move() -> None:
    from agent_ops.deployment import windows_transaction

    destination = Path("managed.txt")
    prior = b"prior bytes\n"
    record_path = Path(".agentops/deployment/transactions") / ("a" * 32) / "record.json"

    class FakeLock:
        def __init__(self) -> None:
            self.files = {destination: prior}

        def write_atomic(self, path: Path, content: bytes) -> None:
            if path != record_path:
                self.files[path] = content

        def read_optional(self, path: Path) -> bytes | None:
            return self.files.get(path)

        def exists(self, path: Path) -> bool:
            return path in self.files

        def read_file(self, path: Path) -> bytes:
            return self.files[path]

        def unlink(self, path: Path) -> None:
            del self.files[path]

        def replace(self, source: Path, target: Path, *, replace: bool) -> None:
            assert not replace
            self.files[target] = self.files.pop(source)

        def remove_empty_dir(self, _path: Path) -> None:
            return

        def verify(self) -> None:
            return

    record = {
        "state": "applying",
        "manifest_path": ".agentops/deployment/manifests/codex.json",
        "manifest_content": "",
        "prior_manifest_content": None,
        "created_directories": [],
        "operations": [
            {
                "kind": "write",
                "destination": destination.as_posix(),
                "backup": ".agentops/deployment/transactions/" + ("a" * 32) + "/backup/0",
                "prior": windows_transaction._fingerprint(prior),
                "expected": windows_transaction._fingerprint(b"replacement\n"),
                "phase": "applying",
            }
        ],
    }
    lock = FakeLock()

    windows_transaction._rollback_record(lock, record_path, record, retain=True)

    assert lock.files[destination] == prior
    assert record["state"] == "rolled-back"


def test_windows_accepted_ref_reconciles_to_durable_metadata() -> None:
    from agent_ops.deployment import windows_source_store

    previous = "1" * 40
    observed = "2" * 40
    calls: list[tuple[str, ...]] = []

    class Store:
        def _git(
            self,
            arguments: tuple[str, ...],
            *,
            accepted_returncodes: frozenset[int] = frozenset({0}),
        ) -> SimpleNamespace:
            del accepted_returncodes
            calls.append(arguments)
            if "rev-parse" in arguments:
                return SimpleNamespace(returncode=0, stdout=observed + "\n")
            return SimpleNamespace(returncode=0, stdout="")

        def _history_error(self, *_args: object) -> None:
            raise AssertionError("durable commit should be available")

    source = SourceSpec("private", "https://example.invalid/private.git", "refs/heads/main")
    accepted_ref = "refs/agentops/accepted/test"

    result = windows_source_store._reconcile_accepted_ref(
        Store(), Path("mirror.git"), accepted_ref, previous, "refs/heads/main", source
    )

    assert result == previous
    assert calls[-1] == (
        "--git-dir",
        "mirror.git",
        "update-ref",
        accepted_ref,
        previous,
        observed,
    )


def test_windows_provider_read_fails_on_premature_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_ops.deployment import windows_fs, windows_source_store

    identity = SimpleNamespace(size=1)

    class Handle:
        value = 1

        def identity(self) -> SimpleNamespace:
            return identity

    class Kernel:
        def ReadFile(self, *_args: object) -> int:
            return 1

    monkeypatch.setattr(windows_fs, "_kernel32", lambda: Kernel())
    entry = windows_source_store._Entry(
        Path("root"),
        Path("file"),
        Handle(),
        kind="file",
        mode=0o644,
        expected_blob="0" * 40,
        object_format="sha1",
    )

    with pytest.raises(RuntimeError, match="changed during consumption"):
        entry.read_bytes()


def _windows_plan(home: Path) -> ProviderPlan:
    return ProviderPlan(
        provider_id="fixture",
        source_revision="1" * 40,
        target=TargetSpec("codex-windows", Framework.CODEX, home, "stable"),
        files=(
            PlannedFile(Path("AGENTS.md"), "Use “UTF-8” policy.\n".encode(), 0o644),
            PlannedFile(
                Path("hooks/portable_stop.py"),
                b"raise SystemExit(0)\n",
                0o755,
            ),
            PlannedFile(
                Path("skills/example/SKILL.md"),
                b"---\nname: example\n---\n",
                0o644,
            ),
        ),
    )


def _acquire_native_windows_lock(home: str, messages: object) -> None:
    from agent_ops.deployment.windows_fs import WindowsTargetLock

    with WindowsTargetLock(Path(home)):
        messages.put("acquired")


def test_public_transaction_api_dispatches_to_native_windows_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _windows_plan(tmp_path / "codex")
    expected = (SimpleNamespace(transaction_id="windows-transaction"),)
    calls: list[tuple[ProviderPlan, ...]] = []
    backend = SimpleNamespace(
        install_provider_plans=lambda plans, channel_transitions=None: (
            calls.append(plans) or expected
        )
    )
    monkeypatch.setattr(transaction_module, "_POSIX_SUPPORTED", False)
    monkeypatch.setattr(transaction_module, "_WINDOWS_SUPPORTED", True, raising=False)
    monkeypatch.setattr(
        transaction_module,
        "_windows_transaction_backend",
        lambda: backend,
        raising=False,
    )

    manifests = install_provider_plans((plan,))

    assert manifests == expected
    assert calls == [(plan,)]


def test_public_registry_api_dispatches_to_native_windows_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_ops.deployment import registry as registry_module

    expected = SimpleNamespace(config="windows-registry")
    backend = SimpleNamespace(load_snapshot=lambda _registry: expected)
    monkeypatch.setattr(registry_module, "_WINDOWS_SUPPORTED", True, raising=False)
    monkeypatch.setattr(
        registry_module,
        "_windows_registry_backend",
        lambda: backend,
        raising=False,
    )

    snapshot = DeploymentRegistry(tmp_path / "deployments.yaml").load_snapshot()

    assert snapshot is expected


def test_public_source_store_dispatches_to_native_windows_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_ops.deployment import source_store as source_store_module

    source = SourceSpec("private", "https://example.invalid/private.git", "refs/heads/main")
    expected = SimpleNamespace(commit="1" * 40)
    backend = SimpleNamespace(fetch=lambda _store, observed, ref, rewrite=None: expected)
    monkeypatch.setattr(source_store_module, "_WINDOWS_SUPPORTED", True, raising=False)
    monkeypatch.setattr(
        source_store_module,
        "_windows_source_store_backend",
        lambda: backend,
        raising=False,
    )

    snapshot = SourceStore(tmp_path / "sources").fetch(source, "refs/heads/main")

    assert snapshot is expected


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows")
def test_native_windows_registry_round_trip_retains_exact_snapshot(tmp_path: Path) -> None:
    registry = DeploymentRegistry(tmp_path / "state" / "deployments.yaml")
    config = RegistryConfig(
        1,
        (SourceSpec("private", "https://example.invalid/private.git", "refs/heads/main"),),
        (ChannelSpec("stable", "private", "refs/heads/main"),),
        (TargetSpec("codex", Framework.CODEX, tmp_path / "codex", "stable"),),
    )

    snapshot = registry.save(config)

    assert registry.load_snapshot() == snapshot
    with registry.retain_snapshot(snapshot) as authority:
        authority.verify()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows")
def test_native_windows_engine_refreshes_one_complete_managed_path_twice(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    catalog = source / "catalog"
    catalog.mkdir(parents=True)
    (catalog / "policy.md").write_text("Use “UTF-8” policy.\n", encoding="utf-8")
    (catalog / "hook.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    (catalog / "skill.md").write_text("---\nname: example\n---\n", encoding="utf-8")
    subprocess.run(("git", "init", "-q", "-b", "main", str(source)), check=True)
    subprocess.run(("git", "-C", str(source), "config", "user.name", "Agent Ops Test"), check=True)
    subprocess.run(
        ("git", "-C", str(source), "config", "user.email", "agentops@example.invalid"),
        check=True,
    )
    subprocess.run(("git", "-C", str(source), "add", "catalog"), check=True)
    subprocess.run(("git", "-C", str(source), "commit", "-q", "-m", "fixture"), check=True)

    class Provider:
        provider_id = "fixture"

        def supports(self, snapshot: object, target: TargetSpec) -> bool:
            return target.framework is Framework.CODEX

        def source_closure(
            self,
            snapshot: object,
            target: TargetSpec,
            selection: tuple[str, ...] | None,
        ) -> tuple[Path, ...]:
            return (Path("catalog"),)

        def plan(
            self,
            snapshot: object,
            target: TargetSpec,
            selection: tuple[str, ...] | None = None,
        ) -> ProviderPlan:
            root = snapshot.root
            return ProviderPlan(
                self.provider_id,
                snapshot.commit,
                target,
                (
                    PlannedFile(
                        Path("AGENTS.md"), (root / "catalog/policy.md").read_bytes(), 0o644
                    ),
                    PlannedFile(
                        Path("hooks/portable_stop.py"),
                        (root / "catalog/hook.py").read_bytes(),
                        0o755,
                    ),
                    PlannedFile(
                        Path("skills/example/SKILL.md"),
                        (root / "catalog/skill.md").read_bytes(),
                        0o644,
                    ),
                ),
                audit_roots=(Path("skills/example"), Path("hooks")),
            )

    home = tmp_path / "codex"
    state = tmp_path / "state"
    registry = DeploymentRegistry(state / "deployments.yaml")
    registry.save(
        RegistryConfig(
            1,
            (SourceSpec("fixture", source.as_uri(), "refs/heads/main"),),
            (ChannelSpec("stable", "fixture", "refs/heads/main"),),
            (TargetSpec("codex", Framework.CODEX, home, "stable"),),
        )
    )
    engine = DeploymentEngine(registry, SourceStore(state), providers=(Provider(),))

    first = engine.refresh(("codex",))
    second = engine.refresh(("codex",))
    audit = engine.audit(("codex",))

    assert first.targets[0].state.value == "stable"
    assert second.targets[0].state.value == "stable"
    assert audit.targets[0].state.value == "stable"
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == "Use “UTF-8” policy.\n"
    assert (home / "hooks/portable_stop.py").is_file()
    assert (home / "skills/example/SKILL.md").is_file()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows")
def test_native_windows_first_install_and_identical_refresh_are_complete_and_auditable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "codex"
    plan = _windows_plan(home)

    first = install_provider_plans((plan,))
    first_bytes = {item.path: (home / item.path).read_bytes() for item in plan.files}
    second = install_provider_plans((plan,))

    assert len(first) == len(second) == 1
    assert first[0].files == second[0].files
    assert {item.path: (home / item.path).read_bytes() for item in plan.files} == first_bytes
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == "Use “UTF-8” policy.\n"
    assert audit_provider_plans((plan,)).matches


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows")
def test_native_windows_refresh_removes_retired_owned_file_and_rolls_back_exactly(
    tmp_path: Path,
) -> None:
    home = tmp_path / "codex"
    original = _windows_plan(home)
    install_provider_plans((original,))
    neighbor = home / "skills/operator-owned/notes.txt"
    neighbor.parent.mkdir(parents=True)
    neighbor.write_text("preserve\n", encoding="utf-8")
    replacement = ProviderPlan(
        "fixture",
        "2" * 40,
        original.target,
        tuple(item for item in original.files if item.path.name != "SKILL.md"),
        removals=(Path("skills/example/SKILL.md"),),
        audit_roots=(Path("skills/example"),),
    )

    manifests = install_provider_plans((replacement,))

    assert not (home / "skills/example/SKILL.md").exists()
    assert neighbor.read_text(encoding="utf-8") == "preserve\n"
    assert audit_provider_plans((replacement,)).matches

    rollback_manifests(manifests)
    assert (home / "skills/example/SKILL.md").read_bytes() == original.files[2].content
    assert neighbor.read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows")
def test_native_windows_recovery_accepts_exact_published_output_after_record_interruption(
    tmp_path: Path,
) -> None:
    from agent_ops.deployment import windows_transaction

    plan = _windows_plan(tmp_path / "codex")
    manifest = install_provider_plans((plan,))[0]
    record_path = windows_transaction._PATHS[manifest.transaction_id]
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["state"] = "applying"
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    recovered = recover_transaction(record_path)

    assert recovered == manifest
    assert json.loads(record_path.read_text(encoding="utf-8"))["state"] == "committed"
    assert audit_provider_plans((plan,)).matches


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows")
def test_native_windows_target_lock_serializes_processes(tmp_path: Path) -> None:
    from agent_ops.deployment.windows_fs import WindowsTargetLock

    home = tmp_path / "codex"
    context = multiprocessing.get_context("spawn")
    messages = context.Queue()
    with WindowsTargetLock(home):
        process = context.Process(
            target=_acquire_native_windows_lock,
            args=(str(home), messages),
        )
        process.start()
        with pytest.raises(queue.Empty):
            messages.get(timeout=0.5)
    assert messages.get(timeout=5) == "acquired"
    process.join(timeout=5)
    assert process.exitcode == 0


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows")
def test_native_windows_install_rejects_junction_in_managed_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "codex"
    home.mkdir()
    subprocess.run(
        ("cmd", "/c", "mklink", "/J", str(home / "skills"), str(outside)),
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(ValueError, match="reparse-point"):
        install_provider_plans((_windows_plan(home),))

    assert not list(outside.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows")
def test_native_windows_parent_pin_blocks_concurrent_directory_replacement(
    tmp_path: Path,
) -> None:
    from agent_ops.deployment.windows_fs import WindowsTargetLock

    home = tmp_path / "codex"
    displaced = tmp_path / "displaced-skills"
    with (
        WindowsTargetLock(home) as lock,
        lock.pin_parent(Path("skills/example/SKILL.md"), create=True),
    ):
        attempt = subprocess.run(
            (
                sys.executable,
                "-c",
                "import os,sys; os.replace(sys.argv[1], sys.argv[2])",
                str(home / "skills"),
                str(displaced),
            ),
            capture_output=True,
            text=True,
        )

        assert attempt.returncode != 0
        assert (home / "skills").is_dir()
        assert not displaced.exists()
