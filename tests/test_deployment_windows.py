from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_ops.deployment import transaction as transaction_module
from agent_ops.deployment.models import PlannedFile, ProviderPlan, TargetSpec
from agent_ops.deployment.transaction import audit_provider_plans, install_provider_plans
from agent_ops.registries.models import Framework


def _windows_plan(home: Path) -> ProviderPlan:
    return ProviderPlan(
        provider_id="fixture",
        source_revision="1" * 40,
        target=TargetSpec("codex-windows", Framework.CODEX, home, "stable"),
        files=(
            PlannedFile(Path("AGENTS.md"), "Use “UTF-8” policy.\n".encode(), 0o644),
            PlannedFile(
                Path("hooks/portable_stop.py"),
                b"raise SystemExit(0)\n",
                0o755,
            ),
            PlannedFile(
                Path("skills/example/SKILL.md"),
                b"---\nname: example\n---\n",
                0o644,
            ),
        ),
    )


def test_public_transaction_api_dispatches_to_native_windows_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _windows_plan(tmp_path / "codex")
    expected = (SimpleNamespace(transaction_id="windows-transaction"),)
    calls: list[tuple[ProviderPlan, ...]] = []
    backend = SimpleNamespace(
        install_provider_plans=lambda plans, channel_transitions=None: (
            calls.append(plans) or expected
        )
    )
    monkeypatch.setattr(transaction_module, "_POSIX_SUPPORTED", False)
    monkeypatch.setattr(transaction_module, "_WINDOWS_SUPPORTED", True, raising=False)
    monkeypatch.setattr(
        transaction_module,
        "_windows_transaction_backend",
        lambda: backend,
        raising=False,
    )

    manifests = install_provider_plans((plan,))

    assert manifests == expected
    assert calls == [(plan,)]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows")
def test_native_windows_first_install_and_identical_refresh_are_complete_and_auditable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "codex"
    plan = _windows_plan(home)

    first = install_provider_plans((plan,))
    first_bytes = {item.path: (home / item.path).read_bytes() for item in plan.files}
    second = install_provider_plans((plan,))

    assert len(first) == len(second) == 1
    assert first[0].files == second[0].files
    assert {item.path: (home / item.path).read_bytes() for item in plan.files} == first_bytes
    assert (home / "AGENTS.md").read_text(encoding="utf-8") == "Use “UTF-8” policy.\n"
    assert audit_provider_plans((plan,)).matches
