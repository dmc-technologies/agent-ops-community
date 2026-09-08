"""Machine commands delegate shared content activation to the deployment engine."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from agent_ops.deployment.cli import _call, _command_runtime, _emit_json, _json_command
from agent_ops.deployment.models import DeploymentReceipt, SharedTargetStatus, TargetState

machine_app = typer.Typer(
    help="Activate and inspect shared skill content. Harness enrollment is configured separately."
)
ConfigOption = Annotated[Path, typer.Option("--config", help="Initialized machine registry path.")]
StateOption = Annotated[Path | None, typer.Option("--state-home")]
JsonOption = Annotated[bool, typer.Option("--json")]


def _emit(result: DeploymentReceipt | SharedTargetStatus, json_output: bool) -> None:
    if json_output:
        _emit_json(result)
    else:
        statuses = result.targets if isinstance(result, DeploymentReceipt) else (result,)
        for status in statuses:
            typer.echo(
                f"{status.target_id}: {status.state.value}; "
                f"selection fingerprint={status.selection_fingerprint or 'absent'}"
            )
    statuses = result.targets if isinstance(result, DeploymentReceipt) else (result,)
    if not statuses or any(status.state is not TargetState.STABLE for status in statuses):
        raise typer.Exit(1)


@machine_app.command("install")
@_json_command
def install(
    config: ConfigOption,
    state_home: StateOption = None,
    json_output: JsonOption = False,
) -> None:
    """Install selected shared content; this does not register harness discovery paths."""
    _, engine = _command_runtime(config, state_home)
    _emit(_call(engine.shared_sync), json_output)


@machine_app.command("sync")
@_json_command
def sync(
    config: ConfigOption,
    state_home: StateOption = None,
    json_output: JsonOption = False,
) -> None:
    """Synchronize the configured shared selection through the existing transaction."""
    _, engine = _command_runtime(config, state_home)
    _emit(_call(engine.shared_sync), json_output)


@machine_app.command("status")
@_json_command
def status(
    config: ConfigOption,
    state_home: StateOption = None,
    json_output: JsonOption = False,
) -> None:
    """Inspect installed shared content without fetching or changing sources."""
    _, engine = _command_runtime(config, state_home)
    _emit(_call(engine.shared_status), json_output)


@machine_app.command("rollback")
@_json_command
def rollback(
    config: ConfigOption,
    state_home: StateOption = None,
    json_output: JsonOption = False,
) -> None:
    """Restore the recorded previous shared selection through the existing transaction."""
    _, engine = _command_runtime(config, state_home)
    _emit(_call(engine.shared_rollback), json_output)


@machine_app.command("source")
@_json_command
def source(
    config: ConfigOption,
    skill: Annotated[str, typer.Option("--skill")],
    checkout: Annotated[Path | None, typer.Option("--checkout")] = None,
    state_home: StateOption = None,
    json_output: JsonOption = False,
) -> None:
    """Inspect installed source provenance and an optional authoring checkout."""
    from agent_ops.deployment.shared_lookup import inspect_shared_source

    registry, _ = _command_runtime(config, state_home)
    result = _call(lambda: inspect_shared_source(registry, skill, checkout))
    if json_output:
        _emit_json(result)
    else:
        typer.echo(f"{result['name']}: {result['url']} at {result['commit']}, {result['path']}")
        if result["checkout"]:
            typer.echo(f"Authoring checkout: {result['checkout']}; dirty={result['dirty']}")
    if result["dirty"]:
        raise typer.Exit(1)


@machine_app.command("updates")
@_json_command
def updates(
    config: ConfigOption,
    state_home: StateOption = None,
    json_output: JsonOption = False,
) -> None:
    """Compare selected commits with upstream refs without fetching or activating."""
    from agent_ops.deployment.shared_lookup import inspect_shared_updates

    registry, _ = _command_runtime(config, state_home)
    result = _call(lambda: inspect_shared_updates(registry))
    if json_output:
        _emit_json(result)
    else:
        for row in result:
            typer.echo(f"{row['source_id']}: {row['state']}; upstream={row['upstream_commit']}")
    if not result or any(row["state"] == "unknown" for row in result):
        raise typer.Exit(1)
