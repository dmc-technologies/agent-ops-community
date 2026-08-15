from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import agent_ops.gstack_prime as gstack_prime


def _upstream_repo(path: Path) -> tuple[Path, Path, str]:
    path.mkdir()
    for directory in ("hosts", "scripts", "bin", "review", "qa/templates", "browse/src"):
        (path / directory).mkdir(parents=True, exist_ok=True)
    (path / "hosts/index.ts").write_text(
        "import claude from './claude';\nexport const ALL_HOST_CONFIGS = [claude];\n"
    )
    (path / "hosts/claude.ts").write_text("export default {};\n")
    (path / "scripts/gen-skill-docs.ts").write_text("generator\n")
    (path / "browse/src/cli.ts").write_text(
        "const server = path.resolve(path.dirname(execPath), '..', 'src', 'server.ts');\n"
        "const process = Bun.spawn(['bun', 'run', SERVER_SCRIPT], {\n"
        "  env: { ...process.env, BROWSE_STATE_FILE: config.stateFile, "
        "BROWSE_PARENT_PID: parentPid, ...extraEnv },\n"
        "});\n"
    )
    (path / "browse/src/server.ts").write_text("console.log('server')\n")
    (path / "browse/src/cdp-allowlist.ts").write_text("export const CDP_ALLOWLIST = {}\n")
    (path / "package.json").write_text('{"scripts":{"build":"fixture"}}\n')
    (path / "bun.lock").write_text("frozen lock\n")
    (path / "bin/gstack-config").write_text("#!/bin/sh\necho ~/.claude/skills/gstack\n")
    (path / "ETHOS.md").write_text("runtime ethos\n")
    (path / "review/checklist.md").write_text("review checklist\n")
    (path / "qa/templates/report.md").write_text("qa report\n")
    (path / "review-link").symlink_to("review", target_is_directory=True)
    bun = path.parent / "fake-bun"
    bun.write_text(
        """#!/usr/bin/env python3
from pathlib import Path
import sys
root = Path.cwd()
with (Path(sys.argv[0]).parent / "bun-commands.log").open("a") as stream:
    stream.write(" ".join(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["install", "--frozen-lockfile"]:
    raise SystemExit(0)
if sys.argv[1:3] == ["run", "gen:skill-docs"]:
    assert (root / "hosts/prime.ts").exists()
    assert "--host" in sys.argv and "prime" in sys.argv
    out = root / ".prime/skills/gstack-review"
    out.mkdir(parents=True)
    (out / "SKILL.md").write_text('''---
name: gstack-review
description: Review code safely.
---
# Review
Use /gstack-qa and /qa. Use ~/.claude/skills/gstack/bin/gstack-config.
Use the Bash tool and the Read tool. Use AskUserQuestion for decisions.
Use the Agent tool with subagent_type for parallel review.
Use $HOME/.claude/skills/gstack/bin/gstack-config too.
Read ~/.claude/skills/review/checklist.md before review.
## Skill Invocation During Plan Mode
Use mcp__host__AskUserQuestion, then call ExitPlanMode.
## AskUserQuestion Format
Call AskUserQuestion or report BLOCKED.
## Next step
Continue safely.
## Model-Specific Behavioral Patch (claude)
Claude-only overlay.
''')
    qa = root / ".prime/skills/gstack-qa"
    qa.mkdir(parents=True)
    (qa / "SKILL.md").write_text("---\\nname: gstack-qa\\ndescription: QA safely.\\n---\\n")
    raise SystemExit(0)
if sys.argv[1:3] == ["run", "build"]:
    for rel in (
        "browse/dist/browse", "browse/dist/find-browse",
        "design/dist/design", "make-pdf/dist/pdf",
    ):
        artifact = root / rel
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("#!/bin/sh\\n")
        artifact.chmod(0o755)
    raise SystemExit(0)
if sys.argv[1:4] == ["build", "--compile", "browse/src/server.ts"]:
    artifact = root / "browse/dist/browse-server"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("#!/bin/sh\\n")
    artifact.chmod(0o755)
    raise SystemExit(0)
raise SystemExit(9)
"""
    )
    bun.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    ref = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return path, bun, ref


def test_install_generates_namespaced_prime_skills_and_built_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, bun, ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", ref)
    target = tmp_path / "prime-agent"
    result = gstack_prime.install_prime_gstack(upstream, target, bun=bun)

    skill = target / "skills/agentops-gstack-review/SKILL.md"
    content = skill.read_text()
    assert result.upstream_ref == ref
    assert "name: agentops-gstack-review" in content
    assert "name: agentops-gstack-qa" in (target / "skills/agentops-gstack-qa/SKILL.md").read_text()
    assert "/skill:agentops-gstack-qa" in content
    runtime = f"{target.resolve().as_posix()}/.agentops/runtime/gstack"
    assert f"{runtime}/bin/gstack-config" in content
    assert f"{runtime}/review/checklist.md" in content
    assert "IPython" in content
    assert "RLM" in content and "agent_message" in content
    assert "ordinary question" in content
    assert "Claude" not in content and ".claude" not in content
    assert "Model-Specific Behavioral Patch" not in content
    assert "mcp__" not in content and "ExitPlanMode" not in content
    assert "PRIME_AGENT_CODING_AGENT_DIR" not in content
    assert "ordinary assistant response" in content
    for executable in (
        "browse/dist/browse",
        "browse/dist/find-browse",
        "browse/dist/browse-server",
        "design/dist/design",
        "make-pdf/dist/pdf",
    ):
        assert (target / ".agentops/runtime/gstack" / executable).stat().st_mode & 0o111
    assert (target / ".agentops/runtime/gstack/review/checklist.md").exists()
    assert (target / ".agentops/runtime/gstack/qa/templates/report.md").exists()
    runtime_config = (target / ".agentops/runtime/gstack/bin/gstack-config").read_text()
    assert ".claude/skills/gstack" not in runtime_config
    assert "PRIME_AGENT_CODING_AGENT_DIR" not in runtime_config
    assert target.resolve().as_posix() in runtime_config
    commands = (tmp_path / "bun-commands.log").read_text()
    assert "install --frozen-lockfile" in commands
    assert "run build" in commands
    manifest = json.loads((target / gstack_prime.MANIFEST_NAME).read_text())
    assert manifest["owner"] == gstack_prime.MANIFEST_OWNER
    assert manifest["files"]["skills/agentops-gstack-review/SKILL.md"]


def test_install_uses_explicit_renderer_environment_for_bun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, bun, ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", ref)
    environment_log = tmp_path / "renderer-environment.json"
    wrapper = tmp_path / "environment-bun"
    required = (
        "HOME",
        "BUN_INSTALL",
        "BUN_INSTALL_CACHE_DIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
    )
    wrapper.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"required = {required!r}\n"
        "values = {name: os.environ[name] for name in required}\n"
        "for value in values.values():\n"
        "    path = Path(value)\n"
        "    path.mkdir(parents=True, exist_ok=True)\n"
        "    (path / 'renderer-write').write_text('confined')\n"
        f"Path({str(environment_log)!r}).write_text(json.dumps(values))\n"
        f"os.execv({str(bun)!r}, [{str(bun)!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    workspace = tmp_path / "cache/render"
    renderer_env = {"PATH": os.environ["PATH"]}
    renderer_env.update(
        {
            "HOME": str(workspace / "home"),
            "BUN_INSTALL": str(workspace / "bun/install"),
            "BUN_INSTALL_CACHE_DIR": str(workspace / "bun/cache"),
            "XDG_CACHE_HOME": str(workspace / "xdg/cache"),
            "XDG_CONFIG_HOME": str(workspace / "xdg/config"),
            "XDG_DATA_HOME": str(workspace / "xdg/data"),
            "TMPDIR": str(workspace / "tmp"),
            "TMP": str(workspace / "tmp"),
            "TEMP": str(workspace / "tmp"),
        }
    )

    gstack_prime.install_prime_gstack(
        upstream,
        tmp_path / "prime-agent",
        bun=wrapper,
        renderer_env=renderer_env,
    )

    recorded = json.loads(environment_log.read_text())
    assert recorded == {name: renderer_env[name] for name in required}
    assert all(Path(value).is_relative_to(tmp_path / "cache") for value in recorded.values())
    assert all((Path(value) / "renderer-write").is_file() for value in recorded.values())


def test_install_refuses_colliding_user_file_without_partial_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, bun, ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", ref)
    target = tmp_path / "prime-agent"
    collision = target / "skills/agentops-gstack-review/SKILL.md"
    collision.parent.mkdir(parents=True)
    collision.write_text("user content\n")
    with pytest.raises(gstack_prime.GstackPrimeCollisionError, match="unowned"):
        gstack_prime.install_prime_gstack(upstream, target, bun=bun)
    assert collision.read_text() == "user content\n"
    assert not (target / ".agentops/runtime").exists()
    assert not (target / gstack_prime.MANIFEST_NAME).exists()


def test_update_refuses_modified_owned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, bun, ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", ref)
    target = tmp_path / "prime-agent"
    gstack_prime.install_prime_gstack(upstream, target, bun=bun)
    skill = target / "skills/agentops-gstack-review/SKILL.md"
    skill.write_text("user modification\n")
    with pytest.raises(gstack_prime.GstackPrimeCollisionError, match="changed since installation"):
        gstack_prime.install_prime_gstack(upstream, target, bun=bun)
    assert skill.read_text() == "user modification\n"


def test_install_requires_exact_pinned_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, bun, _ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", "0" * 40)
    with pytest.raises(gstack_prime.GstackPrimeSourceError, match="pinned gstack commit"):
        gstack_prime.install_prime_gstack(upstream, tmp_path / "prime-agent", bun=bun)


def test_install_fails_clearly_when_bun_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, _bun, ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", ref)
    with pytest.raises(gstack_prime.GstackPrimeSourceError, match="Bun executable"):
        gstack_prime.install_prime_gstack(
            upstream, tmp_path / "prime-agent", bun=tmp_path / "missing-bun"
        )


def test_exact_legacy_raw_bundle_is_migrated_but_user_content_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, bun, ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", ref)
    target = tmp_path / "prime-agent"
    legacy = target / "skills/gstack"
    shutil.copytree(upstream, legacy, ignore=shutil.ignore_patterns(".git"))
    gstack_prime.install_prime_gstack(upstream, target, bun=bun)
    assert not legacy.exists()

    other_target = tmp_path / "other-prime-agent"
    other_legacy = other_target / "skills/gstack"
    other_legacy.mkdir(parents=True)
    (other_legacy / "SKILL.md").write_text("user content\n")
    with pytest.raises(gstack_prime.GstackPrimeCollisionError, match="legacy raw gstack"):
        gstack_prime.install_prime_gstack(upstream, other_target, bun=bun)
    assert (other_legacy / "SKILL.md").read_text() == "user content\n"


def test_write_failure_rolls_back_all_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, bun, ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", ref)
    target = tmp_path / "prime-agent"
    original = gstack_prime._atomic_write
    calls = 0

    def fail_second(path: Path, data: bytes, mode: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        original(path, data, mode)

    monkeypatch.setattr(gstack_prime, "_atomic_write", fail_second)
    with pytest.raises(OSError, match="injected"):
        gstack_prime.install_prime_gstack(upstream, target, bun=bun)
    assert not (target / "skills/agentops-gstack-review/SKILL.md").exists()
    assert not (target / gstack_prime.MANIFEST_NAME).exists()


def test_legacy_raw_bundle_with_added_empty_directory_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, bun, ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", ref)
    target = tmp_path / "prime-agent"
    legacy = target / "skills/gstack"
    shutil.copytree(upstream, legacy, ignore=shutil.ignore_patterns(".git"))
    (legacy / "person-owned-empty-directory").mkdir()
    with pytest.raises(gstack_prime.GstackPrimeCollisionError, match="legacy raw gstack"):
        gstack_prime.install_prime_gstack(upstream, target, bun=bun)
    assert (legacy / "person-owned-empty-directory").is_dir()


def test_manifest_path_escape_is_refused_without_touching_outside_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, bun, ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", ref)
    target = tmp_path / "prime-agent"
    outside = tmp_path / "outside.txt"
    outside.write_text("person-owned\n")
    manifest = target / gstack_prime.MANIFEST_NAME
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "owner": gstack_prime.MANIFEST_OWNER,
                "upstream_ref": ref,
                "files": {"../outside.txt": "0" * 64},
            }
        )
    )
    with pytest.raises(gstack_prime.GstackPrimeCollisionError, match="unsafe managed path"):
        gstack_prime.install_prime_gstack(upstream, target, bun=bun)
    assert outside.read_text() == "person-owned\n"


def test_logical_namespaced_skill_collision_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, bun, ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", ref)
    target = tmp_path / "prime-agent"
    collision = target / "skills/person-skill/SKILL.md"
    collision.parent.mkdir(parents=True)
    collision.write_text("---\nname: agentops-gstack-review\ndescription: person\n---\n")
    before = collision.read_bytes()
    with pytest.raises(
        gstack_prime.GstackPrimeCollisionError, match="logical skill-name collision"
    ):
        gstack_prime.install_prime_gstack(upstream, target, bun=bun)
    assert collision.read_bytes() == before


def test_install_rejects_profile_path_unsafe_for_generated_shell_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream, bun, ref = _upstream_repo(tmp_path / "upstream")
    monkeypatch.setattr(gstack_prime, "PINNED_GSTACK_REF", ref)
    target = tmp_path / "Prime Agent"

    with pytest.raises(gstack_prime.GstackPrimeSourceError, match="unquoted POSIX shell word"):
        gstack_prime.install_prime_gstack(upstream, target, bun=bun)

    assert not target.exists()
    assert not (tmp_path / "bun-commands.log").exists()


def test_reference_validator_rejects_provider_and_excluded_skill_routes(tmp_path: Path) -> None:
    target = tmp_path / "prime"
    files = {
        "skills/agentops-gstack-review/SKILL.md": b"Run codex exec and then /ship.\n",
        ".agentops/runtime/gstack/browse/src/cdp-allowlist.ts": b"allowlist\n",
    }

    with pytest.raises(gstack_prime.GstackPrimeSourceError, match="codex exec"):
        gstack_prime._validate_reference_closure(files, target)


def test_unsupported_route_adaptation_preserves_surrounding_safety_and_source_rules() -> None:
    content = """---
name: gstack-review
description: Review safely.
---
Never commit, push, or open a PR; do not invoke /ship during review.
STOP on GitLab or an unknown platform instead of invoking /ship.
Treat the existing design document as source of truth before /ship.
"""

    adapted = gstack_prime._adapt_prime_contract(
        content,
        {"gstack-review"},
        "agentops-gstack-review",
    )

    assert "Never commit, push, or open a PR" in adapted
    assert "STOP on GitLab or an unknown platform" in adapted
    assert "existing design document as source of truth" in adapted
    assert "/ship" not in adapted
    assert "user-approved manual pull-request and release steps" in adapted


def test_reference_validation_does_not_treat_profile_segments_as_skill_routes(
    tmp_path: Path,
) -> None:
    profile = tmp_path / "ship" / "prime"
    files = {
        "skills/agentops-gstack-review/SKILL.md": (
            f"runtime: {profile}/.agentops/runtime/gstack/bin/gstack-config\n".encode()
        ),
        ".agentops/runtime/gstack/browse/src/cdp-allowlist.ts": b"allowlist\n",
    }

    gstack_prime._validate_reference_closure(files, profile)


def test_excluded_workflow_fallbacks_are_actionable() -> None:
    plan_review = gstack_prime._adapt_prime_contract(
        """---
name: gstack-plan-devex-review
description: Review developer experience.
---
## Prerequisite Skill Offer
Run `/office-hours` and read its skill file before continuing.
## Review
Preserve this review body.
""",
        {"gstack-plan-devex-review"},
        "agentops-gstack-plan-devex-review",
    )
    assert "ask whether to use the current plan as the sole review" in plan_review
    assert "Preserve this review body." in plan_review
    assert "/office-hours" not in plan_review

    scrape = gstack_prime._adapt_prime_contract(
        """---
name: gstack-scrape
description: Extract data.
---
## Step 5 — Skillify nudge
Say `/skillify` to install this permanently.
## When the prototype fails
Do not persist a broken prototype.
""",
        {"gstack-scrape"},
        "agentops-gstack-scrape",
    )
    assert "Prime does not automate browser-skill installation" in scrape
    assert "return the extracted JSON and the complete tested script" in scrape
    assert "Do not persist a broken prototype." in scrape
    assert "/skillify" not in scrape

    routing = gstack_prime._adapt_prime_contract(
        """---
name: gstack
description: Route workflows.
---
- User describes a new idea → invoke `ordinary product discussion`
- User asks to create a PR → invoke `manual pull-request and release preparation`
- User asks for a second opinion → invoke `an external review that is not run by Prime`
""",
        {"gstack"},
        "agentops-gstack",
    )
    assert "discuss the product directly with the user" in routing
    assert "inspect the origin remote" in routing
    assert "`glab mr create`" in routing
    assert "unknown provider STOP" in routing
    assert "continue with the retained Prime review" in routing
    assert "→ invoke" not in routing


def test_nested_review_gates_survive_fallback_adaptation() -> None:
    engineering = gstack_prime._adapt_prime_contract(
        """---
name: gstack-plan-eng-review
description: Review engineering.
---
## Prerequisite Skill Offer
Run `/office-hours` before review.
### Step 0: Scope Challenge
If the plan touches more than 8 files, **STOP.** Do NOT proceed until the user responds.
## Section 1
Continue only after Step 0.
""",
        {"gstack-plan-eng-review"},
        "agentops-gstack-plan-eng-review",
    )
    assert "### Step 0: Scope Challenge" in engineering
    assert "more than 8 files" in engineering
    assert "**STOP.** Do NOT proceed" in engineering
    assert "/office-hours" not in engineering

    executive = gstack_prime._adapt_prime_contract(
        """---
name: gstack-plan-ceo-review
description: Review product direction.
---
## Prerequisite Skill Offer
Run `/office-hours` before review.
**Mid-session detection:** Run `/office-hours` if the problem is unclear.
### Retrospective Check
Inspect prior outcomes.
### Frontend/UI Scope Detection
Inspect UI scope.
### Landscape Check
Inspect current alternatives.
## Section 1
Continue the review.
""",
        {"gstack-plan-ceo-review"},
        "agentops-gstack-plan-ceo-review",
    )
    assert "### Retrospective Check" in executive
    assert "### Frontend/UI Scope Detection" in executive
    assert "### Landscape Check" in executive
    assert "Discuss the product directly" in executive
    assert "/office-hours" not in executive


def test_shared_and_root_routing_uses_concrete_supported_actions() -> None:
    content = """---
name: gstack
description: Route requests.
---
Ship/deploy/PR → invoke /ship or /land-and-deploy
Full review pipeline → invoke /autoplan
User asks for safety mode, careful mode → invoke `/careful` or `/guard`
User asks to restrict edits to a directory → invoke `/freeze` or `/unfreeze`
User asks to launch a real browser for QA, "open the browser" → invoke `/open-gstack-browser`
"""
    names = {
        "gstack",
        "gstack-plan-ceo-review",
        "gstack-plan-eng-review",
        "gstack-plan-design-review",
        "gstack-plan-devex-review",
        "gstack-browse",
    }
    adapted = gstack_prime._adapt_prime_contract(content, names, "agentops-gstack")

    assert "Pull-request creation → inspect the origin remote" in adapted
    assert "`gh pr create`" in adapted
    assert "`glab mr create`" in adapted
    assert "unknown provider STOP" in adapted
    assert "repository-owned merge and deployment procedure" in adapted
    assert "STOP if none is supplied" in adapted
    assert "agentops-gstack-land-and-deploy" not in adapted
    assert "ask for an explicit safety boundary" in adapted
    assert "confine all reads and writes" in adapted
    assert "Prime does not provide hook enforcement" in adapted
    assert "/skill:agentops-gstack-browse" in adapted
    for review in ("ceo", "eng", "design", "devex"):
        assert f"/skill:agentops-gstack-plan-{review}-review" in adapted
    for excluded in ("/ship", "/autoplan", "/careful", "/guard", "/freeze", "/unfreeze"):
        assert excluded not in adapted
