from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from agent_ops.contracts.job import AgentJob
from agent_ops.contracts.result import RunResult, RunStatus, VerificationResult
from agent_ops.process import run_command


def _windows_command_processor() -> str:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_system_directory = kernel32.GetSystemDirectoryW
    get_system_directory.argtypes = (ctypes.c_wchar_p, ctypes.c_uint)
    get_system_directory.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_system_directory(buffer, len(buffer))
    if length == 0:
        error = ctypes.get_last_error()
        raise OSError(error, "GetSystemDirectoryW failed")
    if length >= len(buffer):
        raise OSError("Windows system directory exceeds the supported path length")
    command_processor = Path(buffer.value) / "cmd.exe"
    if not command_processor.is_absolute():
        raise OSError("Windows system directory is not absolute")
    return str(command_processor)


def verification_shell_command(command: str) -> list[str]:
    if os.name == "nt":
        return [_windows_command_processor(), "/d", "/s", "/c", command]
    return ["/bin/sh", "-lc", command]


def run_verification(job: AgentJob, base_dir: str | Path) -> RunResult:
    started = datetime.now(UTC)
    root = Path(base_dir).resolve()
    results: list[VerificationResult] = []

    for check in job.verification:
        cwd = Path(check.cwd) if check.cwd else root
        if not cwd.is_absolute():
            cwd = (root / cwd).resolve()
        command_result = run_command(
            verification_shell_command(check.command),
            cwd=cwd,
            timeout_seconds=check.timeout_seconds,
        )
        ok = command_result.exit_code == check.expected_exit
        results.append(
            VerificationResult(
                name=check.name,
                command=check.command,
                status=RunStatus.PASS if ok else RunStatus.FAIL,
                exit_code=command_result.exit_code,
                elapsed_seconds=command_result.elapsed_seconds,
                stdout_tail=command_result.stdout_tail,
                stderr_tail=command_result.stderr_tail,
            )
        )

    status = (
        RunStatus.PASS
        if all(result.status == RunStatus.PASS for result in results)
        else RunStatus.FAIL
    )
    return RunResult(
        job_id=job.id,
        runner=job.runner,
        mode="verify",
        status=status,
        started_at=started,
        finished_at=datetime.now(UTC),
        verification=results,
    )
