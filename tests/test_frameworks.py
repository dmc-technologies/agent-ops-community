from __future__ import annotations

import shlex
from pathlib import Path

import pytest

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
    Framework.PRIME_AGENT,
    Framework.OPENCLAW,
    Framework.LOCAL,
}


SETUP_ARGUMENTS = {
    Framework.CLAUDE_CODE: ("claude",),
    Framework.CODEX: ("codex", "login"),
    Framework.CURSOR: ("cursor-agent", "login"),
    Framework.OPENCODE: ("opencode", "auth", "login"),
    Framework.PRIME_AGENT: ("prime-agent",),
    Framework.OPENCLAW: ("openclaw", "onboard"),
    Framework.LOCAL: ("sh",),
}


def _setup_prerequisite(framework: Framework, home: Path) -> str:
    adapter = get_adapter(framework)
    return shlex.join(
        [
            "env",
            f"{adapter.home_environment_variable}={home}",
            *SETUP_ARGUMENTS[framework],
        ]
    )


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


def test_public_bootstrap_advertises_supported_skill_installs() -> None:
    for framework in GENERIC_PRIVATE_FRAMEWORKS - {Framework.LOCAL}:
        assert f"agentops skills install {framework.value}" in bootstrap_text(framework)
    assert "agentops skills install local" not in bootstrap_text(Framework.LOCAL)
    claude_bootstrap = bootstrap_text(Framework.CLAUDE_CODE)
    assert "CLAUDE_CONFIG_DIR" in claude_bootstrap
    assert "CLAUDE_HOME" not in claude_bootstrap
    openclaw_bootstrap = bootstrap_text(Framework.OPENCLAW)
    assert "OPENCLAW_HOME" not in openclaw_bootstrap
    assert "OPENCLAW_STATE_DIR" not in openclaw_bootstrap
    assert "OPENCLAW_PROFILE" not in openclaw_bootstrap

    prime_bootstrap = bootstrap_text(Framework.PRIME_AGENT)
    assert (
        'export PRIME_AGENT_CODING_AGENT_DIR='
        '"${PRIME_AGENT_CODING_AGENT_DIR:-$HOME/.prime/agent}"'
        in prime_bootstrap
    )


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


def test_prime_agent_handoff_uses_print_mode_and_explicit_cwd() -> None:
    job = make_job()
    context_pack = build_context_pack(job, Framework.PRIME_AGENT)
    cwd = Path("/tmp/agentops-prime-proof")

    command = get_adapter(Framework.PRIME_AGENT).build_command(job, context_pack, cwd)

    assert command.command[:5] == [
        "prime-agent",
        "--print",
        "--cwd",
        str(cwd),
        "--",
    ]
    assert "# Context Pack: framework-proof-prime-agent" in command.command[5]
    assert command.cwd == str(cwd)
    assert {skill.id for skill in context_pack.skills} >= {"gstack", "superpowers"}
    assert {tool.id for tool in context_pack.tools} >= {"github", "prime-agent-cli"}


def test_framework_target_environments_are_exact_and_isolated(tmp_path: Path) -> None:
    expected = {
        Framework.CLAUDE_CODE: "CLAUDE_HOME",
        Framework.CODEX: "CODEX_HOME",
        Framework.CURSOR: "CURSOR_HOME",
        Framework.OPENCLAW: "OPENCLAW_HOME",
        Framework.OPENCODE: "OPENCODE_CONFIG_DIR",
        Framework.PRIME_AGENT: "PRIME_AGENT_CODING_AGENT_DIR",
        Framework.LOCAL: "AGENT_OPS_LOCAL_HOME",
    }

    for framework, variable in expected.items():
        home = tmp_path / framework.value
        assert get_adapter(framework).target_environment(home) == {variable: str(home)}


def test_codex_readiness_checks_native_auth_state_without_reading_it(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    auth = home / "auth.json"
    auth.write_text("do-not-read", encoding="utf-8")

    def refuse_read(*_args, **_kwargs):
        raise AssertionError("credential contents must not be read")

    monkeypatch.setattr(Path, "read_bytes", refuse_read)
    monkeypatch.setattr(Path, "read_text", refuse_read)
    monkeypatch.setattr("agent_ops.frameworks.base.shutil.which", lambda _name: "/bin/tool")

    readiness = get_adapter(Framework.CODEX).target_readiness(home)

    assert readiness.ready is True
    assert readiness.prerequisite is None


def test_codex_readiness_reports_native_login_command(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setattr("agent_ops.frameworks.base.shutil.which", lambda _name: "/bin/codex")

    readiness = get_adapter(Framework.CODEX).target_readiness(home)

    assert readiness.ready is False
    assert readiness.prerequisite == _setup_prerequisite(Framework.CODEX, home)


@pytest.mark.parametrize("marker_kind", ["directory", "symlink", "unreadable"])
def test_codex_readiness_rejects_unsafe_or_uninspectable_auth_marker(
    tmp_path: Path, monkeypatch, marker_kind: str
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    marker = home / "auth.json"
    if marker_kind == "directory":
        marker.mkdir()
    elif marker_kind == "symlink":
        marker.symlink_to(tmp_path / "credential-outside-home")
    else:
        original_lstat = Path.lstat

        def fail_marker_lstat(path: Path):
            if path == marker:
                raise PermissionError("marker cannot be inspected")
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", fail_marker_lstat)
    monkeypatch.setattr("agent_ops.frameworks.base.shutil.which", lambda _name: "/bin/codex")

    readiness = get_adapter(Framework.CODEX).target_readiness(home)

    assert readiness.ready is False
    assert readiness.prerequisite == _setup_prerequisite(Framework.CODEX, home)


@pytest.mark.parametrize(
    "framework",
    [
        Framework.CLAUDE_CODE,
        Framework.CURSOR,
        Framework.OPENCODE,
        Framework.PRIME_AGENT,
        Framework.OPENCLAW,
    ],
)
def test_authenticated_frameworks_fail_closed_without_safe_native_proof(
    tmp_path: Path, monkeypatch, framework: Framework
) -> None:
    home = tmp_path / framework.value
    home.mkdir()
    adapter = get_adapter(framework)
    monkeypatch.setattr("agent_ops.frameworks.base.shutil.which", lambda _name: "/bin/tool")

    readiness = adapter.target_readiness(home)

    assert readiness.ready is False
    assert readiness.prerequisite == _setup_prerequisite(framework, home)


def test_local_readiness_requires_safe_home_and_executable(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "local"
    home.mkdir()
    adapter = get_adapter(Framework.LOCAL)
    monkeypatch.setattr("agent_ops.frameworks.base.shutil.which", lambda _name: "/bin/sh")

    assert adapter.target_readiness(home).ready is True

    home.rename(tmp_path / "moved")
    assert adapter.target_readiness(home).ready is False


def test_readiness_checks_executable_before_accepting_native_marker(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text("do-not-read", encoding="utf-8")
    monkeypatch.setattr("agent_ops.frameworks.base.shutil.which", lambda _name: None)

    readiness = get_adapter(Framework.CODEX).target_readiness(home)

    assert readiness.ready is False
    assert readiness.prerequisite == _setup_prerequisite(Framework.CODEX, home)


@pytest.mark.parametrize(
    "home_name",
    [
        "space home",
        "single'quote",
        'double"quote;$HOME',
        "line\nbreak",
    ],
)
@pytest.mark.parametrize("framework", sorted(GENERIC_PRIVATE_FRAMEWORKS, key=str))
def test_setup_prerequisites_quote_every_home_as_one_inert_argument(
    tmp_path: Path, framework: Framework, home_name: str
) -> None:
    home = tmp_path / home_name

    readiness = get_adapter(framework).target_readiness(home)

    assert readiness.ready is False
    assert readiness.prerequisite == _setup_prerequisite(framework, home)
