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
    SourceSpec,
    TargetSource,
    TargetSpec,
    TargetState,
    TargetStatus,
)
from agent_ops.deployment.registry import ChannelSpec, RegistryConfig, RegistrySnapshot
from agent_ops.registries.models import Framework

runner = CliRunner()
COMMIT = "a" * 40
OLD_COMMIT = "b" * 40


@dataclass
class FakeRegistry:
    config: RegistryConfig
    fingerprint: str = "c" * 64
    identity: tuple[int, int] = (1, 1)

    def load(self) -> RegistryConfig:
        return self.config

    def load_snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot(self.config, self.fingerprint, self.identity)


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
            (
                TargetSource(
                    target.id,
                    target.channel,
                    "community",
                    "refs/heads/feat/demo",
                    COMMIT,
                ),
            ),
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


def test_deployment_plan_associates_equal_commits_with_exact_channel_refs(
    tmp_path: Path, monkeypatch
) -> None:
    registry, engine = _runtime(monkeypatch, tmp_path)
    stable = TargetSpec("codex-stable", Framework.CODEX, tmp_path / "stable", "stable")
    branch = TargetSpec("codex-demo", Framework.CODEX, tmp_path / "demo", "demo")
    other = TargetSpec("claude-other", Framework.CLAUDE_CODE, tmp_path / "other", "other")
    registry.config = RegistryConfig(
        registry.config.schema_version,
        registry.config.sources
        + (SourceSpec("other", "https://example.invalid/other.git"),),
        registry.config.channels
        + (ChannelSpec("other", "other", "refs/heads/feat/demo"),),
        (stable, branch, other),
    )

    def colliding_plan(target_ids):
        engine.calls.append(("plan", target_ids))
        return DeploymentPlan(
            (
                SourceSnapshot("community", "refs/heads/main", COMMIT, Path("/stable")),
                SourceSnapshot(
                    "community", "refs/heads/feat/demo", COMMIT, Path("/branch")
                ),
                SourceSnapshot("other", "refs/heads/feat/demo", COMMIT, Path("/other")),
            ),
            (
                ProviderPlan("skills", COMMIT, stable, (), (Path("old-stable"),)),
                ProviderPlan("skills", COMMIT, branch, (), (Path("old-branch"),)),
                ProviderPlan("skills", COMMIT, other, (), (Path("old-other"),)),
            ),
            (
                TargetSource(
                    stable.id,
                    stable.channel,
                    "community",
                    "refs/heads/main",
                    COMMIT,
                ),
                TargetSource(
                    branch.id,
                    branch.channel,
                    "community",
                    "refs/heads/feat/demo",
                    COMMIT,
                ),
                TargetSource(
                    other.id,
                    other.channel,
                    "other",
                    "refs/heads/feat/demo",
                    COMMIT,
                ),
            ),
        )

    engine.plan = colliding_plan

    result = runner.invoke(app, ["deployment", "plan", "--all", "--json"])

    assert result.exit_code == 0
    rows = {row["target_id"]: row for row in json.loads(result.output)}
    assert rows["codex-stable"]["ref"] == "refs/heads/main"
    assert rows["codex-demo"]["ref"] == "refs/heads/feat/demo"
    assert rows["claude-other"]["source"] == "other"


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


@pytest.mark.parametrize("separator", [[], ["--"]])
def test_channel_launch_json_is_consumed_and_never_forwarded(
    tmp_path: Path, monkeypatch, separator: list[str]
) -> None:
    from agent_ops.deployment import cli as deployment_cli

    _, engine = _runtime(monkeypatch, tmp_path)
    observed = {}

    def fake_exec(executable, argv, environment):
        observed.update(executable=executable, argv=argv, environment=environment)

    monkeypatch.setattr(deployment_cli.os, "execvpe", fake_exec)
    monkeypatch.setattr(deployment_cli.os, "environ", {"PATH": "/bin"})
    home = tmp_path / "codex-demo"
    home.mkdir()
    (home / "auth.json").write_text("secret", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "channel",
            "launch",
            "demo",
            "--framework",
            "codex",
            "--json",
            *separator,
            "--quiet",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "channel": "demo",
        "commit": COMMIT,
        "target_id": "codex-demo",
        "framework": "codex",
        "home": str(home),
        "readiness": {"ready": True, "prerequisite": None},
    }
    assert observed["argv"] == ["codex", "--quiet"]
    assert "--json" not in observed["argv"]
    assert engine.calls == [("audit", ("codex-demo",))]


def test_channel_launch_refuses_unready_target(tmp_path: Path, monkeypatch) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)

    unready = runner.invoke(app, ["channel", "launch", "demo", "--framework", "codex"])
    assert unready.exit_code == 1
    assert f"CODEX_HOME={tmp_path / 'codex-demo'} codex login" in unready.output
    assert engine.calls == [("audit", ("codex-demo",))]


def test_channel_launch_json_reports_unready_target_without_exec(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import cli as deployment_cli

    _, engine = _runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        deployment_cli.os,
        "execvpe",
        lambda *_args: (_ for _ in ()).throw(AssertionError("unready target must not exec")),
    )

    result = runner.invoke(
        app,
        ["channel", "launch", "demo", "--framework", "codex", "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "channel": "demo",
        "commit": COMMIT,
        "target_id": "codex-demo",
        "framework": "codex",
        "home": str(tmp_path / "codex-demo"),
        "readiness": {
            "ready": False,
            "prerequisite": f"CODEX_HOME={tmp_path / 'codex-demo'} codex login",
        },
    }
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


def test_channel_launch_rejects_registry_home_change_during_audit(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import cli as deployment_cli

    registry, engine = _runtime(monkeypatch, tmp_path)
    old_home = tmp_path / "old-codex"
    new_home = tmp_path / "new-codex"
    registry.config = _config(old_home)
    old_home.mkdir()
    (old_home / "auth.json").write_text("old-secret", encoding="utf-8")
    new_home.mkdir()
    (new_home / "auth.json").write_text("new-secret", encoding="utf-8")

    def audit_then_change(target_ids):
        engine.calls.append(("audit", target_ids))
        registry.config = _config(new_home)
        registry.fingerprint = "d" * 64
        registry.identity = (1, 2)
        return DeploymentReceipt("audit", (COMMIT,), (engine.result_status,))

    def must_not_check_readiness(_framework):
        raise AssertionError("changed registry target must not reach readiness")

    engine.audit = audit_then_change
    monkeypatch.setattr(deployment_cli, "get_adapter", must_not_check_readiness)
    monkeypatch.setattr(
        deployment_cli.os,
        "execvpe",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not exec")),
    )

    result = runner.invoke(app, ["channel", "launch", "demo", "--framework", "codex"])

    assert result.exit_code == 1
    assert "registry changed during launch audit" in result.output
    assert engine.calls == [("audit", ("codex-demo",))]


def test_channel_launch_rejects_registry_aba_save_during_audit(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import cli as deployment_cli

    registry, engine = _runtime(monkeypatch, tmp_path)

    def audit_during_aba_save(target_ids):
        engine.calls.append(("audit", target_ids))
        registry.identity = (1, 9)
        return DeploymentReceipt("audit", (COMMIT,), (engine.result_status,))

    engine.audit = audit_during_aba_save
    monkeypatch.setattr(
        deployment_cli.os,
        "execvpe",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not exec")),
    )

    result = runner.invoke(app, ["channel", "launch", "demo", "--framework", "codex"])

    assert result.exit_code == 1
    assert "registry changed during launch audit" in result.output
    assert engine.calls == [("audit", ("codex-demo",))]
