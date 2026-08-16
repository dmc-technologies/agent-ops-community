from __future__ import annotations

import json
from contextlib import contextmanager
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
from agent_ops.deployment.preview import PreviewResult
from agent_ops.deployment.registry import ChannelSpec, RegistryConfig, RegistrySnapshot
from agent_ops.registries.models import Framework

runner = CliRunner()
COMMIT = "a" * 40
OLD_COMMIT = "b" * 40


class FakePreviewEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def preview(self, checkout, skills, target_id):
        self.calls.append((checkout, skills, target_id))
        return PreviewResult(
            "preview",
            "unreviewed-local",
            target_id,
            "preview",
            "d" * 64,
            "d" * 64,
            ("selected-skills",),
            ("skills/demo/SKILL.md",),
        )


@dataclass
class FakeRegistry:
    config: RegistryConfig
    fingerprint: str = "c" * 64
    identity: tuple[int, int] = (1, 1)

    def load(self) -> RegistryConfig:
        return self.config

    def load_snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot(self.config, self.fingerprint, self.identity)


@dataclass
class FakeLaunchAuthorization:
    target: TargetSpec
    status: TargetStatus
    receipt: DeploymentReceipt
    verify_error: BaseException | None = None

    def verify(self) -> None:
        if self.verify_error is not None:
            raise self.verify_error


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

    @contextmanager
    def launch_authorization(self, target_id):
        self.calls.append(("launch_authorization", target_id))
        target = next(target for target in _config(self.home).targets if target.id == target_id)
        receipt = DeploymentReceipt("audit", (COMMIT,), (self.result_status,))
        yield FakeLaunchAuthorization(target, self.result_status, receipt)

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
    engine.home = tmp_path / "codex-demo"
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


def test_deployment_status_renders_preview_fingerprint_in_human_and_json(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import cli as deployment_cli

    fingerprint = "a" * 64
    status = TargetStatus(
        "codex-preview", TargetState.PREVIEW, "preview", fingerprint
    )
    engine = FakeEngine(status)
    registry = FakeRegistry(_config(tmp_path / "codex-preview"))
    monkeypatch.setattr(
        deployment_cli, "_load_runtime", lambda *_args, **_kwargs: (registry, engine)
    )

    human = runner.invoke(app, ["deployment", "status", "--all"])
    structured = runner.invoke(app, ["deployment", "status", "--all", "--json"])

    assert human.exit_code == 0
    assert human.output == (
        f"codex-preview: preview channel=preview commit={fingerprint}\n"
    )
    assert json.loads(structured.output) == [
        {
            "target_id": "codex-preview",
            "state": "preview",
            "channel": "preview",
            "commit": fingerprint,
        }
    ]


def test_deployment_preview_delegates_explicit_inputs_and_renders_deterministically(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import cli as deployment_cli

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    preview = FakePreviewEngine()
    monkeypatch.setattr(
        deployment_cli,
        "_load_preview_runtime",
        lambda *_args, **_kwargs: preview,
    )

    common = [
        "deployment",
        "preview",
        "--source-checkout",
        str(checkout),
        "--skill",
        "demo",
        "--target",
        "codex-preview",
    ]
    human = runner.invoke(app, common)
    structured = runner.invoke(app, [*common, "--json"])

    assert human.exit_code == 0
    assert human.output == (
        "preview: codex-preview channel=preview review=unreviewed-local "
        f"fingerprint={'d' * 64}\n"
        "providers=selected-skills paths=skills/demo/SKILL.md\n"
    )
    assert structured.exit_code == 0
    assert json.loads(structured.output) == {
        "operation": "preview",
        "review_state": "unreviewed-local",
        "target_id": "codex-preview",
        "channel": "preview",
        "fingerprint": "d" * 64,
        "source_revision": "d" * 64,
        "providers": ["selected-skills"],
        "paths": ["skills/demo/SKILL.md"],
    }
    assert preview.calls == [
        (checkout, ("demo",), "codex-preview"),
        (checkout, ("demo",), "codex-preview"),
    ]


def test_deployment_preview_requires_every_explicit_input(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    common = ["deployment", "preview"]
    cases = (
        [*common, "--skill", "demo", "--target", "codex-preview"],
        [*common, "--source-checkout", str(checkout), "--target", "codex-preview"],
        [*common, "--source-checkout", str(checkout), "--skill", "demo"],
    )

    for arguments in cases:
        result = runner.invoke(app, arguments)
        assert result.exit_code == 2


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


def test_missing_ref_json_uses_stable_error_envelope(tmp_path: Path, monkeypatch) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)

    def missing(_targets, *, rewrite=None):
        raise FileNotFoundError("remote ref does not exist: refs/heads/gone")

    engine.refresh = missing
    result = runner.invoke(app, ["deployment", "refresh", "--all", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "ok": False,
        "error": {
            "category": "operation",
            "message": "remote ref does not exist: refs/heads/gone",
        },
        "exit_status": 1,
    }


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["channel", "deploy", "demo", "--json"], "--ref is required"),
        (
            [
                "channel",
                "deploy",
                "--ref",
                "refs/heads/demo",
                "--targets",
                "codex-demo",
                "--json",
            ],
            "channel is required",
        ),
        (
            ["channel", "launch", "demo", "--json"],
            "--framework is required",
        ),
    ],
)
def test_json_usage_failures_use_one_stable_envelope(
    tmp_path: Path, monkeypatch, arguments: list[str], message: str
) -> None:
    _runtime(monkeypatch, tmp_path)

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert json.loads(result.output) == {
        "ok": False,
        "error": {"category": "usage", "message": message},
        "exit_status": 2,
    }


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
    assert engine.calls == [("launch_authorization", "codex-demo")]


@pytest.mark.parametrize(
    "json_arguments",
    [
        ["--json", "--quiet"],
        ["--json", "--", "--quiet"],
    ],
)
def test_channel_launch_json_is_authorization_only_and_never_execs(
    tmp_path: Path, monkeypatch, json_arguments: list[str]
) -> None:
    from agent_ops.deployment import cli as deployment_cli

    _, engine = _runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(
        deployment_cli.os,
        "execvpe",
        lambda *_args: (_ for _ in ()).throw(AssertionError("JSON launch must not exec")),
    )
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
            *json_arguments,
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "ok": True,
        "channel": "demo",
        "commit": COMMIT,
        "target_id": "codex-demo",
        "framework": "codex",
        "home": str(home),
        "readiness": {"ready": True, "prerequisite": None},
        "executed": False,
        "authorization_only": True,
    }
    assert engine.calls == [("launch_authorization", "codex-demo")]


def test_channel_launch_forwards_every_token_after_delimiter_in_human_mode(
    tmp_path: Path, monkeypatch
) -> None:
    from agent_ops.deployment import cli as deployment_cli

    _, engine = _runtime(monkeypatch, tmp_path)
    observed: dict[str, object] = {}
    home = tmp_path / "codex-demo"
    home.mkdir()
    (home / "auth.json").write_text("do-not-read", encoding="utf-8")
    monkeypatch.setattr(
        deployment_cli.os,
        "execvpe",
        lambda executable, arguments, environment: observed.update(
            executable=executable,
            arguments=arguments,
            environment=environment,
        ),
    )

    result = runner.invoke(
        app,
        [
            "channel",
            "launch",
            "demo",
            "--framework",
            "codex",
            "--",
            "--json",
            "quoted value",
            "--unknown=value",
        ],
    )

    assert result.exit_code == 0
    assert result.output == f"channel=demo commit={COMMIT}\n"
    assert observed["arguments"] == [
        "codex",
        "--json",
        "quoted value",
        "--unknown=value",
    ]
    assert engine.calls == [("launch_authorization", "codex-demo")]


@pytest.mark.parametrize(
    "arguments",
    [
        ["deployment", "status", "--json", "--unknown"],
        ["deployment", "status", "--unknown", "--json"],
    ],
)
def test_deployment_json_boundary_converts_unknown_option_parse_errors(
    arguments: list[str],
) -> None:
    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert json.loads(result.output) == {
        "ok": False,
        "error": {"category": "usage", "message": "No such option: --unknown"},
        "exit_status": 2,
    }


def test_deployment_json_boundary_converts_missing_option_value_parse_error() -> None:
    result = runner.invoke(app, ["deployment", "status", "--json", "--registry"])

    assert result.exit_code == 2
    assert json.loads(result.output) == {
        "ok": False,
        "error": {
            "category": "usage",
            "message": "Option '--registry' requires an argument.",
        },
        "exit_status": 2,
    }


def test_channel_json_boundary_converts_invalid_enum_parse_error() -> None:
    result = runner.invoke(
        app,
        ["channel", "launch", "demo", "--framework", "unknown", "--json"],
    )

    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["ok"] is False
    assert payload["error"]["category"] == "usage"
    assert payload["error"]["message"].startswith("Invalid value for '--framework'")
    assert payload["exit_status"] == 2


def test_json_after_delimiter_does_not_change_parse_error_format() -> None:
    result = runner.invoke(
        app,
        ["deployment", "status", "--unknown", "--", "--json"],
    )

    assert result.exit_code == 2
    assert result.output.startswith("Usage:")
    assert "No such option: --unknown" in result.output


def test_channel_launch_refuses_unready_target(tmp_path: Path, monkeypatch) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)

    unready = runner.invoke(app, ["channel", "launch", "demo", "--framework", "codex"])
    assert unready.exit_code == 1
    assert f"env CODEX_HOME={tmp_path / 'codex-demo'} codex login" in unready.output
    assert engine.calls == [("launch_authorization", "codex-demo")]


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
        "ok": False,
        "error": {
            "category": "not-ready",
            "message": f"env CODEX_HOME={tmp_path / 'codex-demo'} codex login",
        },
        "exit_status": 1,
    }
    assert engine.calls == [("launch_authorization", "codex-demo")]


def test_channel_launch_json_emits_no_success_before_authority_release(
    tmp_path: Path, monkeypatch
) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)
    home = tmp_path / "codex-demo"
    home.mkdir()
    (home / "auth.json").write_text("do-not-read", encoding="utf-8")

    @contextmanager
    def failing_release(target_id):
        engine.calls.append(("launch_authorization", target_id))
        receipt = DeploymentReceipt("audit", (COMMIT,), (engine.result_status,))
        yield FakeLaunchAuthorization(_config(home).targets[0], engine.result_status, receipt)
        raise RuntimeError("retained authority release failed")

    engine.launch_authorization = failing_release

    result = runner.invoke(
        app,
        ["channel", "launch", "demo", "--framework", "codex", "--json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "ok": False,
        "error": {
            "category": "operation",
            "message": "retained authority release failed",
        },
        "exit_status": 1,
    }


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
    assert engine.calls == [("launch_authorization", "codex-demo")]


def test_channel_launch_rejects_audit_evidence_for_another_channel(
    tmp_path: Path, monkeypatch
) -> None:
    _, engine = _runtime(monkeypatch, tmp_path)
    engine.result_status = TargetStatus("codex-demo", TargetState.STABLE, "stable", COMMIT)

    result = runner.invoke(app, ["channel", "launch", "demo", "--framework", "codex"])

    assert result.exit_code == 1
    assert "audit evidence does not match the selected target channel" in result.output


@pytest.mark.parametrize(
    "failure",
    [
        "registry no longer matches the required snapshot",
        "deployment canonical home identity changed",
    ],
)
def test_channel_launch_revalidates_retained_authority_before_exec(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    from agent_ops.deployment import cli as deployment_cli

    _, engine = _runtime(monkeypatch, tmp_path)
    home = tmp_path / "codex-demo"
    home.mkdir()
    (home / "auth.json").write_text("do-not-read", encoding="utf-8")

    @contextmanager
    def invalid_authority(target_id):
        engine.calls.append(("launch_authorization", target_id))
        receipt = DeploymentReceipt("audit", (COMMIT,), (engine.result_status,))
        yield FakeLaunchAuthorization(
            _config(home).targets[0],
            engine.result_status,
            receipt,
            ValueError(failure),
        )

    engine.launch_authorization = invalid_authority
    monkeypatch.setattr(
        deployment_cli.os,
        "execvpe",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not exec")),
    )

    result = runner.invoke(app, ["channel", "launch", "demo", "--framework", "codex"])

    assert result.exit_code == 1
    assert result.output == f"{failure}\n"
    assert "channel=demo" not in result.output
    assert engine.calls == [("launch_authorization", "codex-demo")]
