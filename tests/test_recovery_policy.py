"""Retry budgets, activity-renewed leases, reopen, and honest recover actions.

Before 0.5.0 a ``lease-expired`` ticket was re-dispatched forever and without
a word, the heartbeat the subagent was asked to refresh never influenced the
lease, a failed subagent's reason was discarded, and a succeeded receipt made
a ticket permanent with no way back.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest

from baton_helpers import (  # noqa: E402
    action_of,
    controller_for,
    dispatches,
    git,
    heartbeat,
    packet_of,
    project_with,
    warning_of,
    work,
)
import orchestration.receipt as receipt_module  # noqa: E402
from _ticket import check_task, uncheck_task  # noqa: E402
from orchestration.plan_store import ExecutionAttemptError  # noqa: E402
from orchestration.receipt import ReceiptError, verify_packet  # noqa: E402
from orchestration.session_controller import SessionControllerError  # noqa: E402
from orchestration.store_base import MAX_ATTEMPTS_PER_TICKET  # noqa: E402

SINGLE = {"A": ["a ticket that never finishes"]}


def _single(tmp_path, **kwargs):
    project = project_with(tmp_path, SINGLE, ignore_dsh=True)
    return project, controller_for(project, ["true"], **kwargs)


def _tasks(project) -> Path:
    return project / "openspec" / "changes" / "feature" / "tasks.md"


def _commit(action, name: str = "wip.txt") -> None:
    (Path(action.cwd) / name).write_text("work in progress\n")
    git(Path(action.cwd), "add", "-A")
    git(Path(action.cwd), "commit", "-q", "-m", "wip")


# --- automatic retries have a budget -------------------------------------------


def test_lease_expired_retries_stop_at_the_budget(tmp_path):
    project, controller = _single(tmp_path, lease_seconds=1)
    seen = []
    for _ in range(MAX_ATTEMPTS_PER_TICKET):
        action = dispatches(controller.actions("s1"))[0]
        seen.append(action.assignment_id)
        time.sleep(1.2)  # the subagent touches nothing: silence

    stopped = controller.actions("s1")

    assert dispatches(stopped) == []
    assert stopped.state == "blocked"
    recover = action_of(stopped, "recover")
    assert recover.reason == "lease-expired"
    assert recover.attempts_used == MAX_ATTEMPTS_PER_TICKET
    assert recover.attempts_max == MAX_ATTEMPTS_PER_TICKET
    assert "budget is spent" in recover.detail
    assert any("reopen 1.1" in step for step in recover.next)
    assert len(controller.store.list_execution_attempts()) == MAX_ATTEMPTS_PER_TICKET
    assert len(set(seen)) == MAX_ATTEMPTS_PER_TICKET

    # ``reopen`` is the operator's override: the budget starts over.
    reopened = controller.reopen("1.1")

    assert reopened["voided_assignments"] == []  # nothing had been delivered
    assert reopened["attempts_reset_from"] == MAX_ATTEMPTS_PER_TICKET
    fresh = dispatches(controller.actions("s1"))
    assert len(fresh) == 1
    assert controller.store.get_execution_attempt(fresh[0].assignment_id).attempt == (
        MAX_ATTEMPTS_PER_TICKET + 1
    )


def test_a_retry_is_announced_with_the_stale_agent(tmp_path):
    project, controller = _single(tmp_path, lease_seconds=1)
    stale = dispatches(controller.actions("s1"))[0]
    controller.bind_agent(stale.assignment_id, stale.claim_token, "agent-old")
    time.sleep(1.2)

    batch = controller.actions("s1")

    retry = dispatches(batch)[0]
    assert retry.assignment_id != stale.assignment_id
    assert action_of(batch, "recover") is None
    announced = warning_of(batch, "lease-expired-retried")
    assert announced["assignment_id"] == stale.assignment_id
    assert announced["agent_id"] == "agent-old"
    assert announced["successor_assignment_id"] == retry.assignment_id
    assert announced["attempts_used"] == 2
    assert "stop the old agent agent-old" in announced["message"]
    retired = controller.store.get_execution_attempt(stale.assignment_id)
    assert retired.state == "superseded"
    assert "superseded-by-retry" in retired.failure_code


# --- the lease is silence, not wall clock --------------------------------------


def test_a_fresh_heartbeat_keeps_a_working_attempt_alive(tmp_path):
    """The lease follows the heartbeat: activity after the original deadline
    still renews it, and silence does not."""
    project, controller = _single(tmp_path, lease_seconds=3)
    action = dispatches(controller.actions("s1"))[0]
    time.sleep(3.5)  # the original 3 s lease is over by now
    heartbeat(action).touch()

    batch = controller.actions("s1")

    assert [item.assignment_id for item in dispatches(batch)] == [action.assignment_id]
    assert len(controller.store.list_execution_attempts()) == 1
    assert controller.store.get_execution_attempt(action.assignment_id).state == "leased"


def test_worktree_edits_count_as_activity(tmp_path):
    project, controller = _single(tmp_path, lease_seconds=3)
    action = dispatches(controller.actions("s1"))[0]
    time.sleep(3.5)
    (Path(action.cwd) / "wip.txt").write_text("still working\n")

    batch = controller.actions("s1")

    assert [item.assignment_id for item in dispatches(batch)] == [action.assignment_id]
    assert controller.store.get_execution_attempt(action.assignment_id).state == "leased"


def test_the_hard_cap_ends_even_a_heartbeating_attempt(tmp_path):
    project, controller = _single(tmp_path, lease_seconds=1)
    first = dispatches(controller.actions("s1"))[0]
    deadline = time.time() + 6
    batch = None
    while time.time() < deadline:
        time.sleep(0.6)
        heartbeat(first).touch()
        batch = controller.actions("s1")
        if dispatches(batch)[0].assignment_id != first.assignment_id:
            break

    assert batch is not None
    assert dispatches(batch)[0].assignment_id != first.assignment_id
    assert warning_of(batch, "lease-expired-retried")["assignment_id"] == first.assignment_id


def test_a_lease_that_ends_mid_call_never_double_dispatches(tmp_path, monkeypatch):
    """One ``actions()`` call judges each lease once.

    A lease that ran out between the sweep at the top of the call and the
    claim at its end used to be judged twice: pass 2 re-issued the attempt and
    the claim then retired it and dispatched its retry — two subagents on one
    ticket, in one batch.
    """
    project, controller = _single(tmp_path, lease_seconds=1)
    first = dispatches(controller.actions("s1"))[0]
    store = controller.store
    real_expire = store.expire_execution_attempts

    def set_lease(expires_at: str) -> None:
        db = sqlite3.connect(store.db_path)
        db.execute(
            "UPDATE execution_attempts SET lease_expires_at = ? WHERE assignment_id = ?",
            (expires_at, first.assignment_id),
        )
        db.commit()
        db.close()

    def sweep_then_let_the_lease_end():
        # Alive beyond doubt when the sweep looks, however slow the machine …
        set_lease("2999-01-01T00:00:00+00:00")
        real_expire()
        # … and dead by the time the claim looks.
        set_lease("2000-01-01T00:00:00+00:00")

    monkeypatch.setattr(store, "expire_execution_attempts", sweep_then_let_the_lease_end)
    batch = controller.actions("s1")

    assert [item.assignment_id for item in dispatches(batch)] == [first.assignment_id]
    assert len(store.list_execution_attempts()) == 1

    # The next call sees the dead lease and retries it exactly once. The
    # refresh first moves the backdated deadline to "claim + lease" (the base
    # commit counts as old activity), so wait past that.
    monkeypatch.undo()
    set_lease("2000-01-01T00:00:00+00:00")
    time.sleep(1.2)
    later = controller.actions("s1")
    assert warning_of(later, "lease-expired-retried")["assignment_id"] == first.assignment_id
    assert [item.assignment_id for item in dispatches(later)] != [first.assignment_id]


# --- retired attempts and finished tickets -------------------------------------


def test_a_superseded_attempt_can_be_adopted_once_its_successor_stops(tmp_path):
    project, controller = _single(tmp_path, lease_seconds=1)
    stale = dispatches(controller.actions("s1"))[0]
    controller.bind_agent(stale.assignment_id, stale.claim_token, "agent-old")
    time.sleep(1.2)
    retry = dispatches(controller.actions("s1"))[0]
    # The first subagent was slow, not dead: it finishes with a valid receipt.
    work(stale, {"late.txt": "the first agent actually finished\n"})
    entry = next(
        row
        for row in controller.status()["attempts"]
        if row["assignment_id"] == stale.assignment_id
    )
    assert entry["relay"]["suggested_action"] == "adopt"

    with pytest.raises(SessionControllerError) as refused:
        controller.adopt(stale.assignment_id)
    assert refused.value.code == "successor-in-flight"

    controller.abandon(retry.assignment_id, force=True)
    result = controller.adopt(stale.assignment_id)

    assert result["state"] == "succeeded"
    assert result["adopted_from"] == "superseded"
    assert controller.store.latest_completion()["complete"]


def test_a_stale_unknown_attempt_retires_once_its_ticket_is_delivered(tmp_path):
    project = project_with(tmp_path, {"A": ["slow", "stuck"]}, ignore_dsh=True)
    controller = controller_for(project, ["true"], lease_seconds=1)
    batch = controller.actions("s1", capacity=2)
    slow, stuck = sorted(dispatches(batch), key=lambda action: action.ticket_num)

    # 1.2 fails without a receipt: its recover blocks every new claim, so 1.1
    # cannot be retried when its lease runs out and stays parked as unknown.
    blocked = controller.reconcile(
        stuck.assignment_id, stuck.claim_token, agent_terminal="failed", session_id="s1"
    )
    recoveries = {item.assignment_id for item in blocked.actions if item.kind == "recover"}
    assert stuck.assignment_id in recoveries  # the blocking one
    time.sleep(1.2)
    parked = controller.actions("s1")
    assert {item.assignment_id for item in parked.actions if item.kind == "recover"} == {
        slow.assignment_id,
        stuck.assignment_id,
    }

    # The ticket is delivered another way (here: marked done by hand).
    check_task(_tasks(project), 101)
    after = controller.actions("s1")

    retired = controller.store.get_execution_attempt(slow.assignment_id)
    assert retired.state == "superseded"
    assert retired.failure_code == "superseded-by-receipt"
    assert [item.assignment_id for item in after.actions if item.kind == "recover"] == [
        stuck.assignment_id
    ]


def test_an_unchecked_ticket_is_flagged_not_silently_kept(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project)
    action = dispatches(controller.actions("s1"))[0]
    work(action, {"a.txt": "a\n"})
    done = controller.reconcile(
        action.assignment_id, action.claim_token, agent_terminal="completed", session_id="s1"
    )
    assert done.state == "complete"
    uncheck_task(_tasks(project), 101)

    again = controller.actions("s1")

    # A succeeded receipt is the durable fact: the checkbox alone cannot
    # re-run it, and baton says so instead of quietly ignoring the edit.
    assert dispatches(again) == []
    assert warning_of(again, "resolved-but-unchecked")["tickets"] == ["1.1"]
    assert controller.status()["resolved_but_unchecked"] == ["1.1"]

    reopened = controller.reopen("1.1")

    assert reopened["voided_assignments"] == [action.assignment_id]
    assert "- [ ] 1.1" in _tasks(project).read_text()
    fresh = dispatches(controller.actions("s1"))
    assert [item.ticket_num for item in fresh] == [101]
    assert controller.status()["resolved_but_unchecked"] == []


def test_reopen_refuses_while_an_attempt_owns_the_ticket(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project)
    dispatches(controller.actions("s1"))

    with pytest.raises(ExecutionAttemptError, match="abandon it first"):
        controller.reopen("1.1")
    with pytest.raises(SessionControllerError) as unknown:
        controller.reopen("9.9")
    assert unknown.value.code == "unknown-ticket"


# --- why a subagent failed ------------------------------------------------------


def test_reconcile_records_why_the_agent_failed(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project, ["test -f never-written.txt"])
    action = dispatches(controller.actions("s1"))[0]
    controller.bind_agent(action.assignment_id, action.claim_token, "agent-7")
    _commit(action)
    with pytest.raises(ReceiptError):
        verify_packet(action.packet_path)  # the subagent's own red run

    batch = controller.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="failed",
        session_id="s1",
        reason="ImportError: cannot import 'foo' from lib.py",
    )

    recover = action_of(batch, "recover")
    assert recover.reason == "agent-failed-without-sealed-receipt"
    assert "ImportError: cannot import 'foo'" in recover.detail
    assert "test -f never-written.txt exited 1" in recover.detail
    assert recover.agent_id == "agent-7"
    assert recover.worktree_path == action.cwd
    assert recover.next

    # The account is durable: a later call says the same thing.
    again = action_of(controller.actions("s1"), "recover")
    assert again.detail == recover.detail


def test_a_failed_verification_keeps_its_output(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project, ['sh -c "echo EVIDENCE_ASSERTION >&2; exit 3"'])
    action = dispatches(controller.actions("s1"))[0]
    _commit(action)

    with pytest.raises(ReceiptError) as failed:
        verify_packet(action.packet_path)

    assert "exited 3" in str(failed.value)
    assert "EVIDENCE_ASSERTION" in str(failed.value)
    report = json.loads(Path(packet_of(action)["verificationPath"]).read_text())
    assert report["ok"] is False
    assert report["verification"][0]["exitCode"] == 3


def test_a_hanging_verification_is_killed_and_reported(tmp_path, monkeypatch):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(
        project, ["sh -c 'sleep 30 & echo $! > sleeper.pid; wait'"]
    )
    action = dispatches(controller.actions("s1"))[0]
    monkeypatch.setattr(receipt_module, "VERIFICATION_TIMEOUT_SECONDS", 1)
    _commit(action)

    with pytest.raises(ReceiptError, match="timed out after 1s"):
        verify_packet(action.packet_path)

    pid = int((Path(action.cwd) / "sleeper.pid").read_text().strip())
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # the whole process group died, not just the shell


def test_verification_commands_see_the_callers_path(tmp_path, monkeypatch):
    """A login shell rebuilds PATH (macOS ``path_helper``); a plain one
    inherits the caller's, so `python3` is the same interpreter the subagent
    and the user see."""
    monkeypatch.setenv("BATON_EXPECTED_PATH", os.environ["PATH"])
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project, ['test "$PATH" = "$BATON_EXPECTED_PATH"'])
    action = dispatches(controller.actions("s1"))[0]

    envelope = work(action, {"a.txt": "a\n"})

    assert envelope.verification[0]["exitCode"] == 0
