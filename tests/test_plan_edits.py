"""Live plan edits: what baton does when tasks.md changes under a running
attempt, and which checkbox shapes are tickets at all.

Before 0.5.0 a task could be redefined (or removed) while its attempt was in
flight and the old work was integrated and checked off against the new text;
and a top-level checkbox without an id vanished without a word.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from baton_helpers import (  # noqa: E402
    action_of,
    controller_for,
    dispatches,
    git,
    integration_of,
    project_with,
    tasks_md,
    work,
)
import ticket_board  # noqa: E402
from _ticket import TasksMarkdownError, parse_tasks_md  # noqa: E402

ORIGINAL = "build the ORIGINAL feature"
REDEFINED = "build a COMPLETELY DIFFERENT feature"


# --- which checkboxes are tickets ----------------------------------------------


def _parse(tmp_path, body: str):
    path = tmp_path / "tasks.md"
    path.write_text(body, encoding="utf-8")
    return parse_tasks_md(path)


def test_a_top_level_checkbox_without_an_id_is_an_error(tmp_path):
    body = (
        "## 1. A\n\n- [ ] 1.1 real task\n- [ ] write the docs\n"
        "- [ ] 1.2 another real task\n"
    )
    with pytest.raises(TasksMarkdownError, match="line 4 .*no task id.*write the docs"):
        _parse(tmp_path, body)


@pytest.mark.parametrize("line", ["- [x] done but unnumbered", "* [ ] note"])
def test_every_unnumbered_top_level_checkbox_shape_is_an_error(tmp_path, line):
    with pytest.raises(TasksMarkdownError, match="no task id"):
        _parse(tmp_path, f"## 1. A\n\n- [ ] 1.1 real\n{line}\n")


def test_an_indented_checkbox_that_carries_an_id_is_an_error(tmp_path):
    body = "## 1. A\n\n- [ ] 1.1 real task\n  - [ ] 1.2 looks like a task\n"
    with pytest.raises(TasksMarkdownError, match="task 1.2 on line 4 .*indented"):
        _parse(tmp_path, body)


def test_nested_sub_steps_without_ids_are_neither_tickets_nor_errors(tmp_path):
    """The common OpenSpec shape: verification steps nested under a task."""
    body = (
        "## 1. A\n\n"
        "- [ ] 1.1 Implement the thing\n"
        "  - [ ] verify: it works\n"
        "    - [ ] second sub-step\n"
        "- [ ] 1.2 next\n"
    )
    assert [ticket.task_id for ticket in _parse(tmp_path, body)] == ["1.1", "1.2"]


def test_unnumbered_checkboxes_before_any_section_are_still_ignored(tmp_path):
    body = "- [ ] a preamble note\n\n## 1. A\n\n- [ ] 1.1 real\n"
    assert [ticket.task_id for ticket in _parse(tmp_path, body)] == ["1.1"]


def test_the_board_names_a_change_whose_checkbox_has_no_id(tmp_path):
    project = project_with(tmp_path, tasks="## 1. A\n\n- [ ] 1.1 ok\n- [ ] no id\n")

    board = ticket_board.scan_board(project, "feature")

    assert "no task id" in board["error"]


# --- the ticket text changed under a running attempt ---------------------------


def _tasks(project) -> Path:
    return project / "openspec" / "changes" / "feature" / "tasks.md"


def _swap(project, old: str, new: str) -> None:
    _tasks(project).write_text(_tasks(project).read_text().replace(old, new))


def test_an_in_flight_ticket_that_was_redefined_is_parked_for_a_decision(tmp_path):
    project = project_with(tmp_path, {"A": [ORIGINAL, "unrelated work"]}, ignore_dsh=True)
    controller = controller_for(project)
    batch = controller.actions("s1", capacity=2)
    original = next(a for a in dispatches(batch) if a.ticket_num == 101)
    controller.bind_agent(original.assignment_id, original.claim_token, "agent-1")
    _swap(project, ORIGINAL, REDEFINED)

    parked = controller.actions("s1")

    block = action_of(parked, "blocked")
    assert block.code == "ticket-redefined"
    assert block.assignment_id == original.assignment_id
    assert block.agent_id == "agent-1"  # the subagent is still on the old text
    assert repr(ORIGINAL) in block.reason and repr(REDEFINED) in block.reason
    assert "abandon" in block.reason
    attempt = controller.store.get_execution_attempt(original.assignment_id)
    assert attempt.state == "blocked-review"
    assert attempt.failure_code == "ticket-redefined"
    assert dispatches(parked) == [] or all(
        a.ticket_num == 102 for a in dispatches(parked)
    )

    # Restoring the text lets the attempt resume where it stood.
    _swap(project, REDEFINED, ORIGINAL)
    controller.actions("s1")
    attempt = controller.store.get_execution_attempt(original.assignment_id)
    assert attempt.state == "spawned"
    assert attempt.failure_code == ""

    # Keeping the new text retires the old work and re-claims under the new
    # definition.
    _swap(project, ORIGINAL, REDEFINED)
    controller.actions("s1")
    controller.abandon(original.assignment_id)
    fresh = controller.actions("s1")
    redefined = next(a for a in dispatches(fresh) if a.ticket_num == 101)
    assert redefined.assignment_id != original.assignment_id
    assert redefined.ticket_title == REDEFINED


def test_a_receipt_sealed_under_the_old_text_is_not_integrated(tmp_path):
    project = project_with(tmp_path, {"A": [ORIGINAL]}, ignore_dsh=True)
    controller = controller_for(project)
    action = dispatches(controller.actions("s1"))[0]
    _swap(project, ORIGINAL, REDEFINED)
    work(action, {"original.txt": "built the ORIGINAL feature\n"})
    integration = integration_of(action)
    before = git(integration, "rev-parse", "HEAD")

    batch = controller.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="completed",
        session_id="s1",
    )

    block = action_of(batch, "blocked")
    assert block.code == "ticket-redefined"
    assert block.assignment_id == action.assignment_id
    attempt = controller.store.get_execution_attempt(action.assignment_id)
    assert attempt.state == "blocked-review"
    assert attempt.failure_code == "ticket-redefined-after-seal"
    assert git(integration, "rev-parse", "HEAD") == before
    assert not (integration / "original.txt").exists()
    assert controller.store.latest_completion()["missing_required_tickets"] == [101]
    assert "- [x]" not in _tasks(project).read_text()

    # The operator restores the text: the sealed work is exactly it again.
    _swap(project, REDEFINED, ORIGINAL)
    landed = controller.actions("s1")

    assert controller.store.get_execution_attempt(action.assignment_id).state == "succeeded"
    assert landed.state == "complete"


def test_an_in_flight_ticket_that_left_the_plan_is_parked(tmp_path):
    project = project_with(tmp_path, {"A": ["first", ORIGINAL]}, ignore_dsh=True)
    controller = controller_for(project)
    batch = controller.actions("s1", capacity=2)
    target = next(a for a in dispatches(batch) if a.ticket_num == 102)
    _tasks(project).write_text(tasks_md({"A": ["first"]}))

    parked = controller.actions("s1")

    block = action_of(parked, "blocked")
    assert block.code == "ticket-removed"
    assert block.assignment_id == target.assignment_id
    assert "removed from tasks.md" in block.reason
    assert controller.store.get_execution_attempt(target.assignment_id).state == (
        "blocked-review"
    )
