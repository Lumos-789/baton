"""test_worktree.py — git worktree workspace-isolation tests.

Covers:
- serial mode (worker_id='main') does not create a worktree and returns the main workspace
- parallel workers create isolated worktrees + branches
- idempotency: re-preparing the same worker returns the same slot
- workspace isolation: two worktrees cannot see each other's files
- list_worktrees enumeration
- remove_worktree cleanup (safe-delete by default; never deletes the main workspace)
- error case: a non-git repository raises WorktreeError
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from orchestration.worktree import (  # noqa: E402
    WorktreeError,
    WorktreeSlot,
    assignment_branch,
    assignment_worktree_path,
    branch_is_merged,
    integration_branch,
    integration_worktree_path,
    list_worktrees,
    prepare_assignment_worktree,
    prepare_integration_worktree,
    prepare_worktree,
    remove_worktree,
    worktree_branch,
    worktree_path,
    worktree_is_clean,
)


def _init_repo(path: Path) -> None:
    """Initialize a git repository at path with an initial commit."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, capture_output=True, check=False)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=path, capture_output=True)
    (path / "README.md").write_text("# test")
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True)


def _commit(path: Path, name: str, msg: str) -> None:
    (path / name).write_text(msg)
    subprocess.run(["git", "add", "-A"], cwd=path, capture_output=True)
    subprocess.run(["git", "commit", "-m", msg], cwd=path, capture_output=True)


def test_serial_mode_returns_main_workspace(tmp_path):
    """worker_id='main' → returns the main workspace without creating a worktree."""
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    slot = prepare_worktree(project, "feat-x", "main")
    assert slot.path == project.resolve()
    assert slot.worker_id == "main"


def test_worker_creates_isolated_worktree(tmp_path):
    """worker_id='w0' → creates an isolated worktree + branch."""
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    slot = prepare_worktree(project, "feat-x", "w0")
    assert slot.path == worktree_path(project, "w0")
    assert slot.path.exists()
    assert slot.branch == "baton/feat-x-w0"
    assert slot.path != project.resolve()


def test_multiple_workers_get_separate_worktrees(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    w0 = prepare_worktree(project, "feat", "w0")
    w1 = prepare_worktree(project, "feat", "w1")
    assert w0.path != w1.path
    assert w0.branch != w1.branch


def test_prepare_is_idempotent(tmp_path):
    """Re-preparing the same worker returns the same slot."""
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    s1 = prepare_worktree(project, "feat", "w0")
    s2 = prepare_worktree(project, "feat", "w0")
    assert s1.path == s2.path
    assert s1.branch == s2.branch


def test_worktrees_are_isolated(tmp_path):
    """Two worktree workspaces are fully isolated — commits do not trample each other."""
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    w0 = prepare_worktree(project, "feat", "w0")
    w1 = prepare_worktree(project, "feat", "w1")
    _commit(w0.path, "f0.txt", "w0 work")
    _commit(w1.path, "f1.txt", "w1 work")
    assert not (w1.path / "f0.txt").exists()
    assert not (w0.path / "f1.txt").exists()


def test_list_worktrees_includes_main_and_workers(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    prepare_worktree(project, "feat", "w0")
    prepare_worktree(project, "feat", "w1")
    slots = list_worktrees(project)
    ids = {s.worker_id for s in slots}
    assert "main" in ids
    assert "w0" in ids
    assert "w1" in ids


def test_remove_worktree_deletes_path(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    slot = prepare_worktree(project, "feat", "w0")
    assert remove_worktree(slot)
    assert not slot.path.exists()


def test_remove_main_workspace_is_noop(tmp_path):
    """The main workspace is never deleted."""
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    slot = prepare_worktree(project, "feat", "main")
    assert not remove_worktree(slot)
    assert project.exists()


def test_non_git_repo_raises(tmp_path):
    """A non-git repository → raises WorktreeError."""
    not_a_repo = tmp_path / "no-git"
    not_a_repo.mkdir()
    with pytest.raises(WorktreeError):
        prepare_worktree(not_a_repo, "feat", "w0")


def test_naming_helpers():
    """worktree_path / worktree_branch naming conventions."""
    project = Path("/abs/myproj")
    assert worktree_path(project, "main") == project
    assert worktree_path(project, "w0") == Path("/abs/myproj-w0")
    assert worktree_branch("feat", "main") == "baton/feat"
    assert worktree_branch("feat", "w0") == "baton/feat-w0"


def test_prepare_integration_worktree_never_switches_main_workspace(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    before = subprocess.run(
        ["git", "branch", "--show-current"], cwd=project, capture_output=True, text=True
    ).stdout.strip()

    slot = prepare_integration_worktree(project, "feat-x", base_branch="main")

    after = subprocess.run(
        ["git", "branch", "--show-current"], cwd=project, capture_output=True, text=True
    ).stdout.strip()
    assert before == after == "main"
    assert slot.path == integration_worktree_path(project, "feat-x")
    assert slot.branch == integration_branch("feat-x")


def test_assignment_worktrees_are_isolated_and_idempotent(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    prepare_integration_worktree(project, "feat")

    first = prepare_assignment_worktree(project, "feat", 2, 1)
    second = prepare_assignment_worktree(project, "feat", 4, 1)
    again = prepare_assignment_worktree(project, "feat", 2, 1)

    assert first.path == assignment_worktree_path(project, "feat", 2, 1)
    assert first.branch == assignment_branch("feat", 2, 1)
    assert first.path != second.path
    assert first.branch != second.branch
    assert again.path == first.path


def test_worktree_clean_and_branch_merge_helpers(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    integration = prepare_integration_worktree(project, "feat")
    assignment = prepare_assignment_worktree(project, "feat", 2, 1)
    _commit(assignment.path, "ticket.txt", "ticket")

    assert worktree_is_clean(assignment.path)
    assert not branch_is_merged(
        integration.path, assignment.branch, integration.branch
    )
