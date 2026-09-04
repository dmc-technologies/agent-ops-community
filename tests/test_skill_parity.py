from __future__ import annotations

import json
from pathlib import Path

from agent_ops.registries.models import Framework, SkillDependency
from agent_ops.skill_parity import (
    MANAGED,
    MISSING,
    UNMANAGED,
    audit_home,
    expected_skills,
    format_report,
    main,
)


def _dependency(skills: list[str]) -> SkillDependency:
    return SkillDependency.model_validate(
        {
            "id": "example",
            "name": "Example",
            "repo": "https://example.invalid/skills.git",
            "ref": "0" * 40,
            "install": {
                "codex": {
                    "strategy": "copy-named-skills",
                    "source": "skills",
                    "destination": "skills",
                    "skills": skills,
                }
            },
        }
    )


def _write_index(home: Path, provider_id: str, skills: list[str]) -> None:
    index = home / "skills" / ".agentops-public-provider-index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text(
        json.dumps(
            {
                "framework": "codex",
                "providers": [
                    {
                        "provider_id": provider_id,
                        "paths": [f"skills/{skill}/SKILL.md" for skill in skills],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_declared_skill_owned_by_its_provider_is_managed(tmp_path: Path) -> None:
    _write_index(tmp_path, "public-skill:example", ["alpha"])
    (tmp_path / "skills" / "alpha").mkdir(parents=True)

    states = audit_home(Framework.CODEX, [_dependency(["alpha"])], tmp_path)

    assert [(state.skill, state.state) for state in states] == [("alpha", MANAGED)]
    assert all(state.ok for state in states)


def test_hand_placed_skill_directory_reports_unmanaged(tmp_path: Path) -> None:
    """A hand copy satisfies "the skill is there" and still fails the audit.

    This is the defect the audit exists to catch: the directory resolves, so the
    skill works today, but Agent Ops does not own it and cannot refresh, verify,
    or retire it. Reporting it as present would hide exactly the state that let a
    declared skill sit uninstalled for months.
    """

    _write_index(tmp_path, "public-skill:other", [])
    (tmp_path / "skills" / "alpha").mkdir(parents=True)

    states = audit_home(Framework.CODEX, [_dependency(["alpha"])], tmp_path)

    assert [(state.skill, state.state) for state in states] == [("alpha", UNMANAGED)]
    assert not any(state.ok for state in states)


def test_absent_skill_reports_missing_and_is_distinct_from_unmanaged(tmp_path: Path) -> None:
    _write_index(tmp_path, "public-skill:example", [])

    states = audit_home(Framework.CODEX, [_dependency(["alpha"])], tmp_path)

    assert [(state.skill, state.state) for state in states] == [("alpha", MISSING)]


def test_home_with_no_ownership_index_reports_every_declared_skill(tmp_path: Path) -> None:
    """A home Agent Ops never provisioned must not audit as empty and therefore clean."""

    states = audit_home(Framework.CODEX, [_dependency(["alpha", "beta"])], tmp_path)

    assert [(state.skill, state.state) for state in states] == [
        ("alpha", MISSING),
        ("beta", MISSING),
    ]


def test_strategies_without_a_declared_name_list_are_not_audited_by_name() -> None:
    """gstack renders a whole repository and copy-skills installs whatever ships.

    Neither declares the names it creates, so neither can be checked name by
    name. Auditing them as if they declared nothing would report success having
    examined nothing.
    """

    gstack = SkillDependency.model_validate(
        {
            "id": "gstack",
            "name": "GStack",
            "repo": "https://example.invalid/gstack.git",
            "ref": "0" * 40,
            "install": {"codex": {"strategy": "gstack", "destination": "skills/gstack"}},
        }
    )
    assert expected_skills(gstack.install["codex"]) == ()


def test_report_and_exit_status_agree(tmp_path: Path, capsys, monkeypatch) -> None:
    """The printed report and the exit status must describe the same run."""

    _write_index(tmp_path, "public-skill:example", ["alpha"])
    (tmp_path / "skills" / "alpha").mkdir(parents=True)
    dependency = _dependency(["alpha", "beta"])
    monkeypatch.setattr("agent_ops.skill_parity.load_skill_dependencies", lambda: [dependency])
    monkeypatch.setattr("agent_ops.skill_parity.default_framework_home", lambda _f: tmp_path)

    status = main(["--framework", "codex"])
    output = capsys.readouterr().out

    assert status == 1
    assert "codex:example/beta" in output
    assert "1/2 managed" in output


def test_format_report_counts_only_the_states_it_was_given() -> None:
    states = audit_home(Framework.CODEX, [], Path("/nonexistent"))
    assert states == []
    assert format_report(states) == ""
