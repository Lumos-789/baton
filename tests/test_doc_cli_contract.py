"""Drift tripwire: documented CLI invocations must match the real CLI.

The 0.3.0 skill taught a ``run`` subcommand that never existed (plus a default
``session_id``) — an agent following it failed on its first call. The stage-5
docs gate only proves that backticked *paths* exist, so nothing caught it.

A documented command is part of the contract with a mechanical forwarder, so a
wrong verb or flag is a defect. This module parses every documented
``session_control.py`` / ``receipt.py`` invocation and asserts the verb is
registered and every flag belongs to it.
"""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import session_control  # noqa: E402

DOCS = [
    ROOT / "README.md",
    ROOT / "README_CN.md",
    ROOT / "skills" / "baton" / "SKILL.md",
    ROOT / "docs" / "architecture.md",
]

# Docs that must keep showing at least one real invocation: if the scanner
# silently stops matching, these fail instead of passing vacuously.
DOCS_THAT_MUST_HAVE_COMMANDS = {
    ROOT / "README.md",
    ROOT / "skills" / "baton" / "SKILL.md",
}

CLI_MARKERS = ("session_control.py", "<cli>")
RECEIPT_MARKERS = ("receipt.py", "<receipt>")
PROGRAM_TOKENS = {"session_control.py", "<cli>", "receipt.py", "<receipt>"}


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("session_control has no subcommands")


def _flags(parser: argparse.ArgumentParser) -> dict[str, bool]:
    """``{"--flag": takes_a_value}`` for one parser level."""
    found: dict[str, bool] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        for option in action.option_strings:
            found[option] = action.nargs != 0
    return found


def _snippets(doc: Path) -> list[str]:
    """Runnable snippets inside fenced code blocks, continuations joined.

    Only fenced blocks are scanned: prose mentions (``scripts/receipt.py``
    ``verify``/``seal`` appears mid-sentence) are not commands an agent is told
    to run, and treating them as such would make the tripwire cry wolf.
    """
    lines = doc.read_text(encoding="utf-8").splitlines()
    snippets: list[str] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.strip().startswith("```"):
            in_fence = not in_fence
            index += 1
            continue
        if not in_fence or not any(
            marker in line for marker in CLI_MARKERS + RECEIPT_MARKERS
        ):
            index += 1
            continue
        parts = [line.strip().rstrip("\\").strip()]
        while lines[index].strip().endswith("\\") and index + 1 < len(lines):
            index += 1
            parts.append(lines[index].strip().rstrip("\\").strip())
        snippets.append(" ".join(parts))
        index += 1
    return snippets


def _tokens(snippet: str) -> list[str]:
    cleaned = snippet.strip().strip("`").strip().split(" #", 1)[0].strip()
    try:
        return shlex.split(cleaned)
    except ValueError:
        return cleaned.split()


def _walk(tokens: list[str], flags: dict[str, bool]) -> tuple[str, list[str]]:
    """Return ``(verb, flags_used)`` for a documented invocation.

    Skips the program token and, for the control CLI, the two positional
    placeholders (project, feature) that always precede the verb.
    """
    index = next(i for i, token in enumerate(tokens) if token in PROGRAM_TOKENS)
    is_receipt = tokens[index] in {"receipt.py", "<receipt>"}
    index += 1
    positionals_left = 0 if is_receipt else 2

    verb = ""
    used: list[str] = []
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            # docs write ranges as ``--capacity<=5`` / ``--capacity=5``
            name = re.split(r"[=<]", token, maxsplit=1)[0]
            used.append(name)
            takes_value = "=" not in token and "<" not in token and flags.get(name, False)
            index += 2 if takes_value else 1
            continue
        if positionals_left and not verb:
            positionals_left -= 1
            index += 1
            continue
        if not verb:
            verb = token
        index += 1
    return verb, used


@pytest.mark.parametrize("doc", DOCS, ids=lambda path: path.name)
def test_documented_invocations_match_the_real_cli(doc):
    parser = session_control.build_parser()
    verbs = _subcommands(parser)
    top_level = _flags(parser)

    checked = 0
    for snippet in _snippets(doc):
        tokens = _tokens(snippet)
        is_receipt = any(token in {"receipt.py", "<receipt>"} for token in tokens)
        verb, used = _walk(tokens, {} if is_receipt else top_level)

        if not re.fullmatch(r"[a-z][a-z-]*", verb):
            continue  # an ASCII-diagram label, not a runnable command

        if is_receipt:
            assert verb in {"verify", "seal"}, (
                f"{doc.name}: documents receipt verb `{verb}` (real: verify, seal)"
            )
            assert used in ([], ["--packet"]), (
                f"{doc.name}: receipt.py takes only --packet, got {used}"
            )
        else:
            assert verb, f"{doc.name}: no verb in {snippet!r}"
            assert verb in verbs, (
                f"{doc.name}: documents `{verb}`, which the CLI does not register "
                f"(real verbs: {sorted(verbs)})"
            )
            known = top_level | _flags(verbs[verb])
            unknown = [flag for flag in used if flag not in known]
            assert not unknown, (
                f"{doc.name}: `{verb}` does not accept {unknown} "
                f"(accepted: {sorted(known)})"
            )
        checked += 1

    if doc in DOCS_THAT_MUST_HAVE_COMMANDS:
        assert checked, f"{doc.name}: no baton CLI invocation found — scanner broke"


def test_skill_documents_every_verb_the_loop_needs():
    skill = (ROOT / "skills" / "baton" / "SKILL.md").read_text(encoding="utf-8")
    for verb in ("actions", "bind", "reconcile", "status", "cleanup", "abandon"):
        assert f"| `{verb}" in skill, f"SKILL.md has no row for `{verb}`"


def test_skill_does_not_promise_a_removed_verb_or_session_default():
    skill = (ROOT / "skills" / "baton" / "SKILL.md").read_text(encoding="utf-8")
    assert re.search(r"`run\b", skill) is None
    assert "Default session_id" not in skill
