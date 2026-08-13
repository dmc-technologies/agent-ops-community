from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_ops.gstack_prime import install_prime_gstack
from agent_ops.registries.models import Framework, SkillDependency, SkillDependencyInstall
from agent_ops.show_me_adapter import install_show_me
from agent_ops.superpowers_adapter import install_prime_superpowers


@dataclass(frozen=True)
class InstalledSkillDependency:
    id: str
    framework: Framework
    destination: Path
    strategy: str
    dry_run: bool = False


def default_framework_home(framework: Framework) -> Path:
    environment_defaults = {
        Framework.CODEX: ("CODEX_HOME", "~/.codex"),
        Framework.CLAUDE_CODE: ("CLAUDE_CONFIG_DIR", "~/.claude"),
        Framework.PRIME_AGENT: (
            "PRIME_AGENT_CODING_AGENT_DIR",
            "~/.prime/agent",
        ),
    }
    fixed_defaults = {
        Framework.CURSOR: "~/.cursor",
        Framework.OPENCODE: "~/.agents",
        Framework.LOCAL: "~/.agentops",
    }
    if framework is Framework.OPENCLAW:
        return _default_openclaw_home()
    if framework in fixed_defaults:
        return Path(fixed_defaults[framework]).expanduser()
    variable, default = environment_defaults[framework]
    return Path(os.environ.get(variable) or default).expanduser()


def _normalize_home_value(value: str | None) -> str | None:
    normalized = value.strip() if value is not None else ""
    return None if normalized in {"", "undefined", "null"} else normalized


def _native_account_home() -> Path:
    if os.name == "posix":
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 40, None, 0, buffer)
        if result == 0 and buffer.value:
            return Path(buffer.value).resolve()
    return Path.home().resolve()


def _openclaw_os_home() -> Path:
    configured = _normalize_home_value(os.environ.get("HOME"))
    if configured is None:
        configured = _normalize_home_value(os.environ.get("USERPROFILE"))
    if configured is not None:
        return Path(configured).resolve()
    prefix = _normalize_home_value(os.environ.get("PREFIX"))
    android_data = _normalize_home_value(os.environ.get("ANDROID_DATA"))
    if (
        prefix is not None
        and android_data is not None
        and re.search(r"(?:^|/)com\.termux/files/usr/?$", prefix.replace("\\", "/"))
    ):
        return (Path(prefix).resolve().parent / "home").resolve()
    return _native_account_home()


def _default_openclaw_home() -> Path:
    os_home = _openclaw_os_home()
    configured_home = _normalize_home_value(os.environ.get("OPENCLAW_HOME"))
    base_home = (
        _expand_openclaw_path(configured_home, os_home)
        if configured_home is not None
        else os_home
    )
    # OPENCLAW_CONFIG_PATH relocates the managed skills root to the config file's
    # parent when no explicit state directory is selected.
    state_override = _normalize_home_value(os.environ.get("OPENCLAW_STATE_DIR"))
    if state_override is not None:
        return _expand_openclaw_path(state_override, base_home)
    config_path = _normalize_home_value(os.environ.get("OPENCLAW_CONFIG_PATH"))
    if config_path is not None:
        return _expand_openclaw_path(config_path, base_home).parent
    profile = _normalize_home_value(os.environ.get("OPENCLAW_PROFILE"))
    if profile and profile.lower() != "default":
        if not profile[0].isalnum() or len(profile) > 64 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in profile
        ):
            raise ValueError("OPENCLAW_PROFILE must use letters, numbers, '_' or '-'")
        return base_home / f".openclaw-{profile}"
    return base_home / ".openclaw"


def _expand_openclaw_path(value: str, base_home: Path) -> Path:
    if value == "~":
        return base_home
    if value.startswith("~/"):
        return (base_home / value[2:]).resolve()
    if value.startswith("~\\"):
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            return (base_home / value[2:]).resolve()
        return Path(f"{base_home}{value[1:]}").resolve()
    return Path(value).resolve()


def _openclaw_uses_default_state(target_home: Path) -> bool:
    os_home = _openclaw_os_home()
    configured_home = _normalize_home_value(os.environ.get("OPENCLAW_HOME"))
    effective_home = (
        _expand_openclaw_path(configured_home, os_home)
        if configured_home is not None
        else os_home
    )
    state_override = _normalize_home_value(os.environ.get("OPENCLAW_STATE_DIR"))
    config_path = _normalize_home_value(os.environ.get("OPENCLAW_CONFIG_PATH"))
    profile = _normalize_home_value(os.environ.get("OPENCLAW_PROFILE"))
    if state_override is not None or config_path is not None:
        return False
    if profile is not None and profile.lower() != "default":
        return False
    return target_home.resolve() == (effective_home / ".openclaw").resolve()

def _show_me_collision_roots(framework: Framework, target_home: Path) -> tuple[Path, ...]:
    os_home = _openclaw_os_home()
    if framework is Framework.OPENCODE:
        xdg_config = _normalize_home_value(os.environ.get("XDG_CONFIG_HOME"))
        default_config = (
            Path(xdg_config).resolve() if xdg_config is not None else os_home / ".config"
        ) / "opencode"
        configured = _normalize_home_value(os.environ.get("OPENCODE_CONFIG_DIR"))
        roots = [
            default_config / "skills",
            *([Path(configured).resolve() / "skills"] if configured is not None else []),
            os_home / ".claude" / "skills",
            os_home / ".agents" / "skills",
        ]
    elif framework is Framework.OPENCLAW and _openclaw_uses_default_state(target_home):
        roots = [os_home / ".agents" / "skills"]
    else:
        roots = []
    destination_root = (target_home / "skills").resolve()
    return tuple(root.resolve() for root in roots if root.resolve() != destination_root)


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
            framework=framework,
            target_home=target_home,
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
    framework: Framework,
    target_home: Path,
    dependency_id: str,
    source: Path,
    destination: Path,
    install: SkillDependencyInstall,
) -> None:
    if install.strategy == "prime-gstack":
        if dependency_id != "gstack":
            raise ValueError("prime-gstack strategy is only valid for gstack")
        install_prime_gstack(source, destination)
        return
    if install.strategy in {"gstack", "copy-repo"}:
        _replace_tree(source, destination)
        return
    if install.strategy == "prime-superpowers":
        if dependency_id != "superpowers":
            raise ValueError("prime-superpowers strategy is only valid for Superpowers")
        install_prime_superpowers(source, destination)
        return
    if install.strategy == "humanlayer-show-me":
        if dependency_id != "humanlayer-show-me":
            raise ValueError(
                "humanlayer-show-me strategy is only valid for HumanLayer Show Me"
            )
        install_show_me(
            source,
            destination / "show-me",
            collision_roots=_show_me_collision_roots(framework, target_home),
            flat_markdown=framework is Framework.OPENCODE,
        )
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
