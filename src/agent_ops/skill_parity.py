"""Report whether every declared third-party skill is installed and Agent Ops owned.

An engineer runs this to answer one question: does this framework home actually
hold the skills the registry says it should, installed by Agent Ops rather than
by hand? It reads only the registry and the home's own ownership index, writes
nothing, and reaches no network.

Each declared skill lands in exactly one of three states.

``managed``
    Agent Ops installed it and an ownership record claims it. This is the only
    passing state. Two ownership records exist and either one counts: the shared
    provider index at ``skills/.agentops-public-provider-index.json``, written by
    the transactional engine, and the older per-dependency manifests under
    ``.agentops/skill-dependencies/``, written by the standalone installers that
    predate it. A home provisioned before the engine landed carries only the
    second, and reading just the first would report every one of its skills as a
    hand copy.
``unmanaged``
    A directory of that name exists in the home, but no provider claims it. A
    hand copy looks exactly like this. It reports as a failure because Agent Ops
    cannot refresh, verify, or retire a directory it does not own.
``missing``
    No directory of that name is installed, whether or not an ownership record
    still claims it. A record outlives the files it describes, so the filesystem
    decides this first.

Both failing states exit non-zero, because both mean the declaration is not in
force on that machine. That is the defect this module exists to catch: a skill
declared in the registry on one day and still absent from a home months later,
with nothing reporting the difference.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_ops.deployment.public_skills import PROVIDER_INDEX_PATH
from agent_ops.registries import load_skill_dependencies
from agent_ops.registries.models import Framework, SkillDependency, SkillDependencyInstall
from agent_ops.skill_installer import default_framework_home

#: Per-dependency ownership manifests written by the standalone installers.
LEGACY_OWNERSHIP_DIR = Path(".agentops/skill-dependencies")

MANAGED = "managed"
UNMANAGED = "unmanaged"
MISSING = "missing"

#: Frameworks this audit covers. Codex and Claude Code are the primary agent
#: harnesses and are required to hold the same skills.
DEFAULT_FRAMEWORKS = (Framework.CLAUDE_CODE, Framework.CODEX)


@dataclass(frozen=True)
class UnauditedBundle:
    """A declared bundle whose installed skill names cannot be checked by name."""

    framework: Framework
    dependency_id: str
    strategy: str


@dataclass(frozen=True)
class SkillState:
    """One declared skill, and what the home actually holds for it."""

    framework: Framework
    dependency_id: str
    skill: str
    state: str

    @property
    def ok(self) -> bool:
        return self.state == MANAGED


def expected_skills(install: SkillDependencyInstall) -> tuple[str, ...]:
    """Return the skill directory names an install strategy is expected to create.

    Only strategies with a finite, declared set of names are audited. ``gstack``
    renders a whole repository into one directory and ``copy-skills`` installs
    whatever the upstream ships, so neither has a declared list to check against
    and both are reported at provider level instead.
    """

    if install.strategy == "copy-named-skills":
        return tuple(install.skills)
    if install.strategy == "humanlayer-show-me":
        return ("show-me",)
    return ()


def _read_ownership_document(path: Path) -> dict | None:
    """Return a JSON object from `path`, or None if it is absent or unusable.

    An ownership record is written by an install that can be interrupted, so a
    truncated or empty file is a state this audit must describe rather than die
    on. An unreadable record claims nothing, which makes the skills it should
    have covered report as unmanaged -- a loud, named failure instead of a
    traceback that names nothing.
    """

    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        print(f"warning: cannot read ownership record {path}: {error}", file=sys.stderr)
        return None
    if not isinstance(document, dict):
        print(f"warning: ownership record {path} is not a JSON object", file=sys.stderr)
        return None
    return document


def _load_provider_index(home: Path) -> dict[str, set[str]]:
    """Map each provider id to the skill directory names it owns in this home.

    Returns an empty mapping when the home has no ownership index, which is what
    a home that Agent Ops has never provisioned looks like.
    """

    document = _read_ownership_document(home / PROVIDER_INDEX_PATH)
    if document is None:
        return {}
    providers = document.get("providers")
    if not isinstance(providers, list):
        return {}
    owned: dict[str, set[str]] = {}
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_id = provider.get("provider_id")
        if not isinstance(provider_id, str):
            continue
        paths = provider.get("paths")
        names: set[str] = set()
        for path in paths if isinstance(paths, list) else ():
            if not isinstance(path, str):
                continue
            parts = Path(path).parts
            if len(parts) >= 2 and parts[0] == "skills":
                names.add(parts[1])
        owned[provider_id] = names
    return owned


def _load_legacy_ownership(home: Path, dependency_id: str) -> set[str]:
    """Return the skill names the per-dependency ownership manifest records.

    These manifests predate the shared provider index. `copy-named-skills`
    records a `skills` mapping of name to file fingerprints; the show-me adapter
    records the single `skill` it owns.
    """

    document = _read_ownership_document(home / LEGACY_OWNERSHIP_DIR / f"{dependency_id}.json")
    if document is None:
        return set()
    if isinstance(document.get("skills"), dict):
        return set(document["skills"])
    if isinstance(document.get("skill"), str):
        return {document["skill"]}
    return set()


def audit_home(
    framework: Framework,
    dependencies: Sequence[SkillDependency],
    home: Path,
) -> list[SkillState]:
    """Classify every skill the registry declares for this framework."""

    owned = _load_provider_index(home)
    skills_root = home / "skills"
    states: list[SkillState] = []
    for dependency in dependencies:
        install = dependency.install.get(framework.value)
        if install is None:
            continue
        provider_owned = owned.get(f"public-skill:{dependency.id}", set())
        provider_owned |= _load_legacy_ownership(home, dependency.id)
        for skill in expected_skills(install):
            # An ownership record is a claim about the past. The directory is the
            # present. A skill deleted from a home still has its record, so
            # checking the record alone reports a skill that is not there as
            # installed -- the audit must agree with the filesystem first.
            installed = (skills_root / skill).is_dir()
            if not installed:
                state = MISSING
            elif skill in provider_owned:
                state = MANAGED
            else:
                state = UNMANAGED
            states.append(SkillState(framework, dependency.id, skill, state))
    return states


def unaudited_bundles(
    frameworks: Iterable[Framework],
    dependencies: Sequence[SkillDependency],
) -> list[UnauditedBundle]:
    """List the declared bundles this audit cannot check name by name.

    `gstack` renders a whole repository into one directory and `copy-skills`
    installs whatever the upstream ships, so neither declares the names it
    creates. A report that stayed silent about them would let a reader believe
    every declared bundle had been examined.
    """

    skipped: list[UnauditedBundle] = []
    for framework in frameworks:
        for dependency in dependencies:
            install = dependency.install.get(framework.value)
            if install is None or expected_skills(install):
                continue
            skipped.append(UnauditedBundle(framework, dependency.id, install.strategy))
    return skipped


def audit(
    frameworks: Iterable[Framework],
    dependencies: Sequence[SkillDependency] | None = None,
    homes: dict[Framework, Path] | None = None,
) -> list[SkillState]:
    """Audit each framework's home and return every declared skill's state."""

    dependencies = list(dependencies if dependencies is not None else load_skill_dependencies())
    homes = homes or {}
    states: list[SkillState] = []
    for framework in frameworks:
        home = homes.get(framework) or default_framework_home(framework)
        states.extend(audit_home(framework, dependencies, home))
    return states


def format_report(states: Sequence[SkillState]) -> str:
    """Render one line per declared skill, grouped by framework."""

    lines: list[str] = []
    for framework in dict.fromkeys(state.framework for state in states):
        rows = [state for state in states if state.framework is framework]
        failed = [state for state in rows if not state.ok]
        lines.append(f"{framework.value}: {len(rows) - len(failed)}/{len(rows)} managed")
        for state in rows:
            marker = "  ok     " if state.ok else "  FAIL   "
            lines.append(f"{marker}{state.dependency_id}/{state.skill}: {state.state}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Print the report and return 0 only when every declared skill is managed."""

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--framework",
        action="append",
        choices=[framework.value for framework in Framework],
        help="Framework to audit. Repeat to audit several. Defaults to claude-code and codex.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Audit every framework any dependency declares.",
    )
    arguments = parser.parse_args(argv)

    if arguments.all and arguments.framework:
        parser.error("--all audits every declared framework; do not also pass --framework")

    dependencies = load_skill_dependencies()
    if arguments.all:
        declared = {
            framework for dependency in dependencies for framework in dependency.install
        }
        frameworks = tuple(
            framework for framework in Framework if framework.value in declared
        )
    elif arguments.framework:
        # Repeating a framework would audit its home twice and double every
        # count in the report, so the same home is never audited more than once.
        frameworks = tuple(dict.fromkeys(Framework(value) for value in arguments.framework))
    else:
        frameworks = DEFAULT_FRAMEWORKS

    states = audit(frameworks, dependencies)
    print(format_report(states))
    skipped = unaudited_bundles(frameworks, dependencies)
    if skipped:
        names = ", ".join(
            f"{bundle.framework.value}:{bundle.dependency_id} ({bundle.strategy})"
            for bundle in skipped
        )
        print(f"\nnot checked by skill name, no declared name list: {names}")
    failed = [state for state in states if not state.ok]
    if failed:
        names = ", ".join(
            f"{state.framework.value}:{state.dependency_id}/{state.skill}" for state in failed
        )
        plural = "skill is" if len(failed) == 1 else "skills are"
        print(f"\n{len(failed)} declared {plural} not managed by Agent Ops: {names}")
        return 1
    print(f"\nall {len(states)} checked skills are managed by Agent Ops")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
