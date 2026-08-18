from types import SimpleNamespace

from agent_ops import verify as verify_module
from agent_ops.contracts.job import AgentJob, VerificationCommand


def test_run_verification_uses_cmd_on_windows(tmp_path, monkeypatch) -> None:
    commands: list[list[str]] = []

    def run_command(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(
            exit_code=0,
            elapsed_seconds=0.01,
            stdout_tail="",
            stderr_tail="",
        )

    monkeypatch.setattr(verify_module, "os", SimpleNamespace(name="nt"), raising=False)
    monkeypatch.setattr(verify_module, "run_command", run_command)
    job = AgentJob(
        id="windows-shell",
        title="Windows shell",
        verification=[
            VerificationCommand(name="shell", command="echo agent-ops-local-smoke")
        ],
    )

    verify_module.run_verification(job, tmp_path)

    assert commands == [
        ["cmd.exe", "/d", "/s", "/c", "echo agent-ops-local-smoke"]
    ]


def test_run_verification_preserves_posix_shell(tmp_path, monkeypatch) -> None:
    commands: list[list[str]] = []

    def run_command(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(
            exit_code=0,
            elapsed_seconds=0.01,
            stdout_tail="",
            stderr_tail="",
        )

    monkeypatch.setattr(verify_module, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(verify_module, "run_command", run_command)
    job = AgentJob(
        id="posix-shell",
        title="POSIX shell",
        verification=[
            VerificationCommand(name="shell", command="echo agent-ops-local-smoke")
        ],
    )

    verify_module.run_verification(job, tmp_path)

    assert commands == [["/bin/sh", "-lc", "echo agent-ops-local-smoke"]]
