from __future__ import annotations

import subprocess
import time
from pathlib import Path

from agent_ops.contracts.result import CommandResult


def tail_text(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def run_command(
    command: list[str],
    cwd: str | Path,
    timeout_seconds: int | None = None,
) -> CommandResult:
    started = time.monotonic()
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    elapsed = time.monotonic() - started
    return CommandResult(
        command=command,
        cwd=str(cwd),
        exit_code=completed.returncode,
        stdout_tail=tail_text(completed.stdout),
        stderr_tail=tail_text(completed.stderr),
        elapsed_seconds=elapsed,
    )
