from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path, PurePosixPath, PureWindowsPath

from agent_ops.deployment.models import PlannedFile, ProviderPlan, TargetSpec
from agent_ops.registries.models import Framework, SkillDependency, SkillDependencyInstall
from agent_ops.show_me_adapter import ShowMeCollisionError

CheckoutDependency = Callable[[SkillDependency, Path], Path]
InstallDependency = Callable[..., None]

_SHADOW_PATHS = (
    Path("skills"),
    Path("dependencies"),
    Path(".agentops/gstack-prime-manifest.json"),
    Path(".agentops/runtime/gstack"),
    Path(".agentops/skill-dependencies"),
)
_SOURCE_METADATA_DIRECTORIES = {
    ".cache",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}
_MANIFEST_KEYS = {
    "schema_version",
    "target_id",
    "framework",
    "source_revision",
    "provider_ids",
    "transaction_id",
    "directories",
    "files",
}
PROVIDER_INDEX_PATH = Path("skills/.agentops-public-provider-index.json")
_PROVIDER_INDEX_MODE = 0o600


def build_public_skill_plans(
    *,
    framework: Framework,
    dependencies: list[SkillDependency],
    target_home: Path,
    cache_root: Path,
    checkout_dependency: CheckoutDependency,
    install_dependency: InstallDependency | None = None,
) -> tuple[ProviderPlan, ...]:
    """Resolve and render every public bundle before returning immutable plans."""

    if install_dependency is None:
        install_dependency = _default_install_dependency
    target_home = target_home.expanduser()
    cache_root = cache_root.expanduser()
    captured_cwd = Path.cwd()
    _validate_cache_target_separation(
        cache_root=_captured_absolute_path(cache_root, cwd=captured_cwd),
        target_home=_captured_absolute_path(target_home, cwd=captured_cwd),
    )
    target = TargetSpec(
        id=f"public-skills:{framework.value}",
        framework=framework,
        home=target_home,
        channel="public",
    )
    _validate_target_ancestors(
        target_home,
        show_me=any(dependency.id == "humanlayer-show-me" for dependency in dependencies),
    )
    prior_files = _prior_shared_files(target)
    _validate_prior_shared_files(
        target,
        prior_files,
        show_me=any(dependency.id == "humanlayer-show-me" for dependency in dependencies),
    )
    prior_paths = set(prior_files)
    prior_index = _prior_provider_index(target, prior_paths)
    plans: list[ProviderPlan] = []
    for dependency in dependencies:
        install = dependency.install[framework.value]
        source = checkout_dependency(dependency, cache_root)
        _validate_checkout(dependency, source, install)
        plan = _render_dependency(
            framework=framework,
            dependency=dependency,
            install=install,
            source=source,
            target=target,
            prior_paths=prior_paths,
            cache_root=cache_root,
            install_dependency=install_dependency,
        )
        plans.append(plan)

    return _complete_provider_plans(
        target=target,
        dependencies=dependencies,
        selected_plans=plans,
        prior_files=prior_files,
        prior_index=prior_index,
    )


def _captured_absolute_path(path: Path, *, cwd: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = cwd / expanded
    return Path(os.path.normcase(os.path.abspath(os.fspath(expanded))))


def _validate_cache_target_separation(*, cache_root: Path, target_home: Path) -> None:
    candidates = [(cache_root, target_home)]
    with suppress(OSError, RuntimeError):
        candidates.append((cache_root.resolve(strict=False), target_home.resolve(strict=False)))
    for cache, target in candidates:
        if cache == target or cache.is_relative_to(target) or target.is_relative_to(cache):
            raise ValueError(
                "dependency cache and selected target home must not overlap: "
                f"cache={cache_root}, target={target_home}"
            )


def _complete_provider_plans(
    *,
    target: TargetSpec,
    dependencies: list[SkillDependency],
    selected_plans: list[ProviderPlan],
    prior_files: dict[Path, tuple[str, int]],
    prior_index: dict[str, dict[str, object]],
) -> tuple[ProviderPlan, ...]:
    selected = {plan.provider_id: plan for plan in selected_plans}
    revisions = {f"public-skill:{dependency.id}": dependency.ref for dependency in dependencies}
    descriptors = {
        f"public-skill:{dependency.id}": {
            "id": dependency.id,
            "repo": dependency.repo,
            "ref": dependency.ref,
            "install": dependency.install[target.framework.value].model_dump(mode="json"),
        }
        for dependency in dependencies
    }
    if prior_files and not prior_index:
        if len(selected_plans) != 1:
            raise ValueError("shared ownership manifest is missing public provider ownership")
        provider_id = selected_plans[0].provider_id
        prior_index = {
            provider_id: {
                "provider_id": provider_id,
                "source_revision": revisions[provider_id],
                "source_descriptor": descriptors[provider_id],
                "paths": sorted(
                    path.as_posix() for path in prior_files if path != PROVIDER_INDEX_PATH
                ),
            }
        }

    complete = list(selected_plans)
    for provider_id, entry in sorted(prior_index.items()):
        if provider_id in selected:
            continue
        files = tuple(
            PlannedFile(
                Path(path),
                (target.home / path).read_bytes(),
                prior_files[Path(path)][1],
            )
            for path in entry["paths"]
        )
        complete.append(
            ProviderPlan(
                provider_id,
                str(entry["source_revision"]),
                target,
                files,
            )
        )
        revisions[provider_id] = str(entry["source_revision"])
        descriptors[provider_id] = entry["source_descriptor"]

    ownership: dict[str, tuple[Path, ...]] = {}
    claimed: set[Path] = set()
    for plan in complete:
        paths = tuple(sorted((item.path for item in plan.files), key=lambda path: path.as_posix()))
        if PROVIDER_INDEX_PATH in paths:
            raise ValueError("rendered bundle overlaps public provider ownership index")
        overlap = claimed.intersection(paths)
        if overlap:
            raise ValueError(f"public provider ownership overlaps: {min(overlap)}")
        claimed.update(paths)
        ownership[plan.provider_id] = paths

    for index, plan in enumerate(complete):
        prior_owned = {
            Path(path) for path in prior_index.get(plan.provider_id, {}).get("paths", [])
        }
        if plan.provider_id in selected:
            removals = tuple(
                sorted(
                    prior_owned - set(ownership[plan.provider_id]), key=lambda path: path.as_posix()
                )
            )
            complete[index] = replace(plan, removals=removals)

    index_content = _provider_index_bytes(target, ownership, revisions, descriptors)
    owner = min(range(len(complete)), key=lambda item: complete[item].provider_id)
    complete[owner] = replace(
        complete[owner],
        files=(
            *complete[owner].files,
            PlannedFile(PROVIDER_INDEX_PATH, index_content, _PROVIDER_INDEX_MODE),
        ),
    )
    aggregate_revision = "public-skills:" + json.dumps(
        [
            descriptors[provider_id]
            for provider_id in sorted(ownership)
        ],
        separators=(",", ":"),
        sort_keys=True,
    )
    return tuple(replace(plan, source_revision=aggregate_revision) for plan in complete)


def _provider_index_bytes(
    target: TargetSpec,
    ownership: dict[str, tuple[Path, ...]],
    revisions: dict[str, str],
    descriptors: dict[str, object],
) -> bytes:
    data = {
        "schema_version": 1,
        "target_id": target.id,
        "framework": target.framework.value,
        "providers": [
            {
                "provider_id": provider_id,
                "source_revision": revisions[provider_id],
                "source_descriptor": descriptors[provider_id],
                "paths": [path.as_posix() for path in ownership[provider_id]],
            }
            for provider_id in sorted(ownership)
        ],
    }
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()


def _source_closure(source: Path, install: SkillDependencyInstall) -> tuple[Path, ...]:
    if install.strategy in {"copy-skills", "prime-superpowers"}:
        if install.source is None:
            raise ValueError(f"{install.strategy} dependency install requires a source path")
        return (source / install.source,)
    if install.strategy == "humanlayer-show-me":
        return (source / "plugins/show-me/skills/show-me", source / "LICENSE")
    return (source,)


def _validate_checkout(
    dependency: SkillDependency,
    source: Path,
    install: SkillDependencyInstall,
) -> None:
    source = Path(source)
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"dependency source is not a regular directory: {source}")
    if (source / ".git").exists():
        git_environment = _read_only_git_environment(source)
        head = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            env=git_environment,
        ).stdout.strip()
        if head != dependency.ref:
            raise ValueError(
                f"pinned {dependency.id} checkout is at {head}, expected {dependency.ref}"
            )
        status_output = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=git_environment,
        ).stdout
        if status_output:
            raise ValueError(f"pinned {dependency.id} checkout contains changed or untracked files")
    for closure in _source_closure(source, install):
        _validate_regular_closure(closure, source_root=source)


def _read_only_git_environment(source: Path) -> dict[str, str]:
    inherited = (
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "COMSPEC",
        "WINDIR",
        "LANG",
        "LC_ALL",
        "TZ",
    )
    environment = {name: os.environ[name] for name in inherited if name in os.environ}
    confined = source.parent
    environment.update(
        {
            "HOME": str(confined),
            "XDG_CONFIG_HOME": str(confined),
            "TMPDIR": str(confined),
            "TMP": str(confined),
            "TEMP": str(confined),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(source / ".git/.agentops-disabled-hooks"),
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": "false",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _validate_regular_closure(root: Path, *, source_root: Path) -> None:
    source_root = source_root.resolve()
    _validate_materialized_source_entry(root, source_root=source_root, active_directories=set())


def _validate_materialized_source_entry(
    item: Path,
    *,
    source_root: Path,
    active_directories: set[tuple[int, int]],
) -> None:
    try:
        mode = item.lstat().st_mode
        resolved = item.resolve(strict=True) if stat.S_ISLNK(mode) else item.resolve()
        relative = resolved.relative_to(source_root)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"unsupported source entry: {item}") from exc
    if any(part in _SOURCE_METADATA_DIRECTORIES for part in relative.parts):
        raise ValueError(f"unsupported source entry: {item}")
    resolved_mode = resolved.stat().st_mode
    if stat.S_ISREG(resolved_mode):
        return
    if not stat.S_ISDIR(resolved_mode):
        raise ValueError(f"unsupported source entry: {item}")
    identity = (resolved.stat().st_dev, resolved.stat().st_ino)
    if identity in active_directories:
        raise ValueError(f"unsupported source entry: {item}")
    active_directories.add(identity)
    try:
        for child in sorted(resolved.iterdir(), key=lambda path: path.name):
            if child.name in _SOURCE_METADATA_DIRECTORIES:
                continue
            _validate_materialized_source_entry(
                child,
                source_root=source_root,
                active_directories=active_directories,
            )
    finally:
        active_directories.remove(identity)


def _render_dependency(
    *,
    framework: Framework,
    dependency: SkillDependency,
    install: SkillDependencyInstall,
    source: Path,
    target: TargetSpec,
    prior_paths: set[Path],
    cache_root: Path,
    install_dependency: InstallDependency,
) -> ProviderPlan:
    cache_created = False
    temporary_parent = None
    if install.strategy == "prime-gstack":
        if not cache_root.exists():
            cache_root.mkdir(parents=True)
            cache_created = True
        temporary_parent = cache_root
    try:
        temporary_context = tempfile.TemporaryDirectory(
            prefix=f"agentops-public-{dependency.id}-",
            dir=temporary_parent,
        )
        with temporary_context as temporary:
            return _render_dependency_in_workspace(
                temporary=temporary,
                framework=framework,
                dependency=dependency,
                install=install,
                source=source,
                target=target,
                prior_paths=prior_paths,
                install_dependency=install_dependency,
            )
    finally:
        if cache_created:
            with suppress(OSError):
                cache_root.rmdir()


def _render_dependency_in_workspace(
    *,
    temporary: str,
    framework: Framework,
    dependency: SkillDependency,
    install: SkillDependencyInstall,
    source: Path,
    target: TargetSpec,
    prior_paths: set[Path],
    install_dependency: InstallDependency,
) -> ProviderPlan:
    temporary_root = Path(temporary).resolve()
    shadow_home = temporary_root / "home"
    _copy_shadow_state(target.home, shadow_home)
    _remove_prior_shared_files(shadow_home, prior_paths)
    if prior_paths:
        _remove_superseded_legacy_state(shadow_home, dependency.id)
    destination = shadow_home / install.destination
    install_dependency(
        framework=framework,
        target_home=shadow_home,
        context_home=target.home,
        dependency_id=dependency.id,
        source=source,
        destination=destination,
        install=install,
        renderer_env=(
            _renderer_environment(temporary_root / "renderer-environment")
            if install.strategy == "prime-gstack"
            else None
        ),
    )
    files = _managed_files(
        dependency=dependency,
        install=install,
        source=source,
        shadow_home=shadow_home,
        target_home=target.home,
    )
    return ProviderPlan(
        provider_id=f"public-skill:{dependency.id}",
        source_revision=dependency.ref,
        target=target,
        files=files,
    )


def _renderer_environment(workspace: Path) -> dict[str, str]:
    inherited = {
        name: os.environ[name]
        for name in (
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "COMSPEC",
            "WINDIR",
            "LANG",
            "LC_ALL",
            "TZ",
        )
        if name in os.environ
    }
    paths = {
        "HOME": workspace / "home",
        "BUN_INSTALL": workspace / "bun/install",
        "BUN_INSTALL_CACHE_DIR": workspace / "bun/cache",
        "XDG_CACHE_HOME": workspace / "xdg/cache",
        "XDG_CONFIG_HOME": workspace / "xdg/config",
        "XDG_DATA_HOME": workspace / "xdg/data",
        "TMPDIR": workspace / "tmp",
        "TMP": workspace / "tmp",
        "TEMP": workspace / "tmp",
    }
    for path in set(paths.values()):
        path.mkdir(parents=True, exist_ok=True)
    return {**inherited, **{name: str(path) for name, path in paths.items()}}


def _copy_shadow_state(target_home: Path, shadow_home: Path) -> None:
    for relative in _SHADOW_PATHS:
        source = target_home / relative
        if not source.exists() and not source.is_symlink():
            continue
        destination = shadow_home / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and not source.is_symlink():
            shutil.copytree(source, destination, symlinks=True)
        elif source.is_file() and not source.is_symlink():
            shutil.copy2(source, destination)
        else:
            destination.symlink_to(os.readlink(source), target_is_directory=source.is_dir())


def _remove_prior_shared_files(shadow_home: Path, prior_paths: set[Path]) -> None:
    for relative in sorted(prior_paths, key=lambda path: len(path.parts), reverse=True):
        path = _confined_shadow_path(shadow_home, relative)
        if path.is_file() and not path.is_symlink():
            path.unlink()
        cursor = path.parent
        while cursor != shadow_home:
            try:
                cursor.rmdir()
            except OSError:
                break
            cursor = cursor.parent


def _confined_shadow_path(shadow_home: Path, relative: Path) -> Path:
    shadow = Path(os.path.abspath(shadow_home))
    candidate = Path(os.path.abspath(shadow / relative))
    if candidate == shadow or not candidate.is_relative_to(shadow):
        raise ValueError(f"shared ownership manifest path escapes staging: {relative}")
    cursor = shadow
    for part in relative.parts[:-1]:
        cursor /= part
        try:
            item = cursor.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(item.st_mode):
            raise ValueError(
                f"shared ownership manifest path traverses a staging symlink: {relative}"
            )
    return candidate


def _remove_superseded_legacy_state(shadow_home: Path, dependency_id: str) -> None:
    known: list[Path] = []
    if dependency_id == "gstack":
        known.append(shadow_home / ".agentops/gstack-prime-manifest.json")
    elif dependency_id == "superpowers":
        known.extend(
            (
                shadow_home / ".agentops/skill-dependencies/superpowers.json",
                shadow_home / "skills/.agentops-superpowers-manifest.json",
            )
        )
    elif dependency_id == "humanlayer-show-me":
        state = shadow_home / ".agentops/skill-dependencies"
        known.extend(
            (
                state / "humanlayer-show-me.json",
                state / "humanlayer-show-me-transaction.json",
                state / "humanlayer-show-me.lock",
            )
        )
        if state.is_dir():
            known.extend(state.glob(".humanlayer-show-me-stage-*"))
            known.extend(state.glob(".humanlayer-show-me-backup-*"))
            known.extend(state.glob(".humanlayer-show-me-preserved-*"))
    for path in known:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()


def _managed_files(
    *,
    dependency: SkillDependency,
    install: SkillDependencyInstall,
    source: Path,
    shadow_home: Path,
    target_home: Path,
) -> tuple[PlannedFile, ...]:
    roots: list[Path]
    exact_paths: list[Path] | None = None
    if install.strategy in {"gstack", "copy-repo"}:
        roots = [shadow_home / install.destination]
    elif install.strategy == "copy-skills":
        assert install.source is not None
        children = sorted(child.name for child in (source / install.source).iterdir())
        roots = [shadow_home / install.destination / child for child in children]
    elif install.strategy == "humanlayer-show-me":
        roots = [shadow_home / install.destination / "show-me"]
    elif install.strategy == "prime-superpowers":
        manifest_path = shadow_home / ".agentops/skill-dependencies/superpowers.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        roots = [shadow_home / install.destination / name for name in sorted(data["skills"])]
    elif install.strategy == "prime-gstack":
        manifest_path = shadow_home / ".agentops/gstack-prime-manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        exact_paths = [shadow_home / relative for relative in sorted(data["files"])]
        roots = []
    else:
        raise ValueError(f"unsupported skill dependency install strategy {install.strategy!r}")

    planned: list[PlannedFile] = []
    candidates = exact_paths or [item for root in roots for item in _regular_files(root)]
    shadow_marker = shadow_home.resolve().as_posix().encode()
    target_marker = target_home.expanduser().resolve().as_posix().encode()
    for item in sorted(candidates, key=lambda path: path.as_posix()):
        if item.is_symlink() or not item.is_file():
            raise ValueError(f"rendered bundle contains unsupported entry: {item}")
        relative = item.relative_to(shadow_home)
        content = item.read_bytes()
        if install.strategy in {"prime-gstack", "prime-superpowers"} and b"\0" not in content:
            content = content.replace(shadow_marker, target_marker)
        planned.append(PlannedFile(relative, content, stat.S_IMODE(item.stat().st_mode)))
    return tuple(planned)


def _regular_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.exists():
        raise ValueError(f"rendered bundle contains unsupported entry: {root}")
    if root.is_file():
        return [root]
    result: list[Path] = []
    for directory, names, files in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in names:
            child = current / name
            if child.is_symlink():
                raise ValueError(f"rendered bundle contains unsupported entry: {child}")
        for name in files:
            child = current / name
            if child.is_symlink() or not child.is_file():
                raise ValueError(f"rendered bundle contains unsupported entry: {child}")
            result.append(child)
    return result


def _prior_shared_files(target: TargetSpec) -> dict[Path, tuple[str, int]]:
    key = hashlib.sha256(target.id.encode()).hexdigest()
    manifest = target.home / ".agentops/deployment/manifests" / f"{key}.json"
    if not manifest.exists():
        return {}
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("shared ownership manifest is not a regular file")
    data = _decode_shared_manifest(manifest.read_bytes(), target=target)
    files: dict[Path, tuple[str, int]] = {}
    for item in data["files"]:
        files[Path(item["path"])] = (item["fingerprint"], item["mode"])
    return files


def _prior_provider_index(
    target: TargetSpec,
    prior_paths: set[Path],
) -> dict[str, dict[str, object]]:
    path = target.home / PROVIDER_INDEX_PATH
    if PROVIDER_INDEX_PATH not in prior_paths:
        if path.exists() or path.is_symlink():
            raise ValueError("unmanaged public provider ownership index conflicts with plan")
        return {}
    if path.is_symlink() or not path.is_file():
        raise ValueError("public provider ownership index is not a regular file")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in public provider ownership: {key}")
            result[key] = value
        return result

    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid public provider ownership JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "target_id",
        "framework",
        "providers",
    }:
        raise ValueError("invalid public provider ownership schema")
    if (
        type(data["schema_version"]) is not int
        or data["schema_version"] != 1
        or data["target_id"] != target.id
        or data["framework"] != target.framework.value
        or not isinstance(data["providers"], list)
        or not data["providers"]
    ):
        raise ValueError("invalid public provider ownership identity")
    entries: dict[str, dict[str, object]] = {}
    claimed: set[Path] = set()
    provider_order: list[str] = []
    for entry in data["providers"]:
        if not isinstance(entry, dict) or set(entry) != {
            "provider_id",
            "source_revision",
            "source_descriptor",
            "paths",
        }:
            raise ValueError("invalid public provider ownership entry")
        provider_id = entry["provider_id"]
        revision = entry["source_revision"]
        descriptor = entry["source_descriptor"]
        values = entry["paths"]
        if (
            not isinstance(provider_id, str)
            or not provider_id.startswith("public-skill:")
            or provider_id == "public-skill:"
            or not isinstance(revision, str)
            or not revision
            or not isinstance(descriptor, dict)
            or set(descriptor) != {"id", "repo", "ref", "install"}
            or descriptor["id"] != provider_id.removeprefix("public-skill:")
            or not isinstance(descriptor["repo"], str)
            or not descriptor["repo"]
            or descriptor["ref"] != revision
            or not isinstance(descriptor["install"], dict)
            or not isinstance(values, list)
        ):
            raise ValueError("invalid public provider ownership entry")
        paths = [_manifest_relative_path(value, kind="provider file") for value in values]
        if (
            paths != sorted(set(paths), key=lambda item: item.as_posix())
            or PROVIDER_INDEX_PATH in paths
            or claimed.intersection(paths)
        ):
            raise ValueError("invalid public provider ownership paths")
        claimed.update(paths)
        provider_order.append(provider_id)
        if provider_id in entries:
            raise ValueError("duplicate public provider ownership provider")
        entries[provider_id] = {
            "provider_id": provider_id,
            "source_revision": revision,
            "source_descriptor": descriptor,
            "paths": [item.as_posix() for item in paths],
        }
    if provider_order != sorted(provider_order):
        raise ValueError("public provider ownership providers are not canonical")
    if claimed | {PROVIDER_INDEX_PATH} != prior_paths:
        raise ValueError("public provider ownership does not match shared manifest")
    return entries


def _decode_shared_manifest(content: bytes, *, target: TargetSpec) -> dict:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in shared ownership manifest: {key}")
            result[key] = value
        return result

    try:
        data = json.loads(content.decode(), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid shared ownership manifest JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != _MANIFEST_KEYS:
        raise ValueError("invalid shared ownership manifest schema")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("invalid shared ownership manifest schema")
    if data["target_id"] != target.id or data["framework"] != target.framework.value:
        raise ValueError("shared ownership manifest does not match public skill target")
    if not isinstance(data["source_revision"], str) or not data["source_revision"]:
        raise ValueError("invalid shared ownership manifest source revision")
    providers = data["provider_ids"]
    if (
        not isinstance(providers, list)
        or not providers
        or any(not isinstance(item, str) or not item for item in providers)
        or providers != sorted(set(providers))
    ):
        raise ValueError("invalid shared ownership manifest providers")
    transaction_id = data["transaction_id"]
    if (
        not isinstance(transaction_id, str)
        or len(transaction_id) != 32
        or any(character not in "0123456789abcdef" for character in transaction_id)
    ):
        raise ValueError("invalid shared ownership manifest transaction")
    if not isinstance(data["files"], list) or not isinstance(data["directories"], list):
        raise ValueError("invalid shared ownership manifest entries")

    file_paths: set[Path] = set()
    for item in data["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "fingerprint", "mode"}:
            raise ValueError("invalid shared ownership manifest file")
        path = _manifest_relative_path(item["path"], kind="file")
        fingerprint = item["fingerprint"]
        mode = item["mode"]
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
            or not _valid_manifest_file_mode(mode)
        ):
            raise ValueError("invalid shared ownership manifest file")
        if path in file_paths:
            raise ValueError("invalid shared ownership manifest duplicate file path")
        file_paths.add(path)

    directory_paths: set[Path] = set()
    for item in data["directories"]:
        if not isinstance(item, dict) or set(item) != {"path", "mode"}:
            raise ValueError("invalid shared ownership manifest directory")
        path = _manifest_relative_path(item["path"], kind="directory")
        if not _valid_manifest_directory_mode(item["mode"]):
            raise ValueError("invalid shared ownership manifest directory")
        if path in directory_paths:
            raise ValueError("invalid shared ownership manifest duplicate directory path")
        directory_paths.add(path)

    expected_directories: set[Path] = set()
    for path in file_paths:
        parent = path.parent
        while parent != Path("."):
            expected_directories.add(parent)
            parent = parent.parent
    if directory_paths != expected_directories:
        raise ValueError("invalid shared ownership manifest directory closure")
    if [item["path"] for item in data["files"]] != sorted(item["path"] for item in data["files"]):
        raise ValueError("shared ownership manifest files are not in canonical order")
    if [item["path"] for item in data["directories"]] != sorted(
        item["path"] for item in data["directories"]
    ):
        raise ValueError("shared ownership manifest directories are not in canonical order")
    return data


def _manifest_relative_path(value: object, *, kind: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError(f"unsafe shared ownership manifest {kind} path")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or posix.as_posix() != value
        or posix.parts[:2] == (".agentops", "deployment")
    ):
        raise ValueError(f"unsafe shared ownership manifest {kind} path")
    return Path(*posix.parts)


def _valid_manifest_file_mode(value: object) -> bool:
    return type(value) is int and 0 <= value <= 0o777 and bool(value & 0o400)


def _valid_manifest_directory_mode(value: object) -> bool:
    return type(value) is int and 0 <= value <= 0o777 and value & 0o700 == 0o700


def _validate_prior_shared_files(
    target: TargetSpec,
    files: dict[Path, tuple[str, int]],
    *,
    show_me: bool,
) -> None:
    for relative, (fingerprint, mode) in files.items():
        path = target.home / relative
        valid = False
        try:
            item = path.lstat()
            valid = (
                stat.S_ISREG(item.st_mode)
                and stat.S_IMODE(item.st_mode) == mode
                and hashlib.sha256(path.read_bytes()).hexdigest() == fingerprint
            )
        except OSError:
            pass
        if valid:
            continue
        message = f"prior managed file changed: {relative}"
        if show_me and relative.parts[:2] == ("skills", "show-me"):
            raise ShowMeCollisionError(message)
        raise ValueError(message)


def _validate_target_ancestors(target_home: Path, *, show_me: bool) -> None:
    absolute = Path(os.path.abspath(target_home.expanduser()))
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        try:
            item = cursor.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
            message = (
                f"selected profile path contains a symbolic link or non-directory: {target_home}"
            )
            if show_me:
                raise ShowMeCollisionError(message)
            raise ValueError(message)


def _default_install_dependency(
    *,
    framework: Framework,
    target_home: Path,
    context_home: Path,
    dependency_id: str,
    source: Path,
    destination: Path,
    install: SkillDependencyInstall,
    renderer_env: dict[str, str] | None = None,
) -> None:
    del framework, context_home
    if install.strategy in {"gstack", "copy-repo"}:
        _remove_shadow_path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            destination,
            ignore=shutil.ignore_patterns(*sorted(_SOURCE_METADATA_DIRECTORIES)),
        )
        return
    if install.strategy == "copy-skills":
        if install.source is None:
            raise ValueError("copy-skills dependency install requires a source path")
        destination.mkdir(parents=True, exist_ok=True)
        for child in sorted((source / install.source).iterdir(), key=lambda item: item.name):
            if child.name in _SOURCE_METADATA_DIRECTORIES:
                continue
            target = destination / child.name
            _remove_shadow_path(target)
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
        return
    if install.strategy == "humanlayer-show-me":
        from agent_ops.show_me_adapter import install_show_me

        install_show_me(source, destination / "show-me")
        return
    if install.strategy == "prime-superpowers":
        from agent_ops.superpowers_adapter import install_prime_superpowers

        install_prime_superpowers(source, destination)
        return
    if install.strategy == "prime-gstack":
        from agent_ops.gstack_prime import install_prime_gstack

        install_prime_gstack(source, target_home, renderer_env=renderer_env)
        return
    raise ValueError(f"unsupported skill dependency install strategy {install.strategy!r}")


def _remove_shadow_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()
