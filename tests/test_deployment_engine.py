from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from agent_ops.deployment.engine import DeploymentEngine, DeploymentRecoveryError
from agent_ops.deployment.models import (
    DeploymentAudit,
    PlannedFile,
    ProviderPlan,
    RewriteAcceptance,
    SourceSnapshot,
    SourceSpec,
    TargetSpec,
    TargetState,
    TargetStatus,
)
from agent_ops.deployment.public_skills import build_public_skill_plans
from agent_ops.deployment.registry import ChannelSpec, DeploymentRegistry, RegistryConfig
from agent_ops.deployment.source_store import SourceStore
from agent_ops.deployment.transaction import UnsupportedPlatformError
from agent_ops.deployment.transaction import install_provider_plans as apply_provider_plans
from agent_ops.registries.models import Framework, SkillDependency, SkillDependencyInstall
from agent_ops.show_me_adapter import OWNERSHIP_MANIFEST_RELATIVE, install_show_me
from agent_ops.skill_installer import InstalledSkillDependency, install_skill_dependencies


class _FixtureProvider:
    provider_id = "fixture"

    def supports(self, snapshot: SourceSnapshot, target: TargetSpec) -> bool:
        return target.framework is Framework.CODEX

    def source_closure(
        self,
        snapshot: SourceSnapshot,
        target: TargetSpec,
        selection: tuple[str, ...] | None,
    ) -> tuple[Path, ...]:
        return (Path("payload.txt"),)

    def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
        return ProviderPlan(
            self.provider_id,
            snapshot.commit,
            target,
            (
                PlannedFile(
                    Path("skills/example/payload.txt"),
                    (snapshot.root / "payload.txt").read_bytes(),
                    0o640,
                ),
            ),
        )


def _engine_fixture(tmp_path: Path) -> tuple[DeploymentEngine, DeploymentRegistry, Path]:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "agentops@example.com"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Agent Ops"], cwd=source, check=True)
    (source / "payload.txt").write_bytes(b"deployed\n")
    subprocess.run(["git", "add", "payload.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=source, check=True, capture_output=True)
    registry_path = tmp_path / "registry/deployments.yaml"
    registry_path.parent.mkdir()
    registry = DeploymentRegistry(registry_path)
    home = tmp_path / "home"
    registry.save(
        RegistryConfig(
            1,
            (SourceSpec("community", str(source)),),
            (ChannelSpec("stable", "community", "refs/heads/main"),),
            (TargetSpec("codex", Framework.CODEX, home, "stable"),),
        )
    )
    return (
        DeploymentEngine(
            registry,
            SourceStore(tmp_path / "source-store"),
            providers=(_FixtureProvider(),),
        ),
        registry,
        home,
    )


def _create_branch(source: Path, name: str, content: bytes) -> str:
    subprocess.run(
        ["git", "switch", "-c", name],
        cwd=source,
        check=True,
        capture_output=True,
    )
    (source / "payload.txt").write_bytes(content)
    subprocess.run(
        ["git", "commit", "-am", name],
        cwd=source,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_deploy_adds_first_arbitrary_branch_alias_atomically(tmp_path: Path) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    home.mkdir()
    second_home = tmp_path / "second-home"
    second_home.mkdir()
    current = registry.load()
    registry.save(
        RegistryConfig(
            1,
            current.sources,
            current.channels,
            current.targets
            + (TargetSpec("second", Framework.CODEX, second_home, "stable"),),
        )
    )
    source = tmp_path / "source"
    commit = _create_branch(source, "feature", b"feature\n")

    receipt = engine.deploy("demo", "refs/heads/feature", ("codex",))

    config = registry.load()
    assert ChannelSpec("demo", "community", "refs/heads/feature") in config.channels
    assert config.targets == (
        TargetSpec("codex", Framework.CODEX, home, "demo"),
        TargetSpec("second", Framework.CODEX, second_home, "stable"),
    )
    assert receipt.operation == "deploy"
    assert receipt.commits == (commit,)
    assert receipt.targets == (
        TargetStatus("codex", TargetState.BRANCH, "demo", commit),
    )
    assert (home / "skills/example/payload.txt").read_bytes() == b"feature\n"
    assert tuple(second_home.iterdir()) == ()
    assert registry.receipts() == (receipt,)


def test_deploy_reuses_exact_existing_alias(tmp_path: Path) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    home.mkdir()
    source = tmp_path / "source"
    commit = _create_branch(source, "feature", b"feature\n")
    current = registry.load()
    registry.save(
        RegistryConfig(
            1,
            current.sources,
            current.channels
            + (ChannelSpec("demo", "community", "refs/heads/feature"),),
            current.targets,
        )
    )

    receipt = engine.deploy("demo", "refs/heads/feature", ("codex",))

    assert receipt.operation == "deploy"
    assert receipt.commits == (commit,)
    assert registry.load().targets[0].channel == "demo"
    assert len([item for item in registry.load().channels if item.id == "demo"]) == 1
    assert (home / "skills/example/payload.txt").read_bytes() == b"feature\n"


def test_deploy_rejects_selected_targets_from_multiple_sources(tmp_path: Path) -> None:
    engine, registry, first_home = _engine_fixture(tmp_path)
    second_source = tmp_path / "second-source"
    second_source.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=second_source,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "agentops@example.com"],
        cwd=second_source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Agent Ops"],
        cwd=second_source,
        check=True,
    )
    (second_source / "payload.txt").write_bytes(b"second\n")
    subprocess.run(["git", "add", "payload.txt"], cwd=second_source, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=second_source,
        check=True,
        capture_output=True,
    )
    second_home = tmp_path / "second-home"
    current = registry.load()
    registry.save(
        RegistryConfig(
            1,
            current.sources + (SourceSpec("second", str(second_source)),),
            current.channels
            + (ChannelSpec("second", "second", "refs/heads/main"),),
            current.targets
            + (TargetSpec("second", Framework.CODEX, second_home, "second"),),
        )
    )
    before = registry.load_snapshot()

    with pytest.raises(ValueError, match="exactly one configured source"):
        engine.deploy("demo", "refs/heads/feature", ("codex", "second"))

    assert registry.load_snapshot() == before
    assert not first_home.exists()
    assert not second_home.exists()
    assert registry.receipts() == ()


@pytest.mark.parametrize(
    ("channel", "ref", "message"),
    [
        ("bad/name", "refs/heads/feature", "channel id"),
        ("demo", "feature", "refs/heads"),
        ("demo", "refs/heads/main", "stable ref"),
        ("stable", "refs/heads/feature", "alias.*differs"),
    ],
)
def test_deploy_rejects_invalid_or_conflicting_alias_before_mutation(
    tmp_path: Path, channel: str, ref: str, message: str
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    before = registry.load_snapshot()

    with pytest.raises(ValueError, match=message):
        engine.deploy(channel, ref, ("codex",))

    assert registry.load_snapshot() == before
    assert not home.exists()
    assert registry.receipts() == ()


@pytest.mark.parametrize(
    "ref",
    (
        "refs/tags/v1",
        "refs/remotes/origin/feature",
        "refs/pull/1/head",
        "refs/agentops/internal/test",
        "refs/heads-not-a-branch/feature",
    ),
)
def test_deploy_rejects_non_branch_ref_before_fetch_or_mutation(
    tmp_path: Path, ref: str
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    home.mkdir()
    sentinel = home / "operator.txt"
    sentinel.write_bytes(b"unchanged\n")
    source = tmp_path / "source"
    commit = _create_branch(source, "feature", b"feature\n")
    subprocess.run(["git", "update-ref", ref, commit], cwd=source, check=True)
    before = registry.load_snapshot()

    with pytest.raises(ValueError, match="refs/heads"):
        engine.deploy("demo", ref, ("codex",))

    assert not (tmp_path / "source-store").exists()
    assert registry.load_snapshot() == before
    assert sentinel.read_bytes() == b"unchanged\n"
    assert registry.receipts() == ()


@pytest.mark.parametrize(
    "channel",
    (
        "preview",
        "preview-demo",
        "unreviewed-local",
        "unreviewed-local-demo",
        "unreviewed-locality",
    ),
)
def test_deploy_rejects_preview_reserved_alias_before_fetch_or_mutation(
    tmp_path: Path, channel: str
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    home.mkdir()
    sentinel = home / "operator.txt"
    sentinel.write_bytes(b"unchanged\n")
    source = tmp_path / "source"
    _create_branch(source, "feature", b"feature\n")
    before = registry.load_snapshot()

    with pytest.raises(ValueError, match="preview"):
        engine.deploy(channel, "refs/heads/feature", ("codex",))

    assert not (tmp_path / "source-store").exists()
    assert registry.load_snapshot() == before
    assert sentinel.read_bytes() == b"unchanged\n"
    assert registry.receipts() == ()


def test_deploy_missing_ref_preserves_registry_target_and_receipts(tmp_path: Path) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    sentinel = home / "operator.txt"
    sentinel.parent.mkdir()
    sentinel.write_bytes(b"unchanged\n")
    before = registry.load_snapshot()

    with pytest.raises(RuntimeError, match="requested Git ref"):
        engine.deploy("missing", "refs/heads/missing", ("codex",))

    assert registry.load_snapshot() == before
    assert sentinel.read_bytes() == b"unchanged\n"
    assert registry.receipts() == ()


def test_deploy_rewrite_requires_exact_ref_history_acceptance(tmp_path: Path) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    home.mkdir()
    source = tmp_path / "source"
    prior = _create_branch(source, "feature", b"feature\n")
    first = engine.deploy("demo", "refs/heads/feature", ("codex",))
    configured = registry.load_snapshot()
    before_receipts = registry.receipts()
    subprocess.run(
        ["git", "switch", "main"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    (source / "payload.txt").write_bytes(b"rewritten\n")
    subprocess.run(
        ["git", "commit", "-am", "rewritten"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    rewritten = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "branch", "-f", "feature", rewritten], cwd=source, check=True)

    with pytest.raises(RuntimeError, match="rewrite acceptance"):
        engine.deploy("demo", "refs/heads/feature", ("codex",))

    assert registry.load_snapshot() == configured
    assert registry.receipts() == before_receipts == (first,)
    assert (home / "skills/example/payload.txt").read_bytes() == b"feature\n"

    receipt = engine.deploy(
        "demo",
        "refs/heads/feature",
        ("codex",),
        rewrite=RewriteAcceptance(prior, rewritten),
    )

    assert receipt.commits == (rewritten,)
    assert (home / "skills/example/payload.txt").read_bytes() == b"rewritten\n"


def test_deploy_audit_failure_does_not_reassign_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    home.mkdir()
    source = tmp_path / "source"
    _create_branch(source, "feature", b"feature\n")
    before = registry.load_snapshot()

    def mismatch(plans: tuple[ProviderPlan, ...]) -> DeploymentAudit:
        return DeploymentAudit(plans[0].target.id, matches=False)

    monkeypatch.setattr("agent_ops.deployment.engine.audit_provider_plans", mismatch)

    with pytest.raises(RuntimeError, match="audit did not match"):
        engine.deploy("demo", "refs/heads/feature", ("codex",))

    assert registry.load_snapshot() == before
    assert not (home / "skills/example/payload.txt").exists()
    assert registry.receipts() == ()


def test_deploy_receipt_failure_restores_registry_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    home.mkdir()
    source = tmp_path / "source"
    _create_branch(source, "feature", b"feature\n")
    before = registry.load_snapshot()
    append_error = OSError("injected deploy receipt failure")

    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise append_error

    monkeypatch.setattr(registry, "append_receipt", fail_append)

    with pytest.raises(OSError) as caught:
        engine.deploy("demo", "refs/heads/feature", ("codex",))

    assert caught.value is append_error
    restored = registry.load_snapshot()
    assert restored.config == before.config
    assert restored.fingerprint == before.fingerprint
    assert not (home / "skills/example/payload.txt").exists()
    assert registry.receipts() == ()


def test_deploy_registry_cas_failure_preserves_concurrent_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    home.mkdir()
    source = tmp_path / "source"
    _create_branch(source, "feature", b"feature\n")
    original = registry.load()
    concurrent = RegistryConfig(
        1,
        original.sources,
        original.channels + (ChannelSpec("admin", "community", "refs/heads/main"),),
        original.targets,
    )
    original_save = registry.save
    injected = False

    def save(config: RegistryConfig, **kwargs: object):
        nonlocal injected
        if kwargs.get("expected_snapshot") is not None and not injected:
            injected = True
            original_save(concurrent)
        return original_save(config, **kwargs)

    monkeypatch.setattr(registry, "save", save)

    with pytest.raises(ValueError, match="registry no longer matches"):
        engine.deploy("demo", "refs/heads/feature", ("codex",))

    assert registry.load() == concurrent
    assert not (home / "skills/example/payload.txt").exists()
    assert registry.receipts() == ()


def test_deployment_engine_plans_without_mutation_then_refreshes_once(tmp_path: Path) -> None:
    engine, registry, home = _engine_fixture(tmp_path)

    plan = engine.plan(("codex",))

    assert [item.provider_id for item in plan.provider_plans] == ["fixture"]
    assert [
        (
            item.target_id,
            item.channel,
            item.source_id,
            item.ref,
            item.commit,
        )
        for item in plan.target_sources
    ] == [
        (
            "codex",
            "stable",
            "community",
            "refs/heads/main",
            plan.snapshots[0].commit,
        )
    ]
    assert not home.exists()
    assert registry.receipts() == ()

    receipt = engine.refresh(("codex",))

    assert (home / "skills/example/payload.txt").read_bytes() == b"deployed\n"
    assert receipt.operation == "refresh"
    assert receipt.targets[0].state is TargetState.STABLE
    assert registry.receipts() == (receipt,)


def test_engine_rejects_duplicate_selection_before_source_store_mutation(
    tmp_path: Path,
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    source_store_root = tmp_path / "source-store"

    with pytest.raises(ValueError, match="duplicate target"):
        engine.plan(("codex", "codex"))

    assert not source_store_root.exists()
    assert not home.exists()
    assert registry.receipts() == ()


def test_all_selected_targets_finish_provider_planning_before_target_mutation(
    tmp_path: Path,
) -> None:
    class FailingSecondProvider(_FixtureProvider):
        def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
            if target.id == "second":
                raise ValueError("injected second target planning failure")
            return super().plan(snapshot, target)

    _engine, registry, first_home = _engine_fixture(tmp_path)
    second_home = tmp_path / "second-home"
    config = registry.load()
    registry.save(
        RegistryConfig(
            1,
            config.sources,
            config.channels,
            config.targets
            + (TargetSpec("second", Framework.CODEX, second_home, "stable"),),
        )
    )
    engine = DeploymentEngine(
        registry,
        SourceStore(tmp_path / "other-source-store"),
        providers=(FailingSecondProvider(),),
    )

    with pytest.raises(ValueError, match="second target planning failure"):
        engine.refresh(("codex", "second"))

    assert not first_home.exists()
    assert not second_home.exists()
    assert registry.receipts() == ()


def test_missing_switch_ref_preserves_registry_target_and_receipts(tmp_path: Path) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    sentinel = home / "operator.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"unchanged\n")
    config = registry.load()
    registry.save(
        RegistryConfig(
            1,
            config.sources,
            config.channels
            + (ChannelSpec("missing", "community", "refs/heads/missing"),),
            config.targets,
        )
    )
    before = registry.load_snapshot()

    with pytest.raises(RuntimeError):
        engine.switch("missing", ("codex",))

    assert registry.load_snapshot() == before
    assert sentinel.read_bytes() == b"unchanged\n"
    assert registry.receipts() == ()


def test_waiting_same_home_refresh_replans_only_after_acquiring_operation_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class CoordinatedProvider(_FixtureProvider):
        def __init__(self) -> None:
            self.calls: dict[str, list[bool]] = {}
            self.guard = threading.Lock()

        def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
            name = threading.current_thread().name
            with self.guard:
                self.calls.setdefault(name, []).append(receipt_published.is_set())
                call = len(self.calls[name])
            if name == "waiting-refresh" and call == 1:
                waiting_initial_plan.set()
            return super().plan(snapshot, target)

    _engine, registry, _home = _engine_fixture(tmp_path)
    provider = CoordinatedProvider()
    first_engine = DeploymentEngine(
        registry,
        SourceStore(tmp_path / "first-store"),
        providers=(provider,),
    )
    waiting_engine = DeploymentEngine(
        registry,
        SourceStore(tmp_path / "waiting-store"),
        providers=(provider,),
    )
    first_at_receipt = threading.Event()
    release_first_receipt = threading.Event()
    receipt_published = threading.Event()
    waiting_initial_plan = threading.Event()
    errors: list[BaseException] = []
    original_append = registry.append_receipt

    def append_receipt(*args: object, **kwargs: object) -> Path:
        if threading.current_thread().name == "first-refresh":
            first_at_receipt.set()
            assert release_first_receipt.wait(timeout=5)
            result = original_append(*args, **kwargs)
            receipt_published.set()
            return result
        return original_append(*args, **kwargs)

    def refresh(engine: DeploymentEngine) -> None:
        try:
            engine.refresh(("codex",))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(registry, "append_receipt", append_receipt)
    first = threading.Thread(target=refresh, args=(first_engine,), name="first-refresh")
    waiting = threading.Thread(
        target=refresh,
        args=(waiting_engine,),
        name="waiting-refresh",
    )
    first.start()
    assert first_at_receipt.wait(timeout=5)
    waiting.start()
    assert waiting_initial_plan.wait(timeout=5)
    assert receipt_published.is_set() is False
    release_first_receipt.set()
    first.join(timeout=5)
    waiting.join(timeout=5)

    assert not first.is_alive()
    assert not waiting.is_alive()
    assert errors == []
    assert len(provider.calls["waiting-refresh"]) >= 2
    assert provider.calls["waiting-refresh"][-1] is True
    assert len(registry.receipts()) == 2


@pytest.mark.parametrize("operation", ["refresh", "switch"])
def test_grouped_audit_rejects_byte_identical_replacement_of_locked_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    if operation == "switch":
        source = tmp_path / "source"
        subprocess.run(
            ["git", "switch", "-c", "feature"],
            cwd=source,
            check=True,
            capture_output=True,
        )
        (source / "payload.txt").write_bytes(b"feature\n")
        subprocess.run(
            ["git", "commit", "-am", "feature"],
            cwd=source,
            check=True,
            capture_output=True,
        )
        config = registry.load()
        registry.save(
            RegistryConfig(
                1,
                config.sources,
                config.channels
                + (ChannelSpec("feature", "community", "refs/heads/feature"),),
                config.targets,
            )
        )
    original_registry = registry.load_snapshot()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    displaced = tmp_path / "displaced-home"
    observed: dict[str, dict[Path, tuple[int, bytes | None]]] = {}
    original_audit = __import__(
        "agent_ops.deployment.transaction", fromlist=["audit_provider_plans"]
    ).audit_provider_plans

    def tree_snapshot(root: Path) -> dict[Path, tuple[int, bytes | None]]:
        return {
            path.relative_to(root): (
                stat.S_IMODE(path.lstat().st_mode),
                path.read_bytes() if path.is_file() else None,
            )
            for path in root.rglob("*")
        }

    def replace_before_audit(plans: tuple[ProviderPlan, ...]) -> DeploymentAudit:
        os.replace(home, displaced)
        shutil.copytree(displaced, home, copy_function=shutil.copy2)
        observed["displaced"] = tree_snapshot(displaced)
        observed["replacement"] = tree_snapshot(home)
        return original_audit(plans)

    monkeypatch.setattr(
        "agent_ops.deployment.engine.audit_provider_plans",
        replace_before_audit,
    )

    with pytest.raises(DeploymentRecoveryError, match="recovery incomplete"):
        if operation == "refresh":
            engine.refresh(("codex",))
        else:
            engine.switch("feature", ("codex",))

    assert registry.load_snapshot() == original_registry
    assert registry.receipts() == ()
    assert outside.read_bytes() == b"outside\n"
    assert tree_snapshot(displaced) == observed["displaced"]
    assert tree_snapshot(home) == observed["replacement"]
    assert list((displaced / ".agentops/deployment/transactions").glob("*/record.json"))


def test_engine_audit_reports_modified_without_repairing_target(tmp_path: Path) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    engine.refresh(("codex",))
    installed = home / "skills/example/payload.txt"
    installed.write_bytes(b"operator change\n")

    receipt = engine.audit(("codex",))

    assert receipt.operation == "audit"
    assert receipt.targets[0].state is TargetState.MODIFIED
    assert installed.read_bytes() == b"operator change\n"
    assert registry.receipts()[-1] == receipt


def test_engine_audit_reports_exact_installed_target_as_stable(tmp_path: Path) -> None:
    engine, registry, _home = _engine_fixture(tmp_path)
    engine.refresh(("codex",))

    receipt = engine.audit(("codex",))

    assert receipt.targets[0].state is TargetState.STABLE
    assert registry.receipts()[-1] == receipt


def test_launch_authorization_retains_exact_audit_and_revalidates_home(
    tmp_path: Path,
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    refreshed = engine.refresh(("codex",))

    with engine.launch_authorization("codex") as authorization:
        assert authorization.target == registry.load().targets[0]
        assert authorization.status == TargetStatus(
            "codex", TargetState.STABLE, "stable", refreshed.commits[0]
        )
        assert authorization.receipt.operation == "audit"
        assert authorization.plan.provider_plans[0].target == authorization.target
        authorization.verify()

    assert registry.receipts()[-1] == authorization.receipt


@pytest.mark.parametrize("operation", ["refresh", "switch", "save"])
def test_launch_authorization_blocks_cooperative_target_or_registry_changes(
    tmp_path: Path, operation: str
) -> None:
    engine, registry, _home = _engine_fixture(tmp_path)
    engine.refresh(("codex",))
    if operation == "switch":
        source = tmp_path / "source"
        _create_branch(source, "feature", b"feature\n")
        current = registry.load()
        registry.save(
            RegistryConfig(
                1,
                current.sources,
                current.channels
                + (ChannelSpec("feature", "community", "refs/heads/feature"),),
                current.targets,
            )
        )
    entered = threading.Event()
    completed = threading.Event()
    errors: list[BaseException] = []

    def change() -> None:
        entered.set()
        try:
            if operation == "refresh":
                engine.refresh(("codex",))
            elif operation == "switch":
                engine.switch("feature", ("codex",))
            else:
                snapshot = registry.load_snapshot()
                registry.save(snapshot.config, expected_snapshot=snapshot)
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    with engine.launch_authorization("codex") as authorization:
        worker = threading.Thread(target=change)
        worker.start()
        assert entered.wait(timeout=5)
        assert completed.wait(timeout=0.1) is False
        authorization.verify()
    worker.join(timeout=5)

    assert worker.is_alive() is False
    assert errors == []


def test_launch_authorization_rejects_renamed_home_before_handoff(
    tmp_path: Path,
) -> None:
    engine, _registry, home = _engine_fixture(tmp_path)
    engine.refresh(("codex",))
    displaced = tmp_path / "displaced-home"

    with engine.launch_authorization("codex") as authorization:
        os.replace(home, displaced)
        home.mkdir()
        with pytest.raises(ValueError, match="canonical home identity changed"):
            authorization.verify()


def test_launch_authorization_rejects_inheritable_target_authority_descriptor(
    tmp_path: Path,
) -> None:
    import agent_ops.deployment.transaction as transaction

    engine, _registry, home = _engine_fixture(tmp_path)
    engine.refresh(("codex",))

    with engine.launch_authorization("codex") as authorization:
        active = transaction._GROUP_HOME_LOCKS.get()
        assert active is not None
        descriptor = active[home].descriptor
        os.set_inheritable(descriptor, True)
        try:
            with pytest.raises(RuntimeError, match="close-on-exec"):
                authorization.verify()
        finally:
            os.set_inheritable(descriptor, False)


def test_engine_status_is_conservative_and_does_not_fetch(tmp_path: Path) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    engine.refresh(("codex",))
    store_state_before = sorted(
        (path.relative_to(tmp_path / "source-store"), path.stat().st_mtime_ns)
        for path in (tmp_path / "source-store").rglob("*")
    )

    statuses = engine.status(("codex",))

    assert statuses[0].state is TargetState.STALE
    assert home.exists()
    assert len(registry.receipts()) == 1
    assert store_state_before == sorted(
        (path.relative_to(tmp_path / "source-store"), path.stat().st_mtime_ns)
        for path in (tmp_path / "source-store").rglob("*")
    )


def test_engine_rejects_provider_snapshot_tamper_before_target_mutation(
    tmp_path: Path,
) -> None:
    class TamperingProvider(_FixtureProvider):
        def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
            plan = super().plan(snapshot, target)
            (snapshot.root / "payload.txt").write_bytes(b"tampered\n")
            return plan

    engine, registry, home = _engine_fixture(tmp_path)
    engine = DeploymentEngine(
        registry,
        SourceStore(tmp_path / "other-source-store"),
        providers=(TamperingProvider(),),
    )

    with pytest.raises(RuntimeError, match="changed the restricted source snapshot"):
        engine.refresh(("codex",))

    assert not home.exists()
    assert registry.receipts() == ()


def test_engine_requires_provider_support_decision_to_be_boolean(tmp_path: Path) -> None:
    class InvalidSupportsProvider(_FixtureProvider):
        def supports(self, snapshot: SourceSnapshot, target: TargetSpec) -> bool:
            return "yes"  # type: ignore[return-value]

    _engine, registry, home = _engine_fixture(tmp_path)
    engine = DeploymentEngine(
        registry,
        SourceStore(tmp_path / "other-source-store"),
        providers=(InvalidSupportsProvider(),),
    )

    with pytest.raises(ValueError, match="supports decision must be boolean"):
        engine.plan(("codex",))

    assert not home.exists()


def test_engine_materializes_only_the_declared_directory_closure(
    tmp_path: Path,
) -> None:
    class DirectoryProvider(_FixtureProvider):
        def source_closure(
            self,
            snapshot: SourceSnapshot,
            target: TargetSpec,
            selection: tuple[str, ...] | None,
        ) -> tuple[Path, ...]:
            return (Path("catalog"),)

        def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
            assert not (snapshot.root / "payload.txt").exists()
            source = snapshot.root / "catalog/nested/data.txt"
            return ProviderPlan(
                self.provider_id,
                snapshot.commit,
                target,
                (PlannedFile(Path("skills/example/data.txt"), source.read_bytes(), 0o640),),
            )

    _engine, registry, home = _engine_fixture(tmp_path)
    source = tmp_path / "source"
    nested = source / "catalog/nested"
    nested.mkdir(parents=True)
    data = nested / "data.txt"
    data.write_bytes(b"nested\n")
    data.chmod(0o600)
    subprocess.run(["git", "add", "catalog"], cwd=source, check=True)
    subprocess.run(
        ["git", "commit", "-m", "catalog"], cwd=source, check=True, capture_output=True
    )
    engine = DeploymentEngine(
        registry,
        SourceStore(tmp_path / "other-source-store"),
        providers=(DirectoryProvider(),),
    )

    engine.refresh(("codex",))

    assert (home / "skills/example/data.txt").read_bytes() == b"nested\n"


def test_switch_saves_candidate_only_after_install_and_audit(tmp_path: Path) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    source = tmp_path / "source"
    subprocess.run(["git", "switch", "-c", "feature"], cwd=source, check=True, capture_output=True)
    (source / "payload.txt").write_bytes(b"feature\n")
    subprocess.run(["git", "commit", "-am", "feature"], cwd=source, check=True, capture_output=True)
    current = registry.load()
    registry.save(
        RegistryConfig(
            1,
            current.sources,
            current.channels + (ChannelSpec("feature", "community", "refs/heads/feature"),),
            current.targets,
        )
    )

    receipt = engine.switch("feature", ("codex",))

    assert receipt.operation == "switch"
    assert receipt.targets[0].state is TargetState.BRANCH
    assert registry.load().targets[0].channel == "feature"
    assert (home / "skills/example/payload.txt").read_bytes() == b"feature\n"
    assert registry.receipts()[-1] == receipt


def test_second_target_audit_mismatch_restores_every_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, registry, first_home = _engine_fixture(tmp_path)
    config = registry.load()
    second_home = tmp_path / "second-home"
    registry.save(
        RegistryConfig(
            1,
            config.sources,
            config.channels,
            config.targets
            + (TargetSpec("second", Framework.CODEX, second_home, "stable"),),
        )
    )

    def audit(plans: tuple[ProviderPlan, ...]) -> DeploymentAudit:
        target_id = plans[0].target.id
        return DeploymentAudit(target_id, matches=target_id != "second")

    monkeypatch.setattr("agent_ops.deployment.engine.audit_provider_plans", audit)

    with pytest.raises(RuntimeError, match="audit did not match"):
        engine.refresh(("codex", "second"))

    assert not (first_home / "skills/example/payload.txt").exists()
    assert not (second_home / "skills/example/payload.txt").exists()
    assert registry.receipts() == ()


def test_refresh_receipt_failure_rolls_back_installed_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    append_error = OSError("injected receipt failure")

    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise append_error

    monkeypatch.setattr(registry, "append_receipt", fail_append)

    with pytest.raises(OSError) as caught:
        engine.refresh(("codex",))

    assert caught.value is append_error
    assert not (home / "skills/example/payload.txt").exists()
    assert registry.receipts() == ()


def test_engine_recovery_process_control_is_preserved_after_receipt_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    append_error = OSError("ordinary receipt failure")
    control = SystemExit(23)

    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise append_error

    def interrupt_recovery(_manifests: tuple[object, ...]) -> None:
        raise control

    monkeypatch.setattr(registry, "append_receipt", fail_append)
    monkeypatch.setattr("agent_ops.deployment.engine.rollback_manifests", interrupt_recovery)

    with pytest.raises(SystemExit) as caught:
        engine.refresh(("codex",))

    assert caught.value is control
    assert any("recovery incomplete" in note for note in control.__notes__)
    assert (home / "skills/example/payload.txt").read_bytes() == b"deployed\n"


def test_refresh_process_control_during_audit_is_preserved_after_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    control = KeyboardInterrupt("operator stop")

    def interrupt(_plans: tuple[ProviderPlan, ...]) -> DeploymentAudit:
        raise control

    monkeypatch.setattr("agent_ops.deployment.engine.audit_provider_plans", interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        engine.refresh(("codex",))

    assert caught.value is control
    assert not (home / "skills/example/payload.txt").exists()
    assert registry.receipts() == ()


def test_switch_receipt_failure_restores_registry_and_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    source = tmp_path / "source"
    subprocess.run(
        ["git", "switch", "-c", "feature"], cwd=source, check=True, capture_output=True
    )
    (source / "payload.txt").write_bytes(b"feature\n")
    subprocess.run(
        ["git", "commit", "-am", "feature"], cwd=source, check=True, capture_output=True
    )
    original = registry.load()
    registry.save(
        RegistryConfig(
            1,
            original.sources,
            original.channels
            + (ChannelSpec("feature", "community", "refs/heads/feature"),),
            original.targets,
        )
    )
    configured = registry.load()
    append_error = OSError("injected switch receipt failure")

    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise append_error

    monkeypatch.setattr(registry, "append_receipt", fail_append)

    with pytest.raises(OSError) as caught:
        engine.switch("feature", ("codex",))

    assert caught.value is append_error
    assert registry.load() == configured
    assert registry.load().targets[0].channel == "stable"
    assert not (home / "skills/example/payload.txt").exists()
    assert registry.receipts() == ()


def test_switch_registry_recovery_process_control_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    source = tmp_path / "source"
    subprocess.run(
        ["git", "switch", "-c", "feature"], cwd=source, check=True, capture_output=True
    )
    (source / "payload.txt").write_bytes(b"feature\n")
    subprocess.run(
        ["git", "commit", "-am", "feature"], cwd=source, check=True, capture_output=True
    )
    original = registry.load()
    registry.save(
        RegistryConfig(
            1,
            original.sources,
            original.channels
            + (ChannelSpec("feature", "community", "refs/heads/feature"),),
            original.targets,
        )
    )
    append_error = OSError("ordinary switch receipt failure")
    control = KeyboardInterrupt("operator stopped registry recovery")
    original_save = registry.save

    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise append_error

    def save(config: RegistryConfig, **kwargs: object):
        if config.targets[0].channel == "stable" and kwargs.get("expected_snapshot"):
            raise control
        return original_save(config, **kwargs)

    monkeypatch.setattr(registry, "append_receipt", fail_append)
    monkeypatch.setattr(registry, "save", save)

    with pytest.raises(KeyboardInterrupt) as caught:
        engine.switch("feature", ("codex",))

    assert caught.value is control
    assert any("registry" in note and "recovery incomplete" in note for note in control.__notes__)
    assert registry.load().targets[0].channel == "feature"
    assert (home / "skills/example/payload.txt").read_bytes() == b"feature\n"


def test_switch_target_recovery_process_control_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, registry, home = _engine_fixture(tmp_path)
    source = tmp_path / "source"
    subprocess.run(
        ["git", "switch", "-c", "feature"], cwd=source, check=True, capture_output=True
    )
    (source / "payload.txt").write_bytes(b"feature\n")
    subprocess.run(
        ["git", "commit", "-am", "feature"], cwd=source, check=True, capture_output=True
    )
    original = registry.load()
    registry.save(
        RegistryConfig(
            1,
            original.sources,
            original.channels
            + (ChannelSpec("feature", "community", "refs/heads/feature"),),
            original.targets,
        )
    )
    append_error = OSError("ordinary switch receipt failure")
    control = SystemExit(29)

    def fail_append(*_args: object, **_kwargs: object) -> None:
        raise append_error

    def interrupt_recovery(_manifests: tuple[object, ...]) -> None:
        raise control

    monkeypatch.setattr(registry, "append_receipt", fail_append)
    monkeypatch.setattr("agent_ops.deployment.engine.rollback_manifests", interrupt_recovery)

    with pytest.raises(SystemExit) as caught:
        engine.switch("feature", ("codex",))

    assert caught.value is control
    assert any("restoring targets" in note for note in control.__notes__)
    assert registry.load().targets[0].channel == "stable"
    assert (home / "skills/example/payload.txt").read_bytes() == b"feature\n"


def _dependency(
    dependency_id: str,
    *,
    ref: str,
    strategy: str,
    destination: str,
    source: str | None = None,
) -> SkillDependency:
    return SkillDependency(
        id=dependency_id,
        name=dependency_id,
        repo=f"https://example.invalid/{dependency_id}.git",
        ref=ref,
        install={
            "codex": SkillDependencyInstall(
                strategy=strategy,
                source=source,
                destination=destination,
            )
        },
    )


def _show_me_source(root: Path) -> Path:
    skill = root / "plugins/show-me/skills/show-me"
    skill.mkdir(parents=True)
    (root / "LICENSE").write_text("MIT\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        """---
name: show-me
---
Then open it for the user:

```
Bash(open path/to/show-me-{description}.html)
```
""",
        encoding="utf-8",
    )
    return root


def _checkout_from(sources: dict[str, Path]):
    def checkout(dependency: SkillDependency, _cache: Path) -> Path:
        return sources[dependency.id]

    return checkout


def test_public_bundle_plans_contain_exact_bytes_modes_and_identity(tmp_path: Path) -> None:
    gstack = tmp_path / "gstack"
    (gstack / "bin").mkdir(parents=True)
    executable = gstack / "bin/gstack-config"
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o755)
    (gstack / "SKILL.md").write_bytes(b"gstack\n")
    superpowers = tmp_path / "superpowers"
    skill = superpowers / "skills/writing-plans/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"plans\n")
    show_me = _show_me_source(tmp_path / "show-me")
    dependencies = [
        _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack"),
        _dependency(
            "superpowers",
            ref="2" * 40,
            strategy="copy-skills",
            source="skills",
            destination="skills",
        ),
        _dependency(
            "humanlayer-show-me",
            ref="4d8d644ca747517973f58d7953f58d7cd07520cd",
            strategy="humanlayer-show-me",
            source="plugins/show-me/skills",
            destination="skills",
        ),
    ]
    home = tmp_path / "home"

    plans = build_public_skill_plans(
        framework=Framework.CODEX,
        dependencies=dependencies,
        target_home=home,
        cache_root=tmp_path / "cache",
        checkout_dependency=_checkout_from(
            {"gstack": gstack, "superpowers": superpowers, "humanlayer-show-me": show_me}
        ),
    )

    assert [plan.provider_id for plan in plans] == [
        "public-skill:gstack",
        "public-skill:superpowers",
        "public-skill:humanlayer-show-me",
    ]
    assert len({plan.source_revision for plan in plans}) == 1
    assert all(dependency.ref in plans[0].source_revision for dependency in dependencies)
    assert all(
        plan.target == TargetSpec("public-skills:codex", Framework.CODEX, home, "public")
        for plan in plans
    )
    files = {item.path: item for plan in plans for item in plan.files}
    assert files[Path("skills/gstack/bin/gstack-config")] == PlannedFile(
        Path("skills/gstack/bin/gstack-config"), b"#!/bin/sh\n", 0o755
    )
    assert files[Path("skills/gstack/SKILL.md")].content == b"gstack\n"
    assert files[Path("skills/writing-plans/SKILL.md")].content == b"plans\n"
    assert (
        b"supported artifact preview or file-opening capability"
        in files[Path("skills/show-me/SKILL.md")].content
    )
    assert not home.exists()


def test_public_plan_builder_renders_over_shadow_without_mutating_existing_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(b"new\n")
    (source / "SKILL.md").chmod(0o644)
    home = tmp_path / "home"
    installed = home / "skills/gstack/SKILL.md"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"old\n")
    dependency = _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack")

    plans = build_public_skill_plans(
        framework=Framework.CODEX,
        dependencies=[dependency],
        target_home=home,
        cache_root=tmp_path / "cache",
        checkout_dependency=lambda _dependency, _cache: source,
    )

    assert plans[0].files[0] == PlannedFile(Path("skills/gstack/SKILL.md"), b"new\n", 0o644)
    assert plans[0].files[1].path == Path("skills/.agentops-public-provider-index.json")
    assert installed.read_bytes() == b"old\n"


def test_public_plan_builder_rejects_cache_overlap_before_checkout(tmp_path: Path) -> None:
    home = tmp_path / "home"
    dependency = _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack")
    checkout_called = False

    def checkout(_dependency: SkillDependency, _cache: Path) -> Path:
        nonlocal checkout_called
        checkout_called = True
        raise AssertionError("overlapping cache reached checkout")

    with pytest.raises(ValueError, match="cache.*target"):
        build_public_skill_plans(
            framework=Framework.CODEX,
            dependencies=[dependency],
            target_home=home,
            cache_root=home / "cache",
            checkout_dependency=checkout,
        )

    assert checkout_called is False
    assert not home.exists()


def test_all_bundles_plan_before_one_shared_transaction(tmp_path: Path, monkeypatch) -> None:
    dependencies = [
        _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack"),
        _dependency(
            "superpowers",
            ref="2" * 40,
            strategy="copy-skills",
            source="skills",
            destination="skills",
        ),
    ]
    sources = {}
    for dependency in dependencies:
        source = tmp_path / dependency.id
        source.mkdir()
        (source / "SKILL.md").write_text(dependency.id, encoding="utf-8")
        if dependency.id == "superpowers":
            nested = source / "skills/example"
            nested.mkdir(parents=True)
            (nested / "SKILL.md").write_text("example", encoding="utf-8")
        sources[dependency.id] = source
    home = tmp_path / "home"
    home.mkdir()
    existing = home / "existing.txt"
    existing.write_bytes(b"unchanged\n")
    existing.chmod(0o600)
    unrelated_manifest = home / ".agentops/deployment/manifests/unrelated.json"
    unrelated_manifest.parent.mkdir(parents=True)
    unrelated_manifest.write_bytes(b"unrelated manifest\n")
    unrelated_manifest.chmod(0o600)

    def snapshot() -> dict[str, tuple[bytes, int]]:
        return {
            path.relative_to(home).as_posix(): (
                path.read_bytes(),
                path.stat().st_mode & 0o777,
            )
            for path in home.rglob("*")
            if path.is_file()
        }

    before = snapshot()
    original = __import__(
        "agent_ops.deployment.public_skills", fromlist=["_render_dependency"]
    )._render_dependency
    rendered: list[str] = []

    def fail_final(*args, **kwargs):
        dependency = kwargs["dependency"]
        rendered.append(dependency.id)
        if dependency.id == "superpowers":
            raise ValueError("injected final bundle failure")
        return original(*args, **kwargs)

    applied: list[tuple[ProviderPlan, ...]] = []
    monkeypatch.setattr("agent_ops.deployment.public_skills._render_dependency", fail_final)
    monkeypatch.setattr("agent_ops.skill_installer.install_provider_plans", applied.append)
    monkeypatch.setattr("agent_ops.skill_installer._checkout_dependency", _checkout_from(sources))

    with pytest.raises(ValueError, match="injected final bundle failure"):
        install_skill_dependencies(
            framework=Framework.CODEX,
            dependencies=dependencies,
            home=home,
            cache_dir=tmp_path / "cache",
        )

    assert rendered == ["gstack", "superpowers"]
    assert applied == []
    assert snapshot() == before


def test_successful_install_calls_shared_transaction_once_with_complete_tuple(
    tmp_path: Path, monkeypatch
) -> None:
    dependencies = [
        _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack"),
        _dependency(
            "superpowers",
            ref="2" * 40,
            strategy="copy-skills",
            source="skills",
            destination="skills",
        ),
    ]
    gstack = tmp_path / "gstack"
    gstack.mkdir()
    (gstack / "SKILL.md").write_text("gstack\n", encoding="utf-8")
    superpowers = tmp_path / "superpowers/skills/example"
    superpowers.mkdir(parents=True)
    (superpowers / "SKILL.md").write_text("superpowers\n", encoding="utf-8")
    calls: list[tuple[ProviderPlan, ...]] = []

    def install(plans: tuple[ProviderPlan, ...]) -> None:
        calls.append(plans)
        apply_provider_plans(plans)

    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency",
        _checkout_from(
            {
                "gstack": gstack,
                "superpowers": tmp_path / "superpowers",
            }
        ),
    )
    monkeypatch.setattr("agent_ops.skill_installer.install_provider_plans", install)

    rows = install_skill_dependencies(
        framework=Framework.CODEX,
        dependencies=dependencies,
        home=tmp_path / "home",
    )

    assert len(calls) == 1
    assert [plan.provider_id for plan in calls[0]] == [
        "public-skill:gstack",
        "public-skill:superpowers",
    ]
    assert [row.id for row in rows] == ["gstack", "superpowers"]
    manifest = json.loads(
        next((tmp_path / "home/.agentops/deployment/manifests").glob("*.json")).read_text()
    )
    assert manifest["provider_ids"] == [
        "public-skill:gstack",
        "public-skill:superpowers",
    ]


def test_installed_dependency_report_is_derived_from_its_plan(tmp_path: Path) -> None:
    target = TargetSpec("public-skills:codex", Framework.CODEX, tmp_path / "home", "public")
    plan = ProviderPlan("public-skill:gstack", "revision", target, ())
    install = SkillDependencyInstall(strategy="gstack", destination="skills/gstack")

    row = InstalledSkillDependency.from_plan(plan, install=install, dry_run=True)

    assert row == InstalledSkillDependency(
        id="gstack",
        framework=Framework.CODEX,
        destination=tmp_path / "home/skills/gstack",
        strategy="gstack",
        dry_run=True,
    )


def test_non_posix_multi_bundle_route_fails_before_native_application(
    tmp_path: Path, monkeypatch
) -> None:
    import agent_ops.skill_installer as skill_installer

    dependencies = [
        _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack"),
        _dependency(
            "superpowers",
            ref="2" * 40,
            strategy="copy-skills",
            source="skills",
            destination="skills",
        ),
    ]
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(b"gstack\n")
    home = tmp_path / "home"
    planned: list[tuple[str, ...]] = []
    applied: list[str] = []
    original_build = skill_installer.build_public_skill_plans
    original_install = skill_installer._install_dependency

    def build(**kwargs):
        planned.append(tuple(item.id for item in kwargs["dependencies"]))
        if len(kwargs["dependencies"]) == 1:
            return original_build(**kwargs)
        target = TargetSpec("public-skills:codex", Framework.CODEX, kwargs["target_home"], "public")
        return tuple(
            ProviderPlan(f"public-skill:{item.id}", "revision", target, ())
            for item in kwargs["dependencies"]
        )

    def install(**kwargs) -> None:
        if kwargs["target_home"] == home:
            applied.append(kwargs["dependency_id"])
        else:
            original_install(**kwargs)

    monkeypatch.setattr("agent_ops.skill_installer._use_shared_transaction_engine", lambda: False)
    monkeypatch.setattr("agent_ops.skill_installer.build_public_skill_plans", build)
    monkeypatch.setattr("agent_ops.skill_installer._install_dependency", install)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency",
        lambda _dependency, _cache: source,
    )

    with pytest.raises(UnsupportedPlatformError, match="atomic multi-bundle"):
        install_skill_dependencies(
            framework=Framework.CODEX,
            dependencies=dependencies,
            home=home,
        )

    assert planned == [("gstack", "superpowers")]
    assert applied == []
    assert not home.exists()

    with pytest.raises(UnsupportedPlatformError, match="rollback transaction"):
        install_skill_dependencies(
            framework=Framework.CODEX,
            dependencies=[dependencies[0]],
            home=home,
        )

    assert planned[-1] == ("gstack",)
    assert applied == []


def test_shared_manifest_removes_stale_owned_files_and_preserves_unknowns(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    old = source / "old.txt"
    old.write_bytes(b"old\n")
    dependency = _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack")
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency",
        lambda _dependency, _cache: source,
    )
    home = tmp_path / "home"

    install_skill_dependencies(framework=Framework.CODEX, dependencies=[dependency], home=home)
    unknown = home / "skills/notes/private.txt"
    unknown.parent.mkdir(parents=True)
    unknown.write_bytes(b"keep\n")
    old.unlink()
    (source / "new.txt").write_bytes(b"new\n")
    updated = dependency.model_copy(update={"ref": "2" * 40})

    install_skill_dependencies(framework=Framework.CODEX, dependencies=[updated], home=home)

    assert not (home / "skills/gstack/old.txt").exists()
    assert (home / "skills/gstack/new.txt").read_bytes() == b"new\n"
    assert unknown.read_bytes() == b"keep\n"
    manifests = list((home / ".agentops/deployment/manifests").glob("*.json"))
    assert len(manifests) == 1
    data = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert data["provider_ids"] == ["public-skill:gstack"]


def test_subset_update_carries_unselected_provider_ownership_without_checkout(
    tmp_path: Path, monkeypatch
) -> None:
    dependencies = [
        _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack"),
        _dependency(
            "superpowers",
            ref="2" * 40,
            strategy="copy-skills",
            source="skills",
            destination="skills",
        ),
    ]
    gstack = tmp_path / "gstack"
    gstack.mkdir()
    (gstack / "old.txt").write_bytes(b"old a\n")
    superpowers = tmp_path / "superpowers/skills/provider-b"
    superpowers.mkdir(parents=True)
    preserved = superpowers / "SKILL.md"
    preserved.write_bytes(b"provider b\n")
    preserved.chmod(0o640)
    sources = {"gstack": gstack, "superpowers": tmp_path / "superpowers"}
    checkouts: list[str] = []

    def checkout(dependency: SkillDependency, _cache: Path) -> Path:
        checkouts.append(dependency.id)
        return sources[dependency.id]

    monkeypatch.setattr("agent_ops.skill_installer._checkout_dependency", checkout)
    home = tmp_path / "home"
    install_skill_dependencies(framework=Framework.CODEX, dependencies=dependencies, home=home)
    b_target = home / "skills/provider-b/SKILL.md"
    unknown = home / "skills/private-notes.txt"
    unknown.write_bytes(b"unknown\n")
    (gstack / "old.txt").unlink()
    (gstack / "new.txt").write_bytes(b"new a\n")
    dependencies[0] = dependencies[0].model_copy(update={"ref": "3" * 40})
    checkouts.clear()

    install_skill_dependencies(
        framework=Framework.CODEX,
        dependencies=dependencies,
        dependency_ids=["gstack"],
        home=home,
    )

    assert checkouts == ["gstack"]
    assert not (home / "skills/gstack/old.txt").exists()
    assert (home / "skills/gstack/new.txt").read_bytes() == b"new a\n"
    assert b_target.read_bytes() == b"provider b\n"
    assert stat.S_IMODE(b_target.stat().st_mode) == 0o640
    assert unknown.read_bytes() == b"unknown\n"
    manifest = json.loads(
        next((home / ".agentops/deployment/manifests").glob("*.json")).read_text()
    )
    assert manifest["provider_ids"] == [
        "public-skill:gstack",
        "public-skill:superpowers",
    ]
    index = json.loads((home / "skills/.agentops-public-provider-index.json").read_text())
    assert [item["provider_id"] for item in index["providers"]] == manifest["provider_ids"]


@pytest.mark.parametrize("drift", ["missing", "changed"])
def test_subset_dry_run_rejects_unselected_provider_drift_before_checkout(
    tmp_path: Path, monkeypatch, drift: str
) -> None:
    dependencies = [
        _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack"),
        _dependency(
            "superpowers",
            ref="2" * 40,
            strategy="copy-skills",
            source="skills",
            destination="skills",
        ),
    ]
    gstack = tmp_path / "gstack"
    gstack.mkdir()
    (gstack / "SKILL.md").write_bytes(b"provider a\n")
    superpowers = tmp_path / "superpowers/skills/provider-b"
    superpowers.mkdir(parents=True)
    (superpowers / "SKILL.md").write_bytes(b"provider b\n")
    sources = {"gstack": gstack, "superpowers": tmp_path / "superpowers"}
    checkouts: list[str] = []

    def checkout(dependency: SkillDependency, _cache: Path) -> Path:
        checkouts.append(dependency.id)
        return sources[dependency.id]

    monkeypatch.setattr("agent_ops.skill_installer._checkout_dependency", checkout)
    home = tmp_path / "home"
    install_skill_dependencies(framework=Framework.CODEX, dependencies=dependencies, home=home)
    installed = home / "skills/provider-b/SKILL.md"
    if drift == "missing":
        installed.unlink()
    else:
        installed.write_bytes(b"changed\n")
    checkouts.clear()

    with pytest.raises(ValueError, match="prior managed file changed"):
        install_skill_dependencies(
            framework=Framework.CODEX,
            dependencies=dependencies,
            dependency_ids=["gstack"],
            home=home,
            dry_run=True,
        )

    assert checkouts == []


@pytest.mark.parametrize("dry_run", [True, False])
def test_dry_run_and_live_reject_unmanaged_conflict_without_mutation(
    tmp_path: Path, monkeypatch, dry_run: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(b"planned\n")
    dependency = _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack")
    home = tmp_path / "home"
    conflict = home / "skills/gstack/SKILL.md"
    conflict.parent.mkdir(parents=True)
    conflict.write_bytes(b"unmanaged\n")
    before = conflict.read_bytes()
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency",
        lambda _dependency, _cache: source,
    )

    with pytest.raises(ValueError, match="unmanaged destination conflicts"):
        install_skill_dependencies(
            framework=Framework.CODEX,
            dependencies=[dependency],
            home=home,
            dry_run=dry_run,
        )

    assert conflict.read_bytes() == before
    assert not (home / ".agentops/deployment").exists()


@pytest.mark.parametrize("dry_run", [True, False])
def test_dry_run_and_live_reject_prior_directory_drift_without_mutation(
    tmp_path: Path, monkeypatch, dry_run: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(b"planned\n")
    dependency = _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack")
    home = tmp_path / "home"
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency",
        lambda _dependency, _cache: source,
    )
    install_skill_dependencies(framework=Framework.CODEX, dependencies=[dependency], home=home)
    directory = home / "skills/gstack"
    directory.chmod(0o700)
    before = (directory / "SKILL.md").read_bytes()

    with pytest.raises(ValueError, match="prior .*directory changed"):
        install_skill_dependencies(
            framework=Framework.CODEX,
            dependencies=[dependency],
            home=home,
            dry_run=dry_run,
        )

    assert (directory / "SKILL.md").read_bytes() == before
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@pytest.mark.parametrize("owned_path", ["{absolute}", "../outside.txt"])
def test_shared_manifest_rejects_unsafe_owned_paths_before_shadow_removal(
    tmp_path: Path, monkeypatch, owned_path: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(b"managed\n")
    dependency = _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack")
    home = tmp_path / "home"
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency",
        lambda _dependency, _cache: source,
    )
    install_skill_dependencies(framework=Framework.CODEX, dependencies=[dependency], home=home)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"unknown\n")
    outside.chmod(0o640)
    manifest = next((home / ".agentops/deployment/manifests").glob("*.json"))
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["files"] = [
        {
            "path": str(outside) if owned_path == "{absolute}" else owned_path,
            "fingerprint": hashlib.sha256(outside.read_bytes()).hexdigest(),
            "mode": 0o640,
        }
    ]
    manifest.write_text(json.dumps(data), encoding="utf-8")

    error: ValueError | None = None
    try:
        install_skill_dependencies(
            framework=Framework.CODEX,
            dependencies=[dependency],
            home=home,
            dry_run=True,
        )
    except ValueError as exc:
        error = exc

    assert outside.read_bytes() == b"unknown\n"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o640
    assert error is not None
    assert "manifest" in str(error)


def test_shared_manifest_rejects_duplicate_json_members(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_bytes(b"managed\n")
    dependency = _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack")
    home = tmp_path / "home"
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency",
        lambda _dependency, _cache: source,
    )
    install_skill_dependencies(framework=Framework.CODEX, dependencies=[dependency], home=home)
    manifest = next((home / ".agentops/deployment/manifests").glob("*.json"))
    content = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        content.replace('"target_id":', '"target_id": "duplicate", "target_id":', 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate"):
        install_skill_dependencies(
            framework=Framework.CODEX,
            dependencies=[dependency],
            home=home,
            dry_run=True,
        )


def test_exact_legacy_show_me_state_is_adopted_and_no_longer_controls_updates(
    tmp_path: Path, monkeypatch
) -> None:
    source = _show_me_source(tmp_path / "source")
    home = tmp_path / "home"
    install_show_me(source, home / "skills/show-me")
    legacy_manifest = home / OWNERSHIP_MANIFEST_RELATIVE
    legacy_bytes = legacy_manifest.read_bytes()
    dependency = _dependency(
        "humanlayer-show-me",
        ref="4d8d644ca747517973f58d7953f58d7cd07520cd",
        strategy="humanlayer-show-me",
        source="plugins/show-me/skills",
        destination="skills",
    )
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency",
        lambda _dependency, _cache: source,
    )

    install_skill_dependencies(framework=Framework.CODEX, dependencies=[dependency], home=home)
    install_skill_dependencies(framework=Framework.CODEX, dependencies=[dependency], home=home)

    assert legacy_manifest.read_bytes() == legacy_bytes
    assert len(list((home / ".agentops/deployment/manifests").glob("*.json"))) == 1


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_source_closure_rejects_nonregular_entries(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("body", encoding="utf-8")
    unsafe = source / "unsafe"
    if kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("outside", encoding="utf-8")
        unsafe.symlink_to(outside)
    else:
        os.mkfifo(unsafe)
    dependency = _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack")

    with pytest.raises(ValueError, match="unsupported source entry"):
        build_public_skill_plans(
            framework=Framework.CODEX,
            dependencies=[dependency],
            target_home=tmp_path / "home",
            cache_root=tmp_path / "cache",
            checkout_dependency=lambda _dependency, _cache: source,
        )

    assert not (tmp_path / "home").exists()


@pytest.mark.parametrize("kind", ["directory-symlink", "excluded-file-alias"])
def test_source_closure_rejects_unsafe_materialized_aliases(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("body", encoding="utf-8")
    if kind == "directory-symlink":
        real = source / "real"
        real.mkdir()
        (real / "nested.txt").write_bytes(b"nested\n")
        (real / "cycle").symlink_to(real, target_is_directory=True)
        (source / "alias").symlink_to(real, target_is_directory=True)
    else:
        excluded = source / "node_modules/package"
        excluded.mkdir(parents=True)
        (excluded / "secret.txt").write_bytes(b"secret\n")
        (source / "alias.txt").symlink_to(excluded / "secret.txt")
    dependency = _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack")

    with pytest.raises(ValueError, match="unsupported source entry"):
        build_public_skill_plans(
            framework=Framework.CODEX,
            dependencies=[dependency],
            target_home=tmp_path / "home",
            cache_root=tmp_path / "cache",
            checkout_dependency=lambda _dependency, _cache: source,
        )

    assert not (tmp_path / "home").exists()


def test_source_closure_materializes_confined_regular_file_alias(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    regular = source / "regular.txt"
    regular.write_bytes(b"aliased\n")
    regular.chmod(0o640)
    (source / "alias.txt").symlink_to(regular)
    dependency = _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack")

    plan = build_public_skill_plans(
        framework=Framework.CODEX,
        dependencies=[dependency],
        target_home=tmp_path / "home",
        cache_root=tmp_path / "cache",
        checkout_dependency=lambda _dependency, _cache: source,
    )[0]

    assert PlannedFile(Path("skills/gstack/alias.txt"), b"aliased\n", 0o640) in plan.files


def test_source_closure_materializes_fully_validated_directory_alias(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    regular = source / "regular"
    regular.mkdir()
    skill = regular / "SKILL.md"
    skill.write_bytes(b"aliased directory\n")
    skill.chmod(0o640)
    (source / "alias").symlink_to(regular, target_is_directory=True)
    dependency = _dependency("gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack")

    plan = build_public_skill_plans(
        framework=Framework.CODEX,
        dependencies=[dependency],
        target_home=tmp_path / "home",
        cache_root=tmp_path / "cache",
        checkout_dependency=lambda _dependency, _cache: source,
    )[0]

    assert (
        PlannedFile(Path("skills/gstack/alias/SKILL.md"), b"aliased directory\n", 0o640)
        in plan.files
    )


def test_checkout_rejects_untracked_output_before_rendering(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "agentops@example.com"],
        cwd=source,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Agent Ops"], cwd=source, check=True)
    (source / "SKILL.md").write_text("body", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=source, check=True, capture_output=True)
    ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
    ).stdout.strip()
    (source / "generated.txt").write_text("untracked", encoding="utf-8")
    dependency = _dependency("gstack", ref=ref, strategy="gstack", destination="skills/gstack")

    with pytest.raises(ValueError, match="changed or untracked"):
        build_public_skill_plans(
            framework=Framework.CODEX,
            dependencies=[dependency],
            target_home=tmp_path / "home",
            cache_root=tmp_path / "cache",
            checkout_dependency=lambda _dependency, _cache: source,
        )

    assert not (tmp_path / "home").exists()
