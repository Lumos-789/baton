"""What baton leaves in the target project, and what it remembers between calls.

Before 0.5.0 the runtime directory showed up as untracked in the business
repo (and baton's own ``git add -A`` rule would commit it), a ``reconcile``
that omitted ``--capacity`` narrowed a parallel run, a re-run after the
integration branch was merged built on a stale tip, and the packet's base
was read before the receipts pass moved it.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import sqlite3
from pathlib import Path

import pytest

from baton_helpers import (  # noqa: E402
    ROOT,
    action_of,
    controller_for,
    dispatches,
    git,
    integration_of,
    packet_of,
    project_with,
    tasks_md,
    warning_of,
    work,
)
import ticket_board  # noqa: E402
import orchestration.session_model as session_model  # noqa: E402
from baton_helpers import SessionController  # noqa: E402
from orchestration.attempt_store import AttemptStoreMixin  # noqa: E402
from orchestration.model import TaskPacket  # noqa: E402
from orchestration.plan_model import PlanRevision, PlanTicket  # noqa: E402
from orchestration.plan_store import PlanStore, PlanStoreError  # noqa: E402
from orchestration.store_base import ACTIVE_ATTEMPT_STATES  # noqa: E402
from orchestration.store_schema import SCHEMA_SQL, SCHEMA_VERSION  # noqa: E402


def _tasks(project) -> Path:
    return project / "openspec" / "changes" / "feature" / "tasks.md"


def _land(controller, action):
    return controller.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="completed",
        session_id="s1",
    )


# --- the target repository stays clean -----------------------------------------


def test_runtime_state_stays_out_of_the_target_repo(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]})  # .dsh/ is NOT ignored here
    controller = controller_for(project)
    action = dispatches(controller.actions("s1"))[0]
    work(action, {"a.txt": "a\n"})
    _land(controller, action)

    status = git(project, "status", "--porcelain")

    assert ".dsh" not in status
    assert status == "M openspec/changes/feature/tasks.md"  # only the write-back
    exclude = project / ".git" / "info" / "exclude"
    assert exclude.read_text().count(".dsh/baton/") == 1

    controller.actions("s1")  # idempotent: one line, however many runs follow

    assert exclude.read_text().count(".dsh/baton/") == 1


def test_an_existing_gitignore_rule_is_left_alone(tmp_path):
    """``git init`` always ships an ``info/exclude``; the point is that baton
    does not append to it when the project already ignores ``.dsh/``."""
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    exclude = project / ".git" / "info" / "exclude"
    before = exclude.read_text()

    controller_for(project).actions("s1")

    assert exclude.read_text() == before


def test_boards_are_excluded_too(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]})

    ticket_board.save_board(project, "feature")

    assert ".dsh/baton/" in (project / ".git" / "info" / "exclude").read_text()


def test_completion_reports_the_write_back_and_the_deleted_branches(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project)
    action = dispatches(controller.actions("s1"))[0]
    branch = packet_of(action)["worktreeBranch"]
    work(action, {"a.txt": "a\n"})
    done = _land(controller, action)
    complete = action_of(done, "complete")

    assert complete.resolved_tickets == [101]
    assert complete.write_back["uncommitted"] is True
    assert complete.write_back["path"] == str(_tasks(project))
    assert "commit it together" in complete.write_back["next"]
    assert [entry["branch"] for entry in complete.branches_deleted] == [branch]
    assert git(project, "branch", "--list", "--format=%(refname:short)", "baton/*") == (
        "baton/feature"
    )
    assert not Path(action.cwd).exists()


# --- run settings are remembered -----------------------------------------------


def test_a_parallel_run_stays_parallel_when_reconcile_omits_capacity(tmp_path):
    project = project_with(tmp_path, {"A": ["a", "b", "c", "d"]}, ignore_dsh=True)
    controller = controller_for(project)
    batch = controller.actions("s1", capacity=3)
    first = {action.ticket_num: action for action in dispatches(batch)}
    assert sorted(first) == [101, 102, 103]
    work(first[101], {"f101.txt": "x\n"})

    refilled = _land(controller, first[101])

    assert 104 in {action.ticket_num for action in dispatches(refilled)}
    assert controller.store.run_setting("capacity") == 3
    # An explicit value re-declares it.
    controller.actions("s1", capacity=1)
    assert controller.store.run_setting("capacity") == 1


def test_the_base_branch_is_remembered_too(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    git(project, "branch", "develop")
    controller = controller_for(project)
    controller.actions("s1", base_branch="develop")

    assert controller.store.run_setting("base_branch") == "develop"
    assert git(project, "rev-parse", "baton/feature") == git(project, "rev-parse", "develop")
    # A later call without the flag keeps it, and a new controller inherits it.
    controller.actions("s1")
    assert controller.store.run_setting("base_branch") == "develop"
    assert SessionController(project, "feature")._run_settings(None, None)["base_branch"] == (
        "develop"
    )


# --- the integration branch versus its base ------------------------------------


def test_a_rerun_after_merging_fast_forwards_the_integration_branch(tmp_path):
    project = project_with(tmp_path, {"A": ["create a.txt"]}, ignore_dsh=True)
    controller = controller_for(project)
    action = dispatches(controller.actions("s1"))[0]
    work(action, {"a.txt": "a\n"})
    _land(controller, action)
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "plan write-back")
    git(project, "merge", "-q", "--no-edit", "baton/feature")
    (project / "new_on_main.txt").write_text("landed on main after the first run\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "main advances")
    _tasks(project).write_text(_tasks(project).read_text() + "- [ ] 1.2 create b.txt\n")

    rerun = controller.actions("s2")

    second = dispatches(rerun)[0]
    assert second.ticket_num == 102
    assert (Path(second.cwd) / "new_on_main.txt").exists()
    assert warning_of(rerun, "integration-fast-forwarded")
    assert packet_of(second)["baseSha"] == git(project, "rev-parse", "main")


def test_a_diverged_integration_branch_is_reported_not_touched(tmp_path):
    project = project_with(tmp_path, {"A": ["one", "two"]}, ignore_dsh=True)
    controller = controller_for(project)
    action = dispatches(controller.actions("s1"))[0]
    work(action, {"a.txt": "a\n"})
    _land(controller, action)  # 1.1 is on baton/feature, not merged to main
    (project / "main_only.txt").write_text("landed on main\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "main advances")
    integration = integration_of(action)
    before = git(integration, "rev-parse", "HEAD")

    batch = controller.actions("s1")

    behind = warning_of(batch, "integration-behind-base")
    assert behind["behind"] == 1
    assert behind["base_branch"] == "main"
    assert git(integration, "rev-parse", "HEAD") == before


def test_the_resume_path_records_the_base_the_worktree_actually_uses(tmp_path):
    project = project_with(tmp_path, {"A": ["create a.txt"], "B": ["create b.txt"]}, ignore_dsh=True)
    controller = controller_for(project)
    first = dispatches(controller.actions("s1"))[0]
    work(first, {"a.txt": "a\n"})
    # No reconcile: a restarted session calls actions(), which lands the receipt.
    second = dispatches(controller.actions("s1"))[0]
    assert second.ticket_num == 201
    work(second, {"b.txt": "b\n"})

    packet = packet_of(second)
    receipt = json.loads(Path(packet["receiptPath"]).read_text())

    assert packet["baseSha"] == git(Path(second.cwd), "rev-parse", "HEAD^1")
    assert receipt["changed_files"] == ["b.txt"]  # not a.txt from the ticket before it


# --- handing a run to another session ------------------------------------------


def test_an_unbound_claim_is_handed_to_the_session_that_picks_the_run_up(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    controller = controller_for(project)
    first = dispatches(controller.actions("old-session"))[0]

    picked_up = dispatches(controller.actions("new-session"))

    assert [item.assignment_id for item in picked_up] == [first.assignment_id]
    relay = controller.status()["attempts"][0]["relay"]
    assert relay["suggested_action"] == "dispatch"


def test_a_bound_claim_of_another_session_is_left_alone(tmp_path):
    project = project_with(tmp_path, {"A": ["one", "two"]}, ignore_dsh=True)
    controller = controller_for(project)
    bound = dispatches(controller.actions("old-session", capacity=2))[0]
    controller.bind_agent(bound.assignment_id, bound.claim_token, "agent-1")

    batch = controller.actions("new-session", capacity=2)

    dispatched = {item.assignment_id for item in dispatches(batch)}
    assert bound.assignment_id not in dispatched
    assert dispatched  # the other, unbound claim is re-issued


# --- the state machine and the schema agree ------------------------------------


def test_the_active_state_list_is_single_sourced():
    index_sql = SCHEMA_SQL[SCHEMA_SQL.index("idx_execution_attempts_one_active_ticket") :]
    index_sql = index_sql.split(";")[0]
    assert set(re.findall(r"'([a-z-]+)'", index_sql)) == ACTIVE_ATTEMPT_STATES
    assert ACTIVE_ATTEMPT_STATES == {"leased", "spawned", "integrating", "blocked-review"}


def test_a_database_from_an_older_baton_is_migrated_on_connect(tmp_path):
    project = project_with(tmp_path, {"A": ["one", "two", "three"]}, ignore_dsh=True)
    store = PlanStore(project, "feature")
    store.ensure_initial_plan()
    store.claim_ready_assignments(session_id="s", capacity=3, base_sha="abc")
    db = sqlite3.connect(store.db_path)
    db.execute("UPDATE execution_attempts SET state = 'running' WHERE ticket_num = 101")
    db.execute("UPDATE execution_attempts SET state = 'verifying' WHERE ticket_num = 102")
    db.execute("UPDATE execution_attempts SET state = 'failed' WHERE ticket_num = 103")
    db.execute("PRAGMA user_version = 0")
    db.commit()
    db.close()

    states = {a.ticket_num: a.state for a in store.list_execution_attempts()}

    assert states == {101: "spawned", 102: "integrating", 103: "abandoned"}
    db = sqlite3.connect(store.db_path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    db.close()


def test_a_database_from_a_newer_baton_fails_loud(tmp_path):
    project = project_with(tmp_path, {"A": ["one"]}, ignore_dsh=True)
    store = PlanStore(project, "feature")
    store.ensure_initial_plan()
    db = sqlite3.connect(store.db_path)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.commit()
    db.close()

    with pytest.raises(PlanStoreError) as refused:
        store.ensure_initial_plan()
    assert refused.value.code == "schema-too-new"


# --- one git wrapper, and the retired code stays retired -----------------------


def test_every_git_call_goes_through_run_git():
    offenders = []
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        if path.name == "git_run.py" or "__pycache__" in path.parts:
            continue
        if re.search(r'subprocess\.(run|Popen|call|check_output)\(\s*\[\s*"git"', path.read_text()):
            offenders.append(path.name)
    assert offenders == []


def test_retired_fields_and_parameters_stay_retired():
    fields = lambda cls: {field.name for field in dataclasses.fields(cls)}  # noqa: E731
    assert "artifacts" not in fields(TaskPacket)
    assert "required" not in fields(PlanTicket)
    assert "source_revision" not in fields(PlanTicket)
    assert "completion_state" not in fields(PlanRevision)
    assert "final_text" not in inspect.signature(SessionController.reconcile).parameters
    assert "release_lease" not in inspect.signature(
        AttemptStoreMixin.mark_execution_attempt_state
    ).parameters
    assert not hasattr(session_model, "action_from_dict")
