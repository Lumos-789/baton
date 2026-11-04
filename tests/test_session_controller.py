"""SessionController action, bind, resume, and terminal-event behavior."""

from __future__ import annotations

import shutil
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from orchestration.plan_store import ExecutionAttemptError  # noqa: E402
from orchestration.receipt import seal_receipt  # noqa: E402
from orchestration.session_controller import (  # noqa: E402
    SessionController,
    SessionControllerError,
)

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


VERIFY = ["test -f base.txt"]


def _controller(project: Path, **kwargs) -> SessionController:
    kwargs.setdefault("verification_commands", VERIFY)
    return SessionController(project, "feature", **kwargs)


def _dispatches(batch):
    return [action for action in batch.actions if action.kind == "dispatch"]


def test_actions_returns_distinct_dispatches_and_no_prd(tmp_path):
    project = _project(tmp_path)
    controller = _controller(project)

    batch = controller.actions("session-a", capacity=2)
    dispatches = _dispatches(batch)

    assert [action.ticket_num for action in dispatches] == [201, 202]
    assert len({action.assignment_id for action in dispatches}) == 2
    assert all(Path(action.cwd).is_dir() for action in dispatches)
    assert all(Path(action.packet_path).is_file() for action in dispatches)
    assert not (project / "prd-feature.json").exists()


def test_repeated_actions_do_not_create_duplicate_claims(tmp_path):
    controller = _controller(_project(tmp_path))

    first = controller.actions("session-a", capacity=2)
    second = controller.actions("session-a", capacity=2)

    assert {a.assignment_id for a in _dispatches(first)} == {
        a.assignment_id for a in _dispatches(second)
    }
    assert len(controller.store.list_execution_attempts()) == 2


def test_bind_agent_and_resume_waits_for_bound_attempt(tmp_path):
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]

    bound = controller.bind_agent(action.assignment_id, action.claim_token, "agent-1")
    # Deliberately no --verify here: the first claim persisted the run commands.
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
    controller = _controller(project)
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
    controller = _controller(project)
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
    controller = _controller(project)
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
    controller = _controller(project)
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
    controller = _controller(project)
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


def _finish(action, marker: str) -> None:
    worktree = Path(action.cwd)
    (worktree / marker).write_text("done\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", f"finish {marker}")
    seal_receipt(action.packet_path)


def _packet_payload(action) -> dict:
    return json.loads(Path(action.packet_path).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("segment", "tamper"),
    [
        (
            "verification[0].exitCode",
            lambda receipt: receipt["verification"][0].__setitem__("exitCode", 1),
        ),
        (
            "changed_files",
            lambda receipt: receipt["changed_files"].append("injected.txt"),
        ),
        ("commit_sha", lambda receipt: receipt.__setitem__("commit_sha", "0" * 40)),
    ],
)
def test_reconcile_rejects_tampered_sealed_receipt(tmp_path, segment, tamper):
    """Editing any receipt body field must be rejected before integration."""
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    _finish(action, "task-2.1.txt")
    packet = _packet_payload(action)
    integration = Path(packet["integrationPath"])
    before = _git(integration, "rev-parse", "HEAD")

    receipt_path = Path(packet["receiptPath"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    tamper(receipt)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SessionControllerError, match="receiptHash") as excinfo:
        controller.reconcile(
            action.assignment_id,
            action.claim_token,
            agent_terminal="completed",
            session_id="session-a",
        )

    assert segment in str(excinfo.value)
    assert _git(integration, "rev-parse", "HEAD") == before
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "unknown"
    assert attempt.failure_code.startswith("receipt-invalid:")
    assert attempt.ticket_num in controller.store.latest_completion()[
        "missingRequiredTickets"
    ]


def test_wrong_ticket_receipt_rejected_before_branch_merge(tmp_path):
    """A sealed receipt for another ticket must never reach the merge step."""
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    _finish(action, "task-2.1.txt")
    packet = _packet_payload(action)
    integration = Path(packet["integrationPath"])
    before = _git(integration, "rev-parse", "HEAD")

    forged = dict(packet)
    forged["ticketNum"] = action.ticket_num + 1
    forged_packet = Path(action.packet_path).with_name("wrong-ticket-packet.json")
    forged_packet.write_text(json.dumps(forged), encoding="utf-8")
    forged_envelope = seal_receipt(forged_packet)
    assert forged_envelope.ticket_num == action.ticket_num + 1

    with pytest.raises(SessionControllerError, match="ticket_num") as excinfo:
        controller.reconcile(
            action.assignment_id,
            action.claim_token,
            agent_terminal="completed",
            session_id="session-a",
        )

    assert "expected=" + str(action.ticket_num) in str(excinfo.value)
    assert _git(integration, "rev-parse", "HEAD") == before
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state != "integrating"
    assert attempt.state == "unknown"
    # Still recoverable: the ticket stays unresolved and the controller can
    # surface the rejected attempt instead of having merged the wrong branch.
    batch = controller.actions("session-a", capacity=1)
    assert any(action.kind == "recover" for action in batch.actions)
    assert action.ticket_num in controller.store.latest_completion()[
        "missingRequiredTickets"
    ]


def test_lease_expiry_supersedes_stale_attempt_and_completes_later_tickets(tmp_path):
    """A dead lease must be replaced, not block every later claim forever."""
    project = _project(tmp_path)
    controller = _controller(project, lease_seconds=1)
    first = _dispatches(controller.actions("session-a", capacity=1))[0]
    assert first.ticket_num == 201

    time.sleep(1.1)

    retry_batch = controller.actions("session-a", capacity=1)
    retries = [
        action for action in _dispatches(retry_batch) if action.ticket_num == 201
    ]
    assert len(retries) == 1
    retry = retries[0]
    assert retry.assignment_id != first.assignment_id
    assert not any(action.kind == "recover" for action in retry_batch.actions)
    stale = controller.store.get_execution_attempt(first.assignment_id)
    assert stale.state == "superseded"
    assert "superseded-by-retry" in stale.failure_code

    _finish(retry, "task-2.1.txt")
    after_retry = controller.reconcile(
        retry.assignment_id,
        retry.claim_token,
        agent_terminal="completed",
        session_id="session-a",
        capacity=1,
    )
    assert not any(action.kind == "recover" for action in after_retry.actions)
    assert controller.store.get_execution_attempt(first.assignment_id).state == "superseded"
    second = next(
        action for action in _dispatches(after_retry) if action.ticket_num == 202
    )

    _finish(second, "task-2.2.txt")
    after_second = controller.reconcile(
        second.assignment_id,
        second.claim_token,
        agent_terminal="completed",
        session_id="session-a",
        capacity=1,
    )
    final = next(
        action for action in _dispatches(after_second) if action.ticket_num == 301
    )

    _finish(final, "task-3.1.txt")
    after_final = controller.reconcile(
        final.assignment_id,
        final.claim_token,
        agent_terminal="completed",
        session_id="session-a",
        capacity=1,
    )

    assert [action.kind for action in after_final.actions] == ["complete"]
    assert controller.store.latest_completion()["complete"] is True

    # A late completion notice from the stale attempt is ignored instead of
    # wedging the run (the attempt was already retired).
    late = controller.reconcile(
        first.assignment_id,
        first.claim_token,
        agent_terminal="completed",
        session_id="session-a",
        capacity=1,
    )
    assert [action.kind for action in late.actions] == ["complete"]
    assert controller.store.get_execution_attempt(first.assignment_id).state == "superseded"


def test_abandon_clears_stuck_attempt_and_allows_a_retry(tmp_path):
    project = _project(tmp_path)
    controller = _controller(project)
    first = _dispatches(controller.actions("session-a", capacity=1))[0]
    controller.bind_agent(first.assignment_id, first.claim_token, "agent-1")
    stuck = controller.reconcile(
        first.assignment_id,
        first.claim_token,
        agent_terminal="completed",
        session_id="session-a",
        capacity=1,
    )
    assert any(action.kind == "recover" for action in stuck.actions)

    result = controller.abandon(first.assignment_id, reason="test-escape-hatch")

    assert result["state"] == "abandoned"
    assert result["ticketNum"] == first.ticket_num
    assert "worktree" in result["preserves"]
    retried = _dispatches(controller.actions("session-a", capacity=1))
    assert [action.ticket_num for action in retried] == [first.ticket_num]
    assert retried[0].assignment_id != first.assignment_id


def test_verification_commands_persist_across_controller_invocations(tmp_path):
    """A later call without --verify must reuse the run's stored commands."""
    project = _project(tmp_path)
    first = SessionController(
        project, "feature", verification_commands=["test -f base.txt"]
    )
    action = _dispatches(first.actions("session-a", capacity=1))[0]
    packet = _packet_payload(action)
    assert packet["taskPacket"]["verification_commands"] == ["test -f base.txt"]

    _finish(action, "task-2.1.txt")
    bare = SessionController(project, "feature")  # no verification_commands
    batch = bare.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="completed",
        session_id="session-a",
        capacity=1,
    )
    next_dispatch = next(
        action for action in _dispatches(batch) if action.ticket_num == 202
    )
    next_packet = _packet_payload(next_dispatch)
    assert next_packet["taskPacket"]["verification_commands"] == ["test -f base.txt"]
    assert "git diff --check" not in next_packet["taskPacket"]["verification_commands"]
    assert bare.store.run_verification_commands() == ["test -f base.txt"]


def test_explicit_verify_overrides_persisted_commands(tmp_path):
    project = _project(tmp_path)
    first = _controller(project)
    action = _dispatches(first.actions("session-a", capacity=1))[0]
    _finish(action, "task-2.1.txt")

    override = SessionController(
        project, "feature", verification_commands=["true"]
    )
    batch = override.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="completed",
        session_id="session-a",
        capacity=1,
    )
    next_dispatch = next(
        action for action in _dispatches(batch) if action.ticket_num == 202
    )
    assert _packet_payload(next_dispatch)["taskPacket"]["verification_commands"] == [
        "true"
    ]
    assert SessionController(project, "feature").store.run_verification_commands() == [
        "true"
    ]


def test_no_verification_commands_fails_loud_instead_of_defaulting(tmp_path):
    project = _project(tmp_path)
    controller = SessionController(project, "feature")

    with pytest.raises(
        SessionControllerError, match="no verification commands configured"
    ):
        controller.actions("session-a", capacity=1)

    assert controller.store.run_verification_commands() == []
