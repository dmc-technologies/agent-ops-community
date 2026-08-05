from agent_ops.verify import verification_shell_command


def test_verification_shell_command_uses_cmd_on_windows() -> None:
    assert verification_shell_command(
        "echo agent-ops-local-smoke",
        os_name="nt",
    ) == ["cmd.exe", "/d", "/s", "/c", "echo agent-ops-local-smoke"]


def test_verification_shell_command_preserves_posix_shell() -> None:
    assert verification_shell_command(
        "echo agent-ops-local-smoke",
        os_name="posix",
    ) == ["/bin/sh", "-lc", "echo agent-ops-local-smoke"]
