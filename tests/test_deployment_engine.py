from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_ops.deployment.models import PlannedFile, ProviderPlan, TargetSpec
from agent_ops.deployment.public_skills import build_public_skill_plans
from agent_ops.deployment.transaction import UnsupportedPlatformError
from agent_ops.deployment.transaction import install_provider_plans as apply_provider_plans
from agent_ops.registries.models import Framework, SkillDependency, SkillDependencyInstall
from agent_ops.show_me_adapter import OWNERSHIP_MANIFEST_RELATIVE, install_show_me
from agent_ops.skill_installer import InstalledSkillDependency, install_skill_dependencies


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
    assert all(
        dependency.ref in plans[0].source_revision for dependency in dependencies
    )
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
    assert b"supported artifact preview or file-opening capability" in files[
        Path("skills/show-me/SKILL.md")
    ].content
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
    dependency = _dependency(
        "gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack"
    )

    plans = build_public_skill_plans(
        framework=Framework.CODEX,
        dependencies=[dependency],
        target_home=home,
        cache_root=tmp_path / "cache",
        checkout_dependency=lambda _dependency, _cache: source,
    )

    assert plans[0].files == (
        PlannedFile(Path("skills/gstack/SKILL.md"), b"new\n", 0o644),
    )
    assert installed.read_bytes() == b"old\n"


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
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", _checkout_from(sources)
    )

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
    planned: list[tuple[str, ...]] = []
    applied: list[str] = []
    monkeypatch.setattr("agent_ops.skill_installer._use_shared_transaction_engine", lambda: False)
    monkeypatch.setattr(
        "agent_ops.skill_installer.build_public_skill_plans",
        lambda **kwargs: planned.append(tuple(item.id for item in kwargs["dependencies"])) or (),
    )
    monkeypatch.setattr(
        "agent_ops.skill_installer._install_dependency",
        lambda **kwargs: applied.append(kwargs["dependency_id"]),
    )

    with pytest.raises(UnsupportedPlatformError, match="atomic multi-bundle"):
        install_skill_dependencies(
            framework=Framework.CODEX,
            dependencies=dependencies,
            home=tmp_path / "home",
        )

    assert planned == [("gstack", "superpowers")]
    assert applied == []
    assert not (tmp_path / "home").exists()

    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency",
        lambda _dependency, _cache: source,
    )
    install_skill_dependencies(
        framework=Framework.CODEX,
        dependencies=[dependencies[0]],
        home=tmp_path / "home",
    )

    assert planned[-1] == ("gstack",)
    assert applied == ["gstack"]


def test_shared_manifest_removes_stale_owned_files_and_preserves_unknowns(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    old = source / "old.txt"
    old.write_bytes(b"old\n")
    dependency = _dependency(
        "gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack"
    )
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
    dependency = _dependency(
        "gstack", ref="1" * 40, strategy="gstack", destination="skills/gstack"
    )

    with pytest.raises(ValueError, match="unsupported source entry"):
        build_public_skill_plans(
            framework=Framework.CODEX,
            dependencies=[dependency],
            target_home=tmp_path / "home",
            cache_root=tmp_path / "cache",
            checkout_dependency=lambda _dependency, _cache: source,
        )

    assert not (tmp_path / "home").exists()


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
