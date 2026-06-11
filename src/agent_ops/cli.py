from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from agent_ops.bootstrap import write_bootstrap
from agent_ops.contracts.job import load_job
from agent_ops.contracts.result import RunResult
from agent_ops.harness import check_harness, default_verification, init_harness
from agent_ops.plugins import run_with_plugins
from agent_ops.verify import run_verification

app = typer.Typer(help="Community agent operations control plane.")
harness_app = typer.Typer(help="Repository harness checks.")
app.add_typer(harness_app, name="harness")


def _emit_result(result: RunResult, json_output: bool, output: Path | None) -> None:
    if output:
        result.write_json(output)
    if json_output:
        typer.echo(result.model_dump_json(indent=2))
    else:
        typer.echo(f"{result.status.value}: {result.job_id}")


@app.command()
def bootstrap(
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", "-o", help="Where to write bootstrap instructions."),
    ] = Path(".agentops/bootstrap"),
) -> None:
    """Generate framework-neutral bootstrap instructions."""
    typer.echo(f"wrote: {write_bootstrap(output_dir)}")


@app.command()
def validate(job_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]) -> None:
    """Validate an agent job contract."""
    try:
        job = load_job(job_file)
    except (ValidationError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"valid: {job.id}")


@app.command()
def run(
    job_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Build the command but do not run it."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Print result JSON.")] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write result manifest."),
    ] = None,
) -> None:
    """Run a job through an installed runner plugin."""
    result = run_with_plugins(load_job(job_file), dry_run=dry_run)
    _emit_result(result, json_output, output)
    if result.status.value == "fail":
        raise typer.Exit(1)


@app.command()
def verify(
    job_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    json_output: Annotated[bool, typer.Option("--json", help="Print result JSON.")] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write result manifest."),
    ] = None,
) -> None:
    """Run verification commands from a job contract."""
    result = run_verification(load_job(job_file), job_file.parent)
    _emit_result(result, json_output, output)
    if result.status.value == "fail":
        raise typer.Exit(1)


@harness_app.command("init")
def init_harness_command(
    repo_root: Annotated[Path, typer.Argument(file_okay=False)] = Path("."),
    repo_name: Annotated[str | None, typer.Option("--repo-name")] = None,
    repo_type: Annotated[
        str,
        typer.Option("--repo-type", help="generic, python, or agent-ops."),
    ] = "generic",
    verification: Annotated[
        list[str] | None,
        typer.Option("--verification", "-v", help="Verification command. Repeat as needed."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite existing harness files."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create the standard repo-local harness scaffold."""
    root = repo_root.resolve()
    commands = tuple(verification) if verification else default_verification(repo_type)
    writes = init_harness(
        root,
        repo_name=repo_name or root.name,
        repo_type=repo_type,
        verification_commands=commands,
        force=force,
    )
    rows = [
        {"path": str(write.path), "status": "written" if write.written else "exists"}
        for write in writes
    ]
    if json_output:
        typer.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        typer.echo(f"{row['status']}: {row['path']}")


@harness_app.command("check")
def check_harness_command(
    repo_root: Annotated[Path, typer.Argument(file_okay=False)] = Path("."),
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Validate that a repository has the standard harness scaffold."""
    report = check_harness(repo_root)
    if json_output:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(f"{'ok' if report.ok else 'fail'}: {report.root}")
        for finding in report.findings:
            typer.echo(f"{finding.severity} {finding.path}: {finding.message}")
    if not report.ok:
        raise typer.Exit(1)
