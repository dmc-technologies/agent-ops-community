"""Machine selections preserve exact sources without framework-specific copies."""

import json

import pytest
from pydantic import ValidationError

from agent_ops.deployment.shared_selection import SharedSelection, SharedSourceBinding


def binding(**changes):
    return {
        "source_id": "catalog",
        "commit": "a" * 40,
        "skill_paths": ("skills/review", "skills/build"),
        **changes,
    }


def test_round_trip_is_canonical_and_immutable():
    first = SharedSelection(sources=(binding(), binding(source_id="extras")))
    second = SharedSelection(
        sources=(
            binding(source_id="extras", skill_paths=("skills/build", "skills/review")),
            binding(),
        )
    )
    assert first.fingerprint == second.fingerprint
    assert len(first.fingerprint) == 64
    assert first.model_dump_json() == second.model_dump_json()
    assert SharedSelection.model_validate_json(first.model_dump_json()) == first
    assert json.loads(first.model_dump_json())["sources"][0]["commit"] == "a" * 40
    with pytest.raises(ValidationError):
        first.schema_version = 2
    with pytest.raises(ValidationError):
        first.sources[0].source_id = "changed"


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "../skill",
        "skills/../a",
        "/skills/a",
        "C:/skills/a",
        "skills\\a",
        "skills//a",
        "skills/./a",
        "skills/a/",
        "skills/\x00a",
    ],
)
def test_refuses_noncanonical_paths(path):
    with pytest.raises(ValidationError):
        SharedSourceBinding(**binding(skill_paths=(path,)))


@pytest.mark.parametrize(
    "changes",
    [
        {"source_id": ""},
        {"source_id": " catalog"},
        {"commit": "main"},
        {"commit": "g" * 40},
        {"commit": "a" * 39},
        {"commit": 123},
        {"skill_paths": ()},
        {"skill_paths": ("skills/a", "skills/a")},
        {"url": "https://example.invalid/repository"},
    ],
)
def test_refuses_invalid_bindings(changes):
    with pytest.raises(ValidationError):
        SharedSourceBinding(**binding(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"sources": ()},
        {"sources": (binding(), binding())},
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": "1"},
        {"framework_skills": {"client": []}},
    ],
)
def test_refuses_invalid_selection(changes):
    with pytest.raises(ValidationError):
        SharedSelection(**{"sources": (binding(),), **changes})


def test_fingerprint_binds_every_selection_field():
    baseline = SharedSelection(sources=(binding(),)).fingerprint
    for change in (
        {"commit": "b" * 40},
        {"source_id": "other"},
        {"skill_paths": ("skills/other",)},
    ):
        assert SharedSelection(sources=(binding(**change),)).fingerprint != baseline
