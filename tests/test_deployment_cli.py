from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops.cli import app
from agent_ops.deployment.models import (
    DeploymentPlan,
    DeploymentReceipt,
    ProviderPlan,
    RewriteAcceptance,
    SourceSnapshot,
    TargetSpec,
    TargetState,
    TargetStatus,
)
from agent_ops.deployment.registry import ChannelSpec, RegistryConfig
from agent_ops.registries.models import Framework

runner = CliRunner()
COMMIT = "a" * 40
OLD_COMMIT = "b" * 40


@dataclass
class FakeRegistry:
    config: RegistryConfig

    def load(self) -> RegistryConfig:
        return self.config


class FakeEngine:
    def __init__(self, status: TargetStatus) -> None:
        self.result_status = status
        self.calls: list[tuple[object, ...]] = []

    def status(self, target_ids=None):
        self.calls.append(("status", target_ids))
        return (self.result_status,)

    def plan(self, target_ids):
        self.calls.append(("plan", target_ids))
        target = _config().targets[0]
        return DeploymentPlan(
            (SourceSnapshot("community", "refs/heads/feat/demo", COMMIT, Path("/snapshot")),),
            (ProviderPlan("skills", COMMIT, target, (), (Path("obsolete"),)),),
        )

    def refresh(self, target_ids, *, rewrite=None):
        self.calls.append(("refresh", target_ids, rewrite))
        return DeploymentReceipt("refresh", (COMMIT,), (self.result_status,))

    def audit(self, target_ids):
        self.calls.append(("audit", target_ids))
        return DeploymentReceipt("audit", (COMMIT,), (self.result_status,))

    def deploy(self, channel, ref, target_ids, *, rewrite=None):
        self.calls.append(("deploy", channel, ref, target_ids, rewrite))
        return DeploymentReceipt("deploy", (COMMIT,), (self.result_status,))

    def switch(self, channel, target_ids):
        self.calls.append(("switch", channel, target_ids))
        return DeploymentReceipt("switch", (COMMIT,), (self.result_status,))


def _config(home: Path = Path("/tmp/codex-demo")) -> RegistryConfig:
    from agent_ops.deployment.models import SourceSpec

    return RegistryConfig(
        1,
        (SourceSpec("community", "https://example.invalid/community.git"),),
        (
            ChannelSpec("stable", "community", "refs/heads/main"),
            ChannelSpec("demo", "community", "refs/heads/feat/demo"),
        ),
        (TargetSpec("codex-demo", Framework.CODEX, home, "demo"),),
    )


def _runtime(monkeypatch, tmp_path: Path, *, state: TargetState = TargetState.BRANCH):
    from agent_ops.deployment import cli as deployment_cli

    status = TargetStatus("codex-demo", state, "demo", COMMIT)
    engine = FakeEngine(status)
    registry = FakeRegistry(_config(tmp_path / "codex-demo"))
    monkeypatch.setattr(
        deployment_cli, "_load_runtime", lambda *_args, **_kwargs: (registry, engine)
    )
    return registry, engine


def test_deployment_status_has_deterministic_human_and_json_output(
    tmp_path: Path, monkeypatch
) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)

    human = runner.invoke(app, ["deployment", "status", "--all"])
    structured = runner.invoke(app, ["deployment", "status", "--all", "--json"])

    assert human.exit_code == 0
    assert human.output == f"codex-demo: branch channel=demo commit={COMMIT}\n"
    assert json.loads(structured.output) == [
        {"target_id": "codex-demo", "state": "branch", "channel": "demo", "commit": COMMIT}
    ]
    assert engine.calls == [("status", None), ("status", None)]


def test_deployment_plan_is_read_only_and_reports_commit(tmp_path: Path, monkeypatch) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)

    result = runner.invoke(app, ["deployment", "plan", "--target", "codex-demo"])

    assert result.exit_code == 0
    assert result.output == f"codex-demo: demo {COMMIT} providers=skills files=0 removals=1\n"
    assert engine.calls == [("plan", ("codex-demo",))]


def test_refresh_and_audit_delegate_one_engine_operation(tmp_path: Path, monkeypatch) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)

    refresh = runner.invoke(app, ["deployment", "refresh", "--all", "--json"])
    audit = runner.invoke(app, ["deployment", "audit", "--targets", "codex-demo"])

    assert refresh.exit_code == 0
    assert json.loads(refresh.output)["operation"] == "refresh"
    assert audit.exit_code == 0
    assert audit.output.startswith(f"audit: {COMMIT}\n")
    assert engine.calls == [
        ("refresh", ("codex-demo",), None),
        ("audit", ("codex-demo",)),
    ]


def test_refresh_requires_both_rewrite_confirmations(tmp_path: Path, monkeypatch) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)

    refused = runner.invoke(
        app,
        ["deployment", "refresh", "--all", "--accept-rewrite-old", OLD_COMMIT],
    )
    accepted = runner.invoke(
        app,
        [
            "deployment",
            "refresh",
            "--all",
            "--accept-rewrite-old",
            OLD_COMMIT,
            "--accept-rewrite-new",
            COMMIT,
        ],
    )

    assert refused.exit_code == 2
    assert "both exact old and new commits" in refused.output
    assert accepted.exit_code == 0
    assert engine.calls == [("refresh", ("codex-demo",), RewriteAcceptance(OLD_COMMIT, COMMIT))]


def test_missing_ref_is_reported_without_traceback(tmp_path: Path, monkeypatch) -> None:
    from agent_ops.deployment import cli as deployment_cli

    _, engine = _runtime(monkeypatch, tmp_path)

    def missing(_targets, *, rewrite=None):
        raise FileNotFoundError("remote ref does not exist: refs/heads/gone")

    engine.refresh = missing
    result = runner.invoke(app, ["deployment", "refresh", "--all"])

    assert result.exit_code == 1
    assert result.output == "remote ref does not exist: refs/heads/gone\n"
    assert "Traceback" not in result.output
    assert deployment_cli is not None


def test_runtime_configuration_error_is_reported_without_traceback(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import cli as deployment_cli

    def invalid_runtime(*_args, **_kwargs):
        raise ValueError("invalid deployment registry")

    monkeypatch.setattr(deployment_cli, "_load_runtime", invalid_runtime)

    result = runner.invoke(app, ["deployment", "status", "--all"])

    assert result.exit_code == 1
    assert result.output == "invalid deployment registry\n"
    assert "Traceback" not in result.output


def test_channel_deploy_delegates_arbitrary_branch_to_one_engine_operation(
    tmp_path: Path, monkeypatch
) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        [
            "channel",
            "deploy",
            "new-feature",
            "--ref",
            "refs/heads/feat/new-feature",
            "--targets",
            "codex-demo",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output)["operation"] == "deploy"
    assert engine.calls == [
        (
            "deploy",
            "new-feature",
            "refs/heads/feat/new-feature",
            ("codex-demo",),
            None,
        )
    ]


def test_channel_deploy_passes_exact_rewrite_confirmation(tmp_path: Path, monkeypatch) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        [
            "channel",
            "deploy",
            "new-feature",
            "--ref",
            "refs/heads/feat/new-feature",
            "--targets",
            "codex-demo",
            "--accept-rewrite-old",
            OLD_COMMIT,
            "--accept-rewrite-new",
            COMMIT,
        ],
    )

    assert result.exit_code == 0
    assert engine.calls == [
        (
            "deploy",
            "new-feature",
            "refs/heads/feat/new-feature",
            ("codex-demo",),
            RewriteAcceptance(OLD_COMMIT, COMMIT),
        )
    ]


def test_channel_refresh_and_switch_select_targets_deterministically(
    tmp_path: Path, monkeypatch
) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)

    refreshed = runner.invoke(app, ["channel", "refresh", "demo"])
    switched = runner.invoke(app, ["channel", "switch", "stable", "--targets", "codex-demo"])

    assert refreshed.exit_code == 0
    assert switched.exit_code == 0
    assert engine.calls == [
        ("refresh", ("codex-demo",), None),
        ("switch", "stable", ("codex-demo",)),
    ]


def test_channel_launch_prints_identity_and_execs_with_only_target_overlay(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import cli as deployment_cli

    _, engine = _runtime(monkeypatch, tmp_path)
    observed = {}

    def fake_exec(executable, argv, environment):
        observed.update(executable=executable, argv=argv, environment=environment)

    monkeypatch.setattr(deployment_cli.os, "execvpe", fake_exec)
    monkeypatch.setattr(deployment_cli.os, "environ", {"PATH": "/bin", "CODEX_HOME": "/old"})
    (tmp_path / "codex-demo").mkdir()
    (tmp_path / "codex-demo" / "auth.json").write_text("secret", encoding="utf-8")

    def status_must_not_authorize_launch(*_args, **_kwargs):
        raise AssertionError("launch must use a fresh audit, not conservative status")

    engine.status = status_must_not_authorize_launch

    result = runner.invoke(
        app,
        ["channel", "launch", "demo", "--framework", "codex", "--", "--quiet"],
    )

    assert result.exit_code == 0
    assert result.output == f"channel=demo commit={COMMIT}\n"
    assert observed["executable"] == "codex"
    assert observed["argv"] == ["codex", "--quiet"]
    assert observed["environment"] == {"PATH": "/bin", "CODEX_HOME": str(tmp_path / "codex-demo")}
    assert engine.calls == [("audit", ("codex-demo",))]


def test_channel_launch_refuses_unready_target(tmp_path: Path, monkeypatch) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)

    unready = runner.invoke(app, ["channel", "launch", "demo", "--framework", "codex"])
    assert unready.exit_code == 1
    assert f"CODEX_HOME={tmp_path / 'codex-demo'} codex login" in unready.output
    assert engine.calls == [("audit", ("codex-demo",))]


@pytest.mark.parametrize(
    "state",
    [
        TargetState.STALE,
        TargetState.MODIFIED,
        TargetState.FAILED,
        TargetState.MISSING_REF,
        TargetState.PREVIEW,
    ],
)
def test_channel_launch_refuses_unaccepted_audit_state(
    tmp_path: Path, monkeypatch, state: TargetState
) -> None:
    _, engine = _runtime(monkeypatch, tmp_path, state=state)
    home = tmp_path / "codex-demo"
    home.mkdir()
    (home / "auth.json").write_text("secret", encoding="utf-8")

    result = runner.invoke(app, ["channel", "launch", "demo", "--framework", "codex"])

    assert result.exit_code == 1
    assert f"target state {state.value} is not launchable" in result.output
    assert engine.calls == [("audit", ("codex-demo",))]


def test_channel_launch_rejects_audit_evidence_for_another_channel(
    tmp_path: Path, monkeypatch
) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)
    engine.result_status = TargetStatus("codex-demo", TargetState.STABLE, "stable", COMMIT)

    result = runner.invoke(app, ["channel", "launch", "demo", "--framework", "codex"])

    assert result.exit_code == 1
    assert "audit evidence does not match the selected target channel" in result.output
