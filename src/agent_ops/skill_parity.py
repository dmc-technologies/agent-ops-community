"""Report whether every declared third-party skill is installed and Agent Ops owned.

An engineer runs this to answer one question: does this framework home actually
hold the skills the registry says it should, installed by Agent Ops rather than
by hand? It reads only the registry and the home's own ownership index, writes
nothing, and reaches no network.

Each declared skill lands in exactly one of three states.

``managed``
    Agent Ops installed it and the ownership index records the provider that
    owns it. This is the only passing state.
``unmanaged``
    A directory of that name exists in the home, but no provider claims it. A
    hand copy looks exactly like this. It reports as a failure because Agent Ops
    cannot refresh, verify, or retire a directory it does not own.
``missing``
    Nothing of that name is installed at all.

Both failing states exit non-zero, because both mean the declaration is not in
force on that machine. That is the defect this module exists to catch: a skill
declared in the registry on one day and still absent from a home months later,
with nothing reporting the difference.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_ops.deployment.public_skills import PROVIDER_INDEX_PATH
from agent_ops.registries import load_skill_dependencies
from agent_ops.registries.models import Framework, SkillDependency, SkillDependencyInstall
from agent_ops.skill_installer import default_framework_home

MANAGED = "managed"
UNMANAGED = "unmanaged"
MISSING = "missing"

#: Frameworks this audit covers. Codex and Claude Code are the primary agent
#: harnesses and are required to hold the same skills.
DEFAULT_FRAMEWORKS = (Framework.CLAUDE_CODE, Framework.CODEX)


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


def _load_provider_index(home: Path) -> dict[str, set[str]]:
    """Map each provider id to the skill directory names it owns in this home.

    Returns an empty mapping when the home has no ownership index, which is what
    a home that Agent Ops has never provisioned looks like.
    """

    index_path = home / PROVIDER_INDEX_PATH
    if not index_path.is_file():
        return {}
    document = json.loads(index_path.read_text(encoding="utf-8"))
    owned: dict[str, set[str]] = {}
    for provider in document.get("providers", []):
        names: set[str] = set()
        for path in provider.get("paths", []):
            parts = Path(path).parts
            if len(parts) >= 2 and parts[0] == "skills":
                names.add(parts[1])
        owned[provider["provider_id"]] = names
    return owned


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
        for skill in expected_skills(install):
            if skill in provider_owned:
                state = MANAGED
            elif (skills_root / skill).is_dir():
                state = UNMANAGED
            else:
                state = MISSING
            states.append(SkillState(framework, dependency.id, skill, state))
    return states


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

    dependencies = load_skill_dependencies()
    if arguments.all:
        declared = {
            framework for dependency in dependencies for framework in dependency.install
        }
        frameworks = tuple(
            framework for framework in Framework if framework.value in declared
        )
    elif arguments.framework:
        frameworks = tuple(Framework(value) for value in arguments.framework)
    else:
        frameworks = DEFAULT_FRAMEWORKS

    states = audit(frameworks, dependencies)
    print(format_report(states))
    failed = [state for state in states if not state.ok]
    if failed:
        names = ", ".join(
            f"{state.framework.value}:{state.dependency_id}/{state.skill}" for state in failed
        )
        print(f"\n{len(failed)} declared skills are not managed by Agent Ops: {names}")
        return 1
    print(f"\nall {len(states)} declared skills are managed by Agent Ops")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
