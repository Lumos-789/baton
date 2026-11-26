"""Regression tests for the findings of the independent audit of 0.5.0.

Each test pins a defect the audit reproduced against a real git repository
after the first implementation round.
"""

from __future__ import annotations

import json
import sqlite3
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
import orchestration.receipt as receipt_module  # noqa: E402
import orchestration.receipt_flow as receipt_flow  # noqa: E402
import session_control  # noqa: E402
import ticket_board  # noqa: E402
from _ticket import check_task, parse_tasks_md  # noqa: E402
from orchestration.plan_store import PlanStore, PlanStoreError  # noqa: E402
from orchestration.session_controller import SessionController  # noqa: E402
from orchestration.store_base import VERIFICATION_COMMANDS_SETTING  # noqa: E402
from orchestration.store_schema import SCHEMA_VERSION  # noqa: E402
from orchestration.worktree import branch_is_merged, exclude_runtime_dir  # noqa: E402
from orchestration.worktree import prepare_integration_worktree, cleanup_feature_worktrees  # noqa: E402

IMPORT_CHECK = 'python3 -c "import lib, app, extra"'
LIB_FILES = {
    "lib.py": "def old_name():\n    return 1\n",
    "app.py": "import lib\nprint(lib.old_name())\n",
    "extra.py": "# placeholder\n",
}
RENAMED = {
    "lib.py": "def new_name():\n    return 1\n",
    "app.py": "import lib\nprint(lib.new_name())\n",
}


def _breaking_trio(tmp_path):
    """1.1 renames the helper, 1.2 adds a caller of the old name, 1.3 is innocent."""
    project = project_with(
        tmp_path,
        {"Parallel": ["rename lib.old_name to lib.new_name", "call the helper", "add notes.md"]},
        files=LIB_FILES,
        ignore_dsh=True,
    )
    controller = controller_for(project, [IMPORT_CHECK])
    by_ticket = {a.ticket_num: a for a in dispatches(controller.actions("s1", capacity=3))}
    work(by_ticket[101], RENAMED)
    work(by_ticket[102], {"extra.py": "import lib\nprint(lib.old_name())\n"})
    work(by_ticket[103], {"notes.md": "independent of the rename\n"})
    return project, controller, by_ticket


def _land(controller, action):
    return controller.reconcile(
        action.assignment_id, action.claim_token, agent_terminal="completed", session_id="s1"
    )


def _red_base(controller, by_ticket):
    _land(controller, by_ticket[101])
    red = _land(controller, by_ticket[102])
    assert action_of(red, "repair_integration") is not None


def _state(controller, action):
    return controller.store.get_execution_attempt(action.assignment_id).state


def _tasks(project) -> Path:
    return project / "openspec" / "changes" / "feature" / "tasks.md"


# --- 1: the red-base hold must hold on every path ------------------------------


def test_reconcile_honors_the_red_base_hold(tmp_path):
    """``reconcile`` is the verb the skill prescribes for every completion
    notice; it used to integrate before the hold was computed."""
    project, controller, by_ticket = _breaking_trio(tmp_path)
    _red_base(controller, by_ticket)
    integration = integration_of(by_ticket[103])
    before = git(integration, "rev-parse", "HEAD")

    batch = _land(controller, by_ticket[103])

    assert _state(controller, by_ticket[103]) == "leased"
    assert not (integration / "notes.md").exists()
    assert git(integration, "rev-parse", "HEAD") == before
    assert warning_of(batch, "receipt-waiting-for-repair")["assignment_id"] == (
        by_ticket[103].assignment_id
    )
    repairs = [a for a in batch.actions if a.kind == "repair_integration"]
    assert [a.assignment_id for a in repairs] == [by_ticket[102].assignment_id]


def test_adopt_waits_until_a_red_base_is_repaired(tmp_path):
    project, controller, by_ticket = _breaking_trio(tmp_path)
    controller.abandon(by_ticket[103].assignment_id, force=True)  # a mistaken retire …
    _red_base(controller, by_ticket)  # … and the base turns red meanwhile
    integration = integration_of(by_ticket[103])

    adopted = controller.adopt(by_ticket[103].assignment_id)  # … then its receipt lands

    assert "red" in adopted["follow_up"]
    assert adopted["state"] == "integrating"
    assert not (integration / "notes.md").exists()

    (integration / "extra.py").write_text("import lib\nprint(lib.new_name())\n")
    git(integration, "add", "-A")
    git(integration, "commit", "-q", "-m", "repair: use the renamed helper")
    controller.actions("s1")
    assert _state(controller, by_ticket[102]) == "succeeded"
    assert _state(controller, by_ticket[103]) == "succeeded"


def test_the_hold_stays_when_the_parked_owner_receipt_is_rejected(tmp_path):
    project, controller, by_ticket = _breaking_trio(tmp_path)
    _red_base(controller, by_ticket)
    integration = integration_of(by_ticket[103])
    receipt_path = Path(packet_of(by_ticket[102])["receiptPath"])
    body = json.loads(receipt_path.read_text())
    body["changed_files"].append("smuggled.txt")
    receipt_path.write_text(json.dumps(body))

    batch = controller.actions("s1", capacity=3)

    assert _state(controller, by_ticket[102]) == "unknown"
    assert "receipt-invalid" in controller.store.get_execution_attempt(
        by_ticket[102].assignment_id
    ).failure_code
    # The base is still red with nobody owning the repair: it must keep holding.
    assert codes(batch) == ["integration-verification-failed"]
    assert _state(controller, by_ticket[103]) == "leased"
    assert not (integration / "notes.md").exists()


# --- 2: a ticket the budget locks out must stay visible ------------------------


def test_a_ticket_that_spent_its_budget_stays_visible(tmp_path):
    project = project_with(tmp_path, {"A": ["the one ticket", "second ticket"]}, ignore_dsh=True)
    controller = controller_for(project)
    last_abandon = None
    for _ in range(3):  # the recover's own advice: retire it and call actions
        action = next(a for a in dispatches(controller.actions("s1")) if a.ticket_num == 101)
        controller.reconcile(
            action.assignment_id, action.claim_token, agent_terminal="failed", session_id="s1"
        )
        last_abandon = controller.abandon(action.assignment_id)

    assert last_abandon["attempts_used"] == last_abandon["attempts_max"] == 3
    assert "reopen 1.1" in last_abandon["next"]

    batch = controller.actions("s1")

    # Before: the ticket vanished and the run waited forever with nothing to say.
    blocked = action_of(batch, "blocked")
    assert blocked.code == "retry-budget-spent"
    assert blocked.ticket_num == 101
    assert "reopen 1.1" in blocked.reason
    assert 101 not in {a.ticket_num for a in dispatches(batch)}

    controller.reopen("1.1")
    # Ticket 1.2's claim still holds the single slot; widen the window.
    assert 101 in {a.ticket_num for a in dispatches(controller.actions("s1", capacity=2))}


# --- 3 + 8: a verification that cannot run, and corrected commands -------------


def _hanging_verification(tmp_path, monkeypatch):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project)
    action = dispatches(controller.actions("s1"))[0]
    work(action, {"a.txt": "a\n"})  # sealed under the fast command
    # The suite now hangs: same value stored and (not) passed, so no override.
    controller.store.set_run_setting(VERIFICATION_COMMANDS_SETTING, ["sleep 5"])
    monkeypatch.setattr(receipt_module, "VERIFICATION_TIMEOUT_SECONDS", 1)
    return project, SessionController(project, "feature"), action


def test_a_verification_that_cannot_run_parks_the_owner_of_the_merge(tmp_path, monkeypatch):
    project, controller, action = _hanging_verification(tmp_path, monkeypatch)
    integration = integration_of(action)

    batch = controller.reconcile(
        action.assignment_id, action.claim_token, agent_terminal="completed", session_id="s1"
    )

    parked = controller.store.get_execution_attempt(action.assignment_id)
    assert parked.state == "blocked-review"
    assert parked.failure_code == "post-merge-verification-failed"
    assert branch_is_merged(integration, packet_of(action)["worktreeBranch"], "HEAD")
    repair = action_of(batch, "repair_integration")
    assert repair.assignment_id == action.assignment_id
    assert "could not be run" in repair.prompt and "timed out" in repair.prompt
    # Never "nothing was merged": that invited re-claiming merged work.
    assert not any(
        a.kind == "blocked" and "Nothing was merged" in a.reason for a in batch.actions
    )

    # While HEAD and the commands are unchanged the recorded outcome is reused.
    started = time.monotonic()
    again = controller.actions("s1")
    assert action_of(again, "repair_integration") is not None
    assert time.monotonic() - started < 0.9


def test_corrected_verification_commands_re_verify_the_same_head(tmp_path, monkeypatch):
    project, controller, action = _hanging_verification(tmp_path, monkeypatch)
    controller.reconcile(
        action.assignment_id, action.claim_token, agent_terminal="completed", session_id="s1"
    )

    # The operator fixes the environment and declares the commands again.
    fixed = SessionController(project, "feature", verification_commands=["test -f base.txt"])
    batch = fixed.actions("s1")

    assert action_of(batch, "repair_integration") is None
    assert controller.store.get_execution_attempt(action.assignment_id).state == "succeeded"


# --- 4: the project must be the repository --------------------------------------


def _nested_project(tmp_path):
    # The outer repo does not ignore .dsh/: baton has to write the pattern.
    outer = project_with(tmp_path, {"A": ["outer work"]}, name="outer")
    inner = outer / "pkg" / "proj"
    change = inner / "openspec" / "changes" / "feature"
    change.mkdir(parents=True)
    (change / "proposal.md").write_text("# Proposal\n")
    (change / "tasks.md").write_text("## 1. A\n\n- [ ] 1.1 inner work\n")
    return outer, inner


def test_a_project_inside_another_repository_is_refused(tmp_path):
    outer, inner = _nested_project(tmp_path)

    batch = SessionController(inner, "feature", verification_commands=["true"]).actions("s1")

    assert batch.state == "blocked"
    block = action_of(batch, "blocked")
    assert block.code == "integration-worktree"
    assert "not a git repository root" in block.reason
    # Nothing was created in the enclosing repository.
    assert git(outer, "branch", "--list", "baton/*") == ""
    assert len(git(outer, "worktree", "list").splitlines()) == 1
    assert not (outer.parent / "outer-baton-feature-integration").exists()


def test_the_exclusion_pattern_follows_a_nested_runtime_dir(tmp_path):
    outer, inner = _nested_project(tmp_path)
    (inner / ".dsh" / "baton").mkdir(parents=True)

    assert exclude_runtime_dir(inner) is True
    assert "pkg/proj/.dsh/baton/" in (outer / ".git" / "info" / "exclude").read_text()
    assert git(outer, "check-ignore", "-q", "--", "pkg/proj/.dsh/baton/queue.db", check=False) == ""


# --- 6: a failed base alignment is reported ------------------------------------


def test_a_failed_fast_forward_is_reported(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project)
    action = dispatches(controller.actions("s1"))[0]
    integration = integration_of(action)
    (project / "base.txt").write_text("changed on main\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "main advances")
    (integration / "base.txt").write_text("uncommitted local edit\n")  # blocks the ff
    before = git(integration, "rev-parse", "HEAD")

    batch = controller.actions("s1")

    failed = warning_of(batch, "integration-sync-failed")
    assert failed["base_branch"] == "main"
    assert git(integration, "rev-parse", "HEAD") == before


# --- 7: the schema gate comes before any write ---------------------------------


def test_a_newer_store_is_refused_before_anything_is_written(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    store = PlanStore(project, "feature")
    store.ensure_initial_plan()
    db = sqlite3.connect(store.db_path)
    db.execute("DROP INDEX idx_events_job")
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.commit()
    db.close()

    with pytest.raises(PlanStoreError) as refused:
        store.ensure_initial_plan()
    assert refused.value.code == "schema-too-new"
    db = sqlite3.connect(store.db_path)
    still_dropped = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' AND name = 'idx_events_job'"
    ).fetchone()[0]
    db.close()
    assert still_dropped == 0  # "idempotent" DDL did not recreate it


# --- 9: a resumed attempt can earn lease extensions again ----------------------


def test_a_resumed_attempt_can_earn_lease_extensions_again(tmp_path):
    project = project_with(tmp_path, {"A": ["build the ORIGINAL feature"]}, ignore_dsh=True)
    controller = controller_for(project, lease_seconds=3)
    action = dispatches(controller.actions("s1"))[0]
    controller.bind_agent(action.assignment_id, action.claim_token, "agent-1")
    _swap(project, "build the ORIGINAL feature", "build SOMETHING ELSE")
    controller.actions("s1")  # parked: ticket-redefined
    db = sqlite3.connect(controller.store.db_path)
    db.execute(
        "UPDATE execution_attempts SET created_at = '2000-01-01T00:00:00+00:00' "
        "WHERE assignment_id = ?",
        (action.assignment_id,),
    )  # as if the park had outlasted the hard cap
    db.commit()
    db.close()
    _swap(project, "build SOMETHING ELSE", "build the ORIGINAL feature")

    resumed = controller.actions("s1")
    assert resumed.state != "blocked"
    resumed_attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert resumed_attempt.created_at.startswith("20") and not resumed_attempt.created_at.startswith("2000")

    time.sleep(3.5)  # past the fresh 3 s lease: only activity keeps it alive now
    (Path(action.packet_path).parent / "progress").touch()
    controller.actions("s1")

    assert controller.store.get_execution_attempt(action.assignment_id).state == "spawned"
    assert len(controller.store.list_execution_attempts()) == 1


def _swap(project, old: str, new: str) -> None:
    _tasks(project).write_text(_tasks(project).read_text().replace(old, new))


# --- 10: a receipt another process already landed is not an error --------------


def test_a_receipt_another_process_already_landed_is_not_an_error(tmp_path, monkeypatch):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project)
    action = dispatches(controller.actions("s1"))[0]
    work(action, {"a.txt": "a\n"})
    real_integrate = receipt_flow.integrate_assignment

    def integrate_then_other_process_finalizes(*args, **kwargs):
        result = real_integrate(*args, **kwargs)
        receipt = json.loads(Path(packet_of(action)["receiptPath"]).read_text())
        from orchestration.receipt import verify_sealed_receipt

        envelope = verify_sealed_receipt(receipt, claim_token=action.claim_token)
        controller.store.finalize_execution_attempt(
            action.assignment_id, action.claim_token, envelope, result["integration_sha"]
        )
        return result

    monkeypatch.setattr(receipt_flow, "integrate_assignment", integrate_then_other_process_finalizes)

    batch = controller.reconcile(
        action.assignment_id, action.claim_token, agent_terminal="completed", session_id="s1"
    )

    assert batch.state == "complete"


# --- 11: a parked attempt says what happens next --------------------------------


def test_parked_attempts_say_what_happens_next(tmp_path):
    project, controller, by_ticket = _breaking_trio(tmp_path)
    _red_base(controller, by_ticket)

    relay = {row["assignment_id"]: row["relay"]["suggested_action"] for row in controller.status()["attempts"]}
    assert relay[by_ticket[102].assignment_id] == "repair-integration"

    project = project_with(
        tmp_path,
        {"Parallel": ["rewrite shared.txt", "rewrite shared.txt too"]},
        files={"shared.txt": "ok\n"},
        ignore_dsh=True,
        name="conflict",
    )
    controller = controller_for(project, ["grep -q ok shared.txt"])
    first, second = sorted(dispatches(controller.actions("s1", capacity=2)), key=lambda a: a.ticket_num)
    work(first, {"shared.txt": "ok from the first ticket\n"})
    work(second, {"shared.txt": "ok from the second ticket\n"})
    _land(controller, first)
    _land(controller, second)
    relay = {row["assignment_id"]: row["relay"]["suggested_action"] for row in controller.status()["attempts"]}
    assert relay[second.assignment_id] == "resolve-or-abandon"


# --- 12: which headings end a section, and a byte-order mark --------------------


def _parse(tmp_path, body: str | bytes):
    path = tmp_path / "tasks.md"
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body, encoding="utf-8")
    return [ticket.task_id for ticket in parse_tasks_md(path)]


def test_checkboxes_under_an_unnumbered_heading_are_not_tickets(tmp_path):
    body = "## 1. A\n\n- [ ] 1.1 real\n\n## Verification\n\n- [ ] All tests pass\n"
    assert _parse(tmp_path, body) == ["1.1"]


def test_a_deeper_heading_keeps_the_section(tmp_path):
    body = (
        "## 1. A\n\n### Notes\n\n- [ ] 1.1 real\n\n"
        "#### More\n\n- [ ] 1.2 also real\n"
    )
    assert _parse(tmp_path, body) == ["1.1", "1.2"]
    with pytest.raises(Exception, match="no task id"):
        _parse(tmp_path, "## 1. A\n\n### Notes\n\n- [ ] unnumbered\n")


def test_a_byte_order_mark_does_not_hide_the_first_section(tmp_path):
    body = "﻿# Tasks: demo\n\n## 1. Seed\n\n- [ ] 1.1 first\n\n## 2. Build\n\n- [ ] 2.1 second\n"
    assert _parse(tmp_path, body.encode("utf-8")) == ["1.1", "2.1"]


# --- 13: bad arguments die at the CLI boundary ---------------------------------


def test_an_empty_agent_id_dies_at_the_cli_boundary(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    with pytest.raises(SystemExit) as exc:
        session_control.main([str(project), "feature", "bind", "ASG-x", "tok", ""])
    assert exc.value.code == 2


# --- 14: worktrees of attempts still in flight ---------------------------------


def test_completion_keeps_the_worktree_of_an_attempt_in_flight(tmp_path):
    project = project_with(tmp_path, {"A": ["one", "two"]}, ignore_dsh=True)
    controller = controller_for(project)
    batch = controller.actions("s1", capacity=2)
    by_ticket = {a.ticket_num: a for a in dispatches(batch)}
    for action in by_ticket.values():
        controller.bind_agent(action.assignment_id, action.claim_token, "agent-1")
    work(by_ticket[101], {"a.txt": "a\n"})
    _land(controller, by_ticket[101])
    check_task(_tasks(project), 102)  # the last ticket is delivered by hand

    done = controller.actions("s1")

    complete = action_of(done, "complete")
    assert complete is not None
    assert Path(by_ticket[102].cwd).is_dir()  # its subagent is still working there
    assert any(
        entry["branch"] == packet_of(by_ticket[102])["worktreeBranch"]
        for entry in complete.worktrees_skipped
    )


def test_a_redispatched_claim_gets_its_worktree_back(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project)
    action = dispatches(controller.actions("s1"))[0]
    cleanup_feature_worktrees(project, "feature")  # a cleanup run mid-flight
    assert not Path(action.cwd).exists()

    again = dispatches(controller.actions("s1"))

    assert [a.assignment_id for a in again] == [action.assignment_id]
    assert Path(again[0].cwd).is_dir()


def test_the_board_still_excludes_its_output(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]})

    ticket_board.save_board(project, "feature")

    assert ".dsh/baton/" in (project / ".git" / "info" / "exclude").read_text()
