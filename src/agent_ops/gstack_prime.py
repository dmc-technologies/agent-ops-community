from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PINNED_GSTACK_REF = "74895062fb8a3acbf9f66cd088a83359aaaa56cd"
MANIFEST_NAME = ".agentops/gstack-prime-manifest.json"
MANIFEST_OWNER = "agent-ops-community:gstack-prime"
_RUNTIME_TREES = (
    "bin",
    "browse/bin",
    "browse/dist",
    "design/dist",
    "make-pdf/dist",
    "review",
    "design-html/vendor",
    "extension",
    "qa/templates",
    "qa/references",
    "gstack-upgrade/migrations",
    "node_modules/playwright",
    "node_modules/playwright-core",
    "node_modules/diff",
    "node_modules/@ngrok",
)
_RUNTIME_FILES = (
    "ETHOS.md",
    "plan-devex-review/dx-hall-of-fame.md",
    "browse/src/cdp-allowlist.ts",
)
_REQUIRED_EXECUTABLES = (
    "browse/dist/browse",
    "browse/dist/find-browse",
    "browse/dist/browse-server",
    "design/dist/design",
    "make-pdf/dist/pdf",
)


class GstackPrimeError(RuntimeError):
    """Base error for deterministic Prime gstack generation."""


class GstackPrimeSourceError(GstackPrimeError):
    """The supplied checkout cannot provide the pinned upstream input."""


class GstackPrimeCollisionError(GstackPrimeError):
    """Installation would overwrite a file the manifest does not safely own."""


def _validate_shell_safe_profile_root(profile_root: Path) -> None:
    value = profile_root.as_posix()
    if not profile_root.is_absolute() or shlex.quote(value) != value:
        raise GstackPrimeSourceError(
            "Prime gstack requires an absolute profile path that is safe as an "
            "unquoted POSIX shell word"
        )


@dataclass(frozen=True)
class GstackPrimeInstallResult:
    coding_agent_dir: Path
    upstream_ref: str
    files: tuple[Path, ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    return _sha256(path.read_bytes())


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(command, cwd=cwd, env=env, check=True, capture_output=True)
    except FileNotFoundError as exc:
        raise GstackPrimeSourceError(f"Bun executable is unavailable: {command[0]}") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise GstackPrimeSourceError(
            f"gstack host generation failed{': ' + detail if detail else ''}"
        ) from exc


def _extract_pinned_checkout(checkout: Path, destination: Path) -> None:
    checkout = checkout.resolve()
    probe = subprocess.run(
        ["git", "-C", str(checkout), "cat-file", "-e", f"{PINNED_GSTACK_REF}^{{commit}}"],
        capture_output=True,
    )
    if probe.returncode:
        raise GstackPrimeSourceError(
            f"checkout does not contain pinned gstack commit {PINNED_GSTACK_REF}"
        )
    archive = _run(
        ["git", "-C", str(checkout), "archive", "--format=tar", PINNED_GSTACK_REF]
    ).stdout
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        for member in tar.getmembers():
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise GstackPrimeSourceError("pinned archive contains an unsafe path")
        tar.extractall(destination, filter="data")


def _add_prime_host(source: Path) -> None:
    runtime = "${PRIME_AGENT_CODING_AGENT_DIR}/.agentops/runtime/gstack"
    host = source / "hosts/prime.ts"
    host.write_text(
        f"""import type {{ HostConfig }} from '../scripts/host-config';

const prime: HostConfig = {{
  name: 'prime',
  displayName: 'Prime Agent',
  cliCommand: 'prime-agent',
  cliAliases: [],
  globalRoot: '.prime/agent/.agentops/runtime/gstack',
  localSkillRoot: '.prime/agent/.agentops/runtime/gstack',
  hostSubdir: '.prime',
  usesEnvVars: true,
  frontmatter: {{ mode: 'allowlist', keepFields: ['name', 'description'], descriptionLimit: null }},
  generation: {{
    generateMetadata: false,
    skipSkills: [
      'codex', 'claude', 'gstack-upgrade', 'setup-gbrain', 'sync-gbrain',
      'careful', 'freeze', 'guard', 'unfreeze',
      'benchmark-models', 'pair-agent', 'autoplan', 'office-hours',
      'open-gstack-browser', 'connect-chrome', 'ship', 'skillify',
      'document-release', 'land-and-deploy',
    ],
  }},
  pathRewrites: [
    {{ from: '~/.claude/skills/review', to: '{runtime}/review' }},
    {{ from: '.claude/skills/review', to: '{runtime}/review' }},
    {{ from: '~/.claude/skills/gstack', to: '{runtime}' }},
    {{ from: '.claude/skills/gstack', to: '{runtime}' }},
    {{ from: '~/.claude/skills', to: '${{PRIME_AGENT_CODING_AGENT_DIR}}/skills' }},
    {{ from: '.claude/skills', to: '${{PRIME_AGENT_CODING_AGENT_DIR}}/skills' }},
    {{ from: 'CLAUDE.md', to: 'AGENTS.md' }},
  ],
  toolRewrites: {{
    'AskUserQuestion': 'an ordinary question to the user',
    'Bash tool': 'IPython `%%bash`',
    'Read tool': 'Python file reads in IPython',
    'Write tool': 'Python file writes in IPython',
    'Edit tool': 'the Prime `edit` skill in IPython',
    'Grep tool': 'Python search in IPython',
    'Glob tool': 'Python path matching in IPython',
    'WebSearch tool': 'the Prime `websearch` skill in IPython',
    'Agent tool': 'RLM plus `agent_message`',
    'Skill tool': 'a Prime `/skill:name` invocation',
    'subagent_type': 'RLM child task',
  }},
  suppressedResolvers: [
    'DESIGN_OUTSIDE_VOICES', 'ADVERSARIAL_STEP', 'CODEX_SECOND_OPINION',
    'CODEX_PLAN_REVIEW', 'REVIEW_ARMY', 'GBRAIN_CONTEXT_LOAD',
    'GBRAIN_SAVE_RESULTS', 'MODEL_OVERLAY',
  ],
  runtimeRoot: {{ globalSymlinks: [], globalFiles: {{}} }},
  install: {{ prefixable: false, linkingStrategy: 'symlink-generated' }},
  learningsMode: 'basic',
}};
export default prime;
""",
        encoding="utf-8",
    )
    index_path = source / "hosts/index.ts"
    index = index_path.read_text(encoding="utf-8")
    index = "import prime from './prime';\n" + index
    match = re.search(r"(ALL_HOST_CONFIGS[^=]*=\s*\[)([^\]]*)(\])", index, flags=re.DOTALL)
    if not match:
        raise GstackPrimeSourceError("pinned gstack host registry has an unexpected shape")
    entries = match.group(2).rstrip()
    if entries and not entries.endswith(","):
        entries += ","
    index = index[: match.start(2)] + entries + " prime" + index[match.end(2) :]
    index_path.write_text(index, encoding="utf-8")


def _patch_browse_runtime(source: Path) -> None:
    cli = source / "browse/src/cli.ts"
    text = cli.read_text(encoding="utf-8")
    old_resolution = "path.resolve(path.dirname(execPath), '..', 'src', 'server.ts')"
    new_resolution = "path.resolve(path.dirname(execPath), 'browse-server')"
    old_spawn = "Bun.spawn(['bun', 'run', SERVER_SCRIPT], {"
    new_spawn = "Bun.spawn([SERVER_SCRIPT], {"
    old_environment = (
        "env: { ...process.env, BROWSE_STATE_FILE: config.stateFile, "
        "BROWSE_PARENT_PID: parentPid, ...extraEnv },"
    )
    new_environment = (
        "env: { ...process.env, NODE_PATH: path.resolve(path.dirname(process.execPath), "
        "'..', '..', 'node_modules'), BROWSE_STATE_FILE: config.stateFile, "
        "BROWSE_PARENT_PID: parentPid, ...extraEnv },"
    )
    if (
        text.count(old_resolution) != 1
        or text.count(old_spawn) != 1
        or text.count(old_environment) != 1
    ):
        raise GstackPrimeSourceError(
            "pinned gstack browse launcher has an unexpected server contract"
        )
    text = (
        text.replace(old_resolution, new_resolution)
        .replace(old_spawn, new_spawn)
        .replace(old_environment, new_environment)
    )
    cli.write_text(text, encoding="utf-8")


def _strip_model_overlay(content: str) -> str:
    content = re.sub(r"(?ms)^## Model-Specific Behavioral Patch[^\n]*\n.*?(?=^## |\Z)", "", content)
    return re.sub(r"(?m)^.*MODEL_OVERLAY.*\n?", "", content)


def _replace_level_two_section(content: str, heading: str, replacement: str) -> str:
    pattern = rf"(?ms)^## {re.escape(heading)}\n.*?(?=^## |\Z)"
    return re.sub(pattern, replacement.rstrip() + "\n\n", content)


def _adapt_prime_contract(content: str, skill_names: set[str], installed_name: str) -> str:
    content = _strip_model_overlay(content)
    content = _replace_level_two_section(
        content,
        "Skill Invocation During Plan Mode",
        """## Interactive workflow steps in Prime Agent

When the workflow requires a user decision, ask the question in an ordinary assistant response
and stop. Resume the same workflow after the user replies. Prime Agent has no special plan or
question tool, so never report a workflow blocked solely because those tools are absent. Treat
STOP markers as turn boundaries, and keep higher-priority policy in force.""",
    )
    for question_heading in (
        "AskUserQuestion Format",
        "an ordinary question to the user Format",
    ):
        content = _replace_level_two_section(
            content,
            question_heading,
            """## User question format in Prime Agent

Present the decision, why it matters, and concise options in an ordinary assistant response. Ask
one direct question and stop for the user's reply. Do not dispatch a child agent or call an MCP
tool to ask the user. Continue from the answer on the next turn.""",
        )
    if installed_name == "agentops-gstack-investigate":
        content = _replace_level_two_section(
            content,
            "Scope Lock",
            """## Scope boundary in Prime Agent

Prime Agent does not provide gstack's provider-specific edit hook. Identify and state the narrowest
directory containing the affected files before editing. Do not edit outside that directory unless
the user approves the expanded scope. This is an explicit workflow constraint, not automated
enforcement.""",
        )
    runtime = "${PRIME_AGENT_CODING_AGENT_DIR}/.agentops/runtime/gstack"
    replacements = (
        ("$HOME/.claude/skills/review", f"{runtime}/review"),
        ("~/.claude/skills/review", f"{runtime}/review"),
        (".claude/skills/review", f"{runtime}/review"),
        ("$HOME/.claude/skills/gstack", runtime),
        ("$_ROOT/.claude/skills/gstack", runtime),
        ("$HOME/.prime/agent/.agentops/runtime/gstack", runtime),
        ("$HOME${PRIME_AGENT_CODING_AGENT_DIR}/.agentops/runtime/gstack", runtime),
        ("$HOME/${PRIME_AGENT_CODING_AGENT_DIR}/.agentops/runtime/gstack", runtime),
        ("${HOME}/${PRIME_AGENT_CODING_AGENT_DIR}/.agentops/runtime/gstack", runtime),
        ("$_ROOT/${PRIME_AGENT_CODING_AGENT_DIR}/.agentops/runtime/gstack", runtime),
        ("$HOME/${PRIME_AGENT_CODING_AGENT_DIR}", "${PRIME_AGENT_CODING_AGENT_DIR}"),
        ("${HOME}/${PRIME_AGENT_CODING_AGENT_DIR}", "${PRIME_AGENT_CODING_AGENT_DIR}"),
        ("$_ROOT/${PRIME_AGENT_CODING_AGENT_DIR}", "${PRIME_AGENT_CODING_AGENT_DIR}"),
        ("~/.claude/skills/gstack", runtime),
        (".claude/skills/gstack", runtime),
        ("~/.claude/skills", "${PRIME_AGENT_CODING_AGENT_DIR}/skills"),
        (".claude/skills", "${PRIME_AGENT_CODING_AGENT_DIR}/skills"),
        ("$HOME/.claude/", "${PRIME_AGENT_CODING_AGENT_DIR}/"),
        ("~/.claude/", "${PRIME_AGENT_CODING_AGENT_DIR}/"),
        (".claude/", "${PRIME_AGENT_CODING_AGENT_DIR}/"),
        ("$HOME/.claude", "${PRIME_AGENT_CODING_AGENT_DIR}"),
        ("~/.claude", "${PRIME_AGENT_CODING_AGENT_DIR}"),
        (".claude", "${PRIME_AGENT_CODING_AGENT_DIR}"),
        ("$GSTACK_ROOT", runtime),
        ("$GSTACK_BIN", f"{runtime}/bin"),
        ("$GSTACK_BROWSE", f"{runtime}/browse/dist"),
        ("$GSTACK_DESIGN", f"{runtime}/design/dist"),
        ("$GSTACK_MAKE_PDF", f"{runtime}/make-pdf/dist"),
        ("CLAUDE.md", "AGENTS.md"),
        ("Claude-only", "Prime-native"),
        ("Claude Code", "Prime Agent"),
        ("ExitPlanMode", "continue after the user confirms the plan"),
        ("AskUserQuestion", "an ordinary question to the user"),
        ("the Agent tool", "RLM plus `agent_message`"),
        ("Agent tool", "RLM plus `agent_message`"),
        ("subagent_type", "RLM child task"),
        ("the Bash tool", "IPython `%%bash`"),
        ("Bash tool", "IPython `%%bash`"),
        ("the Read tool", "Python file reads in IPython"),
        ("Read tool", "Python file reads in IPython"),
        ("the Write tool", "Python file writes in IPython"),
        ("Write tool", "Python file writes in IPython"),
        ("the Edit tool", "the Prime `edit` skill in IPython"),
        ("Edit tool", "the Prime `edit` skill in IPython"),
        ("the Grep tool", "Python search in IPython"),
        ("Grep tool", "Python search in IPython"),
        ("the Glob tool", "Python path matching in IPython"),
        ("Glob tool", "Python path matching in IPython"),
        ("WebSearch tool", "the Prime `websearch` skill in IPython"),
        ("Skill tool", "a Prime `/skill:name` invocation"),
    )
    for old, new in replacements:
        content = content.replace(old, new)
    for name in skill_names:
        installed_skill_path = (
            f"${{PRIME_AGENT_CODING_AGENT_DIR}}/skills/agentops-{name}/SKILL.md"
        )
        content = content.replace(f"{runtime}/{name}/SKILL.md", installed_skill_path)
        base = name.removeprefix("gstack-")
        content = content.replace(f"{runtime}/{base}/SKILL.md", installed_skill_path)
        content = re.sub(
            rf"(?<!agentops-)(?<!gstack-){re.escape(base)}/SKILL\.md",
            installed_skill_path,
            content,
        )
        content = content.replace(
            f"${{CLAUDE_SKILL_DIR}}/../{name}/SKILL.md", installed_skill_path
        )
        content = content.replace(
            f"${{CLAUDE_SKILL_DIR}}/../{base}/SKILL.md", installed_skill_path
        )
    content = content.replace(f"$HOME{runtime}", runtime)
    content = content.replace(f"$HOME/{runtime}", runtime)
    content = content.replace(f"${{HOME}}/{runtime}", runtime)
    content = content.replace(f"$_ROOT/{runtime}", runtime)
    content = re.sub(
        r"(?<!/)browse/src/cdp-allowlist\.ts",
        f"{runtime}/browse/src/cdp-allowlist.ts",
        content,
    )
    content = content.replace(
        "`$B`, `$D`, `codex exec`/`codex review`,",
        "`$B`, `$D`,",
    )
    design_source_fallback = """## Missing design source in Prime Agent

If a design document exists, read it and use it as the source of truth. If none exists, state
that the design source is missing and ask whether to use the current plan as the sole review
source or stop so the user can supply a design document. Continue only from the user's choice;
do not invent missing product decisions or name a workflow that is not installed."""
    if installed_name == "agentops-gstack-plan-eng-review":
        section_start = content.index("## Prerequisite Skill Offer")
        preserved_start = content.index("### Step 0: Scope Challenge", section_start)
        content = (
            content[:section_start]
            + design_source_fallback
            + "\n\n"
            + content[preserved_start:]
        )
    elif installed_name == "agentops-gstack-plan-ceo-review":
        section_start = content.index("## Prerequisite Skill Offer")
        mid_session_start = content.index("**Mid-session detection:**", section_start)
        retrospective_start = content.index("### Retrospective Check", mid_session_start)
        mid_session_fallback = """**Mid-session product-source check:** During Step 0A, if
the user cannot state a stable problem or is still exploring what to build, pause the review.
Discuss the product directly
with the user until the problem, constraints, and chosen direction are explicit, then ask
whether to resume this review from that source or stop. Do not continue from invented product
context."""
        content = (
            content[:section_start]
            + design_source_fallback
            + "\n\n"
            + mid_session_fallback
            + "\n\n"
            + content[retrospective_start:]
        )
    elif installed_name == "agentops-gstack-plan-devex-review":
        content = _replace_level_two_section(
            content,
            "Prerequisite Skill Offer",
            design_source_fallback,
        )
    if installed_name == "agentops-gstack-scrape":
        content = _replace_level_two_section(
            content,
            "Step 5 — Skillify nudge",
            """## Step 5 — Manual persistence

After a successful prototype, return the extracted JSON and the complete tested script. Explain
that Prime does not automate browser-skill installation; if the user wants reuse, provide the
exact filenames and destination so they can preserve the script manually. Do not claim the
manual copy has been installed or benchmarked.""",
        )
        content = content.replace(
            "suggest `/skillify` so the",
            "return the script and exact manual persistence steps so the",
        )
    content = content.replace(
        "Ship/deploy/PR → invoke /ship or /land-and-deploy",
        "Pull-request creation → inspect the origin remote: on GitHub use `gh pr create` or "
        "the GitHub UI; on GitLab use `glab mr create` when available or the GitLab UI; for "
        "an unknown provider STOP. Merge and deploy an existing GitHub pull request → invoke "
        "/land-and-deploy; on GitLab use the repository's approved GitLab path",
    )
    content = content.replace(
        "Full review pipeline → invoke /autoplan",
        "Full review pipeline → run /plan-ceo-review, /plan-eng-review, "
        "/plan-design-review, and /plan-devex-review individually as applicable",
    )
    content = content.replace(
        'User wants all reviews done automatically, "review everything" → invoke `/autoplan`',
        "User wants all reviews done → run `/plan-ceo-review`, `/plan-eng-review`, "
        "`/plan-design-review`, and `/plan-devex-review` individually as applicable",
    )
    content = content.replace(
        "User asks to merge + deploy + verify as one flow → invoke `/land-and-deploy`",
        "User asks to merge, deploy, and verify → inspect the origin remote: on GitHub invoke "
        "`/land-and-deploy` for an existing pull request; on GitLab use the repository's "
        "approved GitLab merge and deployment path; for an unknown provider STOP",
    )
    content = content.replace(
        "User asks for safety mode, careful mode → invoke `/careful` or `/guard`",
        "User asks for safety mode or careful mode → explain that Prime cannot enforce the "
        "excluded hook mode; ask for an explicit safety boundary, treat it as a controlling "
        "constraint, and stop before any action outside it",
    )
    content = content.replace(
        "User asks to restrict edits to a directory → invoke `/freeze` or `/unfreeze`",
        "User asks to restrict edits to a directory → ask for the exact directory, confine "
        "all reads and writes to it, and require explicit approval before changing the boundary; "
        "state that Prime does not provide hook enforcement",
    )
    content = content.replace(
        'User asks to launch a real browser for QA, "open the browser" → invoke '
        "`/open-gstack-browser`",
        "User asks to open a browser for QA → invoke `/browse` for the retained headless "
        "browser, or state that headed interactive browsing is not installed",
    )
    unsupported_routes = (
        "careful",
        "freeze",
        "guard",
        "unfreeze",
        "benchmark-models",
        "pair-agent",
        "autoplan",
        "office-hours",
        "open-gstack-browser",
        "ship",
        "skillify",
        "document-release",
        "land-and-deploy",
        "codex",
        "claude",
    )
    route_fallbacks = {
        "careful": "the applicable higher-priority safety policy",
        "freeze": "the stated directory boundary",
        "guard": "the applicable higher-priority safety policy",
        "unfreeze": "a user-approved directory-boundary change",
        "benchmark-models": "manual comparison using approved providers",
        "pair-agent": "a separately approved child-agent task",
        "autoplan": "the retained plan-review skills",
        "office-hours": "ordinary product discussion",
        "open-gstack-browser": "the retained headless browser",
        "ship": "manual pull-request and release preparation",
        "skillify": "manual script preservation",
        "document-release": (
            "a source-backed manual release procedure (STOP if none is supplied)"
        ),
        "land-and-deploy": (
            "a repository-owned merge and deployment procedure (STOP if none is supplied)"
        ),
        "codex": "an external review that is not run by Prime",
        "claude": "an external review that is not run by Prime",
    }
    for route in unsupported_routes:
        content = re.sub(
            rf"/{re.escape(route)}\b",
            route_fallbacks[route],
            content,
        )
    content = content.replace(
        "Product ideas/brainstorming → invoke ordinary product discussion",
        "Product ideas/brainstorming → discuss the product directly with the user",
    )
    content = content.replace(
        "Full review pipeline → invoke the retained plan-review skills",
        "Full review pipeline → run the retained plan-review skills individually",
    )
    content = content.replace(
        "Ship/deploy/PR → invoke manual pull-request and release preparation or ",
        "Pull-request creation → inspect the origin remote: on GitHub use `gh pr create` or "
        "the GitHub UI; on GitLab use `glab mr create` when available or the GitLab UI; for "
        "an unknown provider STOP. Merge and deploy an existing GitHub pull request → invoke ",
    )
    content = content.replace(
        "suggest manual script preservation so the",
        "return the script and explain how to preserve it manually so the",
    )
    content = content.replace(
        'verdict "NO REVIEWS YET — run `the retained plan-review skills`"',
        'verdict "NO REVIEWS YET — run the retained plan-review skills individually"',
    )
    content = content.replace(
        "`/skill:agentops-gstack-context-restore` reads `[gstack-context]`; "
        "`manual pull-request and release preparation` squashes WIP commits into clean commits.",
        "`/skill:agentops-gstack-context-restore` reads `[gstack-context]`; manual Git cleanup "
        "may squash WIP commits only after user approval.",
    )
    content = content.replace(
        "Product ideas/brainstorming → invoke `ordinary product discussion`",
        "Product ideas/brainstorming → discuss the product directly with the user",
    )
    content = content.replace(
        "all reviews done automatically, \"review everything\" → invoke "
        "`the retained plan-review skills`",
        "all reviews done automatically, \"review everything\" → run the retained "
        "plan-review skills individually",
    )
    content = content.replace(
        "Ship/deploy/PR → invoke `manual pull-request and release preparation` or ",
        "Ship/deploy/PR → use ",
    )
    content = content.replace(
        "asks to ship, deploy, push, create a PR, \"let's land this\", \"send it\" → invoke "
        "`manual pull-request and release preparation`",
        "asks to create a pull or merge request → inspect the origin remote: use `gh pr "
        "create` or the GitHub UI on GitHub, use `glab mr create` when available or the "
        "GitLab UI on GitLab, and STOP for an unknown provider; asks to merge or deploy → "
        "use an explicit repository-owned procedure and STOP to ask the user if none is supplied",
    )
    content = content.replace(
        "asks for a second opinion, codex review → invoke "
        "`an external review that is not run by Prime`",
        "asks for an external second opinion → state that Prime cannot run that external review "
        "and continue with the retained Prime review",
    )
    content = content.replace(
        "→ invoke `ordinary product discussion`",
        "→ discuss the product directly with the user",
    )
    content = content.replace(
        "→ invoke `manual pull-request and release preparation`",
        "→ inspect the origin remote; on GitHub use `gh pr create` or the GitHub UI, on "
        "GitLab use `glab mr create` when available or the GitLab UI, and for an unknown "
        "provider STOP; for merge or deployment use an explicit repository-owned procedure "
        "and STOP to ask the user if none is supplied",
    )
    content = content.replace(
        "→ invoke `an external review that is not run by Prime`",
        "→ state that Prime cannot run the external review and continue with the retained Prime "
        "review",
    )
    content = content.replace(
        "Run manual pull-request and release preparation when ready.",
        "Inspect the origin remote when ready: create the pull request with `gh pr create` "
        "or the GitHub UI on GitHub, create the merge request with `glab mr create` when "
        "available or the GitLab UI on GitLab, and STOP for an unknown provider.",
    )
    content = content.replace(
        "Ready to implement — run manual pull-request and release preparation when done",
        "Ready to implement — implement, then create the pull request manually when done",
    )
    content = content.replace(
        "rerun manual pull-request and release preparation to pick up the next free slot",
        "manually reconcile VERSION, package metadata, changelog, and pull-request title with "
        "the next free slot",
    )
    content = content.replace(
        "rerun manual pull-request and release preparation to reconcile",
        "manually reconcile VERSION, package metadata, changelog, and pull-request title from",
    )
    content = content.replace(
        "manual pull-request and release preparation's job",
        "left to user-approved manual Git and pull-request steps",
    )
    content = content.replace(
        "codexan external review that is not run by Prime",
        "external review not run in Prime",
    )
    content = content.replace(
        "an external review that is not run by Prime review",
        "external review not run in Prime",
    )
    content = content.replace(
        "an external review that is not run by Prime",
        "external review not run in Prime",
    )
    content = content.replace(
        "manual pull-request and release preparation",
        "user-approved manual pull-request and release steps",
    )
    for prose_fallback in route_fallbacks.values():
        content = content.replace(f"`{prose_fallback}`", prose_fallback)
    manual_steps = "user-approved manual pull-request and release steps"
    content = content.replace(f"`{manual_steps}`", manual_steps)
    content = content.replace(f"\\`{manual_steps}\\`", manual_steps)
    content = content.replace(f"{manual_steps} squashes", f"{manual_steps} may squash")
    content = content.replace(f"{manual_steps} creates", f"{manual_steps} create")
    content = content.replace(f"{manual_steps}'s", manual_steps)
    for name in sorted(skill_names, key=len, reverse=True):
        base = name.removeprefix("gstack-")
        pattern = rf"(?<![\w./-])/(?:gstack-)?{re.escape(base)}\b"
        content = re.sub(pattern, f"/skill:agentops-{name}", content)
    content, substitutions = re.subn(
        r"(?m)^name:\s*[^\n]+$", f"name: {installed_name}", content, count=1
    )
    if substitutions != 1:
        raise GstackPrimeSourceError("generated Prime skill has no frontmatter name")
    contract = (
        "> **Prime Agent tool contract:** Use IPython for shell commands and file operations "
        "(`%%bash` for project commands and Python for file work). Spawn child agents with "
        "RLM and receive replies through `agent_message`. Ask the user an ordinary question.\n\n"
    )
    body = content.find("\n---", 3)
    if body >= 0:
        body = content.find("\n", body + 1) + 1
        content = content[:body] + contract + content[body:]
    else:
        content = contract + content
    forbidden = (
        ".claude/",
        "~/.claude",
        "MODEL_OVERLAY",
        "Model-Specific Behavioral Patch",
        "AskUserQuestion",
        "Bash tool",
        "Read tool",
        "Write tool",
        "Edit tool",
        "Grep tool",
        "Glob tool",
        "ExitPlanMode",
        "$HOME${PRIME_AGENT_CODING_AGENT_DIR}",
        "$_ROOT/${PRIME_AGENT_CODING_AGENT_DIR}",
        "CLAUDE_SKILL_DIR",
    )
    found = [token for token in forbidden if token in content]
    if found:
        token = found[0]
        offset = content.index(token)
        context = content[max(0, offset - 120) : offset + len(token) + 160]
        raise GstackPrimeSourceError(
            f"generated Prime skill {installed_name} retains unavailable contracts: "
            f"{found}; context={context!r}"
        )
    return content


def _adapt_runtime_asset(item: Path) -> bytes:
    data = item.read_bytes()
    if b"\0" in data:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    profile = "${PRIME_AGENT_CODING_AGENT_DIR}"
    runtime = f"{profile}/.agentops/runtime/gstack"
    replacements = (
        ("$HOME/.claude/skills/gstack", runtime),
        ("$_ROOT/.claude/skills/gstack", runtime),
        ("$HOME/.prime/agent/.agentops/runtime/gstack", runtime),
        ("$HOME${PRIME_AGENT_CODING_AGENT_DIR}/.agentops/runtime/gstack", runtime),
        ("$HOME/${PRIME_AGENT_CODING_AGENT_DIR}/.agentops/runtime/gstack", runtime),
        ("${HOME}/${PRIME_AGENT_CODING_AGENT_DIR}/.agentops/runtime/gstack", runtime),
        ("$_ROOT/${PRIME_AGENT_CODING_AGENT_DIR}/.agentops/runtime/gstack", runtime),
        ("$HOME/${PRIME_AGENT_CODING_AGENT_DIR}", "${PRIME_AGENT_CODING_AGENT_DIR}"),
        ("${HOME}/${PRIME_AGENT_CODING_AGENT_DIR}", "${PRIME_AGENT_CODING_AGENT_DIR}"),
        ("$_ROOT/${PRIME_AGENT_CODING_AGENT_DIR}", "${PRIME_AGENT_CODING_AGENT_DIR}"),
        ("~/.claude/skills/gstack", runtime),
        (".claude/skills/gstack", runtime),
        ("$HOME/.claude/plans", f"{profile}/plans"),
        (".claude/plans", f"{profile}/plans"),
        ("CLAUDE.md", "AGENTS.md"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    text = text.replace(f"$HOME/{runtime}", runtime)
    text = text.replace(f"${{HOME}}/{runtime}", runtime)
    text = text.replace(f"$_ROOT/{runtime}", runtime)
    forbidden_paths = ("~/.claude/skills/gstack", ".claude/skills/gstack", "$HOME/.claude/plans")
    if any(token in text for token in forbidden_paths):
        raise GstackPrimeSourceError(f"runtime asset retains a Claude-only path: {item}")
    return text.encode("utf-8")


def _collect_files(source: Path) -> tuple[dict[str, bytes], dict[str, int]]:
    generated = source / ".prime/skills"
    skill_files = sorted(generated.glob("gstack*/SKILL.md")) if generated.is_dir() else []
    if not skill_files:
        raise GstackPrimeSourceError("gstack host generator produced no Prime skills")
    skill_names = {skill.parent.name for skill in skill_files}
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    for skill in skill_files:
        installed_name = f"agentops-{skill.parent.name}"
        relative = f"skills/{installed_name}/SKILL.md"
        files[relative] = _adapt_prime_contract(
            skill.read_text(encoding="utf-8"), skill_names, installed_name
        ).encode()
        modes[relative] = 0o644

    for relative in _RUNTIME_FILES:
        item = source / relative
        if item.is_file() and not item.is_symlink():
            destination = f".agentops/runtime/gstack/{relative}"
            files[destination] = _adapt_runtime_asset(item)
            modes[destination] = item.stat().st_mode & 0o777
    for relative in _RUNTIME_TREES:
        tree = source / relative
        if not tree.is_dir():
            continue
        for item in sorted(tree.rglob("*")):
            if (
                not item.is_file()
                or item.is_symlink()
                or item.name in {"SKILL.md", "SKILL.md.tmpl"}
            ):
                continue
            child = item.relative_to(source).as_posix()
            destination = f".agentops/runtime/gstack/{child}"
            files[destination] = _adapt_runtime_asset(item)
            modes[destination] = item.stat().st_mode & 0o777
    return files, modes


def _bind_profile_root(files: dict[str, bytes], profile_root: Path) -> None:
    marker = b"${PRIME_AGENT_CODING_AGENT_DIR}"
    replacement = profile_root.as_posix().encode("utf-8")
    for relative, data in list(files.items()):
        if b"\0" not in data and marker in data:
            files[relative] = data.replace(marker, replacement)


def _validate_reference_closure(files: dict[str, bytes], profile_root: Path) -> None:
    forbidden_skills = (
        "benchmark-models",
        "pair-agent",
        "autoplan",
        "office-hours",
        "open-gstack-browser",
        "ship",
        "skillify",
        "document-release",
        "land-and-deploy",
        "careful",
        "freeze",
        "guard",
        "unfreeze",
    )
    for name in forbidden_skills:
        if f"skills/agentops-gstack-{name}/SKILL.md" in files:
            raise GstackPrimeSourceError(f"unsupported Prime gstack skill was generated: {name}")
    cdp_allowlist = ".agentops/runtime/gstack/browse/src/cdp-allowlist.ts"
    if cdp_allowlist not in files:
        raise GstackPrimeSourceError(
            f"generated bundle is missing required dependency: {cdp_allowlist}"
        )
    cdp_reference = f"{profile_root.as_posix()}/{cdp_allowlist}".encode()
    for skill in ("skills/agentops-gstack/SKILL.md", "skills/agentops-gstack-browse/SKILL.md"):
        content = files.get(skill)
        if content is not None and cdp_reference not in content:
            raise GstackPrimeSourceError(
                f"generated skill {skill} does not reference packaged dependency: {cdp_allowlist}"
            )
    forbidden_markers = (
        b"Only Prime Agent",
        b"<gstack-install>",
        b"(unavailable in Prime)",
        b"runtime/gstackordinary product discussion",
        f"$HOME/{profile_root.as_posix()}".encode(),
        f"${{HOME}}/{profile_root.as_posix()}".encode(),
    )
    forbidden_routes = tuple(
        re.compile(
            rb"(?<![A-Za-z0-9_.-])/" + re.escape(name.encode()) + rb"(?![A-Za-z0-9_./-])"
        )
        for name in forbidden_skills
    )
    for relative, data in files.items():
        if b"\0" in data:
            continue
        found = [marker for marker in forbidden_markers if marker in data]
        if relative.startswith("skills/"):
            if b"codex exec" in data:
                found.append(b"codex exec")
            found.extend(
                pattern.pattern for pattern in forbidden_routes if pattern.search(data)
            )
        if found:
            raise GstackPrimeSourceError(
                f"generated Prime file {relative} retains an invalid runtime reference: "
                f"{found[0].decode(errors='replace')}"
            )

def _load_manifest(target: Path) -> dict[str, object] | None:
    path = target / MANIFEST_NAME
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise GstackPrimeCollisionError(f"unowned manifest target exists: {path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GstackPrimeCollisionError(f"unowned or invalid manifest: {path}") from exc
    files = manifest.get("files")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("owner") != MANIFEST_OWNER
        or manifest.get("upstream_ref") != PINNED_GSTACK_REF
        or not isinstance(files, dict)
    ):
        raise GstackPrimeCollisionError(f"unowned manifest: {path}")
    for relative, fingerprint in files.items():
        pure = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath("..")
        allowed = (
            pure.parts[:1] == ("skills",)
            and len(pure.parts) >= 3
            and (
                pure.parts[1] == "agentops-gstack"
                or pure.parts[1].startswith("agentops-gstack-")
            )
        ) or pure.parts[:3] == (".agentops", "runtime", "gstack")
        if (
            not isinstance(relative, str)
            or pure.is_absolute()
            or ".." in pure.parts
            or not allowed
            or not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise GstackPrimeCollisionError(f"unsafe managed path in manifest: {relative!r}")
    return manifest


def _safe_target(target: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise GstackPrimeCollisionError(f"unsafe managed path: {relative!r}")
    path = target / relative
    cursor = target
    for part in pure.parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            raise GstackPrimeCollisionError(f"unowned symlink blocks installation: {cursor}")
        if cursor.exists() and not cursor.is_dir():
            raise GstackPrimeCollisionError(f"unowned path blocks installation: {cursor}")
    return path


def _tree_snapshot(root: Path) -> dict[str, tuple[str, int, bytes]] | None:
    if not root.is_dir() or root.is_symlink():
        return None
    result: dict[str, tuple[str, int, bytes]] = {}
    for item in sorted(root.rglob("*")):
        if item.is_symlink() or (not item.is_file() and not item.is_dir()):
            return None
        relative = item.relative_to(root).as_posix()
        mode = item.stat().st_mode & 0o111
        if item.is_dir():
            result[relative] = ("directory", mode, b"")
        else:
            result[relative] = ("file", mode, item.read_bytes())
    return result


def _preflight_logical_skill_names(target: Path, new_files: dict[str, bytes]) -> None:
    expected: dict[str, str] = {}
    for relative, data in new_files.items():
        if not relative.startswith("skills/") or not relative.endswith("/SKILL.md"):
            continue
        match = re.search(rb"(?m)^name\s*:\s*([^\s]+)", data)
        if match:
            expected[match.group(1).decode("utf-8").strip("'\"")] = relative
    skills_root = target / "skills"
    if not skills_root.exists():
        return
    for skill_file in sorted(skills_root.rglob("SKILL.md")):
        relative = skill_file.relative_to(target).as_posix()
        if relative in expected.values():
            continue
        if skill_file.is_symlink() or not skill_file.is_file():
            continue
        try:
            text = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        match = re.search(r"(?m)^name\s*:\s*['\"]?([^'\"\s]+)", text)
        if match and match.group(1) in expected:
            raise GstackPrimeCollisionError(
                f"logical skill-name collision for {match.group(1)} at {skill_file}"
            )


def _preflight(
    target: Path,
    new_files: dict[str, bytes],
    manifest: dict[str, object] | None,
    pristine_source: Path,
) -> bool:
    owned = manifest["files"] if manifest else {}
    assert isinstance(owned, dict)
    _safe_target(target, MANIFEST_NAME)
    _preflight_logical_skill_names(target, new_files)
    for relative in sorted(set(new_files) | set(owned)):
        path = _safe_target(target, relative)
        if not path.exists() and not path.is_symlink():
            continue
        if relative not in owned:
            raise GstackPrimeCollisionError(f"unowned target would be overwritten: {path}")
        if path.is_symlink() or not path.is_file() or _file_sha256(path) != owned[relative]:
            raise GstackPrimeCollisionError(f"owned target changed since installation: {path}")
    legacy = target / "skills/gstack"
    if not legacy.exists() and not legacy.is_symlink():
        return False
    if _tree_snapshot(legacy) != _tree_snapshot(pristine_source):
        raise GstackPrimeCollisionError(
            "legacy raw gstack path is not the exact pinned source and may contain "
            f"user content: {legacy}"
        )
    return True


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _remove_empty_parents(path: Path, stop: Path) -> None:
    cursor = path.parent
    while cursor != stop and cursor.is_relative_to(stop):
        try:
            cursor.rmdir()
        except OSError:
            break
        cursor = cursor.parent


def _apply_transaction(
    target: Path,
    new_files: dict[str, bytes],
    modes: dict[str, int],
    old_files: dict[str, object],
    manifest_bytes: bytes,
    migrate_legacy: bool,
    backup_root: Path,
) -> None:
    changed = sorted(set(new_files) | set(old_files))
    backups: dict[str, tuple[bytes, int]] = {}
    manifest_path = target / MANIFEST_NAME
    old_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
    old_manifest_mode = manifest_path.stat().st_mode & 0o777 if manifest_path.exists() else 0o644
    for relative in changed:
        path = target / relative
        if path.is_file() and not path.is_symlink():
            backups[relative] = (path.read_bytes(), path.stat().st_mode & 0o777)
    legacy = target / "skills/gstack"
    legacy_backup = backup_root / "legacy-gstack"
    if migrate_legacy:
        shutil.copytree(legacy, legacy_backup)
    try:
        target.mkdir(parents=True, exist_ok=True)
        for relative, data in sorted(new_files.items()):
            _atomic_write(_safe_target(target, relative), data, modes[relative])
        for relative in sorted(set(old_files) - set(new_files), reverse=True):
            path = _safe_target(target, relative)
            path.unlink()
            _remove_empty_parents(path, target)
        if migrate_legacy:
            shutil.rmtree(legacy)
            _remove_empty_parents(legacy, target)
        _atomic_write(manifest_path, manifest_bytes, 0o644)
    except BaseException:
        for relative in reversed(changed):
            path = target / relative
            if relative in backups:
                data, mode = backups[relative]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                os.chmod(path, mode)
            elif path.exists() or path.is_symlink():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                _remove_empty_parents(path, target)
        if migrate_legacy and not legacy.exists():
            legacy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(legacy_backup, legacy)
        if old_manifest is None:
            if manifest_path.exists() or manifest_path.is_symlink():
                manifest_path.unlink()
                _remove_empty_parents(manifest_path, target)
        else:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_bytes(old_manifest)
            os.chmod(manifest_path, old_manifest_mode)
        raise


def install_prime_gstack(
    checkout: Path,
    coding_agent_dir: Path | None = None,
    *,
    bun: str | Path = "bun",
    renderer_env: Mapping[str, str] | None = None,
) -> GstackPrimeInstallResult:
    """Build and safely install Prime-native skills from the pinned gstack commit."""
    if coding_agent_dir is None:
        configured = os.environ.get("PRIME_AGENT_CODING_AGENT_DIR")
        if not configured:
            raise GstackPrimeSourceError("PRIME_AGENT_CODING_AGENT_DIR is required")
        coding_agent_dir = Path(configured)
    target = coding_agent_dir.expanduser().resolve()
    _validate_shell_safe_profile_root(target)

    checkout = Path(checkout).resolve()
    with tempfile.TemporaryDirectory(
        prefix=".agentops-gstack-prime-", dir=checkout.parent
    ) as temporary:
        temporary_root = Path(temporary)
        pristine = temporary_root / "pristine"
        legacy_expected = temporary_root / "legacy-expected"
        source = temporary_root / "source"
        _extract_pinned_checkout(checkout, pristine)
        shutil.copytree(pristine, legacy_expected)
        shutil.copytree(pristine, source)
        _add_prime_host(source)
        _patch_browse_runtime(source)
        _run(
            [str(bun), "install", "--frozen-lockfile"],
            cwd=source,
            env=renderer_env,
        )
        _run(
            [str(bun), "run", "gen:skill-docs", "--host", "prime"],
            cwd=source,
            env=renderer_env,
        )
        _run([str(bun), "run", "build"], cwd=source, env=renderer_env)
        _run(
            [
                str(bun),
                "build",
                "--compile",
                "browse/src/server.ts",
                "--outfile",
                "browse/dist/browse-server",
                "--external",
                "electron",
                "--external",
                "playwright",
                "--external",
                "playwright-core",
                "--external",
                "@ngrok/ngrok",
            ],
            cwd=source,
            env=renderer_env,
        )
        missing = [
            relative for relative in _REQUIRED_EXECUTABLES if not (source / relative).is_file()
        ]
        if missing:
            raise GstackPrimeSourceError(
                "gstack build did not produce required runtime executables: " + ", ".join(missing)
            )
        for relative in _REQUIRED_EXECUTABLES:
            if not os.access(source / relative, os.X_OK):
                raise GstackPrimeSourceError(
                    f"gstack build produced a non-executable runtime: {relative}"
                )
        new_files, modes = _collect_files(source)
        _bind_profile_root(new_files, target)
        _validate_reference_closure(new_files, target)

        manifest = _load_manifest(target)
        migrate_legacy = _preflight(target, new_files, manifest, legacy_expected)
        old_files = manifest["files"] if manifest else {}
        assert isinstance(old_files, dict)
        fingerprints = {relative: _sha256(data) for relative, data in sorted(new_files.items())}
        manifest_bytes = (
            json.dumps(
                {
                    "schema_version": 1,
                    "owner": MANIFEST_OWNER,
                    "upstream_ref": PINNED_GSTACK_REF,
                    "files": fingerprints,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        _apply_transaction(
            target,
            new_files,
            modes,
            old_files,
            manifest_bytes,
            migrate_legacy,
            temporary_root / "rollback",
        )

    return GstackPrimeInstallResult(
        coding_agent_dir=target,
        upstream_ref=PINNED_GSTACK_REF,
        files=tuple(target / relative for relative in fingerprints),
    )
