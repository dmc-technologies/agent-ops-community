"""Run the final CI decision against success, failure, and missing evidence."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _gate():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text())
    gate = workflow["jobs"]["ci"]
    assert gate["name"] == "CI"
    assert gate["if"] == "${{ always() }}"
    assert set(gate["needs"]) == set(workflow["jobs"]) - {"ci"}
    assert gate["steps"][0]["env"]["NEEDS"] == "${{ toJSON(needs) }}"
    return gate["needs"], gate["steps"][0]["run"]


def _run(results):
    _, script = _gate()
    return subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "NEEDS": json.dumps(results)},
        capture_output=True,
        text=True,
    )


def _success():
    jobs, _ = _gate()
    return {job: {"result": "success", "outputs": {"run": "true"}} for job in jobs}


def test_ci_publishes_success_only_for_complete_evidence():
    results = _success()
    result = _run(results)
    assert result.returncode == 0, result.stderr
    assert str(len(results)) in result.stdout
    for job in results:
        incomplete = dict(results)
        del incomplete[job]
        assert _run(incomplete).returncode != 0
    assert _run({}).returncode != 0


@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped", "unknown"])
def test_ci_refuses_unsuccessful_required_job(state):
    results = _success()
    for job in results:
        candidate = dict(results)
        candidate[job] = {**results[job], "result": state}
        assert _run(candidate).returncode != 0, (job, state)


def test_ci_allows_only_explicitly_unselected_windows_job():
    results = _success()
    results["windows-paths"]["outputs"]["run"] = "false"
    results["test-windows"]["result"] = "skipped"
    assert _run(results).returncode == 0
    for job in set(results) - {"test-windows"}:
        candidate = {key: dict(value) for key, value in results.items()}
        candidate[job]["result"] = "skipped"
        assert _run(candidate).returncode != 0, job
    for selection in ("true", "", None, "False"):
        results["windows-paths"]["outputs"]["run"] = selection
        assert _run(results).returncode != 0, selection


def _detect(tmp_path, event, changed):
    """Run the workflow's Windows path detector on a pull-request merge commit."""
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text())
    script = workflow["jobs"]["windows-paths"]["steps"][1]["run"]
    repo = tmp_path / "repo"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "checkout", "-qb", "change"], check=True)
    for path in changed:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("change\n")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "change", "--allow-empty"], check=True)
    subprocess.run([*git, "checkout", "-q", "main"], check=True)
    subprocess.run([*git, "merge", "-q", "--no-ff", "-m", "merge", "change"], check=True)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=repo,
        env={**os.environ, "EVENT_NAME": event, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return output.read_text().strip()


@pytest.mark.parametrize(
    ("event", "changed", "expected"),
    [
        ("pull_request", ["docs/guide.md"], "run=false"),
        ("pull_request", ["tests/test_cli.py", "README.md"], "run=false"),
        ("pull_request", ["docs/guide.md", "src/agent_ops/cli.py"], "run=true"),
        ("pull_request", ["tests/test_deployment_windows.py"], "run=true"),
        ("pull_request", ["pyproject.toml"], "run=true"),
        ("pull_request", [".github/workflows/ci.yml"], "run=true"),
        ("pull_request", [], "run=true"),
        ("push", ["docs/guide.md"], "run=true"),
        ("schedule", [], "run=true"),
    ],
)
def test_windows_detector_skips_only_paths_it_does_not_test(tmp_path, event, changed, expected):
    assert _detect(tmp_path, event, changed) == expected
