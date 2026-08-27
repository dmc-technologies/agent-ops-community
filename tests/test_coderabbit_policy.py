"""Removal contract for the retired shared AI review architecture."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGACY_PATHS = (
    ROOT / ".github" / "workflows" / "review-gate-reusable.yml",
    ROOT / ".github" / "workflows" / "review-gate.yml",
    ROOT / ".github" / "scripts" / "review_gate.py",
    ROOT / ".github" / "review-gate-prompt.md",
    ROOT / "docs" / "review-gate-policy.md",
    ROOT / "docs" / "review-gate-evaluation.md",
    ROOT / "tests" / "test_review_gate.py",
)


def test_retired_review_runtime_is_absent() -> None:
    existing = [str(path.relative_to(ROOT)) for path in LEGACY_PATHS if path.exists()]
    assert existing == []


def test_current_handoff_has_no_retired_review_instruction() -> None:
    current = {
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "PROGRESS.md": (ROOT / ".agentops/harness/PROGRESS.md").read_text(
            encoding="utf-8"
        ),
    }
    for name, text in current.items():
        assert text.strip(), name
        for phrase in ("ai review", "Review Gate", "review-gate"):
            assert phrase not in text, f"{name}: {phrase}"
