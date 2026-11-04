#!/usr/bin/env python3
"""Docs/file consistency check — regression stage 5.

Markdown docs drift: files get deleted, tools get removed, tables keep
referencing ghosts. This check scans the repo's instruction/design docs for
backticked repo-relative paths and asserts every one exists on disk. It would
have caught the staleness found in the 2026-08 review (removed ralph chain
still listed in AGENTS.md, "pending removal" tools in two docs).

Not collected by pytest (name does not start with ``test_``); run via
``tests/regression.sh`` or directly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Docs whose backticked repo-relative paths must resolve.
CHECKED_DOCS = [
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / "README_CN.md",
    ROOT / "skills" / "baton" / "SKILL.md",
    ROOT / "scripts" / "orchestration" / "AGENTS.md",
    ROOT / "docs" / "architecture.md",
    ROOT / "docs" / "ticket-format.md",
]

# Repo dirs whose contents docs may reference by relative path.
REPO_DIRS = ("scripts", "tests", "docs", "templates", "skills")

_BACKTICK = re.compile(r"`([^`\n]+)`")
_REPO_PATH = re.compile(
    r"^(?:" + "|".join(REPO_DIRS) + r")/[A-Za-z0-9_.\-/]*$"
)
_TRAILING_PUNCT = ".,;:()\"'"

# Paths that are fine to mention although they do not exist on disk (e.g.
# runtime artifacts in the *business* project, or gitignored patterns).
ALLOWED_MISSING: set[str] = set()


def referenced_paths(doc: Path) -> set[str]:
    paths: set[str] = set()
    for match in _BACKTICK.finditer(doc.read_text(encoding="utf-8")):
        candidate = match.group(1).strip().rstrip(_TRAILING_PUNCT)
        if _REPO_PATH.match(candidate):
            paths.add(candidate)
    return paths


def main() -> int:
    missing: dict[str, list[str]] = {}
    for doc in CHECKED_DOCS:
        if not doc.is_file():
            missing[str(doc.relative_to(ROOT))] = ["<the doc itself is missing>"]
            continue
        ghosts = [
            path
            for path in sorted(referenced_paths(doc))
            if path not in ALLOWED_MISSING and not (ROOT / path).exists()
        ]
        if ghosts:
            missing[str(doc.relative_to(ROOT))] = ghosts

    if missing:
        print("DOCS CONSISTENCY FAIL — docs reference paths that do not exist:")
        for doc, ghosts in missing.items():
            for ghost in ghosts:
                print(f"  {doc}: `{ghost}`")
        return 1

    total = sum(len(referenced_paths(doc)) for doc in CHECKED_DOCS if doc.is_file())
    print(f"DOCS CONSISTENCY PASS: {total} referenced repo paths all exist "
          f"across {len(CHECKED_DOCS)} docs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
