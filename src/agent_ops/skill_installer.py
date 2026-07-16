from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_ops.registries.models import Framework, SkillDependency, SkillDependencyInstall


@dataclass(frozen=True)
class InstalledSkillDependency:
    id: str
    framework: Framework
    destination: Path
    strategy: str
    dry_run: bool = False


def default_framework_home(framework: Framework) -> Path:
    match framework:
        case Framework.CODEX:
            return Path("~/.codex").expanduser()
        case Framework.CLAUDE_CODE:
            return Path("~/.claude").expanduser()
        case Framework.OPENCODE:
            return Path("~/.agents").expanduser()
        case Framework.CURSOR:
            return Path("~/.cursor").expanduser()
        case Framework.OPENCLAW:
            return Path("~/.openclaw").expanduser()
        case Framework.LOCAL:
            return Path("~/.agentops").expanduser()


def install_skill_dependencies(
    *,
    framework: Framework,
    dependencies: list[SkillDependency],
    home: Path | None = None,
    dependency_ids: list[str] | None = None,
    cache_dir: Path | None = None,
    dry_run: bool = False,
) -> list[InstalledSkillDependency]:
    selected = set(dependency_ids or [])
    by_id = {dependency.id: dependency for dependency in dependencies}
    unknown = sorted(selected - set(by_id))
    if unknown:
        known = ", ".join(sorted(by_id))
        raise ValueError(
            f"unknown skill dependency id(s): {', '.join(unknown)}; known dependencies: {known}"
        )

    unsupported = sorted(
        dependency_id
        for dependency_id in selected
        if framework.value not in by_id[dependency_id].install
    )
    if unsupported:
        raise ValueError(
            f"skill dependency id(s) not supported for {framework.value}: "
            f"{', '.join(unsupported)}"
        )
    if not selected and not any(
        framework.value in dependency.install for dependency in dependencies
    ):
        raise ValueError(f"no skill dependencies support framework {framework.value}")

    target_home = (home or default_framework_home(framework)).expanduser()
    cache_root = (cache_dir or Path("~/.cache/agentops/skill-dependencies")).expanduser()
    installed: list[InstalledSkillDependency] = []

    for dependency in dependencies:
        if selected and dependency.id not in selected:
            continue
        install = dependency.install.get(framework.value)
        if install is None:
            continue
        destination = target_home / install.destination
        installed.append(
            InstalledSkillDependency(
                id=dependency.id,
                framework=framework,
                destination=destination,
                strategy=install.strategy,
                dry_run=dry_run,
            )
        )
        if dry_run:
            continue
        source = _checkout_dependency(dependency, cache_root)
        _install_dependency(
            dependency_id=dependency.id,
            source=source,
            destination=destination,
            install=install,
        )

    return installed


def _checkout_dependency(dependency: SkillDependency, cache_root: Path) -> Path:
    destination = cache_root / f"{dependency.id}-{dependency.ref[:12]}"
    if not (destination / ".git").exists():
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", dependency.repo, str(destination)],
            check=True,
            text=True,
            capture_output=True,
        )
    subprocess.run(
        ["git", "-C", str(destination), "fetch", "--all", "--tags"],
        check=True,
        text=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(destination), "checkout", "--detach", dependency.ref],
        check=True,
        text=True,
        capture_output=True,
    )
    return destination


def _install_dependency(
    *,
    dependency_id: str,
    source: Path,
    destination: Path,
    install: SkillDependencyInstall,
) -> None:
    if install.strategy in {"gstack", "copy-repo"}:
        _replace_tree(source, destination)
        return
    if install.strategy == "copy-skills":
        if install.source is None:
            raise ValueError("copy-skills dependency install requires a source path")
        skill_source = source / install.source
        if not skill_source.exists():
            raise FileNotFoundError(skill_source)
        destination.mkdir(parents=True, exist_ok=True)
        children = sorted(child.name for child in skill_source.iterdir())
        for stale in sorted(set(_read_manifest(destination, dependency_id)) - set(children)):
            _remove_path(destination / stale)
        for child_name in children:
            child = skill_source / child_name
            target = destination / child.name
            if child.is_dir():
                _replace_tree(child, target)
            elif child.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(child, target)
        _write_manifest(destination, dependency_id, children)
        return
    raise ValueError(f"unsupported skill dependency install strategy {install.strategy!r}")


def _replace_tree(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        source,
        destination,
        ignore=shutil.ignore_patterns(".git", "node_modules"),
    )


def _manifest_path(destination: Path, dependency_id: str) -> Path:
    return destination / f".agentops-{dependency_id}-manifest.json"


def _read_manifest(destination: Path, dependency_id: str) -> list[str]:
    path = _manifest_path(destination, dependency_id)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def _write_manifest(destination: Path, dependency_id: str, children: list[str]) -> None:
    _manifest_path(destination, dependency_id).write_text(
        json.dumps(children, indent=2) + "\n",
        encoding="utf-8",
    )


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
