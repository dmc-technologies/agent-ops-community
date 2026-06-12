from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECKED_PATHS = [
    ROOT / "data",
    ROOT / "src",
    ROOT / "tests",
    ROOT / "docs",
    ROOT / "examples",
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
    "".join(("Documents", "/", "GitHub")),
]


def test_public_tree_has_no_private_terms_or_local_paths() -> None:
    offenders: list[str] = []
    for root in CHECKED_PATHS:
        paths = [root] if root.is_file() else root.rglob("*")
        for path in paths:
            if not path.is_file():
                continue
            if path.suffix and path.suffix not in TEXT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            for snippet in FORBIDDEN_SNIPPETS:
                if snippet.lower() in lowered:
                    offenders.append(f"{path.relative_to(ROOT)} contains {snippet}")

    assert offenders == []
