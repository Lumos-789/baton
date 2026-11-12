"""Assignment branch integration without destructive conflict cleanup."""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from orchestration.integrator import integrate_assignment  # noqa: E402
from orchestration.worktree import (  # noqa: E402
    prepare_assignment_worktree,
    prepare_integration_worktree,
)


def _run(path: Path, args: list[str]) -> None:
    subprocess.run(["git", *args], cwd=path, capture_output=True, check=True)


def _init(path: Path) -> None:
    _run(path, ["init", "-b", "main"])
    _run(path, ["config", "user.email", "t@t.t"])
    _run(path, ["config", "user.name", "test"])
    (path / "base.txt").write_text("base\n")
    _run(path, ["add", "-A"])
    _run(path, ["commit", "-m", "init"])


def test_integration_merge_is_idempotent(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _init(project)
    integration = prepare_integration_worktree(project, "feat")
    assignment = prepare_assignment_worktree(project, "feat", 1, 1)
    (assignment.path / "ticket.txt").write_text("done\n")
    _run(assignment.path, ["add", "-A"])
    _run(assignment.path, ["commit", "-m", "ticket"])

    first = integrate_assignment(integration.path, assignment.branch)
    second = integrate_assignment(integration.path, assignment.branch)

    assert first["ok"] and not first["already_merged"]
    assert second["ok"] and second["already_merged"]
    assert first["integration_sha"] == second["integration_sha"]


def test_merge_conflict_is_preserved_for_resolve_action(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _init(project)
    integration = prepare_integration_worktree(project, "feat")
    assignment = prepare_assignment_worktree(project, "feat", 1, 1)

    (integration.path / "base.txt").write_text("integration\n")
    _run(integration.path, ["add", "-A"])
    _run(integration.path, ["commit", "-m", "integration change"])
    (assignment.path / "base.txt").write_text("assignment\n")
    _run(assignment.path, ["add", "-A"])
    _run(assignment.path, ["commit", "-m", "assignment change"])

    result = integrate_assignment(integration.path, assignment.branch)

    assert not result["ok"]
    assert result["reason"] == "conflict"
    assert result["conflicts"] == ["base.txt"]
    assert (integration.path / ".git").exists()


def test_pending_conflict_is_not_attributed_to_another_branch(tmp_path):
    """A preserved conflict must not be reported as the *other* branch's.

    Git refuses a second merge while ``MERGE_HEAD`` exists and its unmerged
    path list belongs to the earlier merge; attributing it to the next
    assignment would hand two solvers the same worktree and mark a clean
    branch as conflicted.
    """
    project = tmp_path / "project"
    project.mkdir()
    _init(project)
    integration = prepare_integration_worktree(project, "feat")
    conflicting = prepare_assignment_worktree(project, "feat", 1, 1)
    clean = prepare_assignment_worktree(project, "feat", 2, 1)

    (integration.path / "base.txt").write_text("integration\n")
    _run(integration.path, ["add", "-A"])
    _run(integration.path, ["commit", "-m", "integration change"])
    (conflicting.path / "base.txt").write_text("assignment 1\n")
    _run(conflicting.path, ["add", "-A"])
    _run(conflicting.path, ["commit", "-m", "assignment 1"])
    (clean.path / "only-2.txt").write_text("assignment 2\n")
    _run(clean.path, ["add", "-A"])
    _run(clean.path, ["commit", "-m", "assignment 2"])

    first = integrate_assignment(integration.path, conflicting.branch)
    assert first["reason"] == "conflict"

    # The owning branch still sees its own preserved conflict...
    again = integrate_assignment(integration.path, conflicting.branch)
    assert again["reason"] == "conflict"

    # ...but the clean branch is told to wait, not that it conflicts.
    blocked = integrate_assignment(integration.path, clean.branch)
    assert not blocked["ok"]
    assert blocked["reason"] == "merge-in-progress"
    assert blocked["conflicts"] == ["base.txt"]
    assert not (integration.path / "only-2.txt").exists()

    # Once the real conflict is resolved, the clean branch merges normally.
    (integration.path / "base.txt").write_text("resolved\n")
    _run(integration.path, ["add", "-A"])
    _run(integration.path, ["commit", "--no-edit"])
    merged = integrate_assignment(integration.path, clean.branch)
    assert merged["ok"]
    assert (integration.path / "only-2.txt").exists()


def test_tracked_changes_in_the_worktree_block_the_merge(tmp_path):
    """Merging into a dirty shared worktree could destroy someone's work."""
    project = tmp_path / "project"
    project.mkdir()
    _init(project)
    integration = prepare_integration_worktree(project, "feat")
    assignment = prepare_assignment_worktree(project, "feat", 1, 1)
    (assignment.path / "base.txt").write_text("assignment\n")
    _run(assignment.path, ["add", "-A"])
    _run(assignment.path, ["commit", "-m", "assignment"])

    (integration.path / "base.txt").write_text("uncommitted local edit\n")

    result = integrate_assignment(integration.path, assignment.branch)

    assert not result["ok"]
    assert result["reason"] == "worktree-dirty"
    assert result["conflicts"] == ["M base.txt"]
    assert "assignment" not in (integration.path / "base.txt").read_text()


def test_untracked_files_do_not_block_the_merge(tmp_path):
    """Only tracked modifications are at risk; build artifacts are not."""
    project = tmp_path / "project"
    project.mkdir()
    _init(project)
    integration = prepare_integration_worktree(project, "feat")
    assignment = prepare_assignment_worktree(project, "feat", 1, 1)
    (assignment.path / "ticket.txt").write_text("done\n")
    _run(assignment.path, ["add", "-A"])
    _run(assignment.path, ["commit", "-m", "ticket"])

    (integration.path / "artifact.log").write_text("noise\n")

    result = integrate_assignment(integration.path, assignment.branch)

    assert result["ok"]
    assert (integration.path / "ticket.txt").exists()
    assert (integration.path / "artifact.log").exists()


def test_concurrent_integration_is_serialized(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _init(project)
    integration = prepare_integration_worktree(project, "feat")
    assignments = [
        prepare_assignment_worktree(project, "feat", num, 1) for num in range(1, 5)
    ]
    for num, assignment in enumerate(assignments, 1):
        (assignment.path / f"ticket-{num}.txt").write_text(f"{num}\n")
        _run(assignment.path, ["add", "-A"])
        _run(assignment.path, ["commit", "-m", f"ticket {num}"])

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda assignment: integrate_assignment(
                    integration.path, assignment.branch
                ),
                assignments,
            )
        )

    assert [result["ok"] for result in results] == [True] * 4
    merged = {path.name for path in integration.path.iterdir() if path.is_file()}
    assert {f"ticket-{num}.txt" for num in range(1, 5)} <= merged
    assert not (project / ".git" / "baton-integration.lock").exists()


def test_lock_left_by_dead_process_is_recovered(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _init(project)
    integration = prepare_integration_worktree(project, "feat")
    assignment = prepare_assignment_worktree(project, "feat", 1, 1)
    (assignment.path / "ticket.txt").write_text("done\n")
    _run(assignment.path, ["add", "-A"])
    _run(assignment.path, ["commit", "-m", "ticket"])

    lock = project / ".git" / "baton-integration.lock"
    lock.write_text("999999\n", encoding="ascii")

    result = integrate_assignment(integration.path, assignment.branch)

    assert result["ok"]
    assert not lock.exists()


# --- the branch advanced past the sealed commit ------------------------------


def _tip(path: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", ref],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_branch_moved_past_the_sealed_commit_is_refused(tmp_path):
    """Only the commit the receipt verified may be integrated — not the tip.

    A subagent (or a stray process) can commit to the assignment branch after
    sealing; those commits never ran the verification commands, so merging the
    branch would integrate unverified work.
    """
    project = tmp_path / "project"
    project.mkdir()
    _init(project)
    integration = prepare_integration_worktree(project, "feat")
    assignment = prepare_assignment_worktree(project, "feat", 1, 1)
    (assignment.path / "ticket.txt").write_text("done\n")
    _run(assignment.path, ["add", "-A"])
    _run(assignment.path, ["commit", "-m", "verified work"])
    sealed = _tip(assignment.path)

    (assignment.path / "UNVERIFIED.txt").write_text("after seal\n")
    _run(assignment.path, ["add", "-A"])
    _run(assignment.path, ["commit", "-m", "post-seal commit"])
    moved = _tip(assignment.path)

    before = _tip(integration.path)
    result = integrate_assignment(integration.path, assignment.branch, expected_sha=sealed)

    assert not result["ok"]
    assert result["reason"] == "branch-moved"
    assert result["branch_tip"] == moved
    assert result["expected_sha"] == sealed
    assert _tip(integration.path) == before
    assert not (integration.path / "ticket.txt").exists()
    assert not (integration.path / "UNVERIFIED.txt").exists()


def test_moved_branch_with_sealed_commit_already_landed_is_already_merged(tmp_path):
    """After a resolved conflict, the sealed commit may already be in HEAD.

    The branch tip moving later must not block finalizing the attempt: the
    verified commit is integrated, the extra commits simply stay unmerged.
    """
    project = tmp_path / "project"
    project.mkdir()
    _init(project)
    integration = prepare_integration_worktree(project, "feat")
    assignment = prepare_assignment_worktree(project, "feat", 1, 1)
    (assignment.path / "base.txt").write_text("assignment\n")
    _run(assignment.path, ["add", "-A"])
    _run(assignment.path, ["commit", "-m", "verified work"])
    sealed = _tip(assignment.path)

    # The verified commit lands (here: directly, e.g. via a resolved merge).
    _run(integration.path, ["merge", "--no-ff", "--no-edit", assignment.branch])
    landed = _tip(integration.path)

    (assignment.path / "extra.txt").write_text("post-seal commit\n")
    _run(assignment.path, ["add", "-A"])
    _run(assignment.path, ["commit", "-m", "post-seal commit"])

    result = integrate_assignment(integration.path, assignment.branch, expected_sha=sealed)

    assert result["ok"]
    assert result["already_merged"]
    assert result["integration_sha"] == landed
    assert not (integration.path / "extra.txt").exists()


def test_preserved_conflict_ownership_survives_branch_advance(tmp_path):
    """MERGE_HEAD, not the branch tip, decides who owns a preserved merge.

    The assignment branch may legitimately advance after its conflict was
    preserved; comparing MERGE_HEAD to the (moved) tip re-attributed the merge
    to nobody — the owning assignment silently lost its resolve_merge action
    forever.
    """
    project = tmp_path / "project"
    project.mkdir()
    _init(project)
    integration = prepare_integration_worktree(project, "feat")
    assignment = prepare_assignment_worktree(project, "feat", 1, 1)
    other = prepare_assignment_worktree(project, "feat", 2, 1)

    (integration.path / "base.txt").write_text("integration\n")
    _run(integration.path, ["add", "-A"])
    _run(integration.path, ["commit", "-m", "integration change"])
    (assignment.path / "base.txt").write_text("assignment\n")
    _run(assignment.path, ["add", "-A"])
    _run(assignment.path, ["commit", "-m", "conflicting assignment work"])
    sealed = _tip(assignment.path)

    conflict = integrate_assignment(integration.path, assignment.branch)
    assert conflict["reason"] == "conflict"

    (assignment.path / "extra.txt").write_text("branch advances\n")
    _run(assignment.path, ["add", "-A"])
    _run(assignment.path, ["commit", "-m", "post-conflict commit"])

    owned = integrate_assignment(
        integration.path, assignment.branch, expected_sha=sealed
    )
    assert owned["reason"] == "conflict"

    bystander = integrate_assignment(integration.path, other.branch)
    assert bystander["reason"] == "merge-in-progress"
