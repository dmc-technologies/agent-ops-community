from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_ops.deployment.engine import DeploymentEngine
from agent_ops.deployment.models import (
    PlannedFile,
    ProviderPlan,
    SourceSnapshot,
    SourceSpec,
    TargetSpec,
)
from agent_ops.deployment.preview import PreviewEngine
from agent_ops.deployment.registry import ChannelSpec, DeploymentRegistry, RegistryConfig
from agent_ops.deployment.source_store import SourceStore
from agent_ops.registries.models import Framework


class _SelectedSkillProvider:
    provider_id = "selected-skills"

    def supports(self, snapshot: SourceSnapshot, target: TargetSpec) -> bool:
        return target.framework is Framework.CODEX

    def source_closure(
        self,
        snapshot: SourceSnapshot,
        target: TargetSpec,
        selection: tuple[str, ...] | None,
    ) -> tuple[Path, ...]:
        selected = selection or ("demo",)
        return tuple(Path("skills") / name for name in selected)

    def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
        skill = snapshot.root / "skills/demo/SKILL.md"
        return ProviderPlan(
            self.provider_id,
            snapshot.commit,
            target,
            (PlannedFile(Path("skills/demo/SKILL.md"), skill.read_bytes(), 0o644),),
        )


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git("init", "-b", "main", cwd=checkout)
    _git("config", "user.email", "agentops@example.com", cwd=checkout)
    _git("config", "user.name", "Agent Ops", cwd=checkout)
    skill = checkout / "skills/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Demo\n\nVersion one.\n")
    (checkout / "unrelated.txt").write_text("not selected\n")
    _git("add", ".", cwd=checkout)
    _git("commit", "-m", "fixture", cwd=checkout)
    return checkout


def _registry(tmp_path: Path, *, channel: str = "preview") -> tuple[DeploymentRegistry, Path]:
    checkout = tmp_path / "checkout"
    home = tmp_path / "preview-home"
    registry_path = tmp_path / "state/deployments.yaml"
    registry_path.parent.mkdir(parents=True)
    registry = DeploymentRegistry(registry_path.absolute())
    channels = (ChannelSpec(channel, "community", "refs/heads/main"),)
    if channel == "preview":
        channels += (ChannelSpec("stable", "community", "refs/heads/main"),)
    registry.save(
        RegistryConfig(
            1,
            (SourceSpec("community", str(checkout)),),
            channels,
            (TargetSpec("codex-preview", Framework.CODEX, home.absolute(), channel),),
        )
    )
    return registry, home


def _preview(tmp_path: Path, *, channel: str = "preview") -> tuple[PreviewEngine, Path, Path]:
    checkout = _checkout(tmp_path)
    registry, home = _registry(tmp_path, channel=channel)
    return PreviewEngine(registry, providers=(_SelectedSkillProvider(),)), checkout, home


def test_preview_installs_only_selected_tracked_worktree_closure(tmp_path: Path) -> None:
    engine, checkout, home = _preview(tmp_path)
    selected = checkout / "skills/demo/SKILL.md"
    selected.write_text("# Demo\n\nLocal edit.\n")

    result = engine.preview(checkout, ("demo",), "codex-preview")

    assert result.operation == "preview"
    assert result.review_state == "unreviewed-local"
    assert result.target_id == "codex-preview"
    assert result.channel == "preview"
    assert result.paths == ("skills/demo/SKILL.md",)
    assert len(result.fingerprint) == 64
    assert result.fingerprint == result.source_revision
    assert (home / "skills/demo/SKILL.md").read_text() == "# Demo\n\nLocal edit.\n"
    assert not (home / "unrelated.txt").exists()
    manifest_name = hashlib.sha256(b"codex-preview").hexdigest() + ".json"
    manifest = home / ".agentops/deployment/manifests" / manifest_name
    manifest_data = json.loads(manifest.read_bytes())
    assert manifest_data["source_revision"] == result.fingerprint
    assert manifest_data["review_state"] == "unreviewed-local"


def test_preview_fingerprint_binds_path_mode_and_exact_bytes(tmp_path: Path) -> None:
    engine, checkout, _home = _preview(tmp_path)
    first = engine.preview(checkout, ("demo",), "codex-preview")
    selected = checkout / "skills/demo/SKILL.md"
    selected.write_text("# Demo\n\nDifferent bytes.\n")
    second = engine.preview(checkout, ("demo",), "codex-preview")
    selected.chmod(0o755)
    third = engine.preview(checkout, ("demo",), "codex-preview")

    assert first.fingerprint != second.fingerprint != third.fingerprint
    assert all(len(value) == hashlib.sha256().digest_size * 2 for value in (
        first.fingerprint, second.fingerprint, third.fingerprint
    ))


def test_preview_confines_each_installed_provider_to_its_own_declared_closure(
    tmp_path: Path,
) -> None:
    checkout = _checkout(tmp_path)
    registry, home = _registry(tmp_path)

    class SkillProvider(_SelectedSkillProvider):
        def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
            assert not (snapshot.root / "unrelated.txt").exists()
            return super().plan(snapshot, target)

    class PolicyProvider:
        provider_id = "selected-policy"

        def supports(self, snapshot: SourceSnapshot, target: TargetSpec) -> bool:
            return True

        def source_closure(self, snapshot, target, selection):
            return (Path("unrelated.txt"),)

        def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
            assert not (snapshot.root / "skills").exists()
            return ProviderPlan(
                self.provider_id,
                snapshot.commit,
                target,
                (
                    PlannedFile(
                        Path("policy/unrelated.txt"),
                        (snapshot.root / "unrelated.txt").read_bytes(),
                        0o644,
                    ),
                ),
            )

    engine = PreviewEngine(registry, providers=(SkillProvider(), PolicyProvider()))

    result = engine.preview(checkout, ("demo",), "codex-preview")

    assert result.providers == ("selected-policy", "selected-skills")
    assert (home / "skills/demo/SKILL.md").exists()
    assert (home / "policy/unrelated.txt").exists()


@pytest.mark.parametrize("channel", ("stable", "feature"))
def test_preview_rejects_stable_and_branch_targets_before_writes(
    tmp_path: Path, channel: str
) -> None:
    engine, checkout, home = _preview(tmp_path, channel=channel)

    with pytest.raises(ValueError, match="preview-reserved"):
        engine.preview(checkout, ("demo",), "codex-preview")

    assert not home.exists()


def test_preview_requires_explicit_checkout_selection_and_existing_target(
    tmp_path: Path,
) -> None:
    engine, checkout, home = _preview(tmp_path)

    with pytest.raises(ValueError, match="source checkout"):
        engine.preview(checkout / "missing", ("demo",), "codex-preview")
    with pytest.raises(ValueError, match="skill selection"):
        engine.preview(checkout, (), "codex-preview")
    with pytest.raises(ValueError, match="unknown target"):
        engine.preview(checkout, ("demo",), "missing")

    assert not home.exists()


def test_preview_rejects_untracked_referenced_resource(tmp_path: Path) -> None:
    engine, checkout, home = _preview(tmp_path)
    (checkout / "skills/demo/resource.txt").write_text("untracked\n")

    with pytest.raises(ValueError, match="Git-tracked"):
        engine.preview(checkout, ("demo",), "codex-preview")

    assert not home.exists()


def test_preview_rejects_source_closure_symlink(tmp_path: Path) -> None:
    engine, checkout, home = _preview(tmp_path)
    link = checkout / "skills/demo/link.txt"
    link.symlink_to(checkout / "unrelated.txt")
    _git("add", "skills/demo/link.txt", cwd=checkout)

    with pytest.raises(ValueError, match="symbolic link"):
        engine.preview(checkout, ("demo",), "codex-preview")

    assert not home.exists()


def test_preview_rejects_unsafe_worktree_modes(tmp_path: Path) -> None:
    engine, checkout, home = _preview(tmp_path)
    (checkout / "skills/demo/SKILL.md").chmod(0o2644)

    with pytest.raises(ValueError, match="mode"):
        engine.preview(checkout, ("demo",), "codex-preview")

    assert not home.exists()


@pytest.mark.parametrize("relation", ("source-in-home", "home-in-source"))
def test_preview_rejects_overlapping_source_and_target_paths(
    tmp_path: Path, relation: str
) -> None:
    checkout = _checkout(tmp_path)
    home = (
        tmp_path
        if relation == "source-in-home"
        else checkout / "nested-preview-home"
    )
    registry_path = tmp_path / "state/deployments.yaml"
    registry_path.parent.mkdir()
    registry = DeploymentRegistry(registry_path.absolute())
    registry.save(
        RegistryConfig(
            1,
            (SourceSpec("community", str(checkout)),),
            (ChannelSpec("preview", "community", "refs/heads/main"),),
            (TargetSpec("codex-preview", Framework.CODEX, home.absolute(), "preview"),),
        )
    )
    engine = PreviewEngine(registry, providers=(_SelectedSkillProvider(),))

    with pytest.raises(ValueError, match="must not overlap"):
        engine.preview(checkout, ("demo",), "codex-preview")


def test_managed_engine_rejects_preview_promotion_and_remote_operations(
    tmp_path: Path,
) -> None:
    _checkout(tmp_path)
    registry, home = _registry(tmp_path)
    engine = DeploymentEngine(
        registry,
        SourceStore((tmp_path / "managed-sources").absolute()),
        providers=(_SelectedSkillProvider(),),
    )

    operations = (
        lambda: engine.plan(("codex-preview",)),
        lambda: engine.refresh(("codex-preview",)),
        lambda: engine.audit(("codex-preview",)),
        lambda: engine.switch("stable", ("codex-preview",)),
        lambda: engine.deploy(
            "feature", "refs/heads/feature", ("codex-preview",)
        ),
    )
    for operation in operations:
        with pytest.raises(ValueError, match="preview"):
            operation()

    assert not home.exists()
    assert not (tmp_path / "managed-sources").exists()
    assert registry.receipts() == ()


def test_two_machines_refresh_same_branch_with_independent_state_and_evidence(
    tmp_path: Path,
) -> None:
    source = _checkout(tmp_path)
    _git("switch", "-c", "feature", cwd=source)
    (source / "skills/demo/SKILL.md").write_text("# Demo\n\nBranch.\n")
    _git("commit", "-am", "branch", cwd=source)
    commit = _git("rev-parse", "HEAD", cwd=source)
    machines: list[tuple[DeploymentRegistry, Path, SourceStore]] = []
    for name in ("machine-a", "machine-b"):
        root = tmp_path / name
        (root / "state").mkdir(parents=True)
        registry = DeploymentRegistry((root / "state/deployments.yaml").absolute())
        home = (root / "codex-feature").absolute()
        registry.save(
            RegistryConfig(
                1,
                (SourceSpec("community", str(source)),),
                (ChannelSpec("feature", "community", "refs/heads/feature"),),
                (TargetSpec("codex-feature", Framework.CODEX, home, "feature"),),
            )
        )
        store = SourceStore((root / "sources").absolute())
        receipt = DeploymentEngine(
            registry, store, providers=(_SelectedSkillProvider(),)
        ).refresh(("codex-feature",))
        assert receipt.commits == (commit,)
        machines.append((registry, home, store))

    first_registry, first_home, first_store = machines[0]
    second_registry, second_home, second_store = machines[1]
    assert first_registry.path != second_registry.path
    assert first_home != second_home
    assert first_store._state_root != second_store._state_root
    assert first_registry.receipts() == second_registry.receipts()
    assert first_registry.receipts_path != second_registry.receipts_path
    assert (first_home / "skills/demo/SKILL.md").read_bytes() == (
        second_home / "skills/demo/SKILL.md"
    ).read_bytes()
    assert os.stat(first_home / ".agentops-deployment.lock").st_ino != os.stat(
        second_home / ".agentops-deployment.lock"
    ).st_ino
