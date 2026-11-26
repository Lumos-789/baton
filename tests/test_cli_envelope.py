"""The CLI speaks one envelope.

Before 0.5.0 three conventions shared one CLI (``run_id`` next to ``runId``,
``cleanup`` without ``ok``, a refusal as JSON inside an error string). Every
verb now prints ``{"ok", "command", ...}`` with snake_case keys, and a
failure carries a stable ``code`` plus whatever the caller must act on.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from baton_helpers import git, project_with, work  # noqa: E402
import session_control  # noqa: E402


def _run(capsys, *argv):
    code = session_control.main([str(part) for part in argv])
    out = capsys.readouterr()
    return code, json.loads(out.out), out


def _claim(capsys, project):
    code, actions, _ = _run(
        capsys, project, "feature", "--verify", "true", "actions", "--session-id", "s1"
    )
    assert code == 0
    return actions["actions"][0]


def test_success_payloads_carry_ok_command_and_snake_case_keys(tmp_path, capsys):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    code, actions, out = _run(
        capsys, project, "feature", "--verify", "true", "actions", "--session-id", "s1"
    )
    assert code == 0
    assert actions["ok"] is True
    assert actions["command"] == "actions"
    assert {"run_id", "generation", "state", "actions", "warnings", "created_at"} <= set(
        actions
    )
    action = actions["actions"][0]

    code, bound, _ = _run(
        capsys,
        project,
        "feature",
        "bind",
        action["assignment_id"],
        action["claim_token"],
        "agent-1",
    )
    assert code == 0 and bound["command"] == "bind"
    assert bound["agent_id"] == "agent-1"

    work(SimpleNamespace(**action), {"a.txt": "a\n"})
    code, done, _ = _run(
        capsys,
        project,
        "feature",
        "reconcile",
        action["assignment_id"],
        action["claim_token"],
        "--agent-terminal",
        "completed",
        "--session-id",
        "s1",
    )
    assert code == 0 and done["command"] == "reconcile"
    assert done["state"] == "complete"

    code, snapshot, _ = _run(capsys, project, "feature", "status")
    assert code == 0 and snapshot["command"] == "status"
    assert snapshot["complete"] is True

    code, reopened, _ = _run(capsys, project, "feature", "reopen", "1.1")
    assert code == 0 and reopened["command"] == "reopen"
    assert reopened["ticket_id"] == "1.1"

    code, cleaned, _ = _run(capsys, project, "feature", "cleanup", "--include-integration")
    assert code == 0 and cleaned["command"] == "cleanup"
    assert cleaned["integration_included"] is True

    for payload in (actions, bound, done, snapshot, reopened, cleaned):
        assert payload["ok"] is True
        assert all(re.fullmatch(r"[a-z_]+", key) for key in payload), payload.keys()
    assert out.err == ""


def test_error_payloads_carry_a_stable_code(tmp_path, capsys):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    cases = [
        ((project, "nope", "status"), "status", "tasks-md-invalid"),
        (
            (project, "feature", "--verify", "true", "bind", "ASG-x", "tok", "agent-1"),
            "bind",
            "unknown-assignment",
        ),
        ((project, "feature", "actions", "--session-id", "s1"), "actions", "verification-unconfigured"),
        ((project, "../evil", "status"), "status", "invalid-change-id"),
        ((project, "feature", "reopen", "9.9"), "reopen", "unknown-ticket"),
        ((project, "ghost", "cleanup"), "cleanup", "worktree"),
    ]
    for argv, command, expected in cases:
        code, payload, out = _run(capsys, *argv)
        assert code == 1, argv
        assert payload["ok"] is False
        assert payload["command"] == command
        assert payload["code"] == expected
        assert payload["error"]
        assert out.err == ""


def test_the_reconcile_reason_is_recorded_for_diagnosis(tmp_path, capsys):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    action = _claim(capsys, project)

    code, batch, _ = _run(
        capsys,
        project,
        "feature",
        "reconcile",
        action["assignment_id"],
        action["claim_token"],
        "--agent-terminal",
        "failed",
        "--session-id",
        "s1",
        "--reason",
        "ran out of context after 40 edits",
    )

    assert code == 0
    recover = next(item for item in batch["actions"] if item["kind"] == "recover")
    assert recover["reason"] == "agent-failed-without-sealed-receipt"
    assert "ran out of context after 40 edits" in recover["detail"]
    assert recover["next"]


def test_malformed_ticket_ids_die_at_the_cli_boundary(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    for bad in ("1", "a.b", "1.2.3", ""):
        with pytest.raises(SystemExit) as exc:
            session_control.main([str(project), "feature", "reopen", bad])
        assert exc.value.code == 2


def test_cleanup_stays_truthful_once_nothing_is_left(tmp_path, capsys):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    _claim(capsys, project)

    code, first, _ = _run(capsys, project, "feature", "cleanup", "--include-integration")
    assert code == 0 and first["removed"]
    # Neither branch ever left main's history, so both went.
    assert sorted(entry["branch"] for entry in first["branches_deleted"]) == [
        "baton/feature",
        "baton/feature-tic-101-a01",
    ]

    # Nothing of the run is left, yet the plan store remembers it ran here.
    code, again, _ = _run(capsys, project, "feature", "cleanup")
    assert code == 0
    assert again == {
        "ok": True,
        "command": "cleanup",
        "removed": [],
        "skipped": [],
        "branches_deleted": [],
        "branches_kept": [],
        "integration_included": False,
    }
    # …while a change that never ran here is still an error.
    code, never, _ = _run(capsys, project, "ghost", "cleanup")
    assert code == 1 and never["code"] == "worktree"
