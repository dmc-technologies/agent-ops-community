from __future__ import annotations

import subprocess
from pathlib import Path

from agent_ops.registries.models import Framework, SkillDependency, SkillDependencyInstall
from agent_ops.skill_installer import install_skill_dependencies


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


def test_install_gstack_dependency_copies_bundle_with_office_hours(tmp_path: Path) -> None:
    repo = tmp_path / "gstack-src"
    repo_url = _git_repo(repo)
    (repo / "office-hours").mkdir()
    (repo / "office-hours" / "SKILL.md").write_text(
        "---\nname: office-hours\n---\n",
        encoding="utf-8",
    )
    ref = _commit(repo)

    dependency = SkillDependency(
        id="gstack",
        name="GStack",
        repo=repo_url,
        ref=ref,
        install={
            "codex": SkillDependencyInstall(strategy="gstack", destination="skills/gstack")
        },
    )

    rows = install_skill_dependencies(
        framework=Framework.CODEX,
        dependencies=[dependency],
        home=tmp_path / "home",
        cache_dir=tmp_path / "cache",
    )

    assert rows[0].destination == tmp_path / "home" / "skills" / "gstack"
    assert (tmp_path / "home" / "skills" / "gstack" / "office-hours" / "SKILL.md").exists()


def test_install_copy_skills_dependency_merges_skill_directories(tmp_path: Path) -> None:
    repo = tmp_path / "superpowers-src"
    repo_url = _git_repo(repo)
    (repo / "skills" / "writing-plans").mkdir(parents=True)
    (repo / "skills" / "writing-plans" / "SKILL.md").write_text(
        "---\nname: writing-plans\n---\n",
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

    install_skill_dependencies(
        framework=Framework.CODEX,
        dependencies=[dependency],
        home=tmp_path / "home",
        cache_dir=tmp_path / "cache",
    )

    assert (tmp_path / "home" / "skills" / "writing-plans" / "SKILL.md").exists()


def test_install_copy_skills_dependency_supports_opencode(tmp_path: Path) -> None:
    repo = tmp_path / "superpowers-src"
    repo_url = _git_repo(repo)
    (repo / "skills" / "writing-plans").mkdir(parents=True)
    (repo / "skills" / "writing-plans" / "SKILL.md").write_text(
        "---\nname: writing-plans\n---\n",
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
        install={
            "codex": SkillDependencyInstall(strategy="gstack", destination="skills/gstack")
        },
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
        install={
            "codex": SkillDependencyInstall(strategy="gstack", destination="skills/gstack")
        },
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
        install={
            "codex": SkillDependencyInstall(strategy="gstack", destination="skills/gstack")
        },
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
