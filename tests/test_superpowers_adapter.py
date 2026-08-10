from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from agent_ops.superpowers_adapter import (
    OWNERSHIP_MANIFEST_RELATIVE,
    PINNED_SUPERPOWERS_REF,
    SUPERPOWERS_SKILLS,
    SuperpowersCollisionError,
    install_prime_superpowers,
)


def _write_upstream(root: Path) -> Path:
    skills = root / "skills"
    for name in SUPERPOWERS_SKILLS:
        directory = skills / name
        directory.mkdir(parents=True)
        body = f"""---
name: {name}
description: Upstream {name}.
---

Use superpowers:test-driven-development when needed.
Use the Skill tool before work and create TodoWrite entries.
Use Task tool (general-purpose) for subagents.
Call Task("bounded work") when needed.
Use AskUserQuestion for unresolved questions.
Read files and run Bash commands when useful.
Prime portability check originally named Claude Code and ~/.claude/skills.
"""
        (directory / "SKILL.md").write_text(body, encoding="utf-8")
        (directory / "reference.md").write_text(
            "Task tool (general-purpose): follow superpowers:verification-before-completion.\n",
            encoding="utf-8",
        )
    return root


def _tree_fingerprint(path: Path) -> str:
    value = hashlib.sha256()
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        value.update(file_path.relative_to(path).as_posix().encode())
        value.update(b"\0")
        value.update(file_path.read_bytes())
        value.update(b"\0")
    return value.hexdigest()


def test_clean_install_has_prime_discovery_layout_and_namespaced_frontmatter(
    tmp_path: Path,
) -> None:
    upstream = _write_upstream(tmp_path / "upstream")
    destination = tmp_path / "prime" / "skills"

    manifest = install_prime_superpowers(upstream, destination)

    expected = {f"agentops-superpowers-{name}" for name in SUPERPOWERS_SKILLS}
    assert {path.name for path in destination.iterdir() if path.is_dir()} == expected
    assert set(manifest["skills"]) == expected
    assert manifest["upstream_ref"] == PINNED_SUPERPOWERS_REF
    for directory_name in expected:
        metadata = yaml.safe_load(
            (destination / directory_name / "SKILL.md").read_text().split("---", 2)[1]
        )
        assert metadata["name"] == directory_name


def test_internal_references_and_claude_tools_are_prime_native(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path / "upstream")
    destination = tmp_path / "skills"

    install_prime_superpowers(upstream, destination)

    all_markdown = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(destination.rglob("*.md"))
    )
    assert "superpowers:test-driven-development" not in all_markdown
    assert "superpowers:verification-before-completion" not in all_markdown
    assert "superpowers:" not in all_markdown
    assert "agentops-superpowers-test-driven-development" in all_markdown
    assert "agentops-superpowers-verification-before-completion" in all_markdown
    assert "IPython" in all_markdown
    assert "RLM" in all_markdown
    assert "agent_message" in all_markdown
    assert "ordinary conversation" in all_markdown
    assert "~/.claude" not in all_markdown
    assert "Claude Code" not in all_markdown
    assert 'handle = await rlm("<complete bounded task prompt>")' in all_markdown
    assert "await agent_message.send" in all_markdown
    assert "system, developer, and project policy" in all_markdown
    claude_only_requirements = (
        "Use the Skill tool",
        "create TodoWrite",
        "Use Task tool",
        "Use AskUserQuestion",
    )
    for claude_requirement in claude_only_requirements:
        assert claude_requirement not in all_markdown


def test_existing_user_skill_collision_preserves_bytes(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path / "upstream")
    destination = tmp_path / "skills"
    collision = destination / "agentops-superpowers-writing-plans"
    collision.mkdir(parents=True)
    user_bytes = b"\x00user-owned\r\ncontent\xff"
    (collision / "SKILL.md").write_bytes(user_bytes)
    before = _tree_fingerprint(destination)

    with pytest.raises(SuperpowersCollisionError, match="user-owned collision"):
        install_prime_superpowers(upstream, destination)

    assert _tree_fingerprint(destination) == before
    assert (collision / "SKILL.md").read_bytes() == user_bytes
    assert not (destination / ".agentops-superpowers-manifest.json").exists()
    assert not (destination.parent / OWNERSHIP_MANIFEST_RELATIVE).exists()


def test_modified_managed_skill_fails_without_changing_any_destination_bytes(
    tmp_path: Path,
) -> None:
    upstream = _write_upstream(tmp_path / "upstream")
    destination = tmp_path / "skills"
    install_prime_superpowers(upstream, destination)
    edited = destination / "agentops-superpowers-writing-plans" / "SKILL.md"
    edited.write_text("user edit\n", encoding="utf-8")
    before = _tree_fingerprint(destination)

    with pytest.raises(SuperpowersCollisionError, match="changed since installation"):
        install_prime_superpowers(upstream, destination)

    assert _tree_fingerprint(destination) == before
    assert edited.read_text(encoding="utf-8") == "user edit\n"


def test_manifest_is_deterministic_and_records_collision_fingerprints(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path / "upstream")
    first = tmp_path / "first" / "skills"
    second = tmp_path / "second" / "skills"

    result_one = install_prime_superpowers(upstream, first)
    result_two = install_prime_superpowers(upstream, second)

    assert result_one == result_two
    assert (first.parent / OWNERSHIP_MANIFEST_RELATIVE).read_bytes() == (
        second.parent / OWNERSHIP_MANIFEST_RELATIVE
    ).read_bytes()
    disk = json.loads((first.parent / OWNERSHIP_MANIFEST_RELATIVE).read_text())
    assert all(len(entry["fingerprint"]) == 64 for entry in disk["skills"].values())


def test_rejects_unpinned_input_before_creating_destination(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path / "upstream")
    destination = tmp_path / "skills"

    with pytest.raises(ValueError, match="pinned Superpowers ref"):
        install_prime_superpowers(upstream, destination, upstream_ref="main")

    assert not destination.exists()


def _copy_legacy_install(upstream: Path, destination: Path) -> None:
    destination.mkdir(parents=True)
    for name in SUPERPOWERS_SKILLS:
        shutil.copytree(upstream / "skills" / name, destination / name)
    (destination / ".agentops-superpowers-manifest.json").write_text(
        json.dumps(list(SUPERPOWERS_SKILLS), indent=2) + "\n",
        encoding="utf-8",
    )


def test_migrates_byte_identical_legacy_name_only_install(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path / "upstream")
    destination = tmp_path / "skills"
    _copy_legacy_install(upstream, destination)

    install_prime_superpowers(upstream, destination)

    assert not any((destination / name).exists() for name in SUPERPOWERS_SKILLS)
    assert all(
        (destination / f"agentops-superpowers-{name}" / "SKILL.md").is_file()
        for name in SUPERPOWERS_SKILLS
    )
    assert isinstance(
        json.loads((destination.parent / OWNERSHIP_MANIFEST_RELATIVE).read_text()),
        dict,
    )
    assert not (destination / ".agentops-superpowers-manifest.json").exists()


def test_modified_legacy_install_is_preserved_byte_for_byte(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path / "upstream")
    destination = tmp_path / "skills"
    _copy_legacy_install(upstream, destination)
    edited = destination / "writing-plans" / "SKILL.md"
    edited.write_bytes(b"personally modified\r\n")
    before = _tree_fingerprint(destination)

    with pytest.raises(SuperpowersCollisionError, match="legacy skill.*does not match"):
        install_prime_superpowers(upstream, destination)

    assert _tree_fingerprint(destination) == before
    assert edited.read_bytes() == b"personally modified\r\n"


def test_legacy_install_with_different_directory_structure_is_preserved(
    tmp_path: Path,
) -> None:
    upstream = _write_upstream(tmp_path / "upstream")
    destination = tmp_path / "skills"
    _copy_legacy_install(upstream, destination)
    (destination / "writing-plans" / "unexpected-empty-directory").mkdir()
    before = _tree_fingerprint(destination)

    with pytest.raises(SuperpowersCollisionError, match="legacy skill.*does not match"):
        install_prime_superpowers(upstream, destination)

    assert _tree_fingerprint(destination) == before
    assert (destination / "writing-plans" / "unexpected-empty-directory").is_dir()


def test_existing_manifest_cannot_manage_a_path_outside_destination(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path / "upstream")
    destination = tmp_path / "skills"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("person-owned\n", encoding="utf-8")
    outside_before = _tree_fingerprint(outside)
    destination.mkdir()
    (destination / ".agentops-superpowers-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "upstream_repo": "https://github.com/obra/superpowers.git",
                "upstream_ref": PINNED_SUPERPOWERS_REF,
                "skills": {
                    "../outside": {"fingerprint": outside_before},
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SuperpowersCollisionError, match="manifest"):
        install_prime_superpowers(upstream, destination)

    assert _tree_fingerprint(outside) == outside_before
    assert (outside / "SKILL.md").read_text() == "person-owned\n"


def test_logical_namespaced_frontmatter_collision_is_preserved(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path / "upstream")
    destination = tmp_path / "prime" / "skills"
    collision = destination / "another-profile" / "nested" / "SKILL.md"
    collision.parent.mkdir(parents=True)
    collision.write_text(
        "---\nname: agentops-superpowers-writing-plans\ndescription: person-owned\n---\n",
        encoding="utf-8",
    )
    before = _tree_fingerprint(destination)

    with pytest.raises(SuperpowersCollisionError, match="logical skill-name collision"):
        install_prime_superpowers(upstream, destination)

    assert _tree_fingerprint(destination) == before
    assert "person-owned" in collision.read_text()
    assert not (destination.parent / OWNERSHIP_MANIFEST_RELATIVE).exists()
