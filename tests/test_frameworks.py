from __future__ import annotations

from pathlib import Path

from agent_ops.bootstrap import SUPPORTED_BOOTSTRAPS, bootstrap_text, write_all_bootstraps
from agent_ops.context import build_context_pack
from agent_ops.contracts.job import AgentJob, JobMode, VerificationCommand
from agent_ops.frameworks import ADAPTERS, get_adapter
from agent_ops.registries import Framework

GENERIC_PRIVATE_FRAMEWORKS = {
    Framework.CLAUDE_CODE,
    Framework.CODEX,
    Framework.CURSOR,
    Framework.OPENCODE,
    Framework.OPENCLAW,
    Framework.LOCAL,
}


def make_job() -> AgentJob:
    return AgentJob(
        id="framework-proof",
        title="Framework Proof",
        runner="local",
        mode=JobMode.VERIFY_ONLY,
        verification=[VerificationCommand(name="ok", command="echo ok")],
    )


def test_public_core_supports_private_generic_framework_set() -> None:
    assert set(SUPPORTED_BOOTSTRAPS) == GENERIC_PRIVATE_FRAMEWORKS
    assert set(ADAPTERS) == GENERIC_PRIVATE_FRAMEWORKS


def test_public_bootstrap_writes_generic_framework_files(tmp_path: Path) -> None:
    written = write_all_bootstraps(tmp_path)
    written_paths = {path.relative_to(tmp_path).as_posix() for path in written}

    assert "README.md" in written_paths
    for framework in GENERIC_PRIVATE_FRAMEWORKS:
        assert f"{framework.value}/AGENTOPS.md" in written_paths


def test_public_bootstrap_only_advertises_supported_skill_installs() -> None:
    assert "agentops skills install codex" in bootstrap_text(Framework.CODEX)
    assert "agentops skills install claude-code" in bootstrap_text(Framework.CLAUDE_CODE)
    assert "agentops skills install opencode" in bootstrap_text(Framework.OPENCODE)
    assert "agentops skills install cursor" not in bootstrap_text(Framework.CURSOR)
    assert "agentops skills install local" not in bootstrap_text(Framework.LOCAL)


def test_public_context_and_handoff_work_for_generic_frameworks() -> None:
    job = make_job()

    for framework in GENERIC_PRIVATE_FRAMEWORKS:
        context_pack = build_context_pack(job, framework, sources=["AGENTS.md"])
        command = get_adapter(framework).build_command(job, context_pack, Path.cwd())

        assert context_pack.framework == framework
        assert context_pack.id == f"framework-proof-{framework.value}"
        assert any("agent-knowledge" in item for item in context_pack.instructions)
        assert command.framework == framework
        assert command.command
