"""A branch that advanced past its sealed commit, and parked attempts whose
receipt or packet is gone from disk."""

from __future__ import annotations

from pathlib import Path

from baton_helpers import (  # noqa: E402
    dispatches as _dispatches,
    fixture_controller as _controller,
    fixture_project as _project,
    finish as _finish,
    git as _git,
    packet_payload as _packet_payload,
    seal_all as _seal_all,
)
from orchestration.receipt import seal_receipt  # noqa: E402
from orchestration.worktree import WorktreeError  # noqa: E402


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
    assert action.ticket_num in controller.store.latest_completion()["missing_required_tickets"]

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
    assert attempt.ticket_num not in controller.store.latest_completion()["missing_required_tickets"]


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
    assert action.ticket_num not in controller.store.latest_completion()["missing_required_tickets"]


# --- blocked-review without a re-readable receipt dispatches by failure code


def _drop_receipt_and_packet(action) -> None:
    payload = _packet_payload(action)
    Path(payload["receiptPath"]).unlink()
    Path(action.packet_path).unlink()


def test_branch_moved_blocked_review_without_receipt_is_blocked_not_resolve_merge(
    tmp_path,
):
    """A branch-moved attempt whose receipt vanished from disk must not be
    handed a resolve_merge prompt: nothing was merged, so there is no
    preserved merge to resolve."""
    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    _finish(action, "task-2.1.txt")
    _advance_branch(action, "UNVERIFIED.txt")

    blocked = controller.reconcile(
        action.assignment_id,
        action.claim_token,
        agent_terminal="completed",
        session_id="session-a",
    )
    assert [item.code for item in blocked.actions if item.kind == "blocked"] == [
        "branch-moved-after-seal"
    ]

    _drop_receipt_and_packet(action)
    again = controller.actions("session-a", capacity=1)

    assert not any(item.kind == "resolve_merge" for item in again.actions)
    assert [item.code for item in again.actions if item.kind == "blocked"] == [
        "branch-moved-after-seal"
    ]


def test_integration_failed_blocked_review_without_receipt_is_blocked_not_resolve_merge(
    tmp_path, monkeypatch
):
    """Same rule for integration-failed: the failure merged nothing, so the
    re-dispatch is the named blocked action, never a resolve_merge."""
    import orchestration.receipt_flow as receipt_flow

    project = _project(tmp_path)
    controller = _controller(project)
    action = _dispatches(controller.actions("session-a", capacity=1))[0]
    (Path(action.cwd) / "ticket.txt").write_text("done\n", encoding="utf-8")
    _seal_all([action])

    def boom(*args, **kwargs):
        raise WorktreeError("fatal: refusing to merge unrelated histories")

    monkeypatch.setattr(receipt_flow, "integrate_assignment", boom)
    batch = controller.actions("session-a", capacity=1)
    assert [item.code for item in batch.actions if item.kind == "blocked"] == [
        "integration-failed"
    ]

    _drop_receipt_and_packet(action)
    again = controller.actions("session-a", capacity=1)

    assert not any(item.kind == "resolve_merge" for item in again.actions)
    assert [item.code for item in again.actions if item.kind == "blocked"] == [
        "integration-failed"
    ]
