from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from agent_ops.bootstrap import SUPPORTED_BOOTSTRAPS, write_all_bootstraps, write_bootstrap
from agent_ops.context import build_context_pack
from agent_ops.contracts.job import load_job
from agent_ops.contracts.result import RunResult
from agent_ops.frameworks import ADAPTERS, get_adapter
from agent_ops.harness import check_harness, default_verification, init_harness
from agent_ops.plugins import run_with_plugins
from agent_ops.registries import (
    Framework,
    get_by_id,
    load_capabilities,
    load_skill_dependencies,
    load_skills,
    load_tools,
)
from agent_ops.skill_installer import install_skill_dependencies
from agent_ops.verify import run_verification

app = typer.Typer(help="Community agent operations control plane.")
capabilities_app = typer.Typer(help="Portable capability registry.")
skills_app = typer.Typer(help="Portable skill registry.")
tools_app = typer.Typer(help="Portable tool registry.")
context_app = typer.Typer(help="Build portable context packs.")
frameworks_app = typer.Typer(help="Framework adapter commands.")
harness_app = typer.Typer(help="Repository harness checks.")
app.add_typer(capabilities_app, name="capabilities")
app.add_typer(skills_app, name="skills")
app.add_typer(tools_app, name="tools")
app.add_typer(context_app, name="context")
app.add_typer(frameworks_app, name="frameworks")
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
    framework: Annotated[
        str,
        typer.Argument(help="Framework to bootstrap, or 'all'."),
    ] = "all",
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", "-o", help="Where to write bootstrap instructions."),
    ] = Path(".agentops/bootstrap"),
) -> None:
    """Generate one-command framework bootstrap instructions."""
    if framework == "all":
        written = write_all_bootstraps(output_dir)
    else:
        try:
            selected = Framework(framework)
        except ValueError as exc:
            known = ", ".join(["all", *[item.value for item in SUPPORTED_BOOTSTRAPS]])
            typer.echo(f"unknown framework {framework!r}; expected one of: {known}", err=True)
            raise typer.Exit(1) from exc
        if selected not in SUPPORTED_BOOTSTRAPS:
            known = ", ".join(item.value for item in SUPPORTED_BOOTSTRAPS)
            typer.echo(
                f"framework {framework!r} is not bootstrappable; expected: {known}",
                err=True,
            )
            raise typer.Exit(1)
        written = [write_bootstrap(selected, output_dir)]

    for path in written:
        typer.echo(f"wrote: {path}")


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


@capabilities_app.command("list")
def list_capabilities(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    capabilities = load_capabilities()
    if json_output:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in capabilities], indent=2))
        return
    for capability in capabilities:
        typer.echo(f"{capability.id}: {capability.name}")


@capabilities_app.command("show")
def show_capability(capability_id: str) -> None:
    capability = get_by_id(load_capabilities(), capability_id)
    typer.echo(capability.model_dump_json(indent=2))


@skills_app.command("list")
def list_skills(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    skills = load_skills()
    if json_output:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in skills], indent=2))
        return
    for skill in skills:
        typer.echo(f"{skill.id}: {skill.name}")


@skills_app.command("show")
def show_skill(skill_id: str) -> None:
    skill = get_by_id(load_skills(), skill_id)
    typer.echo(skill.model_dump_json(indent=2))


@skills_app.command("install")
def install_skills(
    framework: Annotated[Framework, typer.Argument(help="Framework home to install into.")],
    dependency: Annotated[
        list[str] | None,
        typer.Option(
            "--dependency",
            "--dep",
            help="Dependency bundle id to install. Repeat to install several. Defaults to all.",
        ),
    ] = None,
    home: Annotated[
        Path | None,
        typer.Option("--home", help="Override the selected framework home."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option("--cache-dir", help="Override dependency checkout cache directory."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Install dependency-backed skill bundles such as gstack and Superpowers."""
    try:
        rows = install_skill_dependencies(
            framework=framework,
            dependencies=load_skill_dependencies(),
            home=home,
            dependency_ids=dependency,
            cache_dir=cache_dir,
            dry_run=dry_run,
        )
    except (FileNotFoundError, ValueError, subprocess.CalledProcessError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc

    verb = "would install" if dry_run else "installed"
    for row in rows:
        typer.echo(f"{verb}: {row.id} -> {row.destination}")


@tools_app.command("list")
def list_tools(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    tools = load_tools()
    if json_output:
        typer.echo(json.dumps([item.model_dump(mode="json") for item in tools], indent=2))
        return
    for tool in tools:
        typer.echo(f"{tool.id}: {tool.name}")


@tools_app.command("show")
def show_tool(tool_id: str) -> None:
    tool = get_by_id(load_tools(), tool_id)
    typer.echo(tool.model_dump_json(indent=2))


@context_app.command("build")
def build_context(
    job_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    framework: Annotated[Framework, typer.Option("--framework", "-f")] = Framework.CODEX,
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", "-o"),
    ] = Path("examples/context-packs"),
    source: Annotated[list[str] | None, typer.Option("--source", "-s")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    job = load_job(job_file)
    context_pack = build_context_pack(job, framework, sources=source or [])
    json_path, markdown_path = context_pack.write(output_dir)
    if json_output:
        typer.echo(context_pack.model_dump_json(indent=2))
    else:
        typer.echo(f"wrote: {json_path}")
        typer.echo(f"wrote: {markdown_path}")


@frameworks_app.command("list")
def list_frameworks(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    rows = [
        {
            "framework": framework.value,
            "available": adapter.available(),
            "executable": adapter.executable,
        }
        for framework, adapter in ADAPTERS.items()
    ]
    if json_output:
        typer.echo(json.dumps(rows, indent=2))
        return
    for row in rows:
        marker = "available" if row["available"] else "missing"
        typer.echo(f"{row['framework']}: {marker} ({row['executable']})")


@frameworks_app.command("command")
def framework_command(
    job_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    framework: Annotated[Framework, typer.Option("--framework", "-f")],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    job = load_job(job_file)
    context_pack = build_context_pack(job, framework)
    adapter = get_adapter(framework)
    command = adapter.build_command(job, context_pack, Path.cwd())
    if json_output:
        typer.echo(command.model_dump_json(indent=2))
    else:
        typer.echo(" ".join(command.command))
