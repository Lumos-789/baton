"""test_session_control.py — CLI adapter's failure contract.

Locks the CLI's error surface: domain failures (unknown change, unknown
assignment, worktree/git trouble) exit 1 with one JSON error line carrying
the exception type, instead of a ~30-line traceback whose only actionable
text is the last line; non-domain exceptions keep their traceback.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import session_control  # noqa: E402


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (repo / "a.txt").write_text("a")
    git("add", "-A")
    git("commit", "-qm", "init")
    return repo


def _add_change(repo: Path, change_id: str) -> None:
    tasks = repo / "openspec" / "changes" / change_id / "tasks.md"
    tasks.parent.mkdir(parents=True)
    tasks.write_text(
        "# Tasks: demo\n\n## 1. Phase\n\n- [ ] 1.1 first task\n", encoding="utf-8"
    )


def test_actions_typoed_change_id_is_one_json_line(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    _add_change(repo, "real-change")

    code = session_control.main(
        [str(repo), "does-not-exist", "actions", "--session-id", "s1"]
    )

    out = capsys.readouterr()
    assert code == 1
    payload = json.loads(out.out)
    assert "PlanStoreError" in payload["error"]
    assert "tasks.md not found" in payload["error"]
    assert payload["command"] == "actions"
    assert out.err == ""


def test_bind_unknown_assignment_clean_error(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    _add_change(repo, "real-change")

    code = session_control.main(
        [str(repo), "real-change", "bind", "tic-0001", "tok", "agent-1"]
    )

    out = capsys.readouterr()
    assert code == 1
    payload = json.loads(out.out)
    assert "ExecutionAttemptError" in payload["error"]
    assert "unknown assignment: tic-0001" in payload["error"]
    assert payload["command"] == "bind"
    assert out.err == ""


def test_reconcile_invalid_terminal_rejected_by_argparse(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    _add_change(repo, "real-change")

    with pytest.raises(SystemExit) as exc:
        session_control.main(
            [
                str(repo),
                "real-change",
                "reconcile",
                "tic-0001",
                "tok",
                "--agent-terminal",
                "done",
                "--session-id",
                "s1",
            ]
        )

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_non_domain_exception_keeps_traceback(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _add_change(repo, "real-change")

    def boom(self, *args, **kwargs):
        raise RuntimeError("controller bug")

    monkeypatch.setattr(session_control.SessionController, "actions", boom)
    with pytest.raises(RuntimeError, match="controller bug"):
        session_control.main(
            [str(repo), "real-change", "actions", "--session-id", "s1"]
        )


@pytest.mark.parametrize(
    ("tail", "message"),
    [
        (["--session-id", "s1", "--capacity", "0"], "capacity must be between 1 and 5"),
        (["--session-id", "s1", "--capacity", "6"], "capacity must be between 1 and 5"),
        (["--session-id", "s1", "--capacity", "two"], "capacity must be an integer"),
        (["--session-id", ""], "session id must be a non-empty string"),
    ],
)
def test_bad_capacity_or_session_id_rejected_by_argparse(
    tmp_path, capsys, tail, message
):
    # The skill documents capacity 1..5 as a hard cap. A violating value must
    # die at the CLI boundary with a usage line — not a ValueError traceback
    # from deep inside the controller.
    repo = _make_repo(tmp_path)
    _add_change(repo, "real-change")

    with pytest.raises(SystemExit) as exc:
        session_control.main([str(repo), "real-change", "actions", *tail])

    out = capsys.readouterr()
    assert exc.value.code == 2
    assert message in out.err
    assert "Traceback (most recent call last)" not in out.err


def test_reconcile_capacity_zero_rejected_by_argparse(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    _add_change(repo, "real-change")

    with pytest.raises(SystemExit) as exc:
        session_control.main(
            [
                str(repo),
                "real-change",
                "reconcile",
                "tic-0001",
                "tok",
                "--agent-terminal",
                "completed",
                "--session-id",
                "s1",
                "--capacity",
                "9",
            ]
        )

    assert exc.value.code == 2
    assert "capacity must be between 1 and 5" in capsys.readouterr().err


def test_status_verb_reports_fresh_run_snapshot(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    _add_change(repo, "real-change")

    code = session_control.main([str(repo), "real-change", "status"])

    out = capsys.readouterr()
    assert code == 0
    payload = json.loads(out.out)
    assert set(payload) == {
        "ok",
        "runId",
        "feature",
        "projectPath",
        "revision",
        "complete",
        "missingRequiredTickets",
        "attempts",
    }
    assert payload["ok"] is True
    assert payload["runId"] == "RUN-real-change"
    assert payload["revision"] == 1
    assert payload["complete"] is False
    assert payload["missingRequiredTickets"] == ["1.1"]
    assert payload["attempts"] == []


def test_status_verb_typoed_change_id_is_one_json_line(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    _add_change(repo, "real-change")

    code = session_control.main([str(repo), "does-not-exist", "status"])

    out = capsys.readouterr()
    assert code == 1
    payload = json.loads(out.out)
    assert "PlanStoreError" in payload["error"]
    assert "tasks.md not found" in payload["error"]
    assert payload["command"] == "status"
    assert out.err == ""


def test_status_verb_shape_equals_controller_status(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    _add_change(repo, "real-change")

    code = session_control.main([str(repo), "real-change", "status"])
    via_cli = json.loads(capsys.readouterr().out)

    controller = session_control.SessionController(str(repo), "real-change")
    via_boundary = controller.status()

    assert code == 0
    assert via_cli == json.loads(json.dumps(via_boundary, ensure_ascii=False))


def test_cleanup_typoed_change_id_is_one_json_line(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    _add_change(repo, "real-change")

    code = session_control.main([str(repo), "never-ran", "cleanup"])

    out = capsys.readouterr()
    assert code == 1
    payload = json.loads(out.out)
    assert "WorktreeError" in payload["error"]
    assert "no baton worktrees or branches found for 'never-ran'" in payload["error"]
    assert payload["command"] == "cleanup"
    assert out.err == ""


def test_cleanup_dry_run_previews_without_removing(tmp_path, capsys):
    repo = _make_repo(tmp_path)
    _add_change(repo, "real-change")
    wt = tmp_path / "proj-baton-real-change-tic-001-a01"
    subprocess.run(
        [
            "git",
            "worktree",
            "add",
            "-b",
            "baton/real-change-tic-001-a01",
            str(wt),
            "main",
        ],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    code = session_control.main([str(repo), "real-change", "cleanup", "--dry-run"])

    out = capsys.readouterr()
    assert code == 0
    payload = json.loads(out.out)
    assert payload["dryRun"] is True
    assert [w["branch"] for w in payload["wouldRemove"]] == [
        "baton/real-change-tic-001-a01"
    ]
    assert wt.exists()  # preview removed nothing
