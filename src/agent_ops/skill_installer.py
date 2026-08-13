from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import json5

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
        Framework.LOCAL: "~/.agentops",
    }
    if framework is Framework.OPENCLAW:
        return _default_openclaw_home()
    if framework is Framework.OPENCODE:
        return _default_opencode_home()
    if framework in fixed_defaults:
        return Path(fixed_defaults[framework]).expanduser()
    variable, default = environment_defaults[framework]
    return Path(os.environ.get(variable) or default).expanduser()


def _environment_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true"}


def _opencode_xdg_home() -> Path:
    xdg_config = _normalize_home_value(os.environ.get("XDG_CONFIG_HOME"))
    base = Path(xdg_config).expanduser() if xdg_config is not None else Path.home() / ".config"
    return (base / "opencode").resolve()


def _opencode_config_home() -> Path:
    configured = _normalize_home_value(os.environ.get("OPENCODE_CONFIG_DIR"))
    if configured is not None:
        return Path(configured).expanduser().resolve()
    return _opencode_xdg_home()


def _default_opencode_home() -> Path:
    if _environment_truthy("OPENCODE_DISABLE_EXTERNAL_SKILLS"):
        return _opencode_config_home()
    return Path("~/.agents").expanduser()


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


def _openclaw_profile() -> str | None:
    profile = _normalize_home_value(os.environ.get("OPENCLAW_PROFILE"))
    if not profile or profile.lower() == "default":
        return None
    if (
        not profile[0].isalnum()
        or len(profile) > 64
        or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in profile
        )
    ):
        raise ValueError("OPENCLAW_PROFILE must use letters, numbers, '_' or '-'")
    return profile


def _default_openclaw_home() -> Path:
    base_home = _openclaw_effective_home()
    state_override = _normalize_home_value(os.environ.get("OPENCLAW_STATE_DIR"))
    if state_override is not None:
        return _expand_openclaw_path(state_override, base_home)
    profile = _openclaw_profile()
    if profile is not None:
        return base_home / f".openclaw-{profile}"
    # Without profile projection, an explicit config path defines CONFIG_DIR,
    # which is also the managed-skills directory.
    config_path = _normalize_home_value(os.environ.get("OPENCLAW_CONFIG_PATH"))
    if config_path is not None:
        return _expand_openclaw_path(config_path, base_home).parent
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


def _load_json5_value(path: Path) -> object:
    if not path.is_file():
        return {}
    try:
        return json5.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read active host configuration {path}: {exc}") from exc


def _load_json5_object(path: Path) -> dict[str, object]:
    loaded = _load_json5_value(path)
    if not isinstance(loaded, dict):
        raise ValueError(f"active host configuration must be an object: {path}")
    return loaded


def _deep_merge_openclaw_config(base: object, override: object) -> object:
    if isinstance(base, dict) and isinstance(override, dict):
        merged = dict(base)
        for key, value in override.items():
            merged[key] = (
                _deep_merge_openclaw_config(merged[key], value) if key in merged else value
            )
        return merged
    if isinstance(base, list) and isinstance(override, list):
        return [*base, *override]
    return override


def _openclaw_include_roots(config_path: Path) -> tuple[Path, ...]:
    roots = [config_path.resolve().parent]
    configured = _normalize_home_value(os.environ.get("OPENCLAW_INCLUDE_ROOTS"))
    if configured is not None:
        for item in configured.split(os.pathsep):
            authored = item.strip()
            if not authored:
                continue
            if authored == "~" or authored.startswith(("~/", "~\\")):
                candidate = _expand_openclaw_path(authored, _openclaw_effective_home())
            else:
                candidate = Path(authored)
                if not candidate.is_absolute():
                    raise ValueError("OPENCLAW_INCLUDE_ROOTS entries must be absolute")
            roots.append(candidate.resolve())
    return tuple(roots)


def _path_is_within_roots(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


def _resolve_openclaw_includes(
    value: object,
    *,
    base_path: Path,
    roots: tuple[Path, ...],
    chain: tuple[Path, ...],
) -> object:
    if isinstance(value, list):
        return [
            _resolve_openclaw_includes(item, base_path=base_path, roots=roots, chain=chain)
            for item in value
        ]
    if not isinstance(value, dict):
        return value
    include = value.get("$include")
    if include is None:
        return {
            key: _resolve_openclaw_includes(item, base_path=base_path, roots=roots, chain=chain)
            for key, item in value.items()
        }
    include_items = [include] if isinstance(include, str) else include
    if not isinstance(include_items, list) or any(
        not isinstance(item, str) for item in include_items
    ):
        raise ValueError("OpenClaw $include must be a string or string list")
    merged: object = {}
    for item in include_items:
        include_path = Path(item)
        if not include_path.is_absolute():
            include_path = base_path.parent / include_path
        include_path = include_path.resolve()
        if not _path_is_within_roots(include_path, roots):
            raise ValueError(f"OpenClaw config include escapes allowed roots: {include_path}")
        if include_path in chain:
            raise ValueError(f"circular OpenClaw config include: {include_path}")
        if len(chain) >= 10:
            raise ValueError("OpenClaw config include depth exceeds 10")
        loaded = _load_json5_value(include_path)
        merged = _deep_merge_openclaw_config(
            merged,
            _resolve_openclaw_includes(
                loaded,
                base_path=include_path,
                roots=roots,
                chain=(*chain, include_path),
            ),
        )
    siblings = {
        key: _resolve_openclaw_includes(item, base_path=base_path, roots=roots, chain=chain)
        for key, item in value.items()
        if key != "$include"
    }
    if siblings:
        if not isinstance(merged, dict):
            raise ValueError("OpenClaw $include sibling keys require an included object")
        merged = _deep_merge_openclaw_config(merged, siblings)
    return merged


def _load_openclaw_config(path: Path) -> dict[str, object]:
    normalized = path.resolve()
    loaded = _load_json5_value(normalized)
    resolved = _resolve_openclaw_includes(
        loaded,
        base_path=normalized,
        roots=_openclaw_include_roots(normalized),
        chain=(normalized,),
    )
    if not isinstance(resolved, dict):
        raise ValueError(f"active host configuration must be an object: {path}")
    return resolved


def _nested_string_list(config: dict[str, object], *keys: str) -> tuple[str, ...]:
    current: object = config
    for key in keys:
        if not isinstance(current, dict):
            return ()
        current = current.get(key)
    if current is None:
        return ()
    if not isinstance(current, list) or any(not isinstance(item, str) for item in current):
        raise ValueError(f"active host configuration {'.'.join(keys)} must be a string list")
    return tuple(item.strip() for item in current if item.strip())


def _deep_merge_host_config(
    base: dict[str, object], override: dict[str, object]
) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_host_config(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged


def _expand_opencode_config_value(value: str, *, config_dir: Path) -> str:
    def env_replace(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    expanded = re.sub(r"\{env:([^}]+)\}", env_replace, value)

    def file_replace(match: re.Match[str]) -> str:
        authored = match.group(1)
        candidate = Path(authored)
        if authored.startswith("~/"):
            candidate = Path.home() / authored[2:]
        elif not candidate.is_absolute():
            candidate = config_dir / candidate
        try:
            return candidate.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(
                f"cannot read OpenCode config file reference {candidate}: {exc}"
            ) from exc

    return re.sub(r"\{file:([^}]+)\}", file_replace, expanded)


def _opencode_managed_config_dir() -> Path:
    test_override = _normalize_home_value(os.environ.get("OPENCODE_TEST_MANAGED_CONFIG_DIR"))
    if test_override is not None:
        return Path(test_override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path("/Library/Application Support/opencode")
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        program_data = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
        return Path(program_data) / "opencode"
    return Path("/etc/opencode")


def _opencode_managed_preferences() -> dict[str, object]:
    if sys.platform != "darwin":
        return {}
    try:
        import getpass

        user = getpass.getuser()
    except (ImportError, OSError):  # pragma: no cover - defensive fallback
        user = "user"
    candidates = [
        Path("/Library/Managed Preferences") / user / "ai.opencode.managed.plist",
        Path("/Library/Managed Preferences/ai.opencode.managed.plist"),
    ]
    for candidate in candidates:
        if not candidate.exists():
            continue
        result = subprocess.run(
            ["plutil", "-convert", "json", "-o", "-", str(candidate)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"cannot read OpenCode managed preferences {candidate}")
        try:
            value = json.loads(result.stdout)
        except ValueError as exc:
            raise ValueError(f"cannot parse OpenCode managed preferences {candidate}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"OpenCode managed preferences must be an object: {candidate}")
        for key in (
            "PayloadDisplayName",
            "PayloadIdentifier",
            "PayloadType",
            "PayloadUUID",
            "PayloadVersion",
            "_manualProfile",
        ):
            value.pop(key, None)
        return value
    return {}


def _opencode_data_home() -> Path:
    configured = _normalize_home_value(os.environ.get("XDG_DATA_HOME"))
    base = (
        Path(configured).expanduser()
        if configured is not None
        else Path.home() / ".local" / "share"
    )
    return (base / "opencode").resolve()


def _opencode_selected_database() -> Path:
    authored = _normalize_home_value(os.environ.get("OPENCODE_DB"))
    if authored is None:
        return _opencode_data_home() / "opencode.db"
    if authored == ":memory:":
        raise ValueError("in-memory OpenCode database cannot be inspected reproducibly")
    selected = Path(authored).expanduser()
    return selected if selected.is_absolute() else _opencode_data_home() / selected


def _reject_uninspectable_opencode_remote_config() -> None:
    auth_path = _opencode_data_home() / "auth.json"
    if auth_path.is_file():
        try:
            auth = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"cannot inspect OpenCode remote configuration state: {auth_path}"
            ) from exc
        if isinstance(auth, dict) and any(
            isinstance(value, dict) and value.get("type") == "wellknown" for value in auth.values()
        ):
            raise ValueError(
                "OpenCode well-known remote configuration cannot be inspected reproducibly "
                "by the global installer"
            )
    database = _opencode_selected_database().absolute()
    if not database.exists():
        if _normalize_home_value(os.environ.get("OPENCODE_DB")) is not None:
            raise ValueError(f"cannot inspect selected OpenCode database: {database}")
        return
    try:
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
            row = connection.execute(
                "SELECT active_org_id FROM account_state WHERE active_org_id IS NOT NULL LIMIT 1"
            ).fetchone()
    except sqlite3.Error as exc:
        raise ValueError(f"cannot inspect selected OpenCode database: {database}") from exc
    if row:
        raise ValueError(
            "OpenCode active-organization remote configuration cannot be inspected "
            "reproducibly by the global installer"
        )


def _reject_uninspectable_opencode_skill_urls(config: dict[str, object]) -> None:
    urls = _nested_string_list(config, "skills", "urls")
    if urls:
        raise ValueError(
            "OpenCode skills.urls cannot be inspected reproducibly by the global installer; "
            "remove the remote skill source before global installation"
        )


def _opencode_configured_skill_roots() -> tuple[Path, ...]:
    config: dict[str, object] = {}
    paths_config_dir = _opencode_xdg_home()
    xdg_home = _opencode_xdg_home()
    config_home = _opencode_config_home()
    config_files = [
        xdg_home / "config.json",
        xdg_home / "opencode.json",
        xdg_home / "opencode.jsonc",
    ]
    if config_home != xdg_home:
        config_files.extend([config_home / "opencode.json", config_home / "opencode.jsonc"])
    custom = _normalize_home_value(os.environ.get("OPENCODE_CONFIG"))
    if custom is not None:
        config_files.append(Path(custom).expanduser())
    for config_file in config_files:
        loaded = _load_json5_object(config_file)
        if _nested_string_list(loaded, "skills", "paths"):
            paths_config_dir = config_file.resolve().parent
        config = _deep_merge_host_config(config, loaded)
    content = _normalize_home_value(os.environ.get("OPENCODE_CONFIG_CONTENT"))
    if content is not None:
        try:
            loaded = json5.loads(content)
        except ValueError as exc:
            raise ValueError(f"cannot parse OPENCODE_CONFIG_CONTENT: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("OPENCODE_CONFIG_CONTENT must be an object")
        if _nested_string_list(loaded, "skills", "paths"):
            paths_config_dir = Path.cwd()
        config = _deep_merge_host_config(config, loaded)
    managed_dir = _opencode_managed_config_dir()
    for config_file in (
        managed_dir / "opencode.json",
        managed_dir / "opencode.jsonc",
    ):
        loaded = _load_json5_object(config_file)
        if _nested_string_list(loaded, "skills", "paths"):
            paths_config_dir = config_file.resolve().parent
        config = _deep_merge_host_config(config, loaded)
    managed_preferences = _opencode_managed_preferences()
    if _nested_string_list(managed_preferences, "skills", "paths"):
        paths_config_dir = Path("/Library/Managed Preferences")
    config = _deep_merge_host_config(config, managed_preferences)
    _reject_uninspectable_opencode_remote_config()
    _reject_uninspectable_opencode_skill_urls(config)
    roots = []
    for authored in _nested_string_list(config, "skills", "paths"):
        item = _expand_opencode_config_value(authored, config_dir=paths_config_dir)
        # OpenCode resolves relative configured skill roots against each active workspace.
        # A global installer cannot enumerate those project-scoped paths, so reject them
        # rather than claim globally collision-safe installation.
        if item == "~":
            roots.append(Path.home())
        elif item.startswith("~/") or (os.name == "nt" and item.startswith("~\\")):
            roots.append(Path.home() / item[2:])
        elif Path(item).is_absolute():
            roots.append(Path(item))
        else:
            raise ValueError(
                "OpenCode skills.paths contains a workspace-relative path; "
                "use an absolute or home-relative path for global installation"
            )
    return tuple(root.resolve() for root in roots)


def _openclaw_effective_home() -> Path:
    os_home = _openclaw_os_home()
    configured = _normalize_home_value(os.environ.get("OPENCLAW_HOME"))
    return _expand_openclaw_path(configured, os_home) if configured is not None else os_home


def _openclaw_active_config_path(target_state: Path | None = None) -> Path:
    effective_home = _openclaw_effective_home()
    configured = _normalize_home_value(os.environ.get("OPENCLAW_CONFIG_PATH"))
    if configured is not None:
        return _expand_openclaw_path(configured, effective_home)
    state_override = _normalize_home_value(os.environ.get("OPENCLAW_STATE_DIR"))
    profile = _openclaw_profile()
    if target_state is not None:
        selected_state = target_state.expanduser().resolve()
    elif state_override is not None:
        selected_state = _expand_openclaw_path(state_override, effective_home)
    elif profile is not None:
        selected_state = effective_home / f".openclaw-{profile}"
    else:
        selected_state = effective_home / ".openclaw"
    state_dirs = [selected_state]
    if target_state is None and profile is None and state_override is None:
        state_dirs.append(effective_home / ".clawdbot")
    candidates = [
        state_dir / filename
        for state_dir in state_dirs
        for filename in ("openclaw.json", "clawdbot.json")
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return selected_state / "openclaw.json"


def _expand_openclaw_config_environment(value: str, config: dict[str, object]) -> str:
    escaped = "\0AGENTOPS_OPENCLAW_ESCAPED\0"
    value = value.replace("$${", escaped + "{")
    configured_env: dict[str, str] = {}
    raw_env = config.get("env")
    if isinstance(raw_env, dict):
        raw_vars = raw_env.get("vars")
        candidates = {key: item for key, item in raw_env.items() if key not in {"vars", "shellEnv"}}
        if isinstance(raw_vars, dict):
            candidates.update(raw_vars)
        configured_env = {
            key: item
            for key, item in candidates.items()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
            and isinstance(item, str)
            and item.strip()
            and "${" not in item
        }

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        replacement = os.environ.get(name) or configured_env.get(name)
        if replacement is None or replacement == "":
            raise ValueError(f"OpenClaw skill root references missing environment variable {name}")
        return replacement

    expanded = re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", replace, value)
    return expanded.replace(escaped + "{", "${")


def _openclaw_plugin_candidate_paths(
    config: dict[str, object], target_state: Path
) -> tuple[Path, ...]:
    effective_home = _openclaw_effective_home()
    paths = [
        _expand_openclaw_path(_expand_openclaw_config_environment(item, config), effective_home)
        for item in _nested_string_list(config, "plugins", "load", "paths")
    ]
    roots = [target_state / "extensions"]
    agents = config.get("agents")
    agent_list = agents.get("list") if isinstance(agents, dict) else None
    if isinstance(agent_list, list):
        for agent in agent_list:
            if not isinstance(agent, dict) or not isinstance(agent.get("workspace"), str):
                continue
            workspace = _expand_openclaw_path(
                _expand_openclaw_config_environment(agent["workspace"], config),
                effective_home,
            )
            roots.append(workspace / ".openclaw" / "extensions")
    for root in roots:
        if root.is_dir():
            paths.extend(sorted(root.iterdir(), key=lambda candidate: candidate.name))
    return tuple(paths)


def _openclaw_plugin_manifest(path: Path) -> tuple[str, list[str]]:
    root = path if path.is_dir() else path.parent
    manifest = root / "openclaw.plugin.json"
    if not manifest.is_file():
        raise ValueError(f"cannot inspect OpenClaw plugin manifest: {path}")
    try:
        loaded = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot inspect OpenClaw plugin manifest: {manifest}") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("id"), str):
        raise ValueError(f"cannot inspect OpenClaw plugin manifest: {manifest}")
    skills = loaded.get("skills", [])
    if not isinstance(skills, list) or not all(isinstance(item, str) for item in skills):
        raise ValueError(f"cannot inspect OpenClaw plugin skill inventory: {manifest}")
    return loaded["id"], skills


def _reject_uninspectable_openclaw_plugin_skills(
    config: dict[str, object], target_state: Path
) -> None:
    plugins = config.get("plugins")
    if isinstance(plugins, dict) and plugins.get("enabled") is False:
        return
    entries = plugins.get("entries", {}) if isinstance(plugins, dict) else {}
    deny = set(_nested_string_list(config, "plugins", "deny"))
    allow = set(_nested_string_list(config, "plugins", "allow"))
    for candidate in _openclaw_plugin_candidate_paths(config, target_state):
        plugin_id, skills = _openclaw_plugin_manifest(candidate)
        entry = entries.get(plugin_id) if isinstance(entries, dict) else None
        if plugin_id in deny or (allow and plugin_id not in allow):
            continue
        if isinstance(entry, dict) and entry.get("enabled") is False:
            continue
        if skills:
            raise ValueError(
                "OpenClaw plugin skill inventory cannot be inspected reproducibly by the "
                "global installer; disable plugin skill sources before global installation"
            )


def _openclaw_configured_skill_roots(target_state: Path) -> tuple[Path, ...]:
    config = _load_openclaw_config(_openclaw_active_config_path(target_state))
    _reject_uninspectable_openclaw_plugin_skills(config, target_state)
    effective_home = _openclaw_effective_home()
    roots = []
    for item in _nested_string_list(config, "skills", "load", "extraDirs"):
        roots.append(
            _expand_openclaw_path(_expand_openclaw_config_environment(item, config), effective_home)
        )
    return tuple(root.resolve() for root in roots)


def _show_me_collision_policy(framework: Framework) -> str:
    if framework is Framework.OPENCLAW:
        return "openclaw"
    if framework is Framework.OPENCODE:
        return "opencode"
    if framework is Framework.CODEX:
        return "codex"
    return "generic"


def _openclaw_collision_options(
    target_state: Path,
) -> tuple[dict[str, int], tuple[Path, ...]]:
    config = _load_openclaw_config(_openclaw_active_config_path(target_state))
    limits: dict[str, int] = {}
    raw_limits: object = config.get("skills", {})
    raw_limits = raw_limits.get("limits", {}) if isinstance(raw_limits, dict) else {}
    defaults = {
        "maxCandidatesPerRoot": 300,
        "maxSkillsLoadedPerSource": 200,
        "maxSkillFileBytes": 256_000,
    }
    for key, default in defaults.items():
        value = raw_limits.get(key, default) if isinstance(raw_limits, dict) else default
        minimum = 0 if key == "maxSkillFileBytes" else 1
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ValueError(f"active host configuration skills.limits.{key} is invalid")
        limits[key] = value
    effective_home = _openclaw_effective_home()
    allowed = tuple(
        _expand_openclaw_path(
            _expand_openclaw_config_environment(item, config), effective_home
        ).resolve()
        for item in _nested_string_list(config, "skills", "load", "allowSymlinkTargets")
    )
    return limits, allowed


def _codex_system_skill_root() -> Path:
    if os.name == "nt":  # pragma: no cover - exercised on Windows
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        result = ctypes.windll.shell32.SHGetFolderPathW(None, 35, None, 0, buffer)
        program_data = (
            Path(buffer.value) if result == 0 and buffer.value else Path(r"C:\ProgramData")
        )
        return program_data / "OpenAI" / "Codex" / "skills"
    return Path("/etc/codex/skills")


def _openclaw_uses_default_state(target_state: Path) -> bool:
    del target_state
    state_override = _normalize_home_value(os.environ.get("OPENCLAW_STATE_DIR"))
    if _openclaw_profile() is not None:
        return False
    if state_override is None:
        return True
    selected = _expand_openclaw_path(state_override, _openclaw_effective_home())
    return selected.resolve() == (_openclaw_effective_home() / ".openclaw").resolve()


def _openclaw_workspace_roots(target_state: Path) -> tuple[Path, ...]:
    config = _load_openclaw_config(_openclaw_active_config_path(target_state))
    effective_home = _openclaw_effective_home()
    configured_default = None
    agents = config.get("agents")
    if isinstance(agents, dict):
        defaults = agents.get("defaults")
        if isinstance(defaults, dict) and isinstance(defaults.get("workspace"), str):
            configured_default = _expand_openclaw_path(
                _expand_openclaw_config_environment(defaults["workspace"], config),
                effective_home,
            )
        agent_list = agents.get("list")
    else:
        agent_list = None
    workspaces: list[Path] = []
    if isinstance(agent_list, list) and agent_list:
        for agent in agent_list:
            if not isinstance(agent, dict):
                raise ValueError("cannot inspect configured OpenClaw agent workspace")
            agent_id = agent.get("id")
            authored = agent.get("workspace")
            if isinstance(authored, str):
                workspace = _expand_openclaw_path(
                    _expand_openclaw_config_environment(authored, config),
                    effective_home,
                )
            elif configured_default is not None and isinstance(agent_id, str):
                workspace = configured_default / agent_id
            elif isinstance(agent_id, str):
                workspace = target_state / f"workspace-{agent_id}"
            else:
                raise ValueError("cannot inspect configured OpenClaw agent workspace")
            workspaces.append(workspace)
    else:
        authored = _normalize_home_value(os.environ.get("OPENCLAW_WORKSPACE_DIR"))
        if authored is not None:
            workspaces.append(_expand_openclaw_path(authored, effective_home))
        elif configured_default is not None:
            workspaces.append(configured_default)
        else:
            workspaces.append(_openclaw_os_home() / ".openclaw" / "workspace")
    roots = []
    for workspace in workspaces:
        roots.extend([workspace / "skills", workspace / ".agents" / "skills"])
    return tuple(roots)


def _show_me_collision_roots(framework: Framework, target_home: Path) -> tuple[Path, ...]:
    os_home = _openclaw_os_home()
    if framework is Framework.CODEX:
        roots = [os_home / ".agents" / "skills", _codex_system_skill_root()]
    elif framework is Framework.CURSOR:
        roots = [
            os_home / ".agents" / "skills",
            os_home / ".claude" / "skills",
            os_home / ".codex" / "skills",
        ]
    elif framework is Framework.OPENCODE:
        config_roots = {_opencode_xdg_home(), _opencode_config_home()}
        roots = [child for root in config_roots for child in (root / "skill", root / "skills")]
        roots.extend(_opencode_configured_skill_roots())
        if not _environment_truthy("OPENCODE_DISABLE_EXTERNAL_SKILLS"):
            roots.append(os_home / ".agents" / "skills")
            if not (
                _environment_truthy("OPENCODE_DISABLE_CLAUDE_CODE")
                or _environment_truthy("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS")
            ):
                roots.append(os_home / ".claude" / "skills")
    elif framework is Framework.OPENCLAW:
        roots = list(_openclaw_configured_skill_roots(target_home))
        roots.extend(_openclaw_workspace_roots(target_home))
        if _openclaw_uses_default_state(target_home):
            roots.append(os_home / ".agents" / "skills")
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
            f"skill dependency id(s) not supported for {framework.value}: {', '.join(unsupported)}"
        )
    if not selected and not any(
        framework.value in dependency.install for dependency in dependencies
    ):
        raise ValueError(f"no skill dependencies support framework {framework.value}")

    target_home = (home or default_framework_home(framework)).expanduser()
    cache_root = (cache_dir or Path("~/.cache/agentops/skill-dependencies")).expanduser()
    installed: list[InstalledSkillDependency] = []

    show_me_selected = any(
        dependency.id == "humanlayer-show-me"
        and (not selected or dependency.id in selected)
        and framework.value in dependency.install
        for dependency in dependencies
    )
    if show_me_selected and not dry_run:
        _show_me_collision_roots(framework, target_home)

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
            raise ValueError("humanlayer-show-me strategy is only valid for HumanLayer Show Me")
        collision_limits: dict[str, int] | None = None
        collision_allowed_symlink_targets: tuple[Path, ...] = ()
        if framework is Framework.OPENCLAW:
            collision_limits, collision_allowed_symlink_targets = _openclaw_collision_options(
                target_home
            )
        install_show_me(
            source,
            destination / "show-me",
            collision_roots=_show_me_collision_roots(framework, target_home),
            flat_markdown=framework is Framework.OPENCODE,
            collision_policy=_show_me_collision_policy(framework),
            collision_limits=collision_limits,
            collision_allowed_symlink_targets=collision_allowed_symlink_targets,
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
