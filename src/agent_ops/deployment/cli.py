"""Thin command surface for machine-local deployment channels."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import asdict
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, TypeVar

import typer

from agent_ops.deployment.engine import DeploymentEngine
from agent_ops.deployment.models import (
    DeploymentPlan,
    DeploymentReceipt,
    RewriteAcceptance,
    TargetState,
    TargetStatus,
)
from agent_ops.deployment.registry import DeploymentRegistry, RegistryConfig
from agent_ops.deployment.source_store import SourceStore
from agent_ops.frameworks import get_adapter
from agent_ops.registries.models import Framework

deployment_app = typer.Typer(help="Inspect, plan, refresh, and audit managed deployments.")
channel_app = typer.Typer(help="Deploy and launch isolated Agent Ops channels.")
_T = TypeVar("_T")
_COMMIT = re.compile(r"[0-9a-fA-F]{40}\Z")


def _state_root(state_home: Path | None) -> Path:
    if state_home is not None:
        return state_home.expanduser().absolute()
    configured = os.environ.get("AGENT_OPS_STATE_HOME")
    if configured:
        return Path(configured).expanduser().absolute()
    return (Path.home() / ".agentops/state").absolute()


def _load_runtime(
    registry_path: Path | None,
    state_home: Path | None,
) -> tuple[DeploymentRegistry, DeploymentEngine]:
    root = _state_root(state_home)
    registry = DeploymentRegistry(
        registry_path.expanduser().absolute()
        if registry_path is not None
        else root / "deployments.yaml"
    )
    return registry, DeploymentEngine(registry, SourceStore(root / "sources"))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return {"size": len(value)}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _emit_json(value: Any) -> None:
    typer.echo(
        json.dumps(
            _jsonable(asdict(value) if hasattr(value, "__dataclass_fields__") else value), indent=2
        )
    )


def _status_data(status: TargetStatus) -> dict[str, object]:
    return {
        "target_id": status.target_id,
        "state": status.state.value,
        "channel": status.channel,
        "commit": status.commit,
    }


def _emit_statuses(statuses: tuple[TargetStatus, ...], json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps([_status_data(status) for status in statuses], indent=2))
        return
    for status in statuses:
        typer.echo(
            f"{status.target_id}: {status.state.value} channel={status.channel} "
            f"commit={status.commit or '-'}"
        )


def _emit_plan(plan: DeploymentPlan, json_output: bool) -> None:
    rows: list[dict[str, object]] = []
    target_sources = {item.target_id: item for item in plan.target_sources}
    target_ids = sorted({provider_plan.target.id for provider_plan in plan.provider_plans})
    for target_id in target_ids:
        plans = tuple(item for item in plan.provider_plans if item.target.id == target_id)
        source = target_sources[target_id]
        rows.append(
            {
                "target_id": target_id,
                "channel": source.channel,
                "source": source.source_id,
                "ref": source.ref,
                "commit": source.commit,
                "providers": [item.provider_id for item in plans],
                "files": sum(len(item.files) for item in plans),
                "removals": sum(len(item.removals) for item in plans),
            }
        )
    if json_output:
        typer.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        typer.echo(
            f"{row['target_id']}: {row['channel']} {row['commit']} "
            f"providers={','.join(row['providers'])} files={row['files']} "
            f"removals={row['removals']}"
        )


def _emit_receipt(receipt: DeploymentReceipt, json_output: bool) -> None:
    if json_output:
        _emit_json(receipt)
        return
    typer.echo(f"{receipt.operation}: {','.join(receipt.commits) or '-'}")
    _emit_statuses(receipt.targets, False)


def _fail(message: str, *, usage: bool = False) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(2 if usage else 1)


def _call(operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except Exception as error:
        _fail(str(error))
    raise AssertionError("unreachable")


def _command_runtime(
    registry_path: Path | None,
    state_home: Path | None,
) -> tuple[DeploymentRegistry, DeploymentEngine]:
    return _call(lambda: _load_runtime(registry_path, state_home))


def _split_targets(targets: str | None, target: list[str] | None) -> tuple[str, ...]:
    values = list(target or [])
    if targets is not None:
        values.extend(item.strip() for item in targets.split(","))
    if not values or any(not item for item in values) or len(values) != len(set(values)):
        _fail("select unique targets with --target/--targets, or use --all", usage=True)
    return tuple(sorted(values))


def _selected_targets(
    config: RegistryConfig,
    *,
    targets: str | None,
    target: list[str] | None,
    all_targets: bool,
    status_all_as_none: bool = False,
) -> tuple[str, ...] | None:
    if all_targets:
        if targets is not None or target:
            _fail("--all cannot be combined with --target or --targets", usage=True)
        if status_all_as_none:
            return None
        return tuple(item.id for item in config.targets)
    return _split_targets(targets, target)


def _rewrite(old: str | None, new: str | None) -> RewriteAcceptance | None:
    if (old is None) != (new is None):
        _fail("rewritten refs require both exact old and new commits", usage=True)
    if old is None:
        return None
    try:
        return RewriteAcceptance(old, new or "")
    except ValueError as error:
        _fail(str(error), usage=True)
    raise AssertionError("unreachable")


def _channel(config: RegistryConfig, channel: str):
    found = next((item for item in config.channels if item.id == channel), None)
    if found is None:
        _fail(f"unknown channel: {channel}", usage=True)
    return found


@deployment_app.command("status")
def status_command(
    targets: Annotated[str | None, typer.Option("--targets")] = None,
    target: Annotated[list[str] | None, typer.Option("--target")] = None,
    all_targets: Annotated[bool, typer.Option("--all")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    registry_path: Annotated[Path | None, typer.Option("--registry")] = None,
    state_home: Annotated[Path | None, typer.Option("--state-home")] = None,
) -> None:
    registry, engine = _command_runtime(registry_path, state_home)
    selected = _selected_targets(
        _call(registry.load),
        targets=targets,
        target=target,
        all_targets=all_targets,
        status_all_as_none=True,
    )
    _emit_statuses(_call(lambda: engine.status(selected)), json_output)


@deployment_app.command("plan")
def plan_command(
    targets: Annotated[str | None, typer.Option("--targets")] = None,
    target: Annotated[list[str] | None, typer.Option("--target")] = None,
    all_targets: Annotated[bool, typer.Option("--all")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    registry_path: Annotated[Path | None, typer.Option("--registry")] = None,
    state_home: Annotated[Path | None, typer.Option("--state-home")] = None,
) -> None:
    registry, engine = _command_runtime(registry_path, state_home)
    config = _call(registry.load)
    selected = _selected_targets(
        config, targets=targets, target=target, all_targets=all_targets
    )
    _emit_plan(_call(lambda: engine.plan(selected or ())), json_output)


def _refresh_common(
    *,
    targets: str | None,
    target: list[str] | None,
    all_targets: bool,
    json_output: bool,
    registry_path: Path | None,
    state_home: Path | None,
    accept_rewrite_old: str | None,
    accept_rewrite_new: str | None,
) -> None:
    registry, engine = _command_runtime(registry_path, state_home)
    selected = _selected_targets(
        _call(registry.load), targets=targets, target=target, all_targets=all_targets
    )
    rewrite = _rewrite(accept_rewrite_old, accept_rewrite_new)
    _emit_receipt(
        _call(lambda: engine.refresh(selected or (), rewrite=rewrite)),
        json_output,
    )


@deployment_app.command("refresh")
def refresh_command(
    targets: Annotated[str | None, typer.Option("--targets")] = None,
    target: Annotated[list[str] | None, typer.Option("--target")] = None,
    all_targets: Annotated[bool, typer.Option("--all")] = False,
    accept_rewrite_old: Annotated[str | None, typer.Option("--accept-rewrite-old")] = None,
    accept_rewrite_new: Annotated[str | None, typer.Option("--accept-rewrite-new")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    registry_path: Annotated[Path | None, typer.Option("--registry")] = None,
    state_home: Annotated[Path | None, typer.Option("--state-home")] = None,
) -> None:
    _refresh_common(
        targets=targets,
        target=target,
        all_targets=all_targets,
        json_output=json_output,
        registry_path=registry_path,
        state_home=state_home,
        accept_rewrite_old=accept_rewrite_old,
        accept_rewrite_new=accept_rewrite_new,
    )


@deployment_app.command("audit")
def audit_command(
    targets: Annotated[str | None, typer.Option("--targets")] = None,
    target: Annotated[list[str] | None, typer.Option("--target")] = None,
    all_targets: Annotated[bool, typer.Option("--all")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    registry_path: Annotated[Path | None, typer.Option("--registry")] = None,
    state_home: Annotated[Path | None, typer.Option("--state-home")] = None,
) -> None:
    registry, engine = _command_runtime(registry_path, state_home)
    selected = _selected_targets(
        _call(registry.load), targets=targets, target=target, all_targets=all_targets
    )
    _emit_receipt(_call(lambda: engine.audit(selected or ())), json_output)


@channel_app.command("deploy")
def deploy_channel_command(
    channel: str,
    ref: Annotated[str, typer.Option("--ref")],
    targets: Annotated[str | None, typer.Option("--targets")] = None,
    target: Annotated[list[str] | None, typer.Option("--target")] = None,
    accept_rewrite_old: Annotated[str | None, typer.Option("--accept-rewrite-old")] = None,
    accept_rewrite_new: Annotated[str | None, typer.Option("--accept-rewrite-new")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    registry_path: Annotated[Path | None, typer.Option("--registry")] = None,
    state_home: Annotated[Path | None, typer.Option("--state-home")] = None,
) -> None:
    _registry, engine = _command_runtime(registry_path, state_home)
    selected = _split_targets(targets, target)
    rewrite = _rewrite(accept_rewrite_old, accept_rewrite_new)
    _emit_receipt(
        _call(lambda: engine.deploy(channel, ref, selected, rewrite=rewrite)),
        json_output,
    )


@channel_app.command("refresh")
def refresh_channel_command(
    channel: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    registry_path: Annotated[Path | None, typer.Option("--registry")] = None,
    state_home: Annotated[Path | None, typer.Option("--state-home")] = None,
) -> None:
    registry, engine = _command_runtime(registry_path, state_home)
    config = _call(registry.load)
    _channel(config, channel)
    selected = tuple(target.id for target in config.targets if target.channel == channel)
    if not selected:
        _fail(f"channel {channel} has no targets", usage=True)
    _emit_receipt(_call(lambda: engine.refresh(selected)), json_output)


@channel_app.command("switch")
def switch_channel_command(
    channel: str,
    targets: Annotated[str | None, typer.Option("--targets")] = None,
    target: Annotated[list[str] | None, typer.Option("--target")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    registry_path: Annotated[Path | None, typer.Option("--registry")] = None,
    state_home: Annotated[Path | None, typer.Option("--state-home")] = None,
) -> None:
    registry, engine = _command_runtime(registry_path, state_home)
    _channel(_call(registry.load), channel)
    selected = _split_targets(targets, target)
    _emit_receipt(_call(lambda: engine.switch(channel, selected)), json_output)


@channel_app.command(
    "launch",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def launch_channel_command(
    context: typer.Context,
    channel: str,
    framework: Annotated[Framework, typer.Option("--framework")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
    registry_path: Annotated[Path | None, typer.Option("--registry")] = None,
    state_home: Annotated[Path | None, typer.Option("--state-home")] = None,
) -> None:
    registry, engine = _command_runtime(registry_path, state_home)
    registry_snapshot = _call(registry.load_snapshot)
    config = registry_snapshot.config
    candidates = tuple(
        target
        for target in config.targets
        if target.channel == channel and target.framework is framework
    )
    if len(candidates) != 1:
        message = (
            f"channel {channel} requires exactly one {framework.value} target; "
            f"found {len(candidates)}"
        )
        _fail(
            message,
            usage=True,
        )
    target = candidates[0]
    receipt = _call(lambda: engine.audit((target.id,)))
    current_snapshot = _call(registry.load_snapshot)
    snapshot_changed = (
        current_snapshot.fingerprint != registry_snapshot.fingerprint
        or current_snapshot.registry_identity != registry_snapshot.registry_identity
        or current_snapshot.config != registry_snapshot.config
    )
    if snapshot_changed:
        _fail("registry changed during launch audit")
    current_targets = {item.id: item for item in current_snapshot.config.targets}
    if current_targets.get(target.id) != target:
        _fail("registry target changed during launch audit")
    if receipt.operation != "audit" or len(receipt.targets) != 1:
        _fail("launch requires fresh audit evidence for exactly one target")
    status = receipt.targets[0]
    evidence_matches = (
        status.target_id == target.id
        and status.channel == channel
        and status.commit is not None
        and _COMMIT.fullmatch(status.commit) is not None
        and status.commit in receipt.commits
    )
    if not evidence_matches:
        _fail("audit evidence does not match the selected target channel")
    if status.state not in {TargetState.STABLE, TargetState.BRANCH} or status.commit is None:
        _fail(f"target state {status.state.value} is not launchable")
    adapter = get_adapter(framework)
    readiness = adapter.target_readiness(target.home)
    launch_data = {
        "channel": channel,
        "commit": status.commit,
        "target_id": target.id,
        "framework": framework.value,
        "home": str(target.home),
        "readiness": {
            "ready": readiness.ready,
            "prerequisite": readiness.prerequisite,
        },
    }
    if not readiness.ready:
        if json_output:
            typer.echo(json.dumps(launch_data, indent=2))
            raise typer.Exit(1)
        _fail(readiness.prerequisite or "framework target is not ready")
    if adapter.executable is None:
        _fail(f"framework {framework.value} has no executable")
    if json_output:
        typer.echo(json.dumps(launch_data, indent=2))
    else:
        typer.echo(f"channel={channel} commit={status.commit}")
    environment = dict(os.environ)
    environment.update(adapter.target_environment(target.home))
    arguments = [adapter.executable, *context.args]
    _call(lambda: os.execvpe(adapter.executable, arguments, environment))


__all__ = ["channel_app", "deployment_app"]
