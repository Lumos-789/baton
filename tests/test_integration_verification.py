"""The merged tree is what gets verified — not only each branch on its own.

Two tickets that are green alone can break each other once merged, and a
conflict resolution is never verified by the branches that produced it.
Before 0.5.0 the controller trusted the per-branch receipts and reported
``complete`` on a red integration branch; these tests pin the repair loop.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from baton_helpers import (  # noqa: E402
    action_of,
    codes,
    controller_for,
    dispatches,
    git,
    integration_of,
    packet_of,
    project_with,
    warning_codes,
    warning_of,
    work,
)
from orchestration.integrator import (
    IntegrationLockBusy,
    integrate_assignment,
    read_integration_verification,
)
from orchestration.worktree import (
    branch_is_merged,
    prepare_assignment_worktree,
    prepare_integration_worktree,
)
import orchestration.integrator as integrator  # noqa: E402
import orchestration.receipt_flow as receipt_flow  # noqa: E402

IMPORT_CHECK = 'python3 -c "import lib, app, extra"'
LIB_FILES = {
    "lib.py": "def old_name():\n    return 1\n",
    "app.py": "import lib\nprint(lib.old_name())\n",
    "extra.py": "# placeholder\n",
}


def _breaking_pair(tmp_path):
    """Ticket 1.1 renames the helper, ticket 1.2 adds a caller of the old name."""
    project = project_with(
        tmp_path,
        {
            "Parallel": [
                "rename lib.old_name to lib.new_name",
                "call the helper from extra.py",
            ],
            "Later": ["later work"],
        },
        files=LIB_FILES,
        ignore_dsh=True,
    )
    controller = controller_for(project, [IMPORT_CHECK])
    batch = controller.actions("s1", capacity=2)
    by_ticket = {action.ticket_num: action for action in dispatches(batch)}
    work(
        by_ticket[101],
        {
            "lib.py": "def new_name():\n    return 1\n",
            "app.py": "import lib\nprint(lib.new_name())\n",
        },
    )
    work(by_ticket[102], {"extra.py": "import lib\nprint(lib.old_name())\n"})
    return project, controller, by_ticket


def _land(controller, action):
    return controller.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="completed",
        session_id="s1",
    )


def test_two_green_branches_that_break_each_other_park_for_repair(tmp_path):
    project, controller, by_ticket = _breaking_pair(tmp_path)

    batch = _land(controller, by_ticket[101])

    # 1.1 lands on its own; 1.2 merges cleanly but the merged tree is red.
    assert controller.store.get_execution_attempt(by_ticket[101].assignment_id).state == "succeeded"
    parked = controller.store.get_execution_attempt(by_ticket[102].assignment_id)
    assert parked.state == "blocked-review"
    assert parked.failure_code == "post-merge-verification-failed"
    repair = action_of(batch, "repair_integration")
    assert repair.assignment_id == by_ticket[102].assignment_id
    assert repair.cwd == parked.integration_path
    assert repair.claim_token == by_ticket[102].claim_token
    assert IMPORT_CHECK in repair.prompt and "AttributeError" in repair.prompt
    assert batch.state == "blocked"
    # Nothing branches from the red base: the next section is not claimed.
    assert dispatches(batch) == []
    integration = integration_of(by_ticket[102])
    assert branch_is_merged(integration, packet_of(by_ticket[102])["worktreeBranch"], "HEAD")
    assert read_integration_verification(integration)["ok"] is False

    # A repair subagent fixes the merged tree where it stands.
    (integration / "extra.py").write_text("import lib\nprint(lib.new_name())\n")
    git(integration, "add", "-A")
    git(integration, "commit", "-q", "-m", "repair: use the renamed helper")
    after = _land(controller, by_ticket[102])

    assert controller.store.get_execution_attempt(by_ticket[102].assignment_id).state == "succeeded"
    assert [action.ticket_num for action in dispatches(after)] == [201]
    assert controller.store.latest_completion()["missing_required_tickets"] == [201]


def test_a_parked_repair_does_not_re_run_the_suite_on_every_call(tmp_path):
    """While HEAD is unchanged, the recorded red outcome is reused."""
    _, controller, by_ticket = _breaking_pair(tmp_path)
    _land(controller, by_ticket[101])
    _land(controller, by_ticket[102])
    integration = integration_of(by_ticket[102])
    first = read_integration_verification(integration)

    again = _land(controller, by_ticket[102])

    assert action_of(again, "repair_integration") is not None
    assert read_integration_verification(integration)["updated_at"] == first["updated_at"]


def test_a_hand_resolved_conflict_is_verified_before_the_ticket_lands(tmp_path):
    project = project_with(
        tmp_path,
        {"Parallel": ["rewrite shared.txt", "rewrite shared.txt too"]},
        files={"shared.txt": "ok\n"},
        ignore_dsh=True,
    )
    controller = controller_for(project, ["grep -q ok shared.txt"])
    batch = controller.actions("s1", capacity=2)
    first, second = sorted(dispatches(batch), key=lambda action: action.ticket_num)
    work(first, {"shared.txt": "ok from the first ticket\n"})
    work(second, {"shared.txt": "ok from the second ticket\n"})

    # Both receipts are on disk, so one reconcile lands 1.1 and hits the
    # conflict for 1.2.
    landed_first = _land(controller, first)
    assert controller.store.get_execution_attempt(first.assignment_id).state == "succeeded"
    assert action_of(landed_first, "resolve_merge").assignment_id == second.assignment_id

    # The resolver commits a merge that breaks the run's own check.
    integration = integration_of(second)
    (integration / "shared.txt").write_text("BROKEN - resolver messed up\n")
    git(integration, "add", "-A")
    git(integration, "commit", "-q", "--no-edit")
    still_red = controller.actions("s1")

    repair = action_of(still_red, "repair_integration")
    assert repair is not None
    assert "grep -q ok shared.txt exited 1" in repair.prompt
    assert controller.store.get_execution_attempt(second.assignment_id).state == "blocked-review"
    tasks_md = (project / "openspec" / "changes" / "feature" / "tasks.md").read_text()
    assert "- [x] 1.2" not in tasks_md

    (integration / "shared.txt").write_text("ok - resolved properly\n")
    git(integration, "add", "-A")
    git(integration, "commit", "-q", "-m", "repair the resolution")
    landed = _land(controller, second)

    assert controller.store.get_execution_attempt(second.assignment_id).state == "succeeded"
    assert landed.state == "complete"


def test_a_red_base_holds_other_receipts_and_claims(tmp_path):
    project = project_with(
        tmp_path,
        {"Parallel": ["one", "two", "add notes.md"]},
        files=LIB_FILES,
        ignore_dsh=True,
    )
    controller = controller_for(project, [IMPORT_CHECK])
    batch = controller.actions("s1", capacity=3)
    by_ticket = {action.ticket_num: action for action in dispatches(batch)}
    work(
        by_ticket[101],
        {
            "lib.py": "def new_name():\n    return 1\n",
            "app.py": "import lib\nprint(lib.new_name())\n",
        },
    )
    work(by_ticket[102], {"extra.py": "import lib\nprint(lib.old_name())\n"})
    work(by_ticket[103], {"notes.md": "independent of the rename\n"})

    held = controller.actions("s1", capacity=3)

    states = {
        attempt.ticket_num: attempt.state
        for attempt in controller.store.list_execution_attempts()
    }
    assert states == {101: "succeeded", 102: "blocked-review", 103: "leased"}
    waiting = warning_of(held, "receipt-waiting-for-repair")
    assert waiting["assignment_id"] == by_ticket[103].assignment_id
    integration = integration_of(by_ticket[103])
    assert not (integration / "notes.md").exists()

    # Retiring the parked attempt does not unblock the base by itself.
    controller.abandon(by_ticket[102].assignment_id)
    orphan = controller.actions("s1", capacity=3)

    assert codes(orphan) == ["integration-verification-failed"]
    assert "AttributeError" in action_of(orphan, "blocked").reason
    assert dispatches(orphan) == []
    assert not (integration / "notes.md").exists()
    assert controller.store.get_execution_attempt(by_ticket[103].assignment_id).state == "leased"

    # With HEAD moved by a fix, the base is re-verified and the run resumes.
    (integration / "extra.py").write_text("import lib\nprint(lib.new_name())\n")
    git(integration, "add", "-A")
    git(integration, "commit", "-q", "-m", "repair: use the renamed helper")
    resumed = controller.actions("s1", capacity=3)

    # The waiting receipt lands; the abandoned ticket is claimed afresh.
    assert controller.store.get_execution_attempt(by_ticket[103].assignment_id).state == "succeeded"
    assert [action.ticket_num for action in dispatches(resumed)] == [102]
    assert (integration / "notes.md").exists()


def test_one_tampered_receipt_does_not_hold_the_valid_one(tmp_path):
    project = project_with(tmp_path, {"Parallel": ["one", "two"]}, ignore_dsh=True)
    controller = controller_for(project)
    batch = controller.actions("s1", capacity=2)
    first, second = sorted(dispatches(batch), key=lambda action: action.ticket_num)
    work(first, {"a.txt": "a\n"})
    work(second, {"b.txt": "b\n"})
    receipt_path = Path(packet_of(first)["receiptPath"])
    body = json.loads(receipt_path.read_text())
    body["changed_files"].append("smuggled.txt")
    receipt_path.write_text(json.dumps(body))

    batch = controller.actions("s1", capacity=2)

    states = {
        attempt.ticket_num: attempt.state
        for attempt in controller.store.list_execution_attempts()
    }
    assert states == {101: "unknown", 102: "succeeded"}
    recover = action_of(batch, "recover")
    assert recover.assignment_id == first.assignment_id
    assert recover.reason == "receipt-invalid"
    assert "changed_files" in recover.detail
    assert batch.state == "blocked"


# --- integrator: the verification hook and its per-HEAD memory -----------------


def _plain_repo(tmp_path):
    project = tmp_path / "project"
    project.mkdir(parents=True)
    git(project, "init", "-q", "-b", "main")
    git(project, "config", "user.email", "t@t.t")
    git(project, "config", "user.name", "test")
    (project / "base.txt").write_text("base\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "init")
    return project


def _committed_assignment(project, marker: str):
    assignment = prepare_assignment_worktree(project, "feat", 1, 1)
    (assignment.path / marker).write_text("done\n")
    git(assignment.path, "add", "-A")
    git(assignment.path, "commit", "-q", "-m", "ticket")
    return assignment


def test_merge_without_a_verifier_keeps_the_old_contract(tmp_path):
    project = _plain_repo(tmp_path)
    integration = prepare_integration_worktree(project, "feat")
    assignment = _committed_assignment(project, "ticket.txt")

    result = integrate_assignment(integration.path, assignment.branch)

    assert result["ok"] and not result["already_merged"]
    assert read_integration_verification(integration.path) is None


def test_the_merged_tree_is_verified_and_a_failure_is_recorded(tmp_path):
    project = _plain_repo(tmp_path)
    integration = prepare_integration_worktree(project, "feat")
    assignment = _committed_assignment(project, "ticket.txt")
    red = [{"command": "pytest", "exitCode": 1, "outputSha256": "x", "outputTail": "boom"}]

    result = integrate_assignment(integration.path, assignment.branch, verify=lambda: red)

    assert not result["ok"]
    assert result["reason"] == "verification-failed"
    assert result["verification"] == red
    # The merge is kept — nothing is reset — and the red outcome stays on record.
    assert branch_is_merged(integration.path, assignment.branch, "HEAD")
    assert result["integration_sha"] == git(integration.path, "rev-parse", "HEAD")
    memo = read_integration_verification(integration.path)
    assert memo["ok"] is False and memo["verification"] == red


def test_the_same_head_is_verified_once(tmp_path):
    project = _plain_repo(tmp_path)
    integration = prepare_integration_worktree(project, "feat")
    assignment = _committed_assignment(project, "ticket.txt")
    calls = []

    def verify():
        calls.append(1)
        return [{"command": "pytest", "exitCode": 0, "outputSha256": "x", "outputTail": ""}]

    first = integrate_assignment(integration.path, assignment.branch, verify=verify)
    again = integrate_assignment(integration.path, assignment.branch, verify=verify)

    assert first["ok"] and again["ok"] and again["already_merged"]
    assert len(calls) == 1


def test_a_verifier_that_cannot_run_leaves_the_head_marked_unverified(tmp_path):
    project = _plain_repo(tmp_path)
    integration = prepare_integration_worktree(project, "feat")
    assignment = _committed_assignment(project, "ticket.txt")

    def explode():
        raise RuntimeError("verification timed out")

    with pytest.raises(RuntimeError, match="timed out"):
        integrate_assignment(integration.path, assignment.branch, verify=explode)

    memo = read_integration_verification(integration.path)
    assert memo["ok"] is False and "verification timed out" in memo["error"]


def test_a_busy_integration_lock_defers_the_receipt_instead_of_failing_it(tmp_path, monkeypatch):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project)
    action = dispatches(controller.actions("s1"))[0]
    work(action, {"a.txt": "a\n"})

    def busy(*args, **kwargs):
        raise IntegrationLockBusy("integration lock busy: held by another process")

    monkeypatch.setattr(receipt_flow, "integrate_assignment", busy)
    batch = controller.actions("s1")

    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state != "blocked-review"
    assert attempt.failure_code == ""
    assert "integration-lock-busy" in warning_codes(batch)

    # The real lock: a live owner makes a waiter give up with the same signal
    # instead of blocking for the whole timeout.
    plain = _plain_repo(tmp_path / "lock")
    integration = prepare_integration_worktree(plain, "feat")
    assignment = _committed_assignment(plain, "ticket.txt")
    lock = plain / ".git" / "baton-integration.lock"
    lock.write_text(f"{os.getpid()}\n", encoding="ascii")
    monkeypatch.setattr(integrator, "_LOCK_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(IntegrationLockBusy):
        integrate_assignment(integration.path, assignment.branch)
    assert lock.exists()  # a live owner's lock is never stolen


def test_a_stale_unknown_owner_lock_is_still_recovered_by_age(tmp_path, monkeypatch):
    project = _plain_repo(tmp_path)
    integration = prepare_integration_worktree(project, "feat")
    assignment = _committed_assignment(project, "ticket.txt")
    lock = project / ".git" / "baton-integration.lock"
    lock.write_text("not a pid\n", encoding="ascii")
    stale = time.time() - 10_000
    os.utime(lock, (stale, stale))
    monkeypatch.setattr(integrator, "_LOCK_TIMEOUT_SECONDS", 0.05)

    assert integrate_assignment(integration.path, assignment.branch)["ok"]
    assert not lock.exists()

