from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

PINNED_SUPERPOWERS_REPO = "https://github.com/obra/superpowers.git"
PINNED_SUPERPOWERS_REF = "f2cbfbefebbfef77321e4c9abc9e949826bea9d7"
SKILL_PREFIX = "agentops-superpowers-"
MANIFEST_NAME = ".agentops-superpowers-manifest.json"
OWNERSHIP_MANIFEST_RELATIVE = Path(".agentops/skill-dependencies/superpowers.json")
SUPERPOWERS_SKILLS = (
    "brainstorming",
    "dispatching-parallel-agents",
    "executing-plans",
    "finishing-a-development-branch",
    "receiving-code-review",
    "requesting-code-review",
    "subagent-driven-development",
    "systematic-debugging",
    "test-driven-development",
    "using-git-worktrees",
    "using-superpowers",
    "verification-before-completion",
    "writing-plans",
    "writing-skills",
)


class SuperpowersCollisionError(RuntimeError):
    """Raised when installation would overwrite bytes not owned by this adapter."""


_PRIME_PREAMBLE = """\
> **Prime Agent mapping:** Global system, developer, and project policy, plus direct user
> instructions, outrank this skill. Use IPython for shell commands and file work
> (`%%bash` for project commands). Dispatch with
> `handle = await rlm("<complete bounded task prompt>")`. A child sends its result with
> `await agent_message.send(message, receiver_role="parent")`; that reply arrives to the parent
> as an ordinary agent message. Retain the child handle and use `agent_message.send` with
> `receiver_role="child"` and `receiver_name=handle.name` only for parent follow-ups. Track
> checklists and ask questions in ordinary conversation.
> Discover and load skills through Prime's session skill inventory; do not assume
> Claude-only tools exist.

"""

_PRIME_STARTUP_BODY = """\
# Using Superpowers in Prime Agent

## Instruction priority

Global system and developer instructions outrank project policy, direct user instructions, and
all skill text. Project policy and direct user instructions also outrank this skill. A skill is a
workflow aid, never authority to weaken a higher-priority safety or repository rule.

## Startup rule

Before acting or replying, check Prime's session skill inventory for a relevant skill. Load the
relevant namespaced skill before using it. A child agent dispatched for a specific bounded task
may skip this startup skill and follow the task it received.

## Prime tool mapping

- Use IPython for filesystem inspection, edits, Python work, and shell commands. Run project shell
  commands in an IPython `%%bash` cell so the project's own environment remains authoritative.
- The upstream `Task` concept maps to Prime-native RLM subagent dispatch:
  `handle = await rlm("<complete bounded task prompt>")`. A child explicitly replies with
  `await agent_message.send(message, receiver_role="parent")`; the parent receives that result
  as an ordinary agent message. Retain the child handle and send parent follow-ups with
  `receiver_role="child"` and `receiver_name=handle.name`. Admission metadata is not a result.
  Keep dependent work sequential.
- The upstream `TodoWrite` concept maps to a checklist maintained in ordinary conversation or the
  repository's approved progress file.
- The upstream `Skill` concept maps to Prime's session skill inventory and loading the selected
  skill through IPython when its full text is needed.
- The upstream `AskUserQuestion` concept maps to asking one direct question in ordinary
  conversation. Do not invent a tool call for it.

When another adapted skill names `agentops-superpowers-<name>`, select that exact namespaced skill.
"""

def _validate_confined_destination(destination: Path) -> tuple[Path, Path]:
    raw_destination = destination.expanduser().absolute()
    profile_root = raw_destination.parent.resolve()
    confined_destination = profile_root / raw_destination.name
    ownership_parent = profile_root / OWNERSHIP_MANIFEST_RELATIVE.parent
    for path in (confined_destination, ownership_parent):
        cursor = profile_root
        try:
            relative = path.relative_to(profile_root)
        except ValueError as exc:
            raise SuperpowersCollisionError(
                f"Superpowers write path escapes the selected profile: {path}"
            ) from exc
        for part in relative.parts:
            cursor /= part
            if cursor.is_symlink():
                raise SuperpowersCollisionError(
                    f"symbolic link blocks confined Superpowers installation: {cursor}"
                )
            if cursor.exists() and not cursor.is_dir():
                raise SuperpowersCollisionError(
                    f"non-directory blocks confined Superpowers installation: {cursor}"
                )
    return confined_destination, profile_root


_TOOL_REPLACEMENTS = (
    (
        "Task tool (general-purpose):",
        "Prime RLM subagent dispatch (upstream Task/general-purpose), "
        "with results returned through agent_message:",
    ),
    (
        "Task tool with `general-purpose` type",
        "Prime RLM subagent dispatch, with results returned through `agent_message`,",
    ),
    (
        "Task tool",
        "Prime RLM subagent dispatch (upstream Task), with results returned through agent_message",
    ),
    (
        "TodoWrite",
        "Prime checklist tracking in ordinary conversation (upstream TodoWrite)",
    ),
    (
        "Skill tool",
        "Prime session skill inventory (upstream Skill tool)",
    ),
    (
        "AskUserQuestion",
        "an ordinary-conversation question (upstream AskUserQuestion)",
    ),
)


def install_prime_superpowers(
    upstream_root: Path,
    destination: Path,
    *,
    upstream_ref: str = PINNED_SUPERPOWERS_REF,
) -> dict[str, Any]:
    """Adapt and transactionally install the pinned Superpowers skills for Prime Agent.

    ``upstream_root`` is the pinned repository checkout, not its ``skills`` directory.
    Existing paths are replaceable only when the previous manifest fingerprint proves that this
    adapter installed them and their bytes have not changed since installation.
    """

    upstream_root = Path(upstream_root)
    destination, profile_root = _validate_confined_destination(Path(destination))
    _validate_input(upstream_root, upstream_ref)
    destination_parent = destination.parent
    destination_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=".agentops-superpowers-stage-", dir=destination_parent
    ) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        source_fingerprints = {
            name: _tree_fingerprint(upstream_root / "skills" / name)
            for name in SUPERPOWERS_SKILLS
        }
        for skill_name in SUPERPOWERS_SKILLS:
            _adapt_skill(upstream_root / "skills" / skill_name, staging, skill_name)
        _validate_reference_closure(staging)
        _bind_profile_root(staging, profile_root)
        manifest = _build_manifest(staging)
        _install_staged(staging, destination, manifest, source_fingerprints)
    return manifest


def _validate_input(upstream_root: Path, upstream_ref: str) -> None:
    if upstream_ref != PINNED_SUPERPOWERS_REF:
        raise ValueError(
            f"expected pinned Superpowers ref {PINNED_SUPERPOWERS_REF}, got {upstream_ref!r}"
        )
    skills_root = upstream_root / "skills"
    missing = [
        name
        for name in SUPERPOWERS_SKILLS
        if not (skills_root / name / "SKILL.md").is_file()
    ]
    if missing:
        missing_names = ", ".join(missing)
        raise FileNotFoundError(f"pinned Superpowers checkout is missing skills: {missing_names}")
    if (upstream_root / ".git").exists():
        result = subprocess.run(
            ["git", "-C", str(upstream_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        actual_ref = result.stdout.strip()
        if actual_ref != upstream_ref:
            raise ValueError(
                f"pinned Superpowers checkout is at {actual_ref}, expected {upstream_ref}"
            )
        checkout_status = subprocess.run(
            [
                "git",
                "-C",
                str(upstream_root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignored=matching",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if checkout_status.stdout:
            raise ValueError(
                "pinned Superpowers checkout contains tracked modifications, "
                "untracked files, or ignored files"
            )


def _adapt_skill(source: Path, staging: Path, skill_name: str) -> None:
    target = staging / f"{SKILL_PREFIX}{skill_name}"
    shutil.copytree(source, target)
    if skill_name == "using-superpowers":
        shutil.rmtree(target / "references", ignore_errors=True)
    for path in sorted(item for item in target.rglob("*") if item.is_file()):
        if path.suffix.lower() not in {".md", ".txt", ".dot", ".ts", ".js", ".sh"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        text = _rewrite_internal_references(text)
        if path.name == "SKILL.md":
            text = _rewrite_frontmatter_name(text, f"{SKILL_PREFIX}{skill_name}")
            if skill_name == "using-superpowers":
                frontmatter, _ = _split_frontmatter(text)
                text = f"---\n{frontmatter}---\n\n{_PRIME_STARTUP_BODY}"
            else:
                frontmatter, body = _split_frontmatter(text)
                text = f"---\n{frontmatter}---\n\n{_PRIME_PREAMBLE}{body.lstrip()}"
        path.write_text(text, encoding="utf-8", newline="\n")


def _rewrite_internal_references(text: str) -> str:
    platform_replacements = (
        ("~/.claude/skills", "${PRIME_AGENT_CODING_AGENT_DIR}/skills"),
        ("~/.claude/CLAUDE.md", "${PRIME_AGENT_CODING_AGENT_DIR}/AGENTS.md"),
        (".claude/skills", ".prime/agent/skills"),
        ("CLAUDE.md", "AGENTS.md"),
        ("Claude Code", "Prime Agent"),
    )
    for old, new in platform_replacements:
        text = text.replace(old, new)
    profile_skills = "${PRIME_AGENT_CODING_AGENT_DIR}/skills"
    legacy_paths = {
        "skills/meta/testing-skills-with-subagents": (
            f"{profile_skills}/{SKILL_PREFIX}writing-skills/"
            "testing-skills-with-subagents.md"
        ),
        "skill-creation/SKILL.md": (
            f"{profile_skills}/{SKILL_PREFIX}writing-skills/SKILL.md"
        ),
        "skills/using-skills": f"{profile_skills}/{SKILL_PREFIX}using-superpowers",
    }
    for old, new in legacy_paths.items():
        text = text.replace(old, new)
    for name in SUPERPOWERS_SKILLS:
        installed_name = f"{SKILL_PREFIX}{name}"
        installed_path = f"{profile_skills}/{installed_name}"
        text = text.replace(f"superpowers:{name}", installed_name)
        text = text.replace(f"{profile_skills}/{name}", installed_path)
        text = re.sub(
            rf"(?<![\w$/-])(?:\.\./)?skills/(?:[a-z0-9-]+/)*{re.escape(name)}",
            installed_path,
            text,
        )
        text = re.sub(
            rf"(?<![\w$/-])(?:\.\./)?{re.escape(name)}/",
            f"{installed_path}/",
            text,
        )
        text = re.sub(
            rf"(?<!{re.escape(SKILL_PREFIX)})\b{re.escape(name)}\b",
            installed_name,
            text,
        )
    reviewer_path = f"{profile_skills}/{SKILL_PREFIX}requesting-code-review/code-reviewer.md"
    text = re.sub(r"(?<![\w/-])code-reviewer\.md", reviewer_path, text)
    for agent_name in ("implementer", "spec-reviewer", "code-reviewer", "code-quality-reviewer"):
        text = text.replace(f"superpowers:{agent_name}", f"bundled {agent_name} prompt template")
    text = re.sub(r'\bTask\("([^"]*)"\)', r'await rlm("\1")', text)
    for old, new in _TOOL_REPLACEMENTS:
        text = text.replace(old, new)
    return text


def _validate_reference_closure(staging: Path) -> None:
    text_suffixes = {".md", ".txt", ".dot", ".ts", ".js", ".sh"}
    expected_names = {f"{SKILL_PREFIX}{name}" for name in SUPERPOWERS_SKILLS}
    profile_reference = re.compile(
        r"\$\{PRIME_AGENT_CODING_AGENT_DIR\}/skills/"
        r"(?P<skill>agentops-superpowers-[a-z0-9-]+)"
        r"(?P<suffix>(?:/[A-Za-z0-9_.-]+)*)"
    )
    installed_reference = re.compile(r"\bagentops-superpowers-[a-z0-9-]+\b")
    legacy_paths = (
        "skills/meta/testing-skills-with-subagents",
        "skill-creation/SKILL.md",
        "skills/using-skills",
    )
    for path in sorted(item for item in staging.rglob("*") if item.is_file()):
        if path.suffix.lower() not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            unresolved: str | None = None
            if "superpowers:" in line:
                unresolved = "superpowers:"
            if unresolved is None:
                for name in SUPERPOWERS_SKILLS:
                    match = re.search(
                        rf"(?<!{re.escape(SKILL_PREFIX)})\b{re.escape(name)}\b",
                        line,
                    )
                    if match:
                        unresolved = match.group(0)
                        break
            if unresolved is None:
                unresolved = next((value for value in legacy_paths if value in line), None)
            if unresolved is not None:
                relative = path.relative_to(staging).as_posix()
                raise ValueError(
                    f"unresolved Superpowers reference {unresolved!r} "
                    f"at {relative}:{line_number}"
                )
            for match in installed_reference.finditer(line):
                if match.group(0) not in expected_names:
                    relative = path.relative_to(staging).as_posix()
                    raise ValueError(
                        f"unresolved Superpowers skill {match.group(0)!r} "
                        f"at {relative}:{line_number}"
                    )
            for match in profile_reference.finditer(line):
                suffix = match.group("suffix").rstrip(".,;:")
                target = staging / match.group("skill")
                if suffix:
                    target = target.joinpath(*suffix.lstrip("/").split("/"))
                if not target.exists():
                    relative = path.relative_to(staging).as_posix()
                    token = match.group(0).rstrip(".,;:")
                    raise ValueError(
                        f"unresolved Superpowers path {token!r} "
                        f"at {relative}:{line_number}"
                    )


def _bind_profile_root(staging: Path, profile_root: Path) -> None:
    marker = "${PRIME_AGENT_CODING_AGENT_DIR}"
    text_suffixes = {".md", ".txt", ".dot", ".ts", ".js", ".sh"}
    replacement = profile_root.as_posix()
    for path in sorted(item for item in staging.rglob("*") if item.is_file()):
        if path.suffix.lower() not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if marker in text:
            path.write_text(text.replace(marker, replacement), encoding="utf-8", newline="\n")


def _split_frontmatter(text: str) -> tuple[str, str]:
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", text, flags=re.DOTALL)
    if match is None:
        raise ValueError("SKILL.md must contain YAML frontmatter")
    return match.group(1).rstrip() + "\n", match.group(2)


def _rewrite_frontmatter_name(text: str, expected_name: str) -> str:
    frontmatter, body = _split_frontmatter(text)
    if not re.search(r"(?m)^name\s*:", frontmatter):
        raise ValueError("SKILL.md frontmatter must contain name")
    frontmatter = re.sub(
        r"(?m)^name\s*:.*$", f"name: {expected_name}", frontmatter, count=1
    )
    return f"---\n{frontmatter}---\n{body}"


def _tree_fingerprint(path: Path) -> str:
    """Fingerprint exact relative directory structure and regular-file bytes."""
    value = hashlib.sha256()

    def visit(directory: Path, relative: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                entry_relative = relative / entry.name
                encoded = entry_relative.as_posix().encode("utf-8")
                if entry.is_symlink():
                    raise SuperpowersCollisionError(
                        f"skill tree contains unsupported symbolic link: {entry.path}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    value.update(b"D\0" + encoded + b"\0")
                    visit(Path(entry.path), entry_relative)
                elif entry.is_file(follow_symlinks=False):
                    value.update(b"F\0" + encoded + b"\0")
                    value.update(Path(entry.path).read_bytes())
                    value.update(b"\0")
                else:
                    raise SuperpowersCollisionError(
                        f"skill tree contains unsupported entry: {entry.path}"
                    )

    visit(path, Path())
    return value.hexdigest()


def _ownership_manifest_path(destination: Path) -> Path:
    return destination.parent / OWNERSHIP_MANIFEST_RELATIVE


def _build_manifest(staging: Path) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "upstream_repo": PINNED_SUPERPOWERS_REPO,
        "upstream_ref": PINNED_SUPERPOWERS_REF,
        "skills": {
            path.name: {"fingerprint": _tree_fingerprint(path)}
            for path in sorted(item for item in staging.iterdir() if item.is_dir())
        },
    }


def _read_manifest_file(path: Path) -> dict[str, Any] | list[str] | None:
    if not path.exists():
        if path.is_symlink():
            raise SuperpowersCollisionError(f"user-owned collision at {path}")
        return None
    if path.is_symlink() or not path.is_file():
        raise SuperpowersCollisionError(f"user-owned collision at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SuperpowersCollisionError(f"user-owned collision at {path}") from exc


def _validated_managed_manifest(
    data: object, path: Path
) -> dict[str, dict[str, Any]]:
    if not isinstance(data, dict):
        raise SuperpowersCollisionError(f"invalid Superpowers ownership manifest at {path}")
    if (
        data.get("schema_version") != 1
        or data.get("upstream_repo") != PINNED_SUPERPOWERS_REPO
        or data.get("upstream_ref") != PINNED_SUPERPOWERS_REF
        or not isinstance(data.get("skills"), dict)
    ):
        raise SuperpowersCollisionError(f"invalid Superpowers ownership manifest at {path}")
    managed = data["skills"]
    for name, entry in managed.items():
        if (
            not isinstance(name, str)
            or name != Path(name).name
            or not name.startswith(SKILL_PREFIX)
            or not isinstance(entry, dict)
            or not isinstance(entry.get("fingerprint"), str)
        ):
            raise SuperpowersCollisionError(f"invalid Superpowers ownership manifest at {path}")
    return managed



def _declared_skill_name(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, flags=re.DOTALL)
    if match is None:
        return None
    name_match = re.search(r"(?m)^name\s*:\s*([^#\r\n]+?)\s*$", match.group(1))
    if name_match is None:
        return None
    return name_match.group(1).strip().strip("\"'")


def _reject_logical_name_collisions(
    destination: Path, owned_directory_names: set[str], candidate_names: set[str]
) -> None:
    if not destination.exists():
        return
    for path in sorted(destination.rglob("SKILL.md")):
        try:
            top_level = path.relative_to(destination).parts[0]
        except (ValueError, IndexError):
            continue
        if top_level in owned_directory_names:
            continue
        declared_name = _declared_skill_name(path)
        if declared_name in candidate_names:
            raise SuperpowersCollisionError(
                f"logical skill-name collision at {path}: {declared_name}"
            )

def _preflight_logical_skill_names(destination: Path, candidate_names: set[str]) -> None:
    if not destination.exists():
        return
    for skill_file in sorted(destination.rglob("SKILL.md")):
        relative = skill_file.relative_to(destination)
        if relative.parts and relative.parts[0] in candidate_names:
            continue
        if skill_file.is_symlink() or not skill_file.is_file():
            continue
        try:
            text = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        match = re.search(r"(?m)^name\s*:\s*['\"]?([^'\"\s]+)", text)
        if match and match.group(1) in candidate_names:
            raise SuperpowersCollisionError(
                f"logical skill-name collision for {match.group(1)} at {skill_file}"
            )


def _preflight(
    destination: Path,
    manifest: dict[str, Any],
    source_fingerprints: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], set[str], Path | None]:
    legacy_path = destination / MANIFEST_NAME
    ownership_path = _ownership_manifest_path(destination)
    legacy = _read_manifest_file(legacy_path) if destination.exists() else None
    current = _read_manifest_file(ownership_path)
    if legacy is not None and current is not None:
        raise SuperpowersCollisionError(
            "both legacy and current Superpowers ownership manifests exist"
        )

    legacy_names: set[str] = set()
    old_manifest_path: Path | None = None
    if legacy is not None:
        old_manifest_path = legacy_path
        if isinstance(legacy, list) and all(isinstance(item, str) for item in legacy):
            legacy_names = set(legacy)
            if len(legacy) != len(legacy_names) or legacy_names != set(SUPERPOWERS_SKILLS):
                raise SuperpowersCollisionError(
                    "legacy Superpowers manifest does not match the pinned skill set"
                )
            for name in sorted(legacy_names):
                target = destination / name
                if (
                    target.is_symlink()
                    or not target.is_dir()
                    or _tree_fingerprint(target) != source_fingerprints[name]
                ):
                    raise SuperpowersCollisionError(
                        f"legacy skill {target} does not match pinned upstream bytes"
                    )
            managed: dict[str, dict[str, Any]] = {}
        else:
            managed = _validated_managed_manifest(legacy, legacy_path)
    elif current is not None:
        managed = _validated_managed_manifest(current, ownership_path)
    else:
        managed = {}

    candidate_names = set(manifest["skills"])
    _preflight_logical_skill_names(destination, candidate_names)
    stale_names = set(managed) - candidate_names
    _reject_logical_name_collisions(
        destination,
        set(managed) | legacy_names | candidate_names,
        candidate_names,
    )
    for name in sorted(candidate_names | stale_names):
        target = destination / name
        if not target.exists():
            if target.is_symlink():
                raise SuperpowersCollisionError(f"user-owned collision at {target}")
            if name in managed:
                raise SuperpowersCollisionError(f"managed skill {target} is missing")
            continue
        entry = managed.get(name)
        if not isinstance(entry, dict) or not isinstance(entry.get("fingerprint"), str):
            raise SuperpowersCollisionError(f"user-owned collision at {target}")
        if (
            target.is_symlink()
            or not target.is_dir()
            or _tree_fingerprint(target) != entry["fingerprint"]
        ):
            raise SuperpowersCollisionError(f"managed skill {target} changed since installation")
    return managed, legacy_names, old_manifest_path


def _install_staged(
    staging: Path,
    destination: Path,
    manifest: dict[str, Any],
    source_fingerprints: dict[str, str],
) -> None:
    validated_destination, _ = _validate_confined_destination(destination)
    if validated_destination != destination:
        raise SuperpowersCollisionError("Superpowers destination changed during installation")
    managed, legacy_names, old_manifest_path = _preflight(
        destination, manifest, source_fingerprints
    )
    destination.mkdir(parents=True, exist_ok=True)
    transaction_root = staging.parent / "backup"
    transaction_root.mkdir()
    names = sorted(set(managed) | set(manifest["skills"]) | legacy_names)
    installed: list[str] = []
    backed_up: list[str] = []
    ownership_path = _ownership_manifest_path(destination)
    ownership_existed = ownership_path.exists()
    old_ownership = ownership_path.read_bytes() if ownership_existed else None
    old_legacy = old_manifest_path.read_bytes() if old_manifest_path is not None else None
    try:
        for name in names:
            target = destination / name
            if target.exists():
                os.replace(target, transaction_root / name)
                backed_up.append(name)
            staged = staging / name
            if staged.exists():
                os.replace(staged, target)
                installed.append(name)

        validated_destination, _ = _validate_confined_destination(destination)
        if validated_destination != destination:
            raise SuperpowersCollisionError("Superpowers destination changed during installation")
        ownership_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_temporary = staging.parent / "ownership-manifest.tmp"
        manifest_temporary.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        os.replace(manifest_temporary, ownership_path)
        if old_manifest_path is not None:
            old_manifest_path.unlink()
    except BaseException:
        for name in reversed(installed):
            target = destination / name
            if target.exists():
                shutil.rmtree(target)
        for name in reversed(backed_up):
            os.replace(transaction_root / name, destination / name)
        if ownership_existed and old_ownership is not None:
            ownership_path.write_bytes(old_ownership)
        else:
            ownership_path.unlink(missing_ok=True)
        if old_manifest_path is not None and old_legacy is not None:
            old_manifest_path.write_bytes(old_legacy)
        raise
