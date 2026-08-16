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
    ProviderSourceClosure,
    SkillSourceClosure,
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
    ) -> tuple[Path, ...] | ProviderSourceClosure:
        selected = selection or ("demo",)
        if selection is None:
            return tuple(Path("skills") / name for name in selected)
        skills = tuple(
            SkillSourceClosure(name, (), (Path("skills") / name,))
            for name in selected
        )
        return ProviderSourceClosure(self.provider_id, skills)

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
    (checkout / "skills").chmod(0o755)
    skill.chmod(0o755)
    (skill / "SKILL.md").chmod(0o644)
    (checkout / "unrelated.txt").chmod(0o644)
    _git("add", ".", cwd=checkout)
    _git("commit", "-m", "fixture", cwd=checkout)
    return checkout


def _registry(tmp_path: Path, *, channel: str = "preview") -> tuple[DeploymentRegistry, Path]:
    checkout = tmp_path / "checkout"
    home = tmp_path / "preview-home"
    home.mkdir()
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


def _registry_receipts(tmp_path: Path):
    registry = DeploymentRegistry((tmp_path / "state/deployments.yaml").absolute())
    return registry.receipts()


def _assert_no_preview_install(home: Path) -> None:
    assert not (home / "skills").exists()
    assert not (home / "policy").exists()
    assert not list((home / ".agentops/deployment/manifests").glob("*.json"))


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
    selected.chmod(0o600)
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
        def source_closure(self, snapshot, target, selection):
            if selection is None:
                return (Path("skills/demo"),)
            return ProviderSourceClosure(
                self.provider_id,
                (SkillSourceClosure("demo", (), (Path("skills/demo"),)),),
            )

        def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
            assert not (snapshot.root / "unrelated.txt").exists()
            return super().plan(snapshot, target)

    class PolicyProvider:
        provider_id = "selected-policy"

        def supports(self, snapshot: SourceSnapshot, target: TargetSpec) -> bool:
            return True

        def source_closure(self, snapshot, target, selection):
            if selection is None:
                return (Path("unrelated.txt"),)
            return ProviderSourceClosure(
                self.provider_id,
                (SkillSourceClosure("demo-policy", (), (Path("unrelated.txt"),)),),
            )

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

    result = engine.preview(checkout, ("demo", "demo-policy"), "codex-preview")

    assert result.providers == ("selected-policy", "selected-skills")
    assert (home / "skills/demo/SKILL.md").exists()
    assert (home / "policy/unrelated.txt").exists()


def test_preview_rejects_legacy_provider_without_skill_identity(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
    registry, home = _registry(tmp_path)

    class LegacyProvider(_SelectedSkillProvider):
        def source_closure(self, snapshot, target, selection):
            return (Path("skills/demo"),)

    engine = PreviewEngine(registry, providers=(LegacyProvider(),))

    with pytest.raises(ValueError, match="identity-bound"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)


@pytest.mark.parametrize("case", ("missing", "ignored", "duplicate-alias"))
def test_preview_requires_each_requested_skill_resolve_exactly_once(
    tmp_path: Path, case: str
) -> None:
    checkout = _checkout(tmp_path)
    registry, home = _registry(tmp_path)

    class InvalidSelectionProvider(_SelectedSkillProvider):
        def source_closure(self, snapshot, target, selection):
            if case == "missing":
                skills = ()
            elif case == "ignored":
                skills = (
                    SkillSourceClosure("other", (), (Path("skills/demo"),)),
                )
            else:
                skills = (
                    SkillSourceClosure(
                        "demo", ("example",), (Path("skills/demo"),)
                    ),
                )
            return ProviderSourceClosure(self.provider_id, skills)

    engine = PreviewEngine(registry, providers=(InvalidSelectionProvider(),))
    requested = ("demo", "example") if case == "duplicate-alias" else ("demo",)

    with pytest.raises(ValueError, match="requested skill|selection"):
        engine.preview(checkout, requested, "codex-preview")

    _assert_no_preview_install(home)


@pytest.mark.parametrize("collision", ("canonical", "alias", "path"))
def test_preview_rejects_cross_provider_identity_and_path_collisions(
    tmp_path: Path, collision: str
) -> None:
    checkout = _checkout(tmp_path)
    registry, home = _registry(tmp_path)

    class First(_SelectedSkillProvider):
        provider_id = "first"

        def source_closure(self, snapshot, target, selection):
            return ProviderSourceClosure(
                self.provider_id,
                (
                    SkillSourceClosure(
                        "demo",
                        ("shared",) if collision == "alias" else (),
                        (Path("skills/demo"),),
                    ),
                ),
            )

    class Second(_SelectedSkillProvider):
        provider_id = "second"

        def source_closure(self, snapshot, target, selection):
            canonical = "demo" if collision == "canonical" else "other"
            aliases = ("shared",) if collision == "alias" else ()
            path = Path("skills/demo/SKILL.md") if collision == "path" else Path("unrelated.txt")
            return ProviderSourceClosure(
                self.provider_id,
                (SkillSourceClosure(canonical, aliases, (path,)),),
            )

    engine = PreviewEngine(registry, providers=(First(), Second()))
    requested = ("demo",) if collision == "canonical" else ("demo", "other")
    if collision == "alias":
        requested = ("demo", "other")

    with pytest.raises(ValueError, match="collision|exactly once|owned"):
        engine.preview(checkout, requested, "codex-preview")

    _assert_no_preview_install(home)


@pytest.mark.parametrize("channel", ("stable", "feature"))
def test_preview_rejects_stable_and_branch_targets_before_writes(
    tmp_path: Path, channel: str
) -> None:
    engine, checkout, home = _preview(tmp_path, channel=channel)

    with pytest.raises(ValueError, match="preview-reserved"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)


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

    _assert_no_preview_install(home)


def test_preview_requires_the_isolated_target_home_to_already_exist(
    tmp_path: Path,
) -> None:
    engine, checkout, home = _preview(tmp_path)
    home.rmdir()

    with pytest.raises(ValueError, match="existing isolated preview target"):
        engine.preview(checkout, ("demo",), "codex-preview")

    assert not home.exists()


def test_preview_rejects_untracked_referenced_resource(tmp_path: Path) -> None:
    engine, checkout, home = _preview(tmp_path)
    (checkout / "skills/demo/resource.txt").write_text("untracked\n")

    with pytest.raises(ValueError, match="Git-tracked"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)


def test_preview_rejects_source_closure_symlink(tmp_path: Path) -> None:
    engine, checkout, home = _preview(tmp_path)
    link = checkout / "skills/demo/link.txt"
    link.symlink_to(checkout / "unrelated.txt")
    _git("add", "skills/demo/link.txt", cwd=checkout)

    with pytest.raises(ValueError, match="symbolic link"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)


@pytest.mark.parametrize("mode", (0o2644, 0o666, 0o620))
def test_preview_rejects_unsafe_worktree_file_modes(tmp_path: Path, mode: int) -> None:
    engine, checkout, home = _preview(tmp_path)
    (checkout / "skills/demo/SKILL.md").chmod(mode)

    with pytest.raises(ValueError, match="mode"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)


def test_preview_rejects_group_writable_worktree_directory(tmp_path: Path) -> None:
    engine, checkout, home = _preview(tmp_path)
    (checkout / "skills/demo").chmod(0o775)

    with pytest.raises(ValueError, match="mode"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)


def test_preview_rejects_git_index_and_worktree_executable_mode_mismatch(
    tmp_path: Path,
) -> None:
    engine, checkout, home = _preview(tmp_path)
    (checkout / "skills/demo/SKILL.md").chmod(0o755)

    with pytest.raises(ValueError, match="Git.*mode"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)


def test_preview_rejects_unsupported_git_entry_mode(tmp_path: Path) -> None:
    engine, checkout, home = _preview(tmp_path)
    submodule = checkout / "skills/submodule"
    submodule.mkdir()
    submodule.chmod(0o755)
    commit = _git("rev-parse", "HEAD", cwd=checkout)
    _git(
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit},skills/submodule",
        cwd=checkout,
    )

    with pytest.raises(ValueError, match="Git entry mode is unsupported"):
        engine.preview(checkout, ("submodule",), "codex-preview")

    _assert_no_preview_install(home)


def test_preview_rejects_atomic_file_replacement_during_capture(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import preview as preview_module

    engine, checkout, home = _preview(tmp_path)
    selected = checkout / "skills/demo/SKILL.md"
    original_hook = preview_module._before_capture_open

    def replace_before_open(path: Path) -> None:
        if path == Path("skills/demo/SKILL.md"):
            replacement = selected.with_name("replacement")
            replacement.write_bytes(selected.read_bytes())
            replacement.chmod(selected.stat().st_mode & 0o777)
            os.replace(replacement, selected)
        original_hook(path)

    monkeypatch.setattr(preview_module, "_before_capture_open", replace_before_open)

    with pytest.raises(RuntimeError, match="changed during capture"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)


@pytest.mark.parametrize(
    "race", ("file-replacement", "parent-replacement", "content", "mode", "index", "head")
)
def test_preview_revalidates_checkout_authority_immediately_before_mutation(
    tmp_path: Path, monkeypatch, race: str
) -> None:
    from agent_ops.deployment import preview as preview_module

    engine, checkout, home = _preview(tmp_path)
    selected = checkout / "skills/demo/SKILL.md"

    def race_checkout(_authority) -> None:
        if race == "file-replacement":
            replacement = selected.with_name("replacement")
            replacement.write_bytes(selected.read_bytes())
            replacement.chmod(selected.stat().st_mode & 0o777)
            os.replace(replacement, selected)
        elif race == "parent-replacement":
            parent = selected.parent
            moved = parent.with_name("demo-old")
            parent.rename(moved)
            parent.mkdir()
            selected.write_bytes((moved / "SKILL.md").read_bytes())
            selected.chmod((moved / "SKILL.md").stat().st_mode & 0o777)
        elif race == "content":
            selected.write_text("changed after planning\n")
        elif race == "mode":
            selected.chmod(0o600)
        elif race == "index":
            _git("update-index", "--chmod=+x", "skills/demo/SKILL.md", cwd=checkout)
        else:
            _git("commit", "--allow-empty", "-m", "move head", cwd=checkout)

    monkeypatch.setattr(preview_module, "_before_preview_apply", race_checkout)

    with pytest.raises(RuntimeError, match="changed"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)
    assert _registry_receipts(tmp_path) == ()


def test_preview_preserves_process_control_during_provider_planning(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
    registry, home = _registry(tmp_path)

    class InterruptingProvider(_SelectedSkillProvider):
        def plan(self, snapshot: SourceSnapshot, target: TargetSpec) -> ProviderPlan:
            raise KeyboardInterrupt

    engine = PreviewEngine(registry, providers=(InterruptingProvider(),))

    with pytest.raises(KeyboardInterrupt):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)


def test_preview_preserves_process_control_after_install_and_rolls_back(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import preview as preview_module

    engine, checkout, home = _preview(tmp_path)
    control = KeyboardInterrupt("operator stop")

    def interrupt_audit(_plans):
        raise control

    monkeypatch.setattr(preview_module, "audit_provider_plans", interrupt_audit)

    with pytest.raises(KeyboardInterrupt) as caught:
        engine.preview(checkout, ("demo",), "codex-preview")

    assert caught.value is control
    _assert_no_preview_install(home)
    assert _registry_receipts(tmp_path) == ()


def test_preview_preserves_process_control_when_recovery_is_incomplete(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import preview as preview_module

    engine, checkout, _home = _preview(tmp_path)
    control = SystemExit(19)

    def interrupt_audit(_plans):
        raise control

    def fail_recovery(_manifests):
        raise RuntimeError("recovery failed")

    monkeypatch.setattr(preview_module, "audit_provider_plans", interrupt_audit)
    monkeypatch.setattr(preview_module, "rollback_manifests", fail_recovery)

    with pytest.raises(SystemExit) as caught:
        engine.preview(checkout, ("demo",), "codex-preview")

    assert caught.value is control
    assert any("recovery was incomplete" in note for note in control.__notes__)
    assert _registry_receipts(tmp_path) == ()


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
    if relation == "home-in-source":
        home.mkdir()
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

    _assert_no_preview_install(home)
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
