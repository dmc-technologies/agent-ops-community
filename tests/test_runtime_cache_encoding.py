"""Managed cache validation accepts equivalent marshal encodings, not trailing data."""

import importlib.util
import marshal
from pathlib import Path

import pytest

from agent_ops.deployment.transaction import _runtime_python_cache_content_is_valid


@pytest.mark.parametrize("version", [2, 4])
def test_valid_cache_does_not_require_identical_reserialization(tmp_path: Path, version: int):
    source = tmp_path / "skill.py"
    source.write_bytes(b'value = "shared skill"\n')
    source.chmod(0o644)
    source_stat = source.stat()
    body = marshal.dumps(compile(source.read_bytes(), str(source), "exec"), version)
    cache = tmp_path / "skill.pyc"
    header = (
        importlib.util.MAGIC_NUMBER
        + bytes(4)
        + int(source_stat.st_mtime).to_bytes(4, "little")
        + len(source.read_bytes()).to_bytes(4, "little")
    )
    cache.write_bytes(header + body)
    cache.chmod(0o644)
    assert _runtime_python_cache_content_is_valid(
        cache.read_bytes(), cache.stat(), source.read_bytes(), source_stat
    )
    cache.write_bytes(header + body + b"unexpected trailing bytes")
    assert not _runtime_python_cache_content_is_valid(
        cache.read_bytes(), cache.stat(), source.read_bytes(), source_stat
    )
    cache.write_bytes(header + marshal.dumps(compile('value = "changed"', str(source), "exec")))
    assert not _runtime_python_cache_content_is_valid(
        cache.read_bytes(), cache.stat(), source.read_bytes(), source_stat
    )
