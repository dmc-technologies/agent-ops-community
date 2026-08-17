from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from agent_ops.deployment.engine import DeploymentEngine, DeploymentRecoveryError
from agent_ops.deployment.models import (
    PlannedFile,
    ProviderPlan,
    ProviderSourceClosure,
    SkillSourceClosure,
    SourceSnapshot,
    SourceSpec,
    TargetSpec,
    TargetState,
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


def _changed_registry_config(
    registry: DeploymentRegistry, tmp_path: Path, change: str
) -> RegistryConfig:
    config = registry.load()
    target = config.targets[0]
    channels = config.channels
    if change == "stable":
        target = replace(target, channel="stable")
    elif change == "branch":
        channels += (ChannelSpec("feature", "community", "refs/heads/feature"),)
        target = replace(target, channel="feature")
    elif change == "home":
        replacement_home = (tmp_path / "replacement-preview-home").absolute()
        replacement_home.mkdir(exist_ok=True)
        target = replace(target, home=replacement_home)
    elif change == "framework":
        target = replace(target, framework=Framework.CLAUDE_CODE)
    elif change != "aba":
        raise AssertionError(f"unknown registry test change: {change}")
    return RegistryConfig(config.schema_version, config.sources, channels, (target,))


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


def test_preview_git_queries_disable_hostile_executable_configuration(
    tmp_path: Path, monkeypatch
) -> None:
    engine, checkout, home = _preview(tmp_path)
    marker = tmp_path / "git-config-executed"
    executable = tmp_path / "hostile-git-helper"
    executable.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 0\n")
    executable.chmod(0o755)
    for key in (
        "core.fsmonitor",
        "core.pager",
        "pager.ls-files",
        "diff.external",
        "credential.helper",
        "core.sshCommand",
    ):
        _git("config", "--local", key, str(executable), cwd=checkout)
    for name in ("GIT_PAGER", "PAGER", "GIT_EDITOR", "GIT_ASKPASS", "SSH_ASKPASS"):
        monkeypatch.setenv(name, str(executable))

    result = engine.preview(checkout, ("demo",), "codex-preview")

    assert result.review_state == "unreviewed-local"
    assert not marker.exists()
    assert (home / "skills/demo/SKILL.md").exists()


def test_preview_git_runner_rejects_commands_outside_read_only_allowlist(
    tmp_path: Path,
) -> None:
    from agent_ops.deployment import preview as preview_module

    checkout = _checkout(tmp_path)

    with pytest.raises(ValueError, match="not approved"):
        preview_module._git(("status",), checkout)


def test_preview_preserves_linked_git_worktree_support(tmp_path: Path) -> None:
    primary = _checkout(tmp_path)
    linked = tmp_path / "linked-checkout"
    _git("worktree", "add", "-b", "linked-preview", str(linked), cwd=primary)
    (linked / "skills").chmod(0o755)
    (linked / "skills/demo").chmod(0o755)
    (linked / "skills/demo/SKILL.md").chmod(0o644)
    registry, home = _registry(tmp_path)
    engine = PreviewEngine(registry, providers=(_SelectedSkillProvider(),))

    result = engine.preview(linked, ("demo",), "codex-preview")

    assert result.review_state == "unreviewed-local"
    assert len(result.fingerprint) == 64
    assert (home / "skills/demo/SKILL.md").exists()


def test_preview_fingerprint_binds_path_mode_and_exact_bytes(tmp_path: Path) -> None:
    engine, checkout, home = _preview(tmp_path)
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


def test_preview_skips_provider_that_owns_no_requested_skill(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
    registry, home = _registry(tmp_path)
    called = False

    class UnrelatedProvider(_SelectedSkillProvider):
        provider_id = "unrelated"

        def source_closure(self, snapshot, target, selection):
            if selection is None:
                return (Path("unrelated.txt"),)
            return ProviderSourceClosure(self.provider_id, ())

        def plan(self, snapshot, target):
            nonlocal called
            called = True
            return ProviderPlan(
                self.provider_id,
                snapshot.commit,
                target,
                (),
                (Path("skills/demo/SKILL.md"),),
            )

    engine = PreviewEngine(
        registry, providers=(_SelectedSkillProvider(), UnrelatedProvider())
    )

    result = engine.preview(checkout, ("demo",), "codex-preview")

    assert called is False
    assert result.providers == ("selected-skills",)
    assert (home / "skills/demo/SKILL.md").exists()


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


@pytest.mark.parametrize("phase", ("install", "audit"))
@pytest.mark.parametrize(
    "race", ("file-replacement", "parent-replacement", "content", "mode", "index", "head")
)
def test_preview_revalidates_complete_source_authority_before_success(
    tmp_path: Path, monkeypatch, phase: str, race: str
) -> None:
    from agent_ops.deployment import preview as preview_module

    engine, checkout, home = _preview(tmp_path)
    selected = checkout / "skills/demo/SKILL.md"

    def race_checkout() -> None:
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
            selected.write_text("changed during target mutation\n")
        elif race == "mode":
            selected.chmod(0o600)
        elif race == "index":
            _git("update-index", "--chmod=+x", "skills/demo/SKILL.md", cwd=checkout)
        else:
            _git("commit", "--allow-empty", "-m", "move head during target mutation", cwd=checkout)

    if phase == "install":
        original_install = preview_module.install_provider_plans

        def install_then_race(plans):
            manifests = original_install(plans)
            race_checkout()
            return manifests

        monkeypatch.setattr(preview_module, "install_provider_plans", install_then_race)
    else:
        original_audit = preview_module.audit_provider_plans

        def audit_then_race(plans):
            audit = original_audit(plans)
            race_checkout()
            return audit

        monkeypatch.setattr(preview_module, "audit_provider_plans", audit_then_race)

    with pytest.raises(RuntimeError, match="changed"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)
    assert _registry_receipts(tmp_path) == ()


@pytest.mark.parametrize("change", ("stable", "branch", "home", "framework", "aba"))
def test_preview_rejects_registry_change_during_provider_planning(
    tmp_path: Path, change: str
) -> None:
    checkout = _checkout(tmp_path)
    registry, home = _registry(tmp_path)
    changed = False

    class RegistryChangingProvider(_SelectedSkillProvider):
        def plan(self, snapshot, target):
            nonlocal changed
            if not changed:
                changed = True
                registry.save(_changed_registry_config(registry, tmp_path, change))
            return super().plan(snapshot, target)

    engine = PreviewEngine(registry, providers=(RegistryChangingProvider(),))

    with pytest.raises((RuntimeError, ValueError), match="registry|required snapshot"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)
    assert _registry_receipts(tmp_path) == ()


@pytest.mark.parametrize("phase", ("install", "audit"))
@pytest.mark.parametrize("change", ("stable", "branch", "home", "framework", "aba"))
def test_preview_rolls_back_registry_replacement_during_target_mutation(
    tmp_path: Path, monkeypatch, phase: str, change: str
) -> None:
    from agent_ops.deployment import preview as preview_module
    from agent_ops.deployment import registry as registry_module

    engine, checkout, home = _preview(tmp_path)
    registry = engine._registry

    def replace_registry() -> None:
        content = registry_module._dump_registry(
            _changed_registry_config(registry, tmp_path, change)
        )
        replacement = registry.path.with_name("replacement-registry.yaml")
        replacement.write_bytes(content)
        replacement.chmod(0o600)
        os.replace(replacement, registry.path)

    if phase == "install":
        original_install = preview_module.install_provider_plans

        def install_then_replace(plans):
            manifests = original_install(plans)
            replace_registry()
            return manifests

        monkeypatch.setattr(preview_module, "install_provider_plans", install_then_replace)
    else:
        original_audit = preview_module.audit_provider_plans

        def audit_then_replace(plans):
            audit = original_audit(plans)
            replace_registry()
            return audit

        monkeypatch.setattr(preview_module, "audit_provider_plans", audit_then_replace)

    with pytest.raises((RuntimeError, ValueError), match="registry|required snapshot"):
        engine.preview(checkout, ("demo",), "codex-preview")

    _assert_no_preview_install(home)
    assert _registry_receipts(tmp_path) == ()


def test_preview_retains_shared_registry_authority_through_terminal_success(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import preview as preview_module

    engine, checkout, home = _preview(tmp_path)
    registry = engine._registry
    candidate = _changed_registry_config(registry, tmp_path, "stable")
    original_audit = preview_module.audit_provider_plans
    entered = threading.Event()
    completed = threading.Event()
    errors: list[BaseException] = []
    worker: threading.Thread | None = None

    def save_registry() -> None:
        entered.set()
        try:
            registry.save(candidate)
        except BaseException as error:
            errors.append(error)
        finally:
            completed.set()

    def audit_while_save_waits(plans):
        nonlocal worker
        worker = threading.Thread(target=save_registry)
        worker.start()
        assert entered.wait(timeout=5)
        assert completed.wait(timeout=0.1) is False
        return original_audit(plans)

    monkeypatch.setattr(preview_module, "audit_provider_plans", audit_while_save_waits)

    result = engine.preview(checkout, ("demo",), "codex-preview")
    assert worker is not None
    worker.join(timeout=5)

    assert worker.is_alive() is False
    assert errors == []
    assert completed.is_set()
    assert result.review_state == "unreviewed-local"
    assert (home / "skills/demo/SKILL.md").exists()


def test_preview_refuses_success_when_target_bytes_change_after_audit(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import preview as preview_module

    engine, checkout, home = _preview(tmp_path)
    original_audit = preview_module.audit_provider_plans

    def audit_then_tamper(plans):
        audit = original_audit(plans)
        (home / "skills/demo/SKILL.md").write_text("tampered after audit\n")
        return audit

    monkeypatch.setattr(preview_module, "audit_provider_plans", audit_then_tamper)

    with pytest.raises((DeploymentRecoveryError, ValueError), match="retained audit evidence"):
        engine.preview(checkout, ("demo",), "codex-preview")

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


def test_preview_incomplete_recovery_retains_primary_and_recovery_failures(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import preview as preview_module

    engine, checkout, home = _preview(tmp_path)
    recovery = RuntimeError("rollback failed")

    def fail_audit(_plans):
        raise ValueError("primary failed")

    def fail_recovery(_manifests):
        raise recovery

    monkeypatch.setattr(preview_module, "audit_provider_plans", fail_audit)
    monkeypatch.setattr(preview_module, "rollback_manifests", fail_recovery)

    with pytest.raises(DeploymentRecoveryError, match="primary failed.*rollback failed") as caught:
        engine.preview(checkout, ("demo",), "codex-preview")

    assert caught.value.__cause__ is recovery
    assert "transaction evidence" in str(caught.value)
    assert list(
        (home / ".agentops/deployment/transactions").glob("*/record.json")
    )
    assert _registry_receipts(tmp_path) == ()


def test_preview_preserves_recovery_process_control_identity(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import preview as preview_module

    engine, checkout, _home = _preview(tmp_path)
    control = KeyboardInterrupt("operator stopped recovery")

    def fail_audit(_plans):
        raise RuntimeError("primary failed")

    def interrupt_recovery(_manifests):
        raise control

    monkeypatch.setattr(preview_module, "audit_provider_plans", fail_audit)
    monkeypatch.setattr(preview_module, "rollback_manifests", interrupt_recovery)

    with pytest.raises(KeyboardInterrupt) as caught:
        engine.preview(checkout, ("demo",), "codex-preview")

    assert caught.value is control
    assert any(
        "primary failed" in note and "recovery was incomplete" in note
        for note in control.__notes__
    )
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


def test_engine_status_reads_valid_preview_manifest_without_managed_fetch(
    tmp_path: Path, monkeypatch
) -> None:
    preview, checkout, home = _preview(tmp_path)
    result = preview.preview(checkout, ("demo",), "codex-preview")
    registry = preview._registry
    store_root = tmp_path / "managed-sources"
    engine = DeploymentEngine(
        registry,
        SourceStore(store_root.absolute()),
        providers=(_SelectedSkillProvider(),),
    )
    before = {
        path.relative_to(home): (
            path.lstat().st_mode,
            path.read_bytes() if path.is_file() else None,
            path.lstat().st_mtime_ns,
        )
        for path in home.rglob("*")
    }
    assert registry.receipts() == ()

    def receipts_must_not_be_read():
        raise AssertionError("preview status must not read managed receipts")

    monkeypatch.setattr(registry, "receipt_records", receipts_must_not_be_read)

    status = engine.status(("codex-preview",))[0]

    assert status.state is TargetState.PREVIEW
    assert status.commit == result.fingerprint
    assert len(status.commit) == 64
    assert not store_root.exists()
    assert before == {
        path.relative_to(home): (
            path.lstat().st_mode,
            path.read_bytes() if path.is_file() else None,
            path.lstat().st_mtime_ns,
        )
        for path in home.rglob("*")
    }


@pytest.mark.parametrize("replacement", ("forged-mode", "same-mode-inode"))
def test_engine_status_rejects_manifest_replacement_races(
    tmp_path: Path, monkeypatch, replacement: str
) -> None:
    from agent_ops.deployment import transaction as transaction_module

    preview, checkout, home = _preview(tmp_path)
    preview.preview(checkout, ("demo",), "codex-preview")
    manifest = next((home / ".agentops/deployment/manifests").glob("*.json"))

    def replace_manifest(_home_fs, _path, _descriptor) -> None:
        data = json.loads(manifest.read_bytes())
        data["source_revision"] = "b" * 64
        content = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
        if replacement == "forged-mode":
            manifest.write_bytes(content)
            manifest.chmod(0o644)
        else:
            forged = manifest.with_name("forged.json")
            forged.write_bytes(content)
            forged.chmod(0o600)
            os.replace(forged, manifest)

    monkeypatch.setattr(
        transaction_module,
        "_after_preview_status_manifest_open",
        replace_manifest,
    )
    engine = DeploymentEngine(
        preview._registry,
        SourceStore((tmp_path / "managed-sources").absolute()),
        providers=(_SelectedSkillProvider(),),
    )

    status = engine.status(("codex-preview",))[0]

    assert status.state is TargetState.FAILED
    assert status.commit is None


@pytest.mark.parametrize("kind", ("file", "directory"))
def test_engine_status_rejects_owned_path_replacement_races(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    from agent_ops.deployment import transaction as transaction_module

    preview, checkout, home = _preview(tmp_path)
    preview.preview(checkout, ("demo",), "codex-preview")
    selected_file = home / "skills/demo/SKILL.md"
    selected_directory = home / "skills/demo"
    replaced = False

    def replace_owned(path: Path, observed_kind: str, _descriptor: int) -> None:
        nonlocal replaced
        if replaced or observed_kind != kind:
            return
        if kind == "file" and path == Path("skills/demo/SKILL.md"):
            forged = selected_file.with_name("forged")
            forged.write_bytes(selected_file.read_bytes())
            forged.chmod(0o644)
            os.replace(forged, selected_file)
            replaced = True
        elif kind == "directory" and path == Path("skills/demo"):
            displaced = selected_directory.with_name("demo-displaced")
            selected_directory.rename(displaced)
            selected_directory.mkdir(mode=0o755)
            replaced = True

    monkeypatch.setattr(
        transaction_module,
        "_after_preview_status_owned_open",
        replace_owned,
    )
    engine = DeploymentEngine(
        preview._registry,
        SourceStore((tmp_path / "managed-sources").absolute()),
        providers=(_SelectedSkillProvider(),),
    )

    status = engine.status(("codex-preview",))[0]

    assert replaced is True
    assert status.state is TargetState.FAILED
    assert status.commit is None


def test_engine_status_preserves_process_control_during_evidence_read(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import transaction as transaction_module

    preview, checkout, _home = _preview(tmp_path)
    preview.preview(checkout, ("demo",), "codex-preview")
    control = KeyboardInterrupt("operator stop")

    def interrupt(*_args) -> None:
        raise control

    monkeypatch.setattr(
        transaction_module,
        "_after_preview_status_manifest_open",
        interrupt,
    )
    engine = DeploymentEngine(
        preview._registry,
        SourceStore((tmp_path / "managed-sources").absolute()),
        providers=(_SelectedSkillProvider(),),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
        engine.status(("codex-preview",))

    assert caught.value is control


def test_preview_manifest_records_exact_target_channel(tmp_path: Path) -> None:
    preview, checkout, home = _preview(tmp_path)
    preview.preview(checkout, ("demo",), "codex-preview")
    manifest = next((home / ".agentops/deployment/manifests").glob("*.json"))

    assert json.loads(manifest.read_bytes())["channel"] == "preview"


def test_engine_status_rejects_preview_channel_alias_change(tmp_path: Path) -> None:
    preview, checkout, _home = _preview(tmp_path)
    preview.preview(checkout, ("demo",), "codex-preview")
    registry = preview._registry
    config = registry.load()
    alias = ChannelSpec("preview-other", "community", "refs/heads/main")
    changed_target = replace(config.targets[0], channel=alias.id)
    registry.save(
        RegistryConfig(
            config.schema_version,
            config.sources,
            config.channels + (alias,),
            (changed_target,),
        )
    )
    engine = DeploymentEngine(
        registry,
        SourceStore((tmp_path / "managed-sources").absolute()),
        providers=(_SelectedSkillProvider(),),
    )

    status = engine.status(("codex-preview",))[0]

    assert status.state is TargetState.FAILED
    assert status.commit is None


def test_engine_status_rejects_contradictory_preview_manifest_channel(
    tmp_path: Path,
) -> None:
    preview, checkout, home = _preview(tmp_path)
    preview.preview(checkout, ("demo",), "codex-preview")
    manifest = next((home / ".agentops/deployment/manifests").glob("*.json"))
    data = json.loads(manifest.read_bytes())
    data["channel"] = "preview-other"
    manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    manifest.chmod(0o600)
    engine = DeploymentEngine(
        preview._registry,
        SourceStore((tmp_path / "managed-sources").absolute()),
        providers=(_SelectedSkillProvider(),),
    )

    status = engine.status(("codex-preview",))[0]

    assert status.state is TargetState.FAILED
    assert status.commit is None


@pytest.mark.parametrize("change", ("content", "mode", "directory"))
def test_engine_status_reports_modified_preview_owned_state(
    tmp_path: Path, change: str
) -> None:
    preview, checkout, home = _preview(tmp_path)
    result = preview.preview(checkout, ("demo",), "codex-preview")
    installed = home / "skills/demo/SKILL.md"
    if change == "content":
        installed.write_text("modified\n")
    elif change == "mode":
        installed.chmod(0o600)
    else:
        (home / "skills/demo").chmod(0o700)
    engine = DeploymentEngine(
        preview._registry,
        SourceStore((tmp_path / "managed-sources").absolute()),
        providers=(_SelectedSkillProvider(),),
    )

    status = engine.status(("codex-preview",))[0]

    assert status.state is TargetState.MODIFIED
    assert status.commit == result.fingerprint
    assert not (tmp_path / "managed-sources").exists()
    assert preview._registry.receipts() == ()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("review_state", None),
        ("target_id", "other-target"),
        ("framework", "claude-code"),
        ("source_revision", "a" * 40),
    ),
)
def test_engine_status_reports_failed_for_invalid_preview_manifest(
    tmp_path: Path, field: str, value: object
) -> None:
    preview, checkout, home = _preview(tmp_path)
    preview.preview(checkout, ("demo",), "codex-preview")
    manifest = next((home / ".agentops/deployment/manifests").glob("*.json"))
    data = json.loads(manifest.read_bytes())
    data[field] = value
    manifest.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    manifest.chmod(0o600)
    engine = DeploymentEngine(
        preview._registry,
        SourceStore((tmp_path / "managed-sources").absolute()),
        providers=(_SelectedSkillProvider(),),
    )

    status = engine.status(("codex-preview",))[0]

    assert status.state is TargetState.FAILED
    assert status.commit is None
    assert not (tmp_path / "managed-sources").exists()
    assert preview._registry.receipts() == ()


def test_engine_status_never_classifies_preview_manifest_on_managed_channel(
    tmp_path: Path,
) -> None:
    preview, checkout, _home = _preview(tmp_path)
    preview.preview(checkout, ("demo",), "codex-preview")
    registry = preview._registry
    registry.save(_changed_registry_config(registry, tmp_path, "stable"))
    engine = DeploymentEngine(
        registry,
        SourceStore((tmp_path / "managed-sources").absolute()),
        providers=(_SelectedSkillProvider(),),
    )

    status = engine.status(("codex-preview",))[0]

    assert status.state is not TargetState.PREVIEW
    assert status.commit is None


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
