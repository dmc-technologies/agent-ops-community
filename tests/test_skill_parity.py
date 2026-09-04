from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def _write_legacy_manifest(home: Path, dependency_id: str, document: dict) -> None:
    manifest = home / ".agentops" / "skill-dependencies" / f"{dependency_id}.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(document), encoding="utf-8")


def test_skill_owned_only_by_a_legacy_manifest_is_managed(tmp_path: Path) -> None:
    """Homes provisioned before the shared provider index carry the older record.

    `~/.claude` is one of them: its `.agentops/skill-dependencies/mattpocock.json`
    records eight skills that no provider index mentions. Reading only the index
    would call all eight hand copies and fail a home that Agent Ops installed
    correctly.
    """

    (tmp_path / "skills" / "alpha").mkdir(parents=True)
    _write_legacy_manifest(
        tmp_path,
        "example",
        {"dependency_id": "example", "skills": {"alpha": {"SKILL.md": "0" * 64}}},
    )

    states = audit_home(Framework.CODEX, [_dependency(["alpha"])], tmp_path)

    assert [(state.skill, state.state) for state in states] == [("alpha", MANAGED)]


def test_legacy_manifest_does_not_launder_a_skill_it_never_recorded(tmp_path: Path) -> None:
    """A manifest present for the dependency must not vouch for other names."""

    (tmp_path / "skills" / "beta").mkdir(parents=True)
    _write_legacy_manifest(
        tmp_path,
        "example",
        {"dependency_id": "example", "skills": {"alpha": {"SKILL.md": "0" * 64}}},
    )

    states = audit_home(Framework.CODEX, [_dependency(["beta"])], tmp_path)

    assert [(state.skill, state.state) for state in states] == [("beta", UNMANAGED)]


def test_show_me_adapter_manifest_records_a_single_skill(tmp_path: Path) -> None:
    """The show-me adapter writes `skill`, not a `skills` mapping."""

    show_me = SkillDependency.model_validate(
        {
            "id": "humanlayer-show-me",
            "name": "HumanLayer Show Me",
            "repo": "https://example.invalid/skills.git",
            "ref": "0" * 40,
            "install": {
                "codex": {
                    "strategy": "humanlayer-show-me",
                    "source": "plugins/show-me/skills",
                    "destination": "skills",
                }
            },
        }
    )
    (tmp_path / "skills" / "show-me").mkdir(parents=True)
    _write_legacy_manifest(tmp_path, "humanlayer-show-me", {"skill": "show-me"})

    states = audit_home(Framework.CODEX, [show_me], tmp_path)

    assert [(state.skill, state.state) for state in states] == [("show-me", MANAGED)]


def test_deleted_skill_reports_missing_even_though_its_record_survives(tmp_path: Path) -> None:
    """An ownership record is a claim about the past; the directory is the present.

    Removing a skill directory leaves its ownership record behind. Checking the
    record alone reports a skill that is not on disk as installed, which makes
    the audit unable to fail on the one thing it exists to detect.
    """

    _write_index(tmp_path, "public-skill:example", ["alpha"])
    _write_legacy_manifest(
        tmp_path,
        "example",
        {"dependency_id": "example", "skills": {"alpha": {"SKILL.md": "0" * 64}}},
    )
    # Both records claim alpha. Nothing is on disk.

    states = audit_home(Framework.CODEX, [_dependency(["alpha"])], tmp_path)

    assert [(state.skill, state.state) for state in states] == [("alpha", MISSING)]
    assert not any(state.ok for state in states)


@pytest.mark.parametrize(
    ("label", "blob"),
    [
        ("truncated", b'{"providers": ['),
        ("empty", b""),
        ("top level is a list", b"[]"),
        ("providers is not a list", b'{"providers": {}}'),
        ("provider has no id", b'{"providers": [{"paths": ["skills/alpha/SKILL.md"]}]}'),
        ("paths is not a list", b'{"providers": [{"provider_id": "p", "paths": 7}]}'),
    ],
)
def test_unusable_ownership_index_is_reported_not_raised(
    tmp_path: Path, label: str, blob: bytes
) -> None:
    """An interrupted install leaves a half-written record; the audit must describe it.

    Crashing on a malformed ownership record exits non-zero with a traceback
    that names no skill, which reads as a failure while telling the reader
    nothing. An unusable record claims nothing instead, so the skills it should
    have covered report as unmanaged and are named.
    """

    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / ".agentops-public-provider-index.json").write_bytes(blob)
    (tmp_path / "skills" / "alpha").mkdir()

    states = audit_home(Framework.CODEX, [_dependency(["alpha"])], tmp_path)

    assert [(state.skill, state.state) for state in states] == [("alpha", UNMANAGED)], label


def test_unusable_legacy_manifest_is_reported_not_raised(tmp_path: Path) -> None:
    (tmp_path / "skills" / "alpha").mkdir(parents=True)
    manifest = tmp_path / ".agentops" / "skill-dependencies" / "example.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_bytes(b'["not", "an", "object"]')

    states = audit_home(Framework.CODEX, [_dependency(["alpha"])], tmp_path)

    assert [(state.skill, state.state) for state in states] == [("alpha", UNMANAGED)]


def test_repeating_a_framework_audits_its_home_once(tmp_path, capsys, monkeypatch) -> None:
    """Auditing one home twice would double every count in the report."""

    _write_index(tmp_path, "public-skill:example", ["alpha"])
    (tmp_path / "skills" / "alpha").mkdir(parents=True)
    dependency = _dependency(["alpha"])
    monkeypatch.setattr("agent_ops.skill_parity.load_skill_dependencies", lambda: [dependency])
    monkeypatch.setattr("agent_ops.skill_parity.default_framework_home", lambda _f: tmp_path)

    status = main(["--framework", "codex", "--framework", "codex"])
    output = capsys.readouterr().out

    assert status == 0
    assert "all 1 checked skills are managed by Agent Ops" in output
    assert "codex: 1/1 managed" in output


def test_all_and_framework_together_are_refused(capsys) -> None:
    """Silently ignoring an explicit --framework would hide what was audited."""

    with pytest.raises(SystemExit) as raised:
        main(["--all", "--framework", "codex"])

    assert raised.value.code == 2
    assert "do not also pass --framework" in capsys.readouterr().err


def test_report_names_the_bundles_it_could_not_check(tmp_path, capsys, monkeypatch) -> None:
    """A passing report must say what it examined, not only that nothing failed.

    gstack renders a whole repository and declares no skill names, so the audit
    cannot check it name by name. Reporting only "all N managed" would let a
    reader believe gstack had been examined and found correct.
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
    _write_index(tmp_path, "public-skill:example", ["alpha"])
    (tmp_path / "skills" / "alpha").mkdir(parents=True)
    monkeypatch.setattr(
        "agent_ops.skill_parity.load_skill_dependencies", lambda: [_dependency(["alpha"]), gstack]
    )
    monkeypatch.setattr("agent_ops.skill_parity.default_framework_home", lambda _f: tmp_path)

    status = main(["--framework", "codex"])
    output = capsys.readouterr().out

    assert status == 0
    assert "codex:gstack (gstack)" in output
    assert "not checked by skill name" in output
    # The count describes what was checked, not what was declared.
    assert "all 1 checked skills are managed" in output
