from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent_ops import show_me_adapter
from agent_ops.registries import load_skill_dependencies
from agent_ops.registries.models import Framework, SkillDependency, SkillDependencyInstall
from agent_ops.show_me_adapter import (
    OWNERSHIP_MANIFEST_RELATIVE as SHOW_ME_OWNERSHIP_MANIFEST_RELATIVE,
)
from agent_ops.show_me_adapter import ShowMeCollisionError
from agent_ops.skill_installer import default_framework_home, install_skill_dependencies
from agent_ops.superpowers_adapter import OWNERSHIP_MANIFEST_RELATIVE, SUPERPOWERS_SKILLS


def _git_repo(path: Path) -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "agentops@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Agent Ops"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return str(path)


def _commit(path: Path) -> str:
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_install_gstack_dependency_copies_full_bundle(tmp_path: Path) -> None:
    repo = tmp_path / "gstack-src"
    repo_url = _git_repo(repo)
    (repo / "office-hours").mkdir(parents=True)
    (repo / "office-hours" / "SKILL.md").write_text(
        "---\nname: office-hours\n---\n",
        encoding="utf-8",
    )
    (repo / "review").mkdir()
    (repo / "review" / "SKILL.md").write_text(
        "---\nname: review\n---\n",
        encoding="utf-8",
    )
    (repo / "bin").mkdir()
    (repo / "bin" / "gstack-config").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "node_modules" / "ignored").mkdir(parents=True)
    (repo / "node_modules" / "ignored" / "index.js").write_text("", encoding="utf-8")
    ref = _commit(repo)

    dependency = SkillDependency(
        id="gstack",
        name="GStack",
        repo=repo_url,
        ref=ref,
        install={"codex": SkillDependencyInstall(strategy="gstack", destination="skills/gstack")},
    )

    assert not (tmp_path / "home" / "skills").exists()
    rows = install_skill_dependencies(
        framework=Framework.CODEX,
        dependencies=[dependency],
        home=tmp_path / "home",
        cache_dir=tmp_path / "cache",
    )

    assert rows[0].destination == tmp_path / "home" / "skills" / "gstack"
    assert (tmp_path / "home" / "skills" / "gstack" / "office-hours" / "SKILL.md").exists()
    assert (tmp_path / "home" / "skills" / "gstack" / "review" / "SKILL.md").exists()
    assert (tmp_path / "home" / "skills" / "gstack" / "bin" / "gstack-config").exists()
    assert not (tmp_path / "home" / "skills" / "gstack" / "node_modules").exists()


def test_install_copy_skills_dependency_merges_skill_directories(tmp_path: Path) -> None:
    repo = tmp_path / "superpowers-src"
    repo_url = _git_repo(repo)
    (repo / "skills" / "writing-plans").mkdir(parents=True)
    (repo / "skills" / "writing-plans" / "SKILL.md").write_text(
        "---\nname: writing-plans\n---\n",
        encoding="utf-8",
    )
    (repo / "skills" / "verification-before-completion").mkdir()
    (repo / "skills" / "verification-before-completion" / "SKILL.md").write_text(
        "---\nname: verification-before-completion\n---\n",
        encoding="utf-8",
    )
    ref = _commit(repo)

    dependency = SkillDependency(
        id="superpowers",
        name="Superpowers",
        repo=repo_url,
        ref=ref,
        install={
            "codex": SkillDependencyInstall(
                strategy="copy-skills",
                source="skills",
                destination="skills",
            )
        },
    )

    assert not (tmp_path / "home" / "skills").exists()
    install_skill_dependencies(
        framework=Framework.CODEX,
        dependencies=[dependency],
        home=tmp_path / "home",
        cache_dir=tmp_path / "cache",
    )

    assert (tmp_path / "home" / "skills" / "writing-plans" / "SKILL.md").exists()
    assert (tmp_path / "home" / "skills" / "verification-before-completion" / "SKILL.md").exists()


def test_install_copy_skills_dependency_supports_opencode(tmp_path: Path) -> None:
    repo = tmp_path / "superpowers-src"
    repo_url = _git_repo(repo)
    (repo / "skills" / "writing-plans").mkdir(parents=True)
    (repo / "skills" / "writing-plans" / "SKILL.md").write_text(
        "---\nname: writing-plans\n---\n",
        encoding="utf-8",
    )
    (repo / "skills" / "verification-before-completion").mkdir()
    (repo / "skills" / "verification-before-completion" / "SKILL.md").write_text(
        "---\nname: verification-before-completion\n---\n",
        encoding="utf-8",
    )
    ref = _commit(repo)

    dependency = SkillDependency(
        id="superpowers",
        name="Superpowers",
        repo=repo_url,
        ref=ref,
        install={
            "opencode": SkillDependencyInstall(
                strategy="copy-skills",
                source="skills",
                destination="skills",
            )
        },
    )

    install_skill_dependencies(
        framework=Framework.OPENCODE,
        dependencies=[dependency],
        home=tmp_path / "home",
        cache_dir=tmp_path / "cache",
    )

    assert (tmp_path / "home" / "skills" / "writing-plans" / "SKILL.md").exists()
    assert (tmp_path / "home" / "skills" / "verification-before-completion" / "SKILL.md").exists()


def test_copy_skills_dependency_removes_stale_manifest_entries(tmp_path: Path) -> None:
    repo = tmp_path / "superpowers-src"
    repo_url = _git_repo(repo)
    (repo / "skills" / "old-skill").mkdir(parents=True)
    (repo / "skills" / "old-skill" / "SKILL.md").write_text(
        "---\nname: old-skill\n---\n",
        encoding="utf-8",
    )
    first_ref = _commit(repo)

    dependency = SkillDependency(
        id="superpowers",
        name="Superpowers",
        repo=repo_url,
        ref=first_ref,
        install={
            "codex": SkillDependencyInstall(
                strategy="copy-skills",
                source="skills",
                destination="skills",
            )
        },
    )
    home = tmp_path / "home"
    cache = tmp_path / "cache"

    install_skill_dependencies(
        framework=Framework.CODEX,
        dependencies=[dependency],
        home=home,
        cache_dir=cache,
    )
    (home / "skills" / "gstack").mkdir()

    (repo / "skills" / "new-skill").mkdir()
    (repo / "skills" / "new-skill" / "SKILL.md").write_text(
        "---\nname: new-skill\n---\n",
        encoding="utf-8",
    )
    for child in (repo / "skills" / "old-skill").iterdir():
        child.unlink()
    (repo / "skills" / "old-skill").rmdir()
    second_ref = _commit(repo)

    updated_dependency = dependency.model_copy(update={"ref": second_ref})
    install_skill_dependencies(
        framework=Framework.CODEX,
        dependencies=[updated_dependency],
        home=home,
        cache_dir=cache,
    )

    assert not (home / "skills" / "old-skill").exists()
    assert (home / "skills" / "new-skill" / "SKILL.md").exists()
    assert (home / "skills" / "gstack").exists()


def test_install_skill_dependencies_dry_run_does_not_clone(tmp_path: Path) -> None:
    dependency = SkillDependency(
        id="gstack",
        name="GStack",
        repo="https://example.invalid/gstack.git",
        ref="abc123",
        install={"codex": SkillDependencyInstall(strategy="gstack", destination="skills/gstack")},
    )

    rows = install_skill_dependencies(
        framework=Framework.CODEX,
        dependencies=[dependency],
        home=tmp_path / "home",
        cache_dir=tmp_path / "cache",
        dry_run=True,
    )

    assert rows[0].dry_run is True
    assert rows[0].destination == tmp_path / "home" / "skills" / "gstack"
    assert not (tmp_path / "cache").exists()


def test_install_skill_dependencies_fails_when_framework_has_no_default_support(
    tmp_path: Path,
) -> None:
    dependency = SkillDependency(
        id="gstack",
        name="GStack",
        repo="https://example.invalid/gstack.git",
        ref="abc123",
        install={"codex": SkillDependencyInstall(strategy="gstack", destination="skills/gstack")},
    )

    try:
        install_skill_dependencies(
            framework=Framework.CURSOR,
            dependencies=[dependency],
            home=tmp_path / "home",
            cache_dir=tmp_path / "cache",
            dry_run=True,
        )
    except ValueError as exc:
        assert "no skill dependencies support framework cursor" in str(exc)
    else:
        raise AssertionError("expected unsupported default framework install to fail")


def test_install_skill_dependencies_fails_on_unsupported_explicit_dependency(
    tmp_path: Path,
) -> None:
    dependency = SkillDependency(
        id="gstack",
        name="GStack",
        repo="https://example.invalid/gstack.git",
        ref="abc123",
        install={"codex": SkillDependencyInstall(strategy="gstack", destination="skills/gstack")},
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCODE,
            dependencies=[dependency],
            dependency_ids=["gstack"],
            home=tmp_path / "home",
            cache_dir=tmp_path / "cache",
            dry_run=True,
        )
    except ValueError as exc:
        assert "not supported for opencode: gstack" in str(exc)
    else:
        raise AssertionError("expected unsupported explicit dependency to fail")


def test_humanlayer_show_me_is_pinned_for_all_managed_frameworks() -> None:
    registered = {dependency.id: dependency for dependency in load_skill_dependencies()}
    dependency = registered["humanlayer-show-me"]

    assert dependency.repo == "https://github.com/humanlayer/skills.git"
    assert dependency.ref == "4d8d644ca747517973f58d7953f58d7cd07520cd"
    assert dependency.version == "1.0.0"
    assert dependency.license == "MIT"
    assert set(dependency.install) == {
        framework.value for framework in Framework if framework is not Framework.LOCAL
    }
    assert all(
        install.strategy == "humanlayer-show-me"
        and install.source == "plugins/show-me/skills"
        and install.destination == "skills"
        for install in dependency.install.values()
    )


def _show_me_dependency(
    framework: Framework = Framework.PRIME_AGENT,
) -> SkillDependency:
    return SkillDependency(
        id="humanlayer-show-me",
        name="HumanLayer Show Me",
        repo="https://github.com/humanlayer/skills.git",
        ref="4d8d644ca747517973f58d7953f58d7cd07520cd",
        version="1.0.0",
        license="MIT",
        install={
            framework.value: SkillDependencyInstall(
                strategy="humanlayer-show-me",
                source="plugins/show-me/skills",
                destination="skills",
            )
        },
    )


def _show_me_source(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "LICENSE").write_text(
        "MIT License\n\nCopyright (c) 2026 HumanLayer\n",
        encoding="utf-8",
    )
    skill = root / "plugins" / "show-me" / "skills" / "show-me"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: Visual explanation.\n---\n"
        "Create the HTML. Then open it for the user:\n\n"
        "```\nBash(open path/to/show-me-{description}.html)\n```\n",
        encoding="utf-8",
    )
    return root


def test_humanlayer_show_me_installs_adapted_skill_with_ownership(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    install_skill_dependencies(
        framework=Framework.PRIME_AGENT,
        dependencies=[_show_me_dependency()],
        home=tmp_path / "home",
    )

    installed = tmp_path / "home" / "skills" / "show-me" / "SKILL.md"
    text = installed.read_text(encoding="utf-8")
    assert "Bash(open" not in text
    assert "absolute path" in text
    assert (tmp_path / "home" / "skills" / "show-me" / "LICENSE").read_text(encoding="utf-8") == (
        source / "LICENSE"
    ).read_text(encoding="utf-8")
    assert (tmp_path / "home" / SHOW_ME_OWNERSHIP_MANIFEST_RELATIVE).is_file()


def test_humanlayer_show_me_refuses_user_owned_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    existing = tmp_path / "home" / "skills" / "show-me"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: personal\n---\nKeep me.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.PRIME_AGENT,
            dependencies=[_show_me_dependency()],
            home=tmp_path / "home",
        )
    except ShowMeCollisionError as exc:
        assert "user-owned collision" in str(exc)
    else:
        raise AssertionError("expected user-owned show-me collision to fail")

    assert "Keep me." in (existing / "SKILL.md").read_text(encoding="utf-8")
    assert not (tmp_path / "home" / SHOW_ME_OWNERSHIP_MANIFEST_RELATIVE).exists()


def test_humanlayer_show_me_updates_only_unchanged_managed_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )
    arguments = {
        "framework": Framework.PRIME_AGENT,
        "dependencies": [_show_me_dependency()],
        "home": tmp_path / "home",
    }
    install_skill_dependencies(**arguments)
    installed = tmp_path / "home" / "skills" / "show-me" / "SKILL.md"
    installed.write_text(installed.read_text(encoding="utf-8") + "personal change\n")

    try:
        install_skill_dependencies(**arguments)
    except ShowMeCollisionError as exc:
        assert "changed since installation" in str(exc)
    else:
        raise AssertionError("expected changed managed show-me skill to fail")

    assert installed.read_text(encoding="utf-8").endswith("personal change\n")


def test_humanlayer_show_me_refuses_unmanifested_exact_upstream_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    existing = tmp_path / "home" / "skills" / "show-me"
    shutil.copytree(source / "plugins" / "show-me" / "skills" / "show-me", existing)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.PRIME_AGENT,
            dependencies=[_show_me_dependency()],
            home=tmp_path / "home",
        )
    except ShowMeCollisionError as exc:
        assert "user-owned collision" in str(exc)
    else:
        raise AssertionError("expected unmanifested exact upstream skill to fail")

    assert "Bash(open" in (existing / "SKILL.md").read_text(encoding="utf-8")


def test_humanlayer_show_me_refuses_logical_name_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    collision = tmp_path / "home" / "skills" / "visual-helper"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: collision\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.PRIME_AGENT,
            dependencies=[_show_me_dependency()],
            home=tmp_path / "home",
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected logical show-me collision to fail")


def test_humanlayer_show_me_preserves_changed_crash_recovery_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )
    arguments = {
        "framework": Framework.PRIME_AGENT,
        "dependencies": [_show_me_dependency()],
        "home": tmp_path / "home",
    }
    install_skill_dependencies(**arguments)
    home = tmp_path / "home"
    destination = home / "skills" / "show-me"
    manifest_path = home / SHOW_ME_OWNERSHIP_MANIFEST_RELATIVE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup = manifest_path.parent / ".humanlayer-show-me-backup-crash"
    shutil.copytree(destination, backup)
    (destination / "SKILL.md").write_text(
        (destination / "SKILL.md").read_text(encoding="utf-8") + "user edit\n",
        encoding="utf-8",
    )
    transaction = manifest_path.with_name("humanlayer-show-me-transaction.json")
    transaction.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "prepared",
                "stage": ".humanlayer-show-me-stage-crash",
                "backup": backup.name,
                "had_destination": True,
                "old_manifest": manifest,
                "new_manifest": manifest,
            }
        ),
        encoding="utf-8",
    )

    try:
        install_skill_dependencies(**arguments)
    except ShowMeCollisionError as exc:
        assert "preserving transaction data" in str(exc)
    else:
        raise AssertionError("expected changed crash-recovery target to fail")

    assert (destination / "SKILL.md").read_text(encoding="utf-8").endswith("user edit\n")
    assert backup.is_dir()
    assert transaction.is_file()


def test_humanlayer_show_me_refuses_symlinked_profile_ancestor(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    actual = tmp_path / "actual"
    actual.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.PRIME_AGENT,
            dependencies=[_show_me_dependency()],
            home=linked / "profile",
        )
    except ShowMeCollisionError as exc:
        assert "symbolic link or non-directory" in str(exc)
    else:
        raise AssertionError("expected symlinked profile ancestor to fail")

    assert not (actual / "profile").exists()


def test_show_me_windows_path_transaction_installs_and_updates(
    tmp_path: Path,
) -> None:
    source = _show_me_source(tmp_path / "source")
    profile = tmp_path / "windows-profile"
    destination = profile / "skills" / "show-me"

    first = show_me_adapter._install_show_me_windows(
        source / show_me_adapter.SOURCE_RELATIVE,
        destination,
        profile,
        show_me_adapter.PINNED_REF,
    )
    second = show_me_adapter._install_show_me_windows(
        source / show_me_adapter.SOURCE_RELATIVE,
        destination,
        profile,
        show_me_adapter.PINNED_REF,
    )

    assert first == second
    assert destination.is_dir()
    assert "supported artifact preview or file-opening capability" in (
        destination / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert not list((profile / "skills").glob(".humanlayer-show-me-*-*"))


def test_show_me_windows_transaction_refuses_replaced_skills_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    profile = tmp_path / "windows-profile"
    destination = profile / "skills" / "show-me"
    show_me_adapter._install_show_me_windows(
        source / show_me_adapter.SOURCE_RELATIVE,
        destination,
        profile,
        show_me_adapter.PINNED_REF,
    )
    original_preflight = show_me_adapter._preflight_windows
    displaced = profile / "displaced-skills"

    def replace_skills_root(*args, **kwargs):
        current = original_preflight(*args, **kwargs)
        (profile / "skills").rename(displaced)
        personal = profile / "skills" / "show-me"
        personal.mkdir(parents=True)
        (personal / "SKILL.md").write_text("personal\n", encoding="utf-8")
        return current

    monkeypatch.setattr(show_me_adapter, "_preflight_windows", replace_skills_root)

    with pytest.raises(ShowMeCollisionError, match="directory identity changed"):
        show_me_adapter._install_show_me_windows(
            source / show_me_adapter.SOURCE_RELATIVE,
            destination,
            profile,
            show_me_adapter.PINNED_REF,
        )

    assert (profile / "skills" / "show-me" / "SKILL.md").read_text(encoding="utf-8") == (
        "personal\n"
    )
    assert (displaced / "show-me" / "SKILL.md").is_file()


def test_show_me_windows_transaction_refuses_replaced_fingerprinted_child(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    profile = tmp_path / "windows-profile"
    destination = profile / "skills" / "show-me"
    show_me_adapter._install_show_me_windows(
        source / show_me_adapter.SOURCE_RELATIVE,
        destination,
        profile,
        show_me_adapter.PINNED_REF,
    )
    original_preflight = show_me_adapter._preflight_windows
    displaced = profile / "skills" / "displaced-show-me"

    def replace_fingerprinted_child(*args, **kwargs):
        current = original_preflight(*args, **kwargs)
        destination.rename(displaced)
        destination.mkdir()
        (destination / "SKILL.md").write_text("personal\n", encoding="utf-8")
        return current

    monkeypatch.setattr(show_me_adapter, "_preflight_windows", replace_fingerprinted_child)

    with pytest.raises(ShowMeCollisionError, match="managed show-me target changed"):
        show_me_adapter._install_show_me_windows(
            source / show_me_adapter.SOURCE_RELATIVE,
            destination,
            profile,
            show_me_adapter.PINNED_REF,
        )

    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "personal\n"
    assert (displaced / "SKILL.md").is_file()


def test_windows_directory_pin_compares_file_identity_across_mapped_aliases(
    monkeypatch,
) -> None:
    expected = show_me_adapter._WindowsDirectoryPin(
        path=Path("Z:/agent/skills"),
        identity=(17, 42),
        native_handle=100,
    )
    observed = show_me_adapter._WindowsDirectoryPin(
        path=Path(r"\\server\share\agent\skills"),
        identity=(17, 42),
        native_handle=101,
    )
    monkeypatch.setattr(
        show_me_adapter,
        "_observe_windows_directory",
        lambda path: observed,
    )
    monkeypatch.setattr(
        show_me_adapter,
        "_close_observed_windows_directory",
        lambda pin: None,
    )

    show_me_adapter._verify_windows_directory_pin(expected)

    changed = show_me_adapter._WindowsDirectoryPin(
        path=observed.path,
        identity=(17, 43),
        native_handle=102,
    )
    monkeypatch.setattr(
        show_me_adapter,
        "_observe_windows_directory",
        lambda path: changed,
    )
    with pytest.raises(ShowMeCollisionError, match="directory identity changed"):
        show_me_adapter._verify_windows_directory_pin(expected)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows sharing semantics")
def test_native_windows_directory_pin_blocks_sibling_replacement(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()

    with show_me_adapter._pin_windows_directory(skills), pytest.raises(OSError):
        skills.rename(tmp_path / "replacement")

    assert skills.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows subst")
def test_native_windows_mapped_alias_uses_file_identity_and_lock(tmp_path: Path) -> None:
    drive = next(
        (f"{letter}:" for letter in "ZYXWVUTSRQP" if not Path(f"{letter}:/").exists()),
        None,
    )
    if drive is None:
        pytest.skip("no unused drive letter")
    result = subprocess.run(
        ["subst", drive, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"subst unavailable: {result.stderr.strip()}")
    try:
        real = tmp_path / "dependencies"
        real.mkdir()
        mapped = Path(f"{drive}/dependencies")
        with show_me_adapter._pin_windows_directory(mapped) as mapped_pin:
            observed = show_me_adapter._observe_windows_directory(real)
            try:
                assert mapped_pin.identity == observed.identity
            finally:
                show_me_adapter._close_observed_windows_directory(observed)
            descriptor = show_me_adapter._open_windows_lock(mapped / "lock", mapped_pin)
            os.close(descriptor)
    finally:
        subprocess.run(["subst", drive, "/d"], check=False, capture_output=True)


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows APIs")
def test_native_windows_install_does_not_require_final_path_diagnostic(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    profile = tmp_path / "profile"
    monkeypatch.setattr(
        show_me_adapter,
        "_normalize_windows_final_path",
        lambda value: (_ for _ in ()).throw(AssertionError("diagnostic path was required")),
    )

    show_me_adapter._install_show_me_windows(
        source / show_me_adapter.SOURCE_RELATIVE,
        profile / "skills" / "show-me",
        profile,
        show_me_adapter.PINNED_REF,
    )

    assert (profile / "skills" / "show-me" / "SKILL.md").is_file()


def test_show_me_windows_lock_fallback_locks_one_byte(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[int, int, int]] = []

    class FakeMsvcrt:
        LK_LOCK = 1

        @staticmethod
        def locking(descriptor: int, mode: int, length: int) -> None:
            calls.append((descriptor, mode, length))

    lock_path = tmp_path / "lock"
    with lock_path.open("a+b") as stream:
        monkeypatch.setattr(show_me_adapter, "fcntl", None)
        monkeypatch.setattr(show_me_adapter, "msvcrt", FakeMsvcrt)
        show_me_adapter._lock_file(stream.fileno())

    assert calls and calls[0][1:] == (FakeMsvcrt.LK_LOCK, 1)
    assert lock_path.read_bytes() == b"\0"


def test_openclaw_home_resolves_active_state_precedence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_home = tmp_path / "openclaw-home"
    monkeypatch.setenv("OPENCLAW_HOME", "   ")
    assert default_framework_home(Framework.OPENCLAW) == Path.home() / ".openclaw"
    monkeypatch.setenv("OPENCLAW_HOME", str(base_home))
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "other" / "config.json"))
    assert default_framework_home(Framework.OPENCLAW) == tmp_path / "other"
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH")
    (base_home / ".clawdbot").mkdir(parents=True)
    assert default_framework_home(Framework.OPENCLAW) == base_home / ".openclaw"
    (base_home / ".openclaw").mkdir()
    assert default_framework_home(Framework.OPENCLAW) == base_home / ".openclaw"

    monkeypatch.setenv("OPENCLAW_PROFILE", "customer-a")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "other" / "config.json"))
    assert default_framework_home(Framework.OPENCLAW) == base_home / ".openclaw-customer-a"
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH")

    monkeypatch.setenv("OPENCLAW_STATE_DIR", "~/selected-state")
    assert default_framework_home(Framework.OPENCLAW) == base_home / "selected-state"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENCLAW_STATE_DIR", "~root/selected-state")
    assert default_framework_home(Framework.OPENCLAW) == (tmp_path / "~root" / "selected-state")
    monkeypatch.delenv("OPENCLAW_STATE_DIR")
    monkeypatch.delenv("OPENCLAW_PROFILE")
    monkeypatch.setenv("OPENCLAW_HOME", "~root/openclaw-home")
    assert default_framework_home(Framework.OPENCLAW) == (
        tmp_path / "~root" / "openclaw-home" / ".openclaw"
    )


def test_openclaw_home_matches_operator_home_precedence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    monkeypatch.setenv("HOME", "undefined")
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "userprofile"))
    assert default_framework_home(Framework.OPENCLAW) == (tmp_path / "userprofile" / ".openclaw")

    monkeypatch.setenv("HOME", str(tmp_path / "home-wins"))
    assert default_framework_home(Framework.OPENCLAW) == (tmp_path / "home-wins" / ".openclaw")

    monkeypatch.setenv("HOME", "null")
    monkeypatch.setenv("USERPROFILE", " ")
    monkeypatch.setenv("ANDROID_DATA", "/data")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert default_framework_home(Framework.OPENCLAW) == (
        Path("/data/data/com.termux/files") / "home" / ".openclaw"
    )


def test_opencode_ignores_flat_markdown_in_other_global_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    target = os_home / ".agents"
    unrelated = os_home / ".config" / "opencode" / "skills" / "show-me.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("---\ndescription: not a loadable skill\n---\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    install_skill_dependencies(
        framework=Framework.OPENCODE,
        dependencies=[_show_me_dependency(Framework.OPENCODE)],
        home=target,
    )

    assert (target / "skills" / "show-me" / "SKILL.md").is_file()


def test_openclaw_default_state_refuses_personal_agent_skill_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    personal = os_home / ".agents" / "skills" / "group" / "show-me-copy"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: personal\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected personal OpenClaw collision to fail")

    assert not (os_home / ".openclaw" / "skills" / "show-me").exists()


def test_openclaw_custom_state_at_default_path_checks_personal_agent_skill_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    personal = os_home / ".agents" / "skills" / "show-me"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: default only\n---\n",
        encoding="utf-8",
    )
    custom_state = os_home / ".openclaw"
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(custom_state))
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-path collision" in str(exc)
    else:
        raise AssertionError("expected default-path personal OpenClaw collision to fail")

    assert not (custom_state / "skills" / "show-me").exists()


def test_openclaw_config_path_selects_managed_skill_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    monkeypatch.setenv(
        "OPENCLAW_CONFIG_PATH",
        str(tmp_path / "configured" / "openclaw.json"),
    )

    assert default_framework_home(Framework.OPENCLAW) == tmp_path / "configured"


def test_codex_system_skill_root_is_checked(tmp_path: Path, monkeypatch) -> None:
    source = _show_me_source(tmp_path / "source")
    system_root = tmp_path / "etc" / "codex" / "skills"
    collision = system_root / "admin-show-me"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: admin\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setattr("agent_ops.skill_installer._codex_system_skill_root", lambda: system_root)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.CODEX,
            dependencies=[_show_me_dependency(Framework.CODEX)],
            home=tmp_path / "codex",
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected Codex system-root collision")


def test_opencode_configured_absolute_skill_root_is_checked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    configured_root = tmp_path / "catalog"
    collision = configured_root / "visual-helper"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: configured\n---\n",
        encoding="utf-8",
    )
    config_home = tmp_path / "xdg" / "opencode"
    config_home.mkdir(parents=True)
    (config_home / "opencode.jsonc").write_text(
        '{ "skills": { "paths": ["' + str(configured_root).replace("\\", "\\\\") + '"] } }',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_CONTENT", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCODE,
            dependencies=[_show_me_dependency(Framework.OPENCODE)],
            home=os_home / ".agents",
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected OpenCode configured-root collision")


def test_opencode_workspace_relative_configured_root_fails_before_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv(
        "OPENCODE_CONFIG_CONTENT",
        '{ "skills": { "paths": ["./project-catalog"] } }',
    )
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCODE,
            dependencies=[_show_me_dependency(Framework.OPENCODE)],
            home=os_home / ".agents",
        )
    except ValueError as exc:
        assert "workspace-relative path" in str(exc)
    else:
        raise AssertionError("expected relative configured root to fail closed")

    assert not (os_home / ".agents" / "skills" / "show-me").exists()


def test_openclaw_extra_skill_root_is_checked(tmp_path: Path, monkeypatch) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    extra_root = tmp_path / "catalog"
    collision = extra_root / "visual-helper"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: extra\n---\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "openclaw.json").write_text(
        json.dumps({"skills": {"load": {"extraDirs": [str(extra_root)]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(state))
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected OpenClaw extra-root collision")


def test_openclaw_named_profile_configured_skill_root_is_checked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    extra_root = tmp_path / "catalog"
    collision = extra_root / "visual-helper"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: profile\n---\n",
        encoding="utf-8",
    )
    profile_state = os_home / ".openclaw-customer-a"
    profile_state.mkdir(parents=True)
    (profile_state / "openclaw.json").write_text(
        json.dumps({"skills": {"load": {"extraDirs": [str(extra_root)]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("OPENCLAW_PROFILE", "customer-a")
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected named-profile extra-root collision")


def test_openclaw_state_without_config_falls_back_to_default_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    effective_home = tmp_path / "effective-home"
    extra_root = tmp_path / "catalog"
    collision = extra_root / "visual-helper"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: fallback\n---\n",
        encoding="utf-8",
    )
    default_state = effective_home / ".openclaw"
    default_state.mkdir(parents=True)
    (default_state / "openclaw.json").write_text(
        json.dumps({"skills": {"load": {"extraDirs": [str(extra_root)]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("OPENCLAW_HOME", str(effective_home))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "empty-state"))
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected default-config fallback extra-root collision")


def test_openclaw_relative_include_root_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    state = tmp_path / "state"
    state.mkdir()
    (state / "openclaw.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(state))
    monkeypatch.setenv("OPENCLAW_INCLUDE_ROOTS", "relative-root")
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )
    except ValueError as exc:
        assert "OPENCLAW_INCLUDE_ROOTS entries must be absolute" in str(exc)
    else:
        raise AssertionError("expected relative include root to fail closed")

    assert not (state / "skills" / "show-me").exists()


def test_openclaw_extra_root_from_included_json5_is_checked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    extra_root = tmp_path / "catalog"
    collision = extra_root / "visual-helper"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: included\n---\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "skills.json5").write_text(
        "{ skills: { load: { extraDirs: ['" + str(extra_root) + "'], }, }, }",
        encoding="utf-8",
    )
    (state / "openclaw.json").write_text(
        "{ $include: './skills.json5' }",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(state))
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected included OpenClaw extra-root collision")


def test_openclaw_nested_extra_dirs_include_is_checked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    extra_root = tmp_path / "catalog"
    collision = extra_root / "visual-helper"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: nested include\n---\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "load.json5").write_text(
        "{ extraDirs: ['" + str(extra_root) + "'] }",
        encoding="utf-8",
    )
    (state / "openclaw.json").write_text(
        "{ skills: { load: { $include: './load.json5' } } }",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "os-home"))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(state))
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected nested included extra-root collision")


def test_openclaw_extra_root_expands_effective_home_and_environment(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    effective_home = tmp_path / "effective-home"
    configured_root = effective_home / "catalog" / "extra"
    collision = configured_root / "visual-helper"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: extra\n---\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "openclaw.json").write_text(
        json.dumps(
            {
                "env": {"vars": {"LEAF": "extra"}},
                "skills": {"load": {"extraDirs": ["~/catalog/${LEAF}"]}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("OPENCLAW_HOME", str(effective_home))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(state))
    monkeypatch.delenv("LEAF", raising=False)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected expanded OpenClaw extra-root collision")


def test_openclaw_custom_config_still_checks_default_state_personal_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    personal = os_home / ".agents" / "skills" / "show-me"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: personal\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv(
        "OPENCLAW_CONFIG_PATH",
        str(tmp_path / "configured" / "openclaw.json"),
    )
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-path collision" in str(exc)
    else:
        raise AssertionError("expected custom-config personal collision")

    assert not (tmp_path / "configured" / "skills" / "show-me").exists()


def test_opencode_xdg_and_config_override_roots_are_checked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    xdg = tmp_path / "xdg"
    override = tmp_path / "override"
    collision = xdg / "opencode" / "skills" / "visual-helper"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: 'show-me'\ndescription: xdg alongside override\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(override))
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCODE,
            dependencies=[_show_me_dependency(Framework.OPENCODE)],
            home=os_home / ".agents",
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected XDG root collision alongside configured override to fail")


def test_show_me_yaml_frontmatter_collision_handles_folded_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    collision = tmp_path / "home" / "skills" / "visual-helper"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: >-\n  show-me\ndescription: folded\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.PRIME_AGENT,
            dependencies=[_show_me_dependency()],
            home=tmp_path / "home",
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected YAML-equivalent collision to fail")


def test_show_me_stage_is_outside_host_discovery_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    home = os_home / ".agents"
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    def inspect_stage(skills_fd, dependencies_fd, stage_name, manifest, current) -> None:
        assert not (show_me_adapter._fd_path(skills_fd) / stage_name).exists()
        assert (show_me_adapter._fd_path(dependencies_fd) / stage_name).is_dir()
        raise KeyboardInterrupt

    monkeypatch.setattr(show_me_adapter, "_install_transaction", inspect_stage)
    try:
        install_skill_dependencies(
            framework=Framework.OPENCODE,
            dependencies=[_show_me_dependency(Framework.OPENCODE)],
            home=home,
        )
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("expected simulated interruption")

    assert not list((home / "skills").glob(".humanlayer-show-me-stage-*"))


def test_show_me_retries_partial_unjournaled_garbage_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    home = tmp_path / "home"
    garbage = (
        home / SHOW_ME_OWNERSHIP_MANIFEST_RELATIVE.parent / ".humanlayer-show-me-stage-interrupted"
    )
    garbage.mkdir(parents=True)
    (garbage / "partial").write_text("partial", encoding="utf-8")
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    install_skill_dependencies(
        framework=Framework.PRIME_AGENT,
        dependencies=[_show_me_dependency()],
        home=home,
    )

    assert not garbage.exists()
    assert (home / "skills" / "show-me" / "SKILL.md").is_file()


def test_windows_helpers_refuse_linked_descendant_and_lock(
    tmp_path: Path,
) -> None:
    assert (
        show_me_adapter._normalize_windows_final_path("\\\\?\\C:\\Users\\agent\\lock")
        == "C:\\Users\\agent\\lock"
    )
    assert (
        show_me_adapter._normalize_windows_final_path("\\\\?\\UNC\\server\\share\\lock")
        == "\\\\server\\share\\lock"
    )

    external = tmp_path / "external"
    external.mkdir()
    linked_ancestor = tmp_path / "linked-ancestor"
    linked_ancestor.symlink_to(external, target_is_directory=True)
    try:
        show_me_adapter._ensure_windows_directory(linked_ancestor / "profile")
    except ShowMeCollisionError as exc:
        assert "symbolic link or junction" in str(exc)
    else:
        raise AssertionError("expected linked Windows ancestor to fail")
    assert not (external / "profile").exists()

    linked_directory = tmp_path / "profile" / ".agentops"
    linked_directory.parent.mkdir()
    linked_directory.symlink_to(external, target_is_directory=True)
    try:
        show_me_adapter._ensure_windows_directory(linked_directory)
    except ShowMeCollisionError as exc:
        assert "symbolic link or junction" in str(exc)
    else:
        raise AssertionError("expected linked Windows descendant to fail")

    target = tmp_path / "user-file"
    target.write_bytes(b"")
    linked_lock = tmp_path / "lock"
    linked_lock.symlink_to(target)
    try:
        show_me_adapter._open_windows_lock(linked_lock)
    except ShowMeCollisionError as exc:
        assert "unsafe show-me lock path" in str(exc)
    else:
        raise AssertionError("expected linked Windows lock to fail")
    assert target.read_bytes() == b""


def test_openclaw_named_profile_excludes_personal_agent_skill_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    personal = os_home / ".agents" / "skills" / "show-me"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: default only\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("OPENCLAW_PROFILE", "customer-a")
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    install_skill_dependencies(
        framework=Framework.OPENCLAW,
        dependencies=[_show_me_dependency(Framework.OPENCLAW)],
    )

    assert (os_home / ".openclaw-customer-a" / "skills" / "show-me" / "SKILL.md").is_file()


def test_openclaw_legacy_state_does_not_relocate_managed_skills(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    (os_home / ".clawdbot").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    install_skill_dependencies(
        framework=Framework.OPENCLAW,
        dependencies=[_show_me_dependency(Framework.OPENCLAW)],
    )

    assert (os_home / ".openclaw" / "skills" / "show-me" / "SKILL.md").is_file()
    assert not (os_home / ".clawdbot" / "skills" / "show-me").exists()


def test_openclaw_config_path_at_default_root_checks_personal_agent_skill(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    personal = os_home / ".agents" / "skills" / "show-me"
    personal.mkdir(parents=True)
    (personal / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: personal\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv(
        "OPENCLAW_CONFIG_PATH",
        str(os_home / ".openclaw" / "openclaw.json"),
    )
    monkeypatch.delenv("OPENCLAW_HOME", raising=False)
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-path collision" in str(exc)
    else:
        raise AssertionError("expected config-only personal collision to fail")


def test_codex_and_cursor_check_all_global_compatibility_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    for framework, root in (
        (Framework.CODEX, os_home / ".agents"),
        (Framework.CURSOR, os_home / ".codex"),
        (Framework.CURSOR, os_home / ".claude"),
        (Framework.CURSOR, os_home / ".agents"),
    ):
        collision = root / "skills" / "show-me"
        collision.mkdir(parents=True)
        (collision / "SKILL.md").write_text(
            "---\nname: show-me\ndescription: existing\n---\n",
            encoding="utf-8",
        )
        try:
            install_skill_dependencies(
                framework=framework,
                dependencies=[_show_me_dependency(framework)],
                home=os_home / f"target-{framework.value}-{root.name}",
            )
        except ShowMeCollisionError as exc:
            assert "logical skill-path collision" in str(exc)
        else:
            raise AssertionError(f"expected {framework.value} compatibility collision")
        shutil.rmtree(root)


def test_opencode_external_disable_uses_native_config_skill_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("OPENCODE_DISABLE_EXTERNAL_SKILLS", "1")
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    assert default_framework_home(Framework.OPENCODE) == xdg / "opencode"
    install_skill_dependencies(
        framework=Framework.OPENCODE,
        dependencies=[_show_me_dependency(Framework.OPENCODE)],
    )

    assert (xdg / "opencode" / "skills" / "show-me" / "SKILL.md").is_file()
    assert not (os_home / ".agents" / "skills" / "show-me").exists()


def test_opencode_collision_scans_singular_and_plural_config_roots(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    config = tmp_path / "config"
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(config))
    monkeypatch.setenv("OPENCODE_DISABLE_EXTERNAL_SKILLS", "true")
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    collision = config / "skill" / "show-me"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: singular\n---\n",
        encoding="utf-8",
    )
    try:
        install_skill_dependencies(
            framework=Framework.OPENCODE,
            dependencies=[_show_me_dependency(Framework.OPENCODE)],
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-path collision" in str(exc)
    else:
        raise AssertionError("expected singular OpenCode collision")


def test_openclaw_home_rejects_unsafe_profile_name(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.setenv("OPENCLAW_PROFILE", "../escape")
    try:
        default_framework_home(Framework.OPENCLAW)
    except ValueError as exc:
        assert "OPENCLAW_PROFILE" in str(exc)
    else:
        raise AssertionError("expected unsafe OpenClaw profile to fail")


def test_humanlayer_show_me_recovers_interrupted_fresh_install(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )
    original_write = show_me_adapter._write_json_at

    def interrupt_manifest(directory_fd: int, name: str, value) -> None:
        if name == SHOW_ME_OWNERSHIP_MANIFEST_RELATIVE.name:
            raise KeyboardInterrupt
        original_write(directory_fd, name, value)

    monkeypatch.setattr(show_me_adapter, "_write_json_at", interrupt_manifest)
    try:
        install_skill_dependencies(
            framework=Framework.PRIME_AGENT,
            dependencies=[_show_me_dependency()],
            home=tmp_path / "home",
        )
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("expected simulated installation interruption")

    assert not (tmp_path / "home" / "skills" / "show-me").exists()
    assert not (tmp_path / "home" / SHOW_ME_OWNERSHIP_MANIFEST_RELATIVE).exists()
    transaction = (
        tmp_path
        / "home"
        / SHOW_ME_OWNERSHIP_MANIFEST_RELATIVE.parent
        / "humanlayer-show-me-transaction.json"
    )
    assert not transaction.exists()


def test_humanlayer_show_me_recovers_interrupted_update(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )
    arguments = {
        "framework": Framework.PRIME_AGENT,
        "dependencies": [_show_me_dependency()],
        "home": tmp_path / "home",
    }
    install_skill_dependencies(**arguments)
    installed = tmp_path / "home" / "skills" / "show-me" / "SKILL.md"
    before = installed.read_bytes()
    original_rename = show_me_adapter.os.rename

    def interrupt_stage(source_name, destination_name, **kwargs) -> None:
        if str(source_name).startswith(".humanlayer-show-me-stage-"):
            raise KeyboardInterrupt
        original_rename(source_name, destination_name, **kwargs)

    monkeypatch.setattr(show_me_adapter.os, "rename", interrupt_stage)
    try:
        install_skill_dependencies(**arguments)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("expected simulated update interruption")

    assert installed.read_bytes() == before
    assert (tmp_path / "home" / SHOW_ME_OWNERSHIP_MANIFEST_RELATIVE).is_file()


def test_humanlayer_show_me_refuses_flat_root_skill_name_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    skills = tmp_path / "home" / "skills"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: flat\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.PRIME_AGENT,
            dependencies=[_show_me_dependency()],
            home=tmp_path / "home",
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
        assert "skills/SKILL.md" in str(exc)
    else:
        raise AssertionError("expected flat logical collision to fail")


def test_humanlayer_show_me_refuses_nested_logical_name_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    collision = tmp_path / "home" / "skills" / "personal" / "visual-helper"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: nested\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.PRIME_AGENT,
            dependencies=[_show_me_dependency()],
            home=tmp_path / "home",
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected nested logical collision to fail")


def test_humanlayer_show_me_allows_unrelated_linked_skill_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    external = tmp_path / "external"
    external.mkdir()
    external_skill = external / "SKILL.md"
    external_skill.write_text(
        "---\nname: gstack\ndescription: unrelated\n---\n",
        encoding="utf-8",
    )
    external_reference = external / "ETHOS.md"
    external_reference.write_text("Unrelated reference.\n", encoding="utf-8")
    skill = tmp_path / "home" / "skills" / "gstack"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").symlink_to(external_skill)
    (skill / "ETHOS.md").symlink_to(external_reference)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    install_skill_dependencies(
        framework=Framework.PRIME_AGENT,
        dependencies=[_show_me_dependency()],
        home=tmp_path / "home",
    )

    assert (tmp_path / "home" / "skills" / "show-me" / "SKILL.md").is_file()


def test_humanlayer_show_me_refuses_symlinked_logical_name_collision(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    external = tmp_path / "external"
    external.mkdir()
    (external / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: external\n---\n",
        encoding="utf-8",
    )
    skills = tmp_path / "home" / "skills"
    skills.mkdir(parents=True)
    (skills / "visual-helper").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    try:
        install_skill_dependencies(
            framework=Framework.PRIME_AGENT,
            dependencies=[_show_me_dependency()],
            home=tmp_path / "home",
        )
    except ShowMeCollisionError as exc:
        assert "logical skill-name collision" in str(exc)
    else:
        raise AssertionError("expected symlinked logical collision to fail")


def test_prime_agent_uses_native_home_and_pinned_bundle_mappings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    registered = {dependency.id: dependency for dependency in load_skill_dependencies()}
    assert all(
        len(registered[dependency_id].ref) == 40
        and set(registered[dependency_id].ref) <= set("0123456789abcdef")
        for dependency_id in ("gstack", "superpowers")
    )
    assert registered["gstack"].version == "1.32.0.0"
    assert registered["gstack"].install["prime-agent"].destination == "."
    assert registered["gstack"].install["prime-agent"].strategy == "prime-gstack"
    assert registered["superpowers"].install["prime-agent"].destination == "skills"
    assert registered["superpowers"].install["prime-agent"].strategy == "prime-superpowers"

    dependencies = [
        SkillDependency(
            id="gstack",
            name="GStack",
            repo="https://example.invalid/gstack.git",
            ref="a" * 40,
            install={
                "prime-agent": SkillDependencyInstall(
                    strategy="prime-gstack",
                    destination=".",
                )
            },
        ),
        SkillDependency(
            id="superpowers",
            name="Superpowers",
            repo="https://example.invalid/superpowers.git",
            ref="b" * 40,
            install={
                "prime-agent": SkillDependencyInstall(
                    strategy="prime-superpowers",
                    source="skills",
                    destination="skills",
                )
            },
        ),
    ]

    rows = install_skill_dependencies(
        framework=Framework.PRIME_AGENT,
        dependencies=dependencies,
        home=tmp_path / "prime-home",
        dry_run=True,
    )

    monkeypatch.delenv("PRIME_AGENT_CODING_AGENT_DIR", raising=False)
    assert default_framework_home(Framework.PRIME_AGENT) == Path("~/.prime/agent").expanduser()
    assert [row.destination.relative_to(tmp_path / "prime-home").as_posix() for row in rows] == [
        ".",
        "skills",
    ]


def test_prime_agent_home_treats_empty_native_environment_variable_as_unset(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PRIME_AGENT_CODING_AGENT_DIR", "")

    assert default_framework_home(Framework.PRIME_AGENT) == Path("~/.prime/agent").expanduser()


def test_prime_agent_home_honors_native_environment_variable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configured_home = tmp_path / "configured-prime-home"
    monkeypatch.setenv("PRIME_AGENT_CODING_AGENT_DIR", str(configured_home))

    assert default_framework_home(Framework.PRIME_AGENT) == configured_home


def test_framework_homes_honor_native_environment_variables(
    tmp_path: Path,
    monkeypatch,
) -> None:
    configured = {
        Framework.CODEX: ("CODEX_HOME", tmp_path / "codex"),
        Framework.CLAUDE_CODE: ("CLAUDE_CONFIG_DIR", tmp_path / "claude"),
        Framework.OPENCLAW: ("OPENCLAW_STATE_DIR", tmp_path / "openclaw"),
        Framework.PRIME_AGENT: (
            "PRIME_AGENT_CODING_AGENT_DIR",
            tmp_path / "prime",
        ),
    }
    for framework, (variable, home) in configured.items():
        monkeypatch.setenv(variable, str(home))
        assert default_framework_home(framework) == home

    monkeypatch.setenv("CURSOR_HOME", str(tmp_path / "ignored-cursor"))
    monkeypatch.setenv("OPENCODE_HOME", str(tmp_path / "ignored-opencode"))
    monkeypatch.setenv("AGENT_OPS_LOCAL_HOME", str(tmp_path / "ignored-local"))
    assert default_framework_home(Framework.CURSOR) == Path("~/.cursor").expanduser()
    assert default_framework_home(Framework.OPENCODE) == Path("~/.agents").expanduser()
    assert default_framework_home(Framework.LOCAL) == Path("~/.agentops").expanduser()


def test_prime_superpowers_strategy_installs_adapted_namespaced_skills(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "superpowers-src"
    for name in SUPERPOWERS_SKILLS:
        directory = source / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: test\n---\nUse superpowers:writing-plans.\n",
            encoding="utf-8",
        )
    dependency = SkillDependency(
        id="superpowers",
        name="Superpowers",
        repo="https://example.invalid/superpowers.git",
        ref="f2cbfbefebbfef77321e4c9abc9e949826bea9d7",
        install={
            "prime-agent": SkillDependencyInstall(
                strategy="prime-superpowers", source="skills", destination="skills"
            )
        },
    )
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    install_skill_dependencies(
        framework=Framework.PRIME_AGENT,
        dependencies=[dependency],
        home=tmp_path / "home",
        cache_dir=tmp_path / "cache",
    )

    installed = tmp_path / "home" / "skills"
    assert (installed.parent / OWNERSHIP_MANIFEST_RELATIVE).is_file()
    assert not (installed / ".agentops-superpowers-manifest.json").exists()
    assert not (installed / "writing-plans").exists()
    skill = installed / "agentops-superpowers-writing-plans" / "SKILL.md"
    assert "name: agentops-superpowers-writing-plans" in skill.read_text()
    assert "agentops-superpowers-writing-plans" in skill.read_text()


def test_prime_gstack_strategy_receives_the_profile_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "gstack-src"
    source.mkdir()
    dependency = SkillDependency(
        id="gstack",
        name="GStack",
        repo="https://example.invalid/gstack.git",
        ref="74895062fb8a3acbf9f66cd088a83359aaaa56cd",
        install={"prime-agent": SkillDependencyInstall(strategy="prime-gstack", destination=".")},
    )
    calls: list[tuple[Path, Path]] = []
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency",
        lambda dependency, cache: source,
    )
    monkeypatch.setattr(
        "agent_ops.skill_installer.install_prime_gstack",
        lambda checkout, coding_agent_dir: calls.append((checkout, coding_agent_dir)),
    )

    install_skill_dependencies(
        framework=Framework.PRIME_AGENT,
        dependencies=[dependency],
        home=tmp_path / "home",
        cache_dir=tmp_path / "cache",
    )

    assert calls == [(source, tmp_path / "home")]


def test_openclaw_configured_root_ignores_undiscoverable_entries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    extra = tmp_path / "catalog"
    ignored = extra / "node_modules" / "show-me"
    ignored.mkdir(parents=True)
    (ignored / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: ignored\n---\n", encoding="utf-8"
    )
    ordinary = extra / "ordinary"
    ordinary.mkdir()
    (ordinary / "SKILL.md").write_text(
        "---\nname: on\ndescription: unrelated YAML 1.2 name\n---\n", encoding="utf-8"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: escaped\n---\n", encoding="utf-8"
    )
    (extra / "escaped").symlink_to(outside, target_is_directory=True)
    config = os_home / ".openclaw" / "openclaw.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"skills": {"load": {"extraDirs": [str(extra)]}}}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    install_skill_dependencies(
        framework=Framework.OPENCLAW,
        dependencies=[_show_me_dependency(Framework.OPENCLAW)],
    )

    assert (os_home / ".openclaw" / "skills" / "show-me" / "SKILL.md").is_file()


def test_opencode_configured_root_scans_visible_node_modules(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    extra = tmp_path / "catalog"
    collision = extra / "node_modules" / "show-me"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: discoverable\n---\n", encoding="utf-8"
    )
    config = os_home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"skills": {"paths": [str(extra)]}}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    with pytest.raises(ShowMeCollisionError, match="logical skill-path collision"):
        install_skill_dependencies(
            framework=Framework.OPENCODE,
            dependencies=[_show_me_dependency(Framework.OPENCODE)],
        )


def test_codex_admin_root_ignores_hidden_skill_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    admin = tmp_path / "admin"
    hidden = admin / ".fixtures" / "show-me"
    hidden.mkdir(parents=True)
    (hidden / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: hidden\n---\n", encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setattr("agent_ops.skill_installer._codex_system_skill_root", lambda: admin)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    install_skill_dependencies(
        framework=Framework.CODEX,
        dependencies=[_show_me_dependency(Framework.CODEX)],
    )

    assert (os_home / ".codex" / "skills" / "show-me" / "SKILL.md").is_file()


def test_openclaw_configured_root_scans_allowed_symlink_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    extra = tmp_path / "catalog"
    extra.mkdir()
    allowed = tmp_path / "shared"
    collision = allowed / "show-me-copy"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: allowed external\n---\n", encoding="utf-8"
    )
    (extra / "shared").symlink_to(allowed, target_is_directory=True)
    config = os_home / ".openclaw" / "openclaw.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "skills": {
                    "load": {
                        "extraDirs": [str(extra)],
                        "allowSymlinkTargets": [str(allowed)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_PROFILE", raising=False)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    with pytest.raises(ShowMeCollisionError, match="logical skill-name collision"):
        install_skill_dependencies(
            framework=Framework.OPENCLAW,
            dependencies=[_show_me_dependency(Framework.OPENCLAW)],
        )


def test_opencode_system_managed_skill_path_is_checked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    managed = tmp_path / "managed"
    extra = tmp_path / "enterprise-skills"
    collision = extra / "show-me-copy"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text(
        "---\nname: show-me\ndescription: managed\n---\n", encoding="utf-8"
    )
    managed.mkdir()
    (managed / "opencode.json").write_text(
        json.dumps({"skills": {"paths": [str(extra)]}}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("OPENCODE_TEST_MANAGED_CONFIG_DIR", str(managed))
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )

    with pytest.raises(ShowMeCollisionError, match="logical skill-name collision"):
        install_skill_dependencies(
            framework=Framework.OPENCODE,
            dependencies=[_show_me_dependency(Framework.OPENCODE)],
        )


def test_opencode_remote_skill_url_fails_before_checkout(
    tmp_path: Path,
    monkeypatch,
) -> None:
    os_home = tmp_path / "os-home"
    config = os_home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"skills": {"urls": ["https://example.test/skills/"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("OPENCODE_TEST_MANAGED_CONFIG_DIR", str(tmp_path / "managed"))
    checkout_called = False

    def checkout(dependency, cache):
        nonlocal checkout_called
        checkout_called = True
        raise AssertionError("checkout must not run")

    monkeypatch.setattr("agent_ops.skill_installer._checkout_dependency", checkout)

    with pytest.raises(ValueError, match=r"skills\.urls cannot be inspected reproducibly"):
        install_skill_dependencies(
            framework=Framework.OPENCODE,
            dependencies=[_show_me_dependency(Framework.OPENCODE)],
        )

    assert not checkout_called
    assert not (os_home / ".agents" / "skills" / "show-me").exists()


def test_opencode_well_known_remote_config_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    os_home = tmp_path / "os-home"
    data = tmp_path / "data"
    auth = data / "opencode" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text(
        json.dumps(
            {"https://enterprise.test": {"type": "wellknown", "key": "TOKEN", "token": "secret"}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("OPENCODE_TEST_MANAGED_CONFIG_DIR", str(tmp_path / "managed"))

    with pytest.raises(ValueError, match="well-known remote configuration"):
        install_skill_dependencies(
            framework=Framework.OPENCODE,
            dependencies=[_show_me_dependency(Framework.OPENCODE)],
        )


def test_opencode_active_organization_remote_config_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    os_home = tmp_path / "os-home"
    data = tmp_path / "data"
    database = data / "opencode" / "opencode.db"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE account_state (active_account_id TEXT, active_org_id TEXT)"
        )
        connection.execute("INSERT INTO account_state VALUES (?, ?)", ("account", "organization"))
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setenv("OPENCODE_TEST_MANAGED_CONFIG_DIR", str(tmp_path / "managed"))

    with pytest.raises(ValueError, match="active-account remote configuration"):
        install_skill_dependencies(
            framework=Framework.OPENCODE,
            dependencies=[_show_me_dependency(Framework.OPENCODE)],
        )


def test_show_me_update_allows_other_unchanged_agentops_managed_copy(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = _show_me_source(tmp_path / "source")
    os_home = tmp_path / "os-home"
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setattr(
        "agent_ops.skill_installer._checkout_dependency", lambda dependency, cache: source
    )
    install_skill_dependencies(
        framework=Framework.CLAUDE_CODE,
        dependencies=[_show_me_dependency(Framework.CLAUDE_CODE)],
    )
    claude_license = os_home / ".claude" / "skills" / "show-me" / "LICENSE"
    claude_license.unlink()
    manifest = os_home / ".claude" / SHOW_ME_OWNERSHIP_MANIFEST_RELATIVE
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["installed_fingerprint"] = show_me_adapter._tree_fingerprint(claude_license.parent)
    manifest.write_text(json.dumps(value), encoding="utf-8")

    install_skill_dependencies(
        framework=Framework.OPENCODE,
        dependencies=[_show_me_dependency(Framework.OPENCODE)],
    )

    assert (os_home / ".agents" / "skills" / "show-me" / "LICENSE").is_file()
