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


def _detect(tmp_path, event, changed, base=(), moves=()):
    """Run the workflow's Windows path detector on a pull-request merge commit."""
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/ci.yml").read_text())
    script = workflow["jobs"]["windows-paths"]["steps"][1]["run"]
    repo = tmp_path / "repo"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("base\n")
    for path in base:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("unchanged body that git can follow through a rename\n" * 20)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "checkout", "-qb", "change"], check=True)
    for old, new in moves:
        (repo / new).parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([*git, "mv", old, new], check=True)
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


def test_windows_detector_sees_both_sides_of_a_rename(tmp_path):
    """A file renamed out of a Windows path changes what Windows tests."""
    old = "src/agent_ops/moved.py"
    result = _detect(tmp_path, "pull_request", [], base=[old], moves=[(old, "docs/moved.py")])
    assert result == "run=true"


def test_windows_detector_matches_a_non_ascii_path(tmp_path):
    """git quotes a non-ASCII path by default, and a quoted path no longer starts with `src/`."""
    assert _detect(tmp_path, "pull_request", ["src/agent_ops/r\u00e9sum\u00e9.py"]) == "run=true"


def test_windows_detector_reads_many_matches_as_a_match(tmp_path):
    """grep -q stops at the first match; a broken pipe must not read as no match."""
    name = "src/agent_ops/generated/module_{:04d}_with_a_long_name_to_fill_the_pipe.py"
    changed = [name.format(n) for n in range(3000)]
    assert _detect(tmp_path, "pull_request", changed) == "run=true"
