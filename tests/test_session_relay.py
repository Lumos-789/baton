"""Relay judgments and the operator hatches: the 2026-11-06 fusion-s1
incident replayed, and the adopt path that repairs it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from baton_helpers import (  # noqa: E402
    dispatches as _dispatches,
    fixture_controller as _controller,
    fixture_project as _project,
    finish as _finish,
    git as _git,
    packet_payload as _packet_payload,
)
from orchestration.receipt import seal_receipt  # noqa: E402
from orchestration.relay import ABANDON_GUARD_FRACTION  # noqa: E402
from orchestration.session_controller import SessionControllerError  # noqa: E402


# --- relay: the 2026-11-06 fusion-s1 incident, replayed ---------------------


def test_incident_replay_spawned_no_receipt_reads_alive_and_guards_abandon(
    tmp_path,
):
    """The incident shape: spawned, mid-lease, no receipt, clean worktree.

    status must say "wait" with the judgment data, and abandon must be
    refused (the escape hatch is one --force away, but never free).
    """
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    controller.bind_agent(action.assignment_id, action.claim_token, "agent-1")

    status = controller.status()
    entry = next(
        item
        for item in status["attempts"]
        if item["assignment_id"] == action.assignment_id
    )
    relay = entry["relay"]
    assert entry["state"] == "spawned"
    assert relay["suggested_action"] == "wait"
    assert relay["receipt_exists"] is False
    assert relay["elapsed_sec"] is not None and relay["elapsed_sec"] < 60
    assert relay["lease_remaining_sec"] > 7000
    assert relay["lease_elapsed_fraction"] < ABANDON_GUARD_FRACTION
    assert relay["worktree"]["exists"] is True

    with pytest.raises(SessionControllerError, match="abandon refused") as excinfo:
        controller.abandon(action.assignment_id)
    refusal = excinfo.value.details["refusal"]
    assert refusal["state"] == "spawned"
    assert refusal["suggested_action"] == "wait"
    assert refusal["lease_remaining_sec"] > 7000
    assert "--force" in refusal["force"]

    forced = controller.abandon(action.assignment_id, force=True)
    assert forced["state"] == "abandoned"


def test_dispatch_prompt_teaches_the_heartbeat(tmp_path):
    """The prompt must tell the subagent to refresh the out-of-worktree
    heartbeat file, so a relay session can observe liveness."""
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]

    assert "heartbeat" in action.prompt
    heartbeat = Path(action.packet_path).parent / "progress"
    assert f"`{heartbeat}`" in action.prompt
    assert "never commit it" in action.prompt


def test_dispatch_prompt_asks_for_seal_only_and_points_at_the_whole_change(tmp_path):
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]

    # `seal` re-runs the verification itself: a separate `verify` step only
    # doubled the suite for every assignment.
    assert " seal --packet" in action.prompt
    assert " verify --packet" not in action.prompt
    # OpenSpec keeps the real acceptance bar in the change's own folder.
    assert "openspec/changes/feature/" in action.prompt
    assert "design.md" in action.prompt and "specs/" in action.prompt
    assert action.ticket_title  # the task text travels with the action


def test_status_relay_says_adopt_for_abandoned_attempt_with_late_receipt(
    tmp_path,
):
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    controller.abandon(action.assignment_id, force=True)
    _finish(action, "task-2.1.txt")

    status = controller.status()
    entry = next(
        item
        for item in status["attempts"]
        if item["assignment_id"] == action.assignment_id
    )
    assert entry["state"] == "abandoned"
    assert entry["relay"]["receipt_exists"] is True
    assert entry["relay"]["suggested_action"] == "adopt"


def test_late_sealed_receipt_after_abandon_is_adopted(tmp_path):
    """The incident repaired: the abandoned subagent finished and sealed.

    The protocol ignores the late receipt (terminal state); `adopt` re-enters
    it through the normal verify → integrate path, so the ticket lands with
    zero re-work.
    """
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    controller.bind_agent(action.assignment_id, action.claim_token, "agent-1")
    controller.abandon(action.assignment_id, force=True)

    # The subagent was alive all along; the receipt lands after the abandon.
    _finish(action, "task-2.1.txt")

    # Sanity: the terminal state makes the protocol ignore the sealed receipt
    # — no envelope was recorded. That silence is the incident.
    assert controller.store.result_envelopes() == []

    result = controller.adopt(action.assignment_id)

    assert result["adopted_from"] == "abandoned"
    assert result["state"] == "succeeded"
    assert result["follow_up"] is None
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "succeeded"
    assert attempt.commit_sha
    assert attempt.ticket_num not in controller.store.latest_completion()["missing_required_tickets"]
    tasks_md = (project / "openspec" / "changes" / "feature" / "tasks.md").read_text(
        encoding="utf-8"
    )
    assert "- [x] 2.1" in tasks_md

    # The run proceeds to the next ticket — no re-claim of 201.
    batch = controller.actions("session-a", capacity=1)
    assert [item.ticket_num for item in _dispatches(batch)] == [202]


def test_adopt_refuses_when_a_successor_already_sealed(tmp_path):
    """Two sealed receipts for one ticket: adopt must not pick a winner."""
    project = _project(tmp_path)
    controller = _controller(project)
    first = _dispatches(controller.actions("session-a", capacity=1))[0]
    controller.abandon(first.assignment_id, force=True)

    retry = _dispatches(controller.actions("session-a", capacity=1))[0]
    assert retry.assignment_id != first.assignment_id
    _finish(retry, "task-2.1-retry.txt")
    controller.reconcile(
        retry.assignment_id,
        retry.claim_token,
        agent_terminal="completed",
        session_id="session-a",
    )

    _finish(first, "task-2.1.txt")  # the late receipt finally arrives

    with pytest.raises(SessionControllerError, match="sealed evidence") as excinfo:
        controller.adopt(first.assignment_id)
    message = str(excinfo.value)
    assert first.assignment_id in message and retry.assignment_id in message
    assert _packet_payload(first)["receiptPath"] in message  # both evidence sets listed
    # Nothing changed: the human still owns the choice.
    assert controller.store.get_execution_attempt(first.assignment_id).state == (
        "abandoned"
    )
    assert controller.store.get_execution_attempt(retry.assignment_id).state == (
        "succeeded"
    )


def test_adopt_refuses_while_a_successor_is_in_flight(tmp_path):
    project = _project(tmp_path)
    controller = _controller(project)
    first = _dispatches(controller.actions("session-a", capacity=1))[0]
    controller.abandon(first.assignment_id, force=True)

    retry = _dispatches(controller.actions("session-a", capacity=1))[0]
    controller.bind_agent(retry.assignment_id, retry.claim_token, "agent-2")
    _finish(first, "task-2.1.txt")

    with pytest.raises(SessionControllerError, match="still in flight") as excinfo:
        controller.adopt(first.assignment_id)
    assert retry.assignment_id in str(excinfo.value)
    assert controller.store.get_execution_attempt(first.assignment_id).state == (
        "abandoned"
    )


def test_adopt_requires_an_abandoned_attempt_with_a_receipt(tmp_path):
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]

    with pytest.raises(SessionControllerError, match="only abandoned"):
        controller.adopt(action.assignment_id)  # still spawned/leased

    controller.abandon(action.assignment_id, force=True)  # no receipt yet

    with pytest.raises(SessionControllerError, match="no sealed receipt"):
        controller.adopt(action.assignment_id)
    assert controller.store.get_execution_attempt(action.assignment_id).state == (
        "abandoned"
    )


def test_adopt_rejects_a_tampered_receipt_and_stays_abandoned(tmp_path):
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    _finish(action, "task-2.1.txt")
    controller.abandon(action.assignment_id, force=True)

    receipt_path = Path(_packet_payload(action)["receiptPath"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["commit_sha"] = "0" * 40
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(SessionControllerError, match="stays abandoned"):
        controller.adopt(action.assignment_id)
    assert controller.store.get_execution_attempt(action.assignment_id).state == (
        "abandoned"
    )
    assert action.ticket_num in controller.store.latest_completion()["missing_required_tickets"]


def test_adopt_surviving_merge_conflict_parks_in_blocked_review(tmp_path):
    """A revived receipt that hits a real conflict follows the normal
    resolve_merge path instead of being force-merged."""
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    packet = _packet_payload(action)
    integration = Path(packet["integrationPath"])
    (integration / "base.txt").write_text("integration\n", encoding="utf-8")
    _git(integration, "add", "-A")
    _git(integration, "commit", "-m", "integration change")
    (Path(action.cwd) / "base.txt").write_text("assignment\n", encoding="utf-8")
    _git(Path(action.cwd), "add", "-A")
    _git(Path(action.cwd), "commit", "-m", "assignment change")
    seal_receipt(action.packet_path)
    controller.abandon(action.assignment_id, force=True)

    result = controller.adopt(action.assignment_id)

    assert result["state"] == "blocked-review"
    assert "resolve_merge" in result["follow_up"]
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "blocked-review"
    assert attempt.failure_code == "merge-conflict"


