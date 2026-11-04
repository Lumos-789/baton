"""SessionController action, bind, resume, and terminal-event behavior."""

from __future__ import annotations

import shutil
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from orchestration.plan_store import ExecutionAttemptError  # noqa: E402
from orchestration.session_controller import SessionController  # noqa: E402
from orchestration.receipt import seal_receipt  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "sample-change"


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "t@t.t")
    _git(project, "config", "user.name", "test")
    (project / ".gitignore").write_text(".dsh/\n", encoding="utf-8")
    (project / "base.txt").write_text("base\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "init")
    change = project / "openspec" / "changes" / "feature"
    change.mkdir(parents=True)
    for source in FIXTURES.glob("*.md"):
        shutil.copy(source, change / source.name)
    return project


def _dispatches(batch):
    return [action for action in batch.actions if action.kind == "dispatch"]


def test_actions_returns_distinct_dispatches_and_no_prd(tmp_path):
    project = _project(tmp_path)
    controller = SessionController(project, "feature")

    batch = controller.actions("session-a", capacity=2)
    dispatches = _dispatches(batch)

    assert [action.ticket_num for action in dispatches] == [201, 202]
    assert len({action.assignment_id for action in dispatches}) == 2
    assert all(Path(action.cwd).is_dir() for action in dispatches)
    assert all(Path(action.packet_path).is_file() for action in dispatches)
    assert not (project / "prd-feature.json").exists()


def test_repeated_actions_do_not_create_duplicate_claims(tmp_path):
    controller = SessionController(_project(tmp_path), "feature")

    first = controller.actions("session-a", capacity=2)
    second = controller.actions("session-a", capacity=2)

    assert {a.assignment_id for a in _dispatches(first)} == {
        a.assignment_id for a in _dispatches(second)
    }
    assert len(controller.store.list_execution_attempts()) == 2


def test_bind_agent_and_resume_waits_for_bound_attempt(tmp_path):
    project = _project(tmp_path)
    controller = SessionController(project, "feature")
    action = _dispatches(controller.actions("session-a", capacity=1))[0]

    bound = controller.bind_agent(action.assignment_id, action.claim_token, "agent-1")
    resumed = SessionController(project, "feature").actions("session-a", capacity=1)

    assert bound.state == "spawned"
    assert resumed.actions[0].kind == "wait"
    assert action.assignment_id in resumed.actions[0].active_assignments

    assert controller.bind_agent(
        action.assignment_id, action.claim_token, "agent-1"
    ).agent_id == "agent-1"
    with pytest.raises(ExecutionAttemptError, match="already bound"):
        controller.bind_agent(action.assignment_id, action.claim_token, "agent-2")


def test_terminal_free_text_without_receipt_becomes_unknown(tmp_path):
    project = _project(tmp_path)
    controller = SessionController(project, "feature")
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    controller.bind_agent(action.assignment_id, action.claim_token, "agent-1")

    batch = controller.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="completed",
        session_id="session-a",
        final_text="Succeeded, commit deadbeef, all tests green",
    )

    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "unknown"
    assert batch.actions[0].kind == "recover"
    assert not controller.store.latest_completion()["complete"]


def test_merge_conflict_returns_resolve_merge_action(tmp_path):
    project = _project(tmp_path)
    controller = SessionController(project, "feature")
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    packet = json.loads(Path(action.packet_path).read_text(encoding="utf-8"))
    integration = Path(packet["integrationPath"])
    worktree = Path(action.cwd)

    (integration / "base.txt").write_text("integration\n", encoding="utf-8")
    _git(integration, "add", "-A")
    _git(integration, "commit", "-m", "integration change")
    (worktree / "base.txt").write_text("assignment\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", "assignment change")
    seal_receipt(action.packet_path)

    batch = controller.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="completed",
        session_id="session-a",
    )

    assert batch.actions[0].kind == "resolve_merge"
    assert controller.store.get_execution_attempt(action.assignment_id).state == "blocked-review"


def test_session_execution_never_creates_prd_file(tmp_path):
    """Driving the controller end-to-end must never write a prd-<feature>.json
    (the retired Ralph-era shared protocol)."""
    project = _project(tmp_path)
    controller = SessionController(project, "feature")
    batch = controller.actions("session-a", capacity=2)
    for action in _dispatches(batch):
        controller.bind_agent(action.assignment_id, action.claim_token, "agent-1")
    controller.reconcile(
        _dispatches(batch)[0].assignment_id,
        _dispatches(batch)[0].claim_token,
        agent_terminal="failed",
        session_id="session-a",
        capacity=2,
    )
    assert not (project / "prd-feature.json").exists()
    assert not any(project.glob("prd-*.json"))


def test_reconcile_ignores_free_text_without_valid_receipt(tmp_path):
    """A terminal event with free-text 'success' but no sealed receipt must not
    advance completion — free text is ignored, only a sealed receipt counts."""
    project = _project(tmp_path)
    controller = SessionController(project, "feature")
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    controller.bind_agent(action.assignment_id, action.claim_token, "agent-1")

    controller.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="completed",
        session_id="session-a",
        final_text="All done! Tests green, committed as deadbeef. Ticket resolved.",
    )

    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "unknown"
    assert not controller.store.latest_completion()["complete"]


def test_resume_after_receipt_auto_reconciles(tmp_path):
    """A second actions() call after a receipt was sealed auto-reconciles it:
    the attempt integrates without an explicit reconcile call."""
    project = _project(tmp_path)
    controller = SessionController(project, "feature")
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    controller.bind_agent(action.assignment_id, action.claim_token, "agent-1")

    # Subagent finishes + seals the receipt (no reconcile called yet).
    worktree = Path(action.cwd)
    (worktree / "done.txt").write_text("done\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", "finish")
    seal_receipt(action.packet_path)

    # The next actions() call auto-reconciles the sealed receipt and advances.
    batch = controller.actions("session-a", capacity=1)
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "succeeded"
    # The integrated ticket is no longer in the missing-required list.
    missing = controller.store.latest_completion()["missingRequiredTickets"]
    assert attempt.ticket_num not in missing
