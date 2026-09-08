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
    return {job: {"result": "success", "outputs": {"selectors": '["full"]'}} for job in jobs}


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
