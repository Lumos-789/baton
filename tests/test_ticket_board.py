"""test_ticket_board.py — CLI contract of the board generator.

Fail-loud contract: the mutating verbs (check/uncheck) and scan must exit
non-zero when the operation fails, so wrapper scripts never mistake a failed
flip for success — the ``[x]`` write-back is the run's audit record.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import ticket_board  # noqa: E402


SAMPLE = """# Tasks: demo

## 1. Seed

- [x] 1.1 seed the data

## 2. Build

- [ ] 2.1 first build
- [ ] 2.2 second build
"""


def _project(tmp_path: Path) -> Path:
    change = tmp_path / "openspec" / "changes" / "demo"
    change.mkdir(parents=True)
    (change / "tasks.md").write_text(SAMPLE, encoding="utf-8")
    return tmp_path


def _tasks_md(project: Path) -> str:
    return (project / "openspec" / "changes" / "demo" / "tasks.md").read_text(
        encoding="utf-8"
    )


def test_scan_success_writes_board_and_exits_zero(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "scan", str(project), "demo"])
    assert rc == 0
    board = json.loads(capsys.readouterr().out)
    assert board["stats"] == {"total": 3, "resolved": 1, "ready": 2}
    assert board["frontier"] == [201, 202]
    assert (project / ".dsh" / "baton" / "demo" / "board.json").is_file()


def test_scan_missing_change_exits_nonzero(tmp_path, capsys):
    rc = ticket_board.main(["ticket_board", "scan", str(tmp_path), "nope"])
    assert rc == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_scan_omitted_id_infers_the_sole_change(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "scan", str(project)])
    assert rc == 0
    board = json.loads(capsys.readouterr().out)
    assert board["feature"] == "demo"
    assert board["stats"]["total"] == 3
    assert (project / ".dsh" / "baton" / "demo" / "board.json").is_file()


def test_build_omitted_id_infers_the_sole_change(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "build", str(project)])
    assert rc == 0
    assert (project / ".dsh" / "baton" / "demo" / "board.html").is_file()


def test_omitted_id_zero_changes_fails_loud_naming_dir(tmp_path, capsys):
    rc = ticket_board.main(["ticket_board", "scan", str(tmp_path)])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert "no change id given" in out["error"]
    assert str(tmp_path / "openspec" / "changes") in out["error"]
    assert not (tmp_path / ".dsh").exists()


def test_omitted_id_two_changes_fails_loud_listing_candidates(tmp_path, capsys):
    project = _project(tmp_path)
    other = project / "openspec" / "changes" / "other"
    other.mkdir()
    (other / "tasks.md").write_text("# Tasks: other\n\n## 1. A\n\n- [ ] 1.1 a\n", encoding="utf-8")
    rc = ticket_board.main(["ticket_board", "build", str(project)])
    assert rc == 1
    out = json.loads(capsys.readouterr().out)
    assert "2 changes found" in out["error"]
    assert "demo" in out["error"] and "other" in out["error"]
    assert not (project / ".dsh").exists()


def test_check_uncheck_roundtrip_exits_zero(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "check", str(project), "demo", "2.1"])
    assert rc == 0
    assert "- [x] 2.1 first build" in _tasks_md(project)
    assert (project / ".dsh" / "baton" / "demo" / "board.json").is_file()

    rc = ticket_board.main(["ticket_board", "uncheck", str(project), "demo", "2.1"])
    assert rc == 0
    assert "- [ ] 2.1 first build" in _tasks_md(project)


def test_check_missing_change_exits_nonzero(tmp_path, capsys):
    rc = ticket_board.main(["ticket_board", "check", str(tmp_path), "demo", "2.1"])
    assert rc == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_check_malformed_task_id_exits_nonzero(tmp_path, capsys):
    project = _project(tmp_path)
    for bad in ("21", "2.x", "2.1.1", ""):
        rc = ticket_board.main(["ticket_board", "check", str(project), "demo", bad])
        assert rc == 1, f"expected exit 1 for task id {bad!r}"
        assert "error" in json.loads(capsys.readouterr().out)
    assert "- [ ] 2.1 first build" in _tasks_md(project)


def test_check_out_of_range_task_id_is_a_clean_error(tmp_path, capsys):
    # 100.1 is out of the supported N*100+M range; it must read like every
    # other bad-id case — one JSON error line, exit 1, no traceback.
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "check", str(project), "demo", "100.1"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "out of supported range" in json.loads(out)["error"]
    assert "- [ ] 2.1 first build" in _tasks_md(project)


def test_check_unknown_task_exits_nonzero(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "check", str(project), "demo", "9.9"])
    assert rc == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_uncheck_already_unchecked_exits_nonzero(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "uncheck", str(project), "demo", "2.2"])
    assert rc == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_check_invalid_change_id_exits_nonzero(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "check", str(project), "../escape", "2.1"])
    assert rc == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_check_output_echoes_reverse_command(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "check", str(project), "demo", "2.1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reverse"].startswith("python3 ")
    assert "uncheck" in out["reverse"]
    assert str(project) in out["reverse"]
    assert "2.1" in out["reverse"]


def test_echoed_reverse_command_actually_undoes_the_flip(tmp_path, capsys):
    """The reverse hint is a real runnable command, not decoration."""
    import shlex

    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "check", str(project), "demo", "2.1"])
    assert rc == 0
    reverse = json.loads(capsys.readouterr().out)["reverse"]
    tokens = shlex.split(reverse)  # python3 <script> uncheck <project> <change> N.M
    assert ticket_board.main([tokens[1]] + tokens[2:]) == 0
    assert "- [ ] 2.1 first build" in _tasks_md(project)
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "unchecked"
    assert " check " in out["reverse"]


def test_check_dry_run_previews_without_writing(tmp_path, capsys):
    project = _project(tmp_path)
    before = _tasks_md(project)
    rc = ticket_board.main(
        ["ticket_board", "check", "--dry-run", str(project), "demo", "2.1"]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "would check"
    assert out["current"] == "ready"
    assert out["dry_run"] is True
    assert out["reverse"]
    assert _tasks_md(project) == before
    assert not (project / ".dsh" / "baton" / "demo" / "board.json").exists()


def test_check_dry_run_on_resolved_task_reports_noop(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(
        ["ticket_board", "check", "--dry-run", str(project), "demo", "1.1"]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["action"] == "no-op"
    assert out["current"] == "resolved"


def test_check_dry_run_unknown_task_exits_nonzero_without_write(tmp_path, capsys):
    project = _project(tmp_path)
    before = _tasks_md(project)
    rc = ticket_board.main(
        ["ticket_board", "check", "--dry-run", str(project), "demo", "9.9"]
    )
    assert rc == 1
    assert "error" in json.loads(capsys.readouterr().out)
    assert _tasks_md(project) == before


def test_uncheck_dry_run_mirrors_not_checked_error(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(
        ["ticket_board", "uncheck", "--dry-run", str(project), "demo", "2.2"]
    )
    assert rc == 1
    assert "not checked" in json.loads(capsys.readouterr().out)["error"]


def test_uncheck_preserves_crlf_through_the_reverse_command(tmp_path, capsys):
    """The documented ``reverse`` of the write-back must be byte-faithful.

    ``uncheck`` used to read/write through universal-newline text I/O, so one
    flip rewrote a whole Windows checkout to LF.
    """
    project = _project(tmp_path)
    tasks = project / "openspec" / "changes" / "demo" / "tasks.md"
    tasks.write_bytes(b"# Tasks: demo\r\n\r\n## 1. Seed\r\n\r\n- [x] 1.1 seed\r\n")
    capsys.readouterr()

    rc = ticket_board.main(["ticket_board", "uncheck", str(project), "demo", "1.1"])

    assert rc == 0
    assert tasks.read_bytes() == b"# Tasks: demo\r\n\r\n## 1. Seed\r\n\r\n- [ ] 1.1 seed\r\n"


def test_uncheck_flips_uppercase_x_the_parser_tolerates(tmp_path, capsys):
    """`_ticket.py` maps hand-written ``[X]`` to resolved; uncheck must undo it."""
    project = _project(tmp_path)
    tasks = project / "openspec" / "changes" / "demo" / "tasks.md"
    tasks.write_text(SAMPLE.replace("- [x] 1.1", "- [X] 1.1"), encoding="utf-8")
    rc = ticket_board.main(["ticket_board", "uncheck", str(project), "demo", "1.1"])
    assert rc == 0
    assert "- [ ] 1.1 seed the data" in _tasks_md(project)


BROKEN = """# Tasks: oops

## 1. Seed

- [ ] 1.1 one thing
- [ ] 1.1 duplicate task id
"""


def _broken_change(project: Path, name: str = "oops") -> None:
    change = project / "openspec" / "changes" / name
    change.mkdir(parents=True, exist_ok=True)
    (change / "tasks.md").write_text(BROKEN, encoding="utf-8")


def test_build_all_surfaces_broken_change_and_still_exits_zero(tmp_path, capsys):
    project = _project(tmp_path)
    _broken_change(project)
    rc = ticket_board.main(["ticket_board", "build-all", str(project)])
    assert rc == 0
    captured = capsys.readouterr()
    assert "Skipped broken change oops" in captured.err
    assert "duplicate task id" in captured.err
    board = json.loads(
        (project / ".dsh" / "baton" / "board-all.json").read_text(encoding="utf-8")
    )
    assert len(board["skipped_changes"]) == 1
    assert board["skipped_changes"][0]["feature"] == "oops"
    assert "duplicate task id" in board["skipped_changes"][0]["error"]
    assert [t["feature"] for t in board["tickets"]] == ["demo", "demo", "demo"]


def test_build_all_all_changes_broken_exits_nonzero_and_names_them(tmp_path, capsys):
    _broken_change(tmp_path)
    rc = ticket_board.main(["ticket_board", "build-all", str(tmp_path)])
    assert rc == 1
    captured = capsys.readouterr()
    assert "all 1 change(s) failed to parse (oops)" in captured.err
    assert not (tmp_path / ".dsh" / "baton" / "board-all.html").exists()
    # parse-failure evidence is still persisted — empty state ≠ broken state
    board = json.loads(
        (tmp_path / ".dsh" / "baton" / "board-all.json").read_text(encoding="utf-8")
    )
    assert board["skipped_changes"][0]["feature"] == "oops"
    assert board["tickets"] == []


def test_build_all_zero_changes_writes_no_artifact_and_names_searched_dir(
    tmp_path, capsys
):
    (tmp_path / "openspec" / "changes").mkdir(parents=True)
    rc = ticket_board.main(["ticket_board", "build-all", str(tmp_path)])
    assert rc == 1
    captured = capsys.readouterr()
    # the message names where it looked, so a typo'd project path or an
    # all-archived state is diagnosable without re-reading the source
    assert str((tmp_path / "openspec" / "changes").resolve()) in captured.err
    assert not (tmp_path / ".dsh").exists()
    assert captured.out == ""


def test_build_all_healthy_project_has_no_skips(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "build-all", str(project)])
    assert rc == 0
    board = json.loads(
        (project / ".dsh" / "baton" / "board-all.json").read_text(encoding="utf-8")
    )
    assert board["skipped_changes"] == []
    assert "Skipped broken change" not in capsys.readouterr().err


TEMPLATE = Path(__file__).parent.parent / "templates" / "ticket-board.html"


def _template_html() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _cli_verbs() -> set[str]:
    import argparse

    for action in ticket_board.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise AssertionError("ticket_board parser has no subcommands")


def test_template_only_references_real_cli_verbs():
    """Drift tripwire: every runnable command string the board shows or copies
    must use a verb the CLI registers — the template once shipped nonexistent
    ``status``/``add`` commands that failed the moment a user pasted them.
    The copied commands ride on the build-time TOOL_CMD prefix."""
    import re

    referenced = set(re.findall(r"\$\{TOOL_CMD\} ([a-z-]+)", _template_html()))
    assert referenced, "the board should surface at least one real command"
    unknown = referenced - _cli_verbs()
    assert not unknown, f"board references CLI verbs that do not exist: {sorted(unknown)}"


def test_template_js_reads_only_keys_scan_board_emits(tmp_path, capsys):
    """Drift tripwire: every ``t.<key>`` the template JS touches must exist in
    the ticket dicts scan_board emits — a missing key crashed the detail modal
    with a TypeError (``t.criteria`` was never emitted)."""
    import re

    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "scan", str(project), "demo"])
    assert rc == 0
    board = json.loads(capsys.readouterr().out)
    emitted = set(board["tickets"][0])

    used = set(re.findall(r"\bt\.([A-Za-z_]+)", _template_html()))
    missing = used - emitted
    assert not missing, f"template reads ticket keys scan_board never emits: {sorted(missing)}"


def test_built_board_html_carries_real_verbs_and_nm_ids(tmp_path, capsys):
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "build", str(project), "demo"])
    assert rc == 0
    html = (project / ".dsh" / "baton" / "demo" / "board.html").read_text(encoding="utf-8")
    assert "`${TOOL_CMD} check" in html
    assert "`${TOOL_CMD} uncheck" in html
    assert "TOOL_CMD} status" not in html
    assert "TOOL_CMD} add" not in html
    assert '"2.1"' in html  # human N.M ids ride in the injected board data


def test_built_board_copies_an_absolute_tool_command(tmp_path, capsys):
    """The board's copied check/uncheck commands must run from any cwd: the
    tool prefix is baked in as an absolute path at build time. The relative
    ``python3 scripts/ticket_board.py …`` form failed with exit 2 unless the
    terminal happened to sit in the baton checkout."""
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "build", str(project), "demo"])
    assert rc == 0
    capsys.readouterr()
    html = (project / ".dsh" / "baton" / "demo" / "board.html").read_text(encoding="utf-8")
    tool = str(Path(ticket_board.__file__).resolve())
    assert f'const TOOL_CMD = "python3 {tool}"' in html
    assert "python3 scripts/ticket_board.py" not in html  # no cwd-dependent command left


def test_built_board_title_carries_feature_id_without_markup(tmp_path, capsys):
    """Drift tripwire: the generated board's ``<title>`` must be markup-free
    and the tab title must be set from the feature id at render time. The
    template once shipped ``<span id="feature-name">`` inside ``<title>``
    (RCDATA — parsed as literal text, never an element), so the browser tab,
    bookmarks and history entries showed raw markup and every board's tab
    read identically regardless of the change it visualized."""
    import re

    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "build", str(project), "demo"])
    assert rc == 0
    html = (project / ".dsh" / "baton" / "demo" / "board.html").read_text(encoding="utf-8")
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    assert m, "board must carry a <title> element"
    assert "<" not in m.group(1) and ">" not in m.group(1), (
        f"markup leaked into <title>: {m.group(1)!r}"
    )
    assert "document.title" in html, "script must set the tab title at render time"
    assert re.search(r"document\.title\s*=\s*`[^`]*\$\{b\.feature\}`", html), (
        "tab title must carry the feature id from BOARD_DATA"
    )


def test_json_for_html_escapes_markup_and_stays_valid_json():
    """``_json_for_html`` must neutralize ``</script>``-style markup while
    remaining parseable JSON with the values intact."""
    board = {"tickets": [{"title": "escape </script> and <!-- --> and &"}]}
    blob = ticket_board._json_for_html(board)
    assert "</script>" not in blob
    assert "<!--" not in blob
    assert json.loads(blob) == board


def test_build_with_markup_in_titles_keeps_script_block_intact(tmp_path, capsys):
    """A task title that literally contains ``</script>`` must not terminate
    the board's script element: the generated HTML keeps exactly the
    template's own closing tag and carries the escaped form instead. Before
    the escape, such a title left BOARD_DATA as a syntax error (board rendered
    empty) and the trailing title text became live markup."""
    change = tmp_path / "openspec" / "changes" / "hygiene"
    change.mkdir(parents=True)
    (change / "tasks.md").write_text(
        "## 1. Web hygiene\n\n"
        "- [ ] 1.1 escape literal </script> tags in generated HTML reports\n"
        "- [ ] 1.2 review the <!-- --> comment handling\n",
        encoding="utf-8",
    )
    rc = ticket_board.main(["ticket_board", "build", str(tmp_path), "hygiene"])
    assert rc == 0
    html = (tmp_path / ".dsh" / "baton" / "hygiene" / "board.html").read_text(encoding="utf-8")
    assert html.count("</script>") == 1  # the template's own closing tag only
    assert "escape literal \\u003c/script\\u003e" in html
    # the template's own HTML comments stay; the titles' markup does not
    assert "escape literal </" not in html
    assert "review the \\u003c!--" in html


# ── flips keep the built boards the user is looking at in sync ──────────


def _embedded_board(html: str) -> dict:
    """The BOARD_DATA blob a built board.html baked in at build time."""
    import re

    blob = re.search(r"const BOARD_DATA = (.*?);\n", html, re.S).group(1)
    return json.loads(blob)


def _board_html(project: Path, feature: str = "demo") -> str:
    return (project / ".dsh" / "baton" / feature / "board.html").read_text(encoding="utf-8")


def test_flip_rebuilds_the_built_board_in_place(tmp_path, capsys):
    """A flip regenerates board.json — it must also rewrite an already-built
    board.html (its data is baked in), or the reloaded browser tab keeps
    showing the pre-flip state until a manual rebuild."""
    project = _project(tmp_path)
    assert ticket_board.main(["ticket_board", "build", str(project), "demo"]) == 0
    capsys.readouterr()
    rc = ticket_board.main(["ticket_board", "check", str(project), "demo", "2.1"])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    html_path = str(project / ".dsh" / "baton" / "demo" / "board.html")
    assert result.get("boards_refreshed") == [html_path]
    data = _embedded_board(_board_html(project))
    assert data["tickets"][1]["status"] == "resolved"
    assert data["stats"] == {"total": 3, "resolved": 2, "ready": 1}


def test_flip_rebuilds_board_all_when_present(tmp_path, capsys):
    project = _project(tmp_path)
    assert ticket_board.main(["ticket_board", "build-all", str(project)]) == 0
    capsys.readouterr()
    rc = ticket_board.main(["ticket_board", "check", str(project), "demo", "2.1"])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    all_html = str(project / ".dsh" / "baton" / "board-all.html")
    assert result.get("boards_refreshed") == [all_html]  # no per-change board was built
    data = _embedded_board((project / ".dsh" / "baton" / "board-all.html").read_text(encoding="utf-8"))
    assert any(t["status"] == "resolved" for t in data["tickets"] if t["task_id"] == "2.1")


def test_flip_without_built_boards_creates_no_html(tmp_path, capsys):
    """The refresh hook rewrites boards that exist; it must not silently
    create them — building stays an explicit choice."""
    project = _project(tmp_path)
    rc = ticket_board.main(["ticket_board", "check", str(project), "demo", "2.1"])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert "boards_refreshed" not in result
    assert not (project / ".dsh" / "baton" / "demo" / "board.html").exists()
    assert not (project / ".dsh" / "baton" / "board-all.html").exists()


def test_dry_run_flip_never_touches_built_boards(tmp_path, capsys):
    project = _project(tmp_path)
    assert ticket_board.main(["ticket_board", "build", str(project), "demo"]) == 0
    capsys.readouterr()
    before = _board_html(project)
    rc = ticket_board.main(["ticket_board", "check", str(project), "demo", "2.1", "--dry-run"])
    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert "boards_refreshed" not in result
    assert _board_html(project) == before  # byte-identical: dry-run writes nothing


def test_scan_rebuilds_the_built_board(tmp_path, capsys):
    """Editing tasks.md is the live-plan flow; the next scan refreshes the
    built board too, so reopen shows the new plan without a manual build."""
    project = _project(tmp_path)
    assert ticket_board.main(["ticket_board", "build", str(project), "demo"]) == 0
    capsys.readouterr()
    (project / "openspec" / "changes" / "demo" / "tasks.md").write_text(
        SAMPLE + "- [ ] 2.3 third build\n", encoding="utf-8"
    )
    rc = ticket_board.main(["ticket_board", "scan", str(project), "demo"])
    assert rc == 0
    board = json.loads(capsys.readouterr().out)
    assert board["boards_refreshed"] == [str(project / ".dsh" / "baton" / "demo" / "board.html")]
    data = _embedded_board(_board_html(project))
    assert data["stats"]["total"] == 4


def test_built_board_modal_is_a_real_dialog_to_keyboards(tmp_path, capsys):
    """Drift tripwire: the detail modal must be modal to keyboards and screen
    readers too — dialog semantics (role/aria-modal/labelled title), focus
    moved into the dialog on open, Tab/Shift+Tab trapped inside while open,
    and focus returned to the invoking card on close (ESC, Close button and
    overlay click alike). Before, Tab escaped into the board behind the modal
    and closing dumped the keyboard user back at the document start."""
    project = _project(tmp_path)
    assert ticket_board.main(["ticket_board", "build", str(project), "demo"]) == 0
    html = _board_html(project)

    # dialog semantics on the persistent container + a labelled title inside
    assert 'role="dialog"' in html and 'aria-modal="true"' in html
    assert 'aria-labelledby="modal-title"' in html
    assert '<h2 id="modal-title">' in html

    # open: remember the opener, focus the dialog's first control
    assert "_modalReturnFocus = document.activeElement" in html
    assert "querySelector('button')" in html and ".focus()" in html

    # trap: Tab handling wired into the document keydown and wraps both ways
    assert "if (e.key === 'Tab') trapModalTab(e)" in html
    assert "e.shiftKey" in html and "last.focus()" in html and "first.focus()" in html

    # close: every path (ESC / Close button / overlay click) restores the opener
    assert "_modalReturnFocus.focus()" in html
    assert "closeModal(o.id)" in html  # overlay click goes through closeModal too


def test_built_board_long_titles_wrap_instead_of_overflowing(tmp_path, capsys):
    """A long unbroken task title (URL, path, long identifier) must wrap inside
    the card and the modal title instead of overflowing the box — the title
    rules carry overflow-wrap."""
    project = _project(tmp_path)
    assert ticket_board.main(["ticket_board", "build", str(project), "demo"]) == 0
    html = _board_html(project)
    import re

    card = re.search(r"\.card-title\s*\{[^}]*\}", html)
    modal = re.search(r"\.modal h2\s*\{[^}]*\}", html)
    assert card and modal, "card-title and modal h2 rules must exist"
    assert "overflow-wrap: anywhere" in card.group(0)
    assert "overflow-wrap: anywhere" in modal.group(0)
