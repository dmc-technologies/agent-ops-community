import json
import subprocess

from typer.testing import CliRunner

from agent_ops.cli import app
from agent_ops.deployment.models import SourceSpec
from agent_ops.deployment.registry import DeploymentRegistry, RegistryConfig, SharedSelectionConfig
from agent_ops.deployment.shared_selection import SharedSelection


def fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(source), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    skill = source / "skills" / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: example\ndescription: Example\n---\nVersion one.\n")
    git("add", ".")
    git("-c", "user.name=Fixture", "-c", "user.email=f@example.invalid", "commit", "-m", "One")
    commit = git("rev-parse", "HEAD")
    selected = SharedSelection(
        sources=({"source_id": "catalog", "commit": commit, "skill_paths": ("skills/example",)},)
    )
    registry = DeploymentRegistry(tmp_path / "registry.yaml")
    registry.save(
        RegistryConfig(
            2,
            (SourceSpec("catalog", str(source)),),
            (),
            (),
            SharedSelectionConfig("machine", tmp_path / "installed", selected),
        )
    )
    args = ["--config", str(registry.path), "--state-home", str(tmp_path / "state"), "--json"]
    return registry, selected, args


def test_machine_install_status_and_idempotent_sync(tmp_path):
    registry, selected, args = fixture(tmp_path)
    runner = CliRunner()
    absent = runner.invoke(app, ["machine", "status", *args])
    assert absent.exit_code == 1, absent.output
    assert json.loads(absent.output)["state"] == "stale"
    installed = runner.invoke(app, ["machine", "install", *args])
    assert installed.exit_code == 0, installed.output
    receipt = json.loads(installed.output)
    assert receipt["targets"][0]["selection_fingerprint"] == selected.fingerprint
    assert receipt["commits"] == [selected.sources[0].commit]
    status = runner.invoke(app, ["machine", "status", *args])
    assert status.exit_code == 0, status.output
    assert json.loads(status.output)["state"] == "stable"
    assert "commit" not in json.loads(status.output)
    synced = runner.invoke(app, ["machine", "sync", *args])
    assert synced.exit_code == 0, synced.output
    assert registry.load().shared_selection.previous is None


def test_machine_usage_error_is_nonzero_json():
    result = CliRunner().invoke(app, ["machine", "sync", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.output)["ok"] is False


def test_machine_update_rollback_and_modified_status(tmp_path):
    from dataclasses import replace

    registry, first, args = fixture(tmp_path)
    runner = CliRunner()
    installed = runner.invoke(app, ["machine", "install", *args])
    assert installed.exit_code == 0, installed.output
    source = tmp_path / "source"
    skill = source / "skills/example/SKILL.md"
    skill.write_text(skill.read_text().replace("Version one", "Version two"))
    subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.invalid",
            "commit",
            "-m",
            "Two",
        ],
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    second = SharedSelection(
        sources=({"source_id": "catalog", "commit": commit, "skill_paths": ("skills/example",)},)
    )
    snapshot = registry.load_snapshot()
    registry.save(
        replace(
            snapshot.config,
            shared_selection=replace(snapshot.config.shared_selection, selected=second),
        ),
        expected_snapshot=snapshot,
    )
    updated = runner.invoke(app, ["machine", "sync", *args])
    assert updated.exit_code == 0, updated.output
    assert json.loads(updated.output)["targets"][0]["selection_fingerprint"] == second.fingerprint
    rolled = runner.invoke(app, ["machine", "rollback", *args])
    assert rolled.exit_code == 0, rolled.output
    assert json.loads(rolled.output)["targets"][0]["selection_fingerprint"] == first.fingerprint
    installed_skill = tmp_path / "installed/current/skills/example/SKILL.md"
    installed_skill.write_text("local edit")
    modified = runner.invoke(app, ["machine", "status", *args])
    assert modified.exit_code == 1, modified.output
    assert json.loads(modified.output)["state"] == "modified"


def test_machine_rollback_without_previous_refuses(tmp_path):
    _, _, args = fixture(tmp_path)
    result = CliRunner().invoke(app, ["machine", "rollback", *args])
    assert result.exit_code == 1
    assert json.loads(result.output)["ok"] is False


def test_source_and_updates_inspect_without_mutation(tmp_path):
    registry, selected, args = fixture(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["machine", "install", *args]).exit_code == 0
    before = registry.path.read_bytes()
    installed = tmp_path / "installed"
    installed_bytes = {
        p.relative_to(installed): p.read_bytes() for p in installed.rglob("*") if p.is_file()
    }
    source_index = (tmp_path / "source/.git/index").read_bytes()
    source = runner.invoke(app, ["machine", "source", "--skill", "example", *args])
    assert source.exit_code == 0, source.output
    data = json.loads(source.output)
    assert data["commit"] == selected.sources[0].commit
    assert data["path"] == "skills/example"
    assert data["checkout"] is None
    updates = runner.invoke(app, ["machine", "updates", *args])
    assert updates.exit_code == 0, updates.output
    assert json.loads(updates.output)[0]["state"] == "current"
    assert registry.path.read_bytes() == before
    assert {
        p.relative_to(installed): p.read_bytes() for p in installed.rglob("*") if p.is_file()
    } == installed_bytes
    assert (tmp_path / "source/.git/index").read_bytes() == source_index


def test_source_checkout_dirty_and_wrong_identity_refuse(tmp_path):
    _, _, args = fixture(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["machine", "install", *args]).exit_code == 0
    checkout = tmp_path / "source"
    clean = runner.invoke(
        app, ["machine", "source", "--skill", "example", "--checkout", str(checkout), *args]
    )
    assert clean.exit_code == 0, clean.output
    (checkout / "skills/example/SKILL.md").write_text("dirty")
    dirty = runner.invoke(
        app, ["machine", "source", "--skill", "example", "--checkout", str(checkout), *args]
    )
    assert dirty.exit_code == 1, dirty.output
    assert json.loads(dirty.output)["dirty"] is True
    wrong = runner.invoke(
        app, ["machine", "source", "--skill", "example", "--checkout", str(tmp_path), *args]
    )
    assert wrong.exit_code == 1
    other = tmp_path / "other"
    other.mkdir()
    fixture(other)
    wrong_repository = runner.invoke(
        app, ["machine", "source", "--skill", "example", "--checkout", str(other / "source"), *args]
    )
    assert wrong_repository.exit_code == 1, wrong_repository.output


def test_updates_unavailable_is_unknown(tmp_path):
    from dataclasses import replace

    registry, _, args = fixture(tmp_path)
    current = registry.load_snapshot()
    registry.save(
        replace(current.config, sources=(SourceSpec("catalog", str(tmp_path / "missing")),)),
        expected_snapshot=current,
    )
    result = CliRunner().invoke(app, ["machine", "updates", *args])
    assert result.exit_code == 1, result.output
    assert json.loads(result.output)[0]["state"] == "unknown"


def test_updates_reports_available_without_changing_selection(tmp_path):
    registry, selected, args = fixture(tmp_path)
    source = tmp_path / "source"
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=f@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "Next",
        ],
        check=True,
        capture_output=True,
    )
    before = registry.path.read_bytes()
    result = CliRunner().invoke(app, ["machine", "updates", *args])
    assert result.exit_code == 0, result.output
    row = json.loads(result.output)[0]
    assert row["state"] == "available"
    assert row["selected_commit"] == selected.sources[0].commit
    assert row["upstream_commit"] != row["selected_commit"]
    assert registry.path.read_bytes() == before


def test_source_url_redacts_authentication():
    from agent_ops.deployment.shared_lookup import _safe_url

    safe = _safe_url("https://user:secret@example.invalid/repo.git?token=private#hidden")
    assert safe == "https://example.invalid/repo.git"
    assert "secret" not in safe and "private" not in safe
