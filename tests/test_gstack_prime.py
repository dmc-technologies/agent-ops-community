from __future__ import annotations

import json
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
