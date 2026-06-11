from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

REQUIRED_FILES = (
    "AGENTS.md",
    "ARCHITECTURE.md",
    ".agentops/harness/BOOTSTRAP.md",
    ".agentops/harness/PROGRESS.md",
    ".agentops/harness/DECISIONS.md",
    ".agentops/harness/TASKS.md",
    ".agentops/harness/VERIFY.md",
)

REQUIRED_SECTIONS = {
    "AGENTS.md": ("## Project", "## Harness", "## Verification"),
    "ARCHITECTURE.md": (
        "## Runtime And Tooling",
        "## Package Boundaries",
        "## Verification Architecture",
    ),
    ".agentops/harness/BOOTSTRAP.md": (
        "## Clock In",
        "## Clock Out",
        "## ACID State Rules",
        "## Definition Of Done",
    ),
    ".agentops/harness/PROGRESS.md": (
        "## Current State",
        "## Current Work",
        "## Session Log",
        "## Verification Log",
        "## Next Actions",
    ),
    ".agentops/harness/DECISIONS.md": ("## Template",),
    ".agentops/harness/TASKS.md": ("## Ready", "## Acceptance Criteria Format"),
    ".agentops/harness/VERIFY.md": ("## Harness Check", "## Fast Gate", "## Full Gate"),
}


class HarnessFinding(BaseModel):
    severity: str
    path: str
    message: str


class HarnessReport(BaseModel):
    ok: bool
    root: str
    findings: list[HarnessFinding]


@dataclass(frozen=True)
class HarnessWrite:
    path: Path
    written: bool


def default_verification(repo_type: str) -> tuple[str, ...]:
    match repo_type:
        case "agent-ops" | "python":
            return ("ruff check .", "pytest")
        case _:
            return ("replace with this repo's fastest deterministic verification command",)


def init_harness(
    repo_root: Path,
    repo_name: str,
    repo_type: str = "generic",
    verification_commands: tuple[str, ...] | None = None,
    force: bool = False,
) -> list[HarnessWrite]:
    commands = verification_commands or default_verification(repo_type)
    files = _render_files(repo_name, repo_type, commands)
    writes: list[HarnessWrite] = []
    for relative_path, body in files.items():
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not force:
            writes.append(HarnessWrite(path=path, written=False))
            continue
        path.write_text(body, encoding="utf-8")
        writes.append(HarnessWrite(path=path, written=True))
    return writes


def check_harness(repo_root: Path) -> HarnessReport:
    root = repo_root.resolve()
    findings: list[HarnessFinding] = []

    for relative_path in REQUIRED_FILES:
        path = root / relative_path
        if not path.exists():
            findings.append(
                HarnessFinding(
                    severity="error",
                    path=relative_path,
                    message="required harness file is missing",
                )
            )
            continue
        if not path.is_file():
            findings.append(
                HarnessFinding(
                    severity="error",
                    path=relative_path,
                    message="required harness path is not a file",
                )
            )
            continue
        text = path.read_text(encoding="utf-8")
        for section in REQUIRED_SECTIONS[relative_path]:
            if section not in text:
                findings.append(
                    HarnessFinding(
                        severity="error",
                        path=relative_path,
                        message=f"missing section {section!r}",
                    )
                )

    return HarnessReport(ok=not findings, root=str(root), findings=findings)


def _command_lines(commands: tuple[str, ...]) -> str:
    return "\n".join(f"- `{command}`" for command in commands)


def _render_files(repo_name: str, repo_type: str, commands: tuple[str, ...]) -> dict[str, str]:
    command_lines = _command_lines(commands)
    return {
        "AGENTS.md": f"""# {repo_name} Agent Instructions

## Project

`{repo_name}` is a `{repo_type}` repository. Keep this file short and route to
topic-specific files instead of expanding it into a full manual.

## Harness

- Read `.agentops/harness/BOOTSTRAP.md` at session start.
- Use `.agentops/harness/PROGRESS.md` for active handoff state.
- Use `.agentops/harness/DECISIONS.md` for durable local decisions.
- Use shared-memory tooling only for distilled cross-agent memory.

## Verification

{command_lines}
""",
        "ARCHITECTURE.md": f"""# {repo_name} Architecture

## Runtime And Tooling

- Record the language/runtime version policy.
- Record the dependency manager and local environment defaults.

## Package Boundaries

- Describe the major source directories and ownership boundaries.
- Keep module-specific architecture notes near the code they describe.

## Verification Architecture

{command_lines}
""",
        ".agentops/harness/BOOTSTRAP.md": f"""# Harness Bootstrap Contract

Repository: `{repo_name}`

## Clock In

1. Read `AGENTS.md`.
2. Read `ARCHITECTURE.md`.
3. Read `.agentops/harness/PROGRESS.md`.
4. Read `.agentops/harness/DECISIONS.md` before architecture or workflow changes.
5. Search or recall relevant shared-memory entries when prior context could
   affect the work.
6. Run `git status --short --branch`.
7. Run the fastest relevant verification command before broad edits when feasible.
8. Continue from `.agentops/harness/PROGRESS.md` "Next Actions".

## Clock Out

1. Update `.agentops/harness/PROGRESS.md` with current state, verification,
   blockers, and next actions.
2. Add durable architecture or workflow decisions to `.agentops/harness/DECISIONS.md`.
3. Write shared-memory entries only for distilled cross-agent memory: important
   decisions, durable discoveries, non-obvious debugging findings, or workflow
   changes. Do not write automatic session summaries.
4. Remove stale debug artifacts and leave the startup path usable.
5. Run the local CI contract or record why it could not be run.

## ACID State Rules

- Atomicity: finish one logical operation at a time and commit only coherent
  verified units.
- Consistency: do not claim completion unless verification and harness checks
  pass or failures are recorded.
- Isolation: use branches, worktrees, or explicit file ownership to avoid
  concurrent-agent collisions.
- Durability: keep cross-session state in git-tracked files, not chat memory.

## Definition Of Done

- Requested behavior is implemented.
- Relevant verification has been run and recorded.
- Durable decisions are recorded locally and promoted to shared memory only when reusable.
- The repository is left in a clean handoff state.
""",
        ".agentops/harness/PROGRESS.md": f"""# Progress

Repository: `{repo_name}`

## Current State

- Branch: unknown
- Latest commit: unknown
- Verification: not recorded yet

## Current Work

- Goal: none recorded
- Active task: none
- Files in play: none
- Blockers: none

## Session Log

- No sessions recorded yet.

## Verification Log

- No verification recorded yet.

## Next Actions

1. Replace this line with the next concrete action during active work.
""",
        ".agentops/harness/DECISIONS.md": f"""# Decisions

Repository: `{repo_name}`

Record durable architecture, workflow, and harness decisions here.

## Template

### YYYY-MM-DD: Decision title

- Decision: what changed or what standard was chosen.
- Rationale: why this is the right tradeoff.
- Applies to: files, commands, workflows, or repositories affected.
- Revisit when: condition that should trigger review or removal.
""",
        ".agentops/harness/TASKS.md": f"""# Harness Tasks

Repository: `{repo_name}`

Use this file for agent-readable task decomposition when work spans sessions or agents.

## Ready

- [ ] Replace this placeholder with a task that has explicit acceptance criteria.

## Acceptance Criteria Format

- Scope: files, modules, or behavior affected.
- Verification: exact command that proves completion.
- Handoff: progress and decision updates required before session end.
""",
        ".agentops/harness/VERIFY.md": f"""# Verification

Repository: `{repo_name}`

## Harness Check

- Preferred local command: `agentops harness check .`

## Fast Gate

{command_lines}

## Full Gate

Use the repo's complete CI-equivalent command when the fast gate is not enough.
Record exact command output summaries in `.agentops/harness/PROGRESS.md`.
""",
    }
