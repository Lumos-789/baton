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
from orchestration.relay import ABANDON_GUARD_FRACTION  # noqa: E402
from orchestration.session_controller import (  # noqa: E402
    SessionController,
    SessionControllerError,
)
from orchestration.worktree import WorktreeError  # noqa: E402

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


def _seal_all(assignments) -> None:
    for action in assignments:
        worktree = Path(action.cwd)
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", f"ticket {action.ticket_num}")
        seal_receipt(action.packet_path)


def test_conflict_is_attributed_only_to_the_conflicting_assignment(tmp_path):
    """One real conflict must not mark every pending assignment conflicted.

    ``git merge`` refuses to start while a preserved conflict exists, and its
    unmerged path list belongs to the earlier merge. Treating that as "this
    branch conflicts" produced a second ``resolve_merge`` action pointing at a
    conflict the branch never touched — and two solvers in one worktree.
    """
    project = _project(tmp_path)
    controller = _controller(project)
    dispatches = _dispatches(controller.actions("session-a", capacity=2))
    by_ticket = {action.ticket_num: action for action in dispatches}
    integration = Path(
        json.loads(Path(by_ticket[201].packet_path).read_text(encoding="utf-8"))[
            "integrationPath"
        ]
    )

    (integration / "base.txt").write_text("from integration\n", encoding="utf-8")
    _git(integration, "add", "-A")
    _git(integration, "commit", "-m", "integration side")
    (Path(by_ticket[201].cwd) / "base.txt").write_text("from 201\n", encoding="utf-8")
    (Path(by_ticket[202].cwd) / "only202.txt").write_text("202\n", encoding="utf-8")
    _seal_all(dispatches)

    batch = controller.actions("session-a", capacity=2)

    merges = [action for action in batch.actions if action.kind == "resolve_merge"]
    assert [action.assignment_id for action in merges] == [
        by_ticket[201].assignment_id
    ]
    assert controller.store.get_execution_attempt(
        by_ticket[202].assignment_id
    ).state not in {"blocked-review"}
    assert not (integration / "only202.txt").exists()

    # Resolving the single real conflict lets both assignments land.
    (integration / "base.txt").write_text("resolved\n", encoding="utf-8")
    _git(integration, "add", "-A")
    _git(integration, "commit", "--no-edit")

    controller.actions("session-a", capacity=2)

    assert (
        controller.store.get_execution_attempt(by_ticket[201].assignment_id).state
        == "succeeded"
    )
    assert controller.store.get_execution_attempt(
        by_ticket[202].assignment_id
    ).state == "succeeded"
    assert (integration / "only202.txt").exists()


def test_dirty_integration_worktree_blocks_without_wedging(tmp_path):
    """A half-finished resolve_merge leaves the worktree dirty.

    That used to raise ``WorktreeError`` from every ``actions`` call while the
    attempt sat in ``integrating`` until its two-hour lease expired.
    """
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    integration = Path(
        json.loads(Path(action.packet_path).read_text(encoding="utf-8"))[
            "integrationPath"
        ]
    )
    (Path(action.cwd) / "base.txt").write_text("assignment\n", encoding="utf-8")
    _seal_all([action])

    (integration / "base.txt").write_text("uncommitted local edit\n", encoding="utf-8")

    batch = controller.actions("session-a", capacity=1)

    blocked = [item for item in batch.actions if item.kind == "blocked"]
    assert batch.state == "blocked"
    assert [item.code for item in blocked] == ["integration-worktree-dirty"]
    assert "abandon" in blocked[0].reason
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state != "integrating"

    # Cleaning the worktree (as the message instructs) lets the merge proceed.
    (integration / "base.txt").write_text("base\n", encoding="utf-8")

    controller.actions("session-a", capacity=1)

    assert (
        controller.store.get_execution_attempt(action.assignment_id).state
        == "succeeded"
    )


def test_hard_integration_failure_is_reported_and_leaves_no_wedge(
    tmp_path, monkeypatch
):
    """A merge that fails without conflict files must not strand the attempt."""
    import orchestration.session_controller as session_controller

    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    (Path(action.cwd) / "ticket.txt").write_text("done\n", encoding="utf-8")
    _seal_all([action])

    def boom(*args, **kwargs):
        raise WorktreeError("fatal: refusing to merge unrelated histories")

    monkeypatch.setattr(session_controller, "integrate_assignment", boom)

    batch = controller.actions("session-a", capacity=1)

    blocked = [item for item in batch.actions if item.kind == "blocked"]
    assert [item.code for item in blocked] == ["integration-failed"]
    assert "abandon" in blocked[0].reason
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "blocked-review"
    assert attempt.failure_code.startswith("integration-failed")


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
    assert relay["suggestedAction"] == "wait"
    assert relay["receiptExists"] is False
    assert relay["elapsedSec"] is not None and relay["elapsedSec"] < 60
    assert relay["leaseRemainingSec"] > 7000
    assert relay["leaseElapsedFraction"] < ABANDON_GUARD_FRACTION
    assert relay["worktree"]["exists"] is True

    with pytest.raises(SessionControllerError, match="abandon refused") as excinfo:
        controller.abandon(action.assignment_id)
    refusal = json.loads(str(excinfo.value).split("abandon refused — ", 1)[1])
    assert refusal["state"] == "spawned"
    assert refusal["suggestedAction"] == "wait"
    assert refusal["leaseRemainingSec"] > 7000
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
    assert entry["relay"]["receiptExists"] is True
    assert entry["relay"]["suggestedAction"] == "adopt"


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

    assert result["adoptedFrom"] == "abandoned"
    assert result["state"] == "succeeded"
    assert result["followUp"] is None
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "succeeded"
    assert attempt.commit_sha
    assert attempt.ticket_num not in controller.store.latest_completion()[
        "missingRequiredTickets"
    ]
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
    assert action.ticket_num in controller.store.latest_completion()[
        "missingRequiredTickets"
    ]


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
    assert "resolve_merge" in result["followUp"]
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "blocked-review"
    assert attempt.failure_code == "merge-conflict"


# --- the branch advanced past the sealed commit (P1 pair) --------------------


def _advance_branch(action, marker: str) -> str:
    """Commit ``marker`` on the assignment branch after the seal; return tip."""
    worktree = Path(action.cwd)
    (worktree / marker).write_text("committed after seal, never verified\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", f"post-seal {marker}")
    return _git(worktree, "rev-parse", "HEAD")


def test_branch_advanced_after_seal_is_never_integrated(tmp_path):
    """A commit added after the seal never ran verification — refuse to merge.

    The receipt verified C1; the reconcile must not silently integrate the
    branch tip instead. The attempt parks in blocked-review with a named
    failure, the action carries the actual tip and the expected sha, and both
    exits (re-seal at the new tip / abandon) stay available.
    """
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    _finish(action, "task-2.1.txt")
    worktree = Path(action.cwd)
    sealed = _git(worktree, "rev-parse", "HEAD")
    integration = Path(_packet_payload(action)["integrationPath"])
    before = _git(integration, "rev-parse", "HEAD")
    moved = _advance_branch(action, "UNVERIFIED.txt")

    batch = controller.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="completed",
        session_id="session-a",
    )

    blocked = [item for item in batch.actions if item.kind == "blocked"]
    assert [item.code for item in blocked] == ["branch-moved-after-seal"]
    assert moved in blocked[0].reason and sealed in blocked[0].reason
    assert "abandon" in blocked[0].reason
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "blocked-review"
    assert attempt.failure_code == "branch-moved-after-seal"
    assert _git(integration, "rev-parse", "HEAD") == before
    assert not (integration / "UNVERIFIED.txt").exists()
    assert action.ticket_num in controller.store.latest_completion()[
        "missingRequiredTickets"
    ]

    # Not a one-shot message: the next actions() keeps surfacing the block.
    again = controller.actions("session-a", capacity=1)
    assert [item.code for item in again.actions if item.kind == "blocked"] == [
        "branch-moved-after-seal"
    ]

    # The abandon exit stays cheap (blocked-review is failure evidence, never
    # guarded) and the ticket is re-claimed fresh.
    controller.abandon(action.assignment_id)
    retried = _dispatches(controller.actions("session-a", capacity=1))
    assert [item.ticket_num for item in retried] == [action.ticket_num]
    assert retried[0].assignment_id != action.assignment_id


def test_branch_moved_after_seal_recovers_by_resealing_at_new_tip(tmp_path):
    """The documented recovery: seal again at the advanced tip.

    ``seal`` re-runs the verification commands on the new HEAD, so the new
    receipt legitimately verifies the tip and integration proceeds.
    """
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    _finish(action, "task-2.1.txt")
    _advance_branch(action, "UNVERIFIED.txt")
    integration = Path(_packet_payload(action)["integrationPath"])

    blocked = controller.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="completed",
        session_id="session-a",
    )
    assert [item.code for item in blocked.actions if item.kind == "blocked"] == [
        "branch-moved-after-seal"
    ]

    seal_receipt(action.packet_path)  # verification re-runs at the new tip
    batch = controller.actions("session-a", capacity=1)

    assert not any(
        item.kind == "blocked" and item.code == "branch-moved-after-seal"
        for item in batch.actions
    )
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "succeeded"
    assert (integration / "UNVERIFIED.txt").exists()
    assert attempt.ticket_num not in controller.store.latest_completion()[
        "missingRequiredTickets"
    ]


def test_conflicted_attempt_keeps_resolve_merge_after_branch_advance(tmp_path):
    """A preserved conflict stays owned by its assignment when the branch moves.

    Ownership was judged by MERGE_HEAD vs the *current* tip; one post-conflict
    commit turned the owner into a bystander and the attempt silently sat in
    blocked-review forever (only wait actions, lease expiry does not cover
    blocked-review). Ownership must track the receipt's verified commit.
    """
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    packet = _packet_payload(action)
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

    moved_tip = _advance_branch(action, "extra.txt")
    merge_head = _git(integration, "rev-parse", "MERGE_HEAD")
    assert moved_tip != merge_head

    # The advanced tip must not cost the assignment its resolve_merge action.
    for _ in range(2):
        batch = controller.actions("session-a", capacity=1)
        merges = [
            item
            for item in batch.actions
            if item.kind == "resolve_merge"
            and item.assignment_id == action.assignment_id
        ]
        assert len(merges) == 1

    # Resolve the preserved merge as the action instructs, then reconcile.
    (integration / "base.txt").write_text("resolved\n", encoding="utf-8")
    _git(integration, "add", "-A")
    _git(integration, "commit", "--no-edit")

    controller.actions("session-a", capacity=1)

    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "succeeded"
    assert attempt.commit_sha == merge_head
    assert not (integration / "extra.txt").exists()
    tasks_md = (
        project / "openspec" / "changes" / "feature" / "tasks.md"
    ).read_text(encoding="utf-8")
    assert "- [x] 2.1" in tasks_md
    assert action.ticket_num not in controller.store.latest_completion()[
        "missingRequiredTickets"
    ]
