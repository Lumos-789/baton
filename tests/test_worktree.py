"""test_worktree.py — git worktree workspace-isolation tests.

Covers:
- the integration worktree is created without switching the main workspace
- assignment worktrees are isolated from each other and idempotent
- the branch helpers report merge status without discarding anything
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from orchestration.worktree import (  # noqa: E402
    assignment_branch,
    assignment_worktree_path,
    branch_is_merged,
    integration_branch,
    integration_worktree_path,
    prepare_assignment_worktree,
    prepare_integration_worktree,
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


def test_branch_merge_helper(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    _init_repo(project)
    integration = prepare_integration_worktree(project, "feat")
    assignment = prepare_assignment_worktree(project, "feat", 2, 1)
    _commit(assignment.path, "ticket.txt", "ticket")

    assert not branch_is_merged(
        integration.path, assignment.branch, integration.branch
    )
