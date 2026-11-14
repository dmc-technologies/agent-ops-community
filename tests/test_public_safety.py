from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CHECKED_PATHS = [
    ROOT / "data",
    ROOT / "src",
    ROOT / "tests",
    ROOT / "docs",
    ROOT / "examples",
    ROOT / ".agentops",
    ROOT / "README.md",
    ROOT / "SECURITY.md",
    ROOT / "pyproject.toml",
]
TEXT_SUFFIXES = {".md", ".py", ".toml", ".yaml", ".yml", ".json"}
FORBIDDEN_SNIPPETS = [
    "".join(("noc", "turnal")),
    "".join(("wig", "gum")),
    "".join(("sha", "ry")),
    "".join(("ra", "lph")),
    "".join(("D", "MC")),
    "".join(("Momen", "tum")),
    "".join(("/", "Users", "/")),
    "".join(("/", "home", "/")),
    "".join(("Documents", "/", "GitHub")),
]
# A maintainer's GitHub handle that a public decision record names. Only this exact,
# case-sensitive handle is allowed, and only as a whole token; every snippet above
# still applies to all other text.
ALLOWED_HANDLE = "".join(("@nicdavidson-", "d", "mc"))
ALLOWED_HANDLE_RE = re.compile(rf"(?<![\w@-]){re.escape(ALLOWED_HANDLE)}(?![\w-])")


def find_offenders(roots: list[Path], base: Path) -> list[str]:
    offenders: list[str] = []
    for root in roots:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file():
                continue
            if any(part.endswith(".egg-info") for part in path.parts):
                continue
            if path.suffix and path.suffix not in TEXT_SUFFIXES:
                continue
            text = ALLOWED_HANDLE_RE.sub(" ", path.read_text(encoding="utf-8"))
            lowered = text.lower()
            for snippet in FORBIDDEN_SNIPPETS:
                if snippet.lower() in lowered:
                    offenders.append(f"{path.relative_to(base)} contains {snippet}")
    return offenders


def test_public_tree_has_no_private_terms_or_local_paths() -> None:
    assert find_offenders(CHECKED_PATHS, ROOT) == []


def test_allowed_handle_is_not_reported(tmp_path: Path) -> None:
    (tmp_path / "owners.md").write_text(f"Owned by `{ALLOWED_HANDLE}`.\n", encoding="utf-8")
    assert find_offenders([tmp_path], tmp_path) == []


@pytest.mark.parametrize(
    "text",
    [
        "".join(("D", "MC")),
        "".join(("d", "mc")),
        "".join(("d", "mc", "-technologies")),
        ALLOWED_HANDLE.upper(),
        ALLOWED_HANDLE + "-technologies",
        "x" + ALLOWED_HANDLE,
        f"`{ALLOWED_HANDLE}` and " + "".join(("D", "MC")),
    ],
)
def test_brand_outside_the_allowed_handle_still_fails(tmp_path: Path, text: str) -> None:
    (tmp_path / "note.md").write_text(text + "\n", encoding="utf-8")
    assert find_offenders([tmp_path], tmp_path) == ["note.md contains " + "".join(("D", "MC"))]
