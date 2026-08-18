from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_ops.deployment import transaction as transaction_module
from agent_ops.deployment.engine import DeploymentEngine
from agent_ops.deployment.models import PlannedFile, ProviderPlan, SourceSpec, TargetSpec
from agent_ops.deployment.registry import (
    ChannelSpec,
    DeploymentRegistry,
    RegistryConfig,
)
from agent_ops.deployment.source_store import SourceStore
from agent_ops.deployment.transaction import audit_provider_plans, install_provider_plans
from agent_ops.registries.models import Framework


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
