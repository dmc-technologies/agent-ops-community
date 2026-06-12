from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    configured = os.environ.get("AGENT_OPS_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]
