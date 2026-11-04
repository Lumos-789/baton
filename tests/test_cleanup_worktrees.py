"""test_cleanup_worktrees.py — tests for worktree.cleanup_feature_worktrees (the `cleanup` verb).

Locks the safety contract:
- removes only baton/<feature>-tic-* assignment worktrees by default
- integration worktree removed only with include_integration=True
- never touches the main working tree
- a dirty worktree is SKIPPED, never force-discarded (red line)
- strips the refs/heads/ prefix from porcelain branch lines

Locks the truthfulness contract:
- a feature with no baton worktree AND no baton/<feature>* branch (typo'd
  change-id) fails loud instead of an empty success
- a feature whose branch exists (it ran; branches outlive worktree removal)
  keeps the empty-but-successful answer
- a non-git directory fails loud instead of an empty success
- dry_run previews the matches and removes nothing
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from orchestration.worktree import (  # noqa: E402
    WorktreeError,
    cleanup_feature_worktrees as cleanup_worktrees,
)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], cwd=repo)
    _git(["config", "user.email", "t@t"], cwd=repo)
    _git(["config", "user.name", "t"], cwd=repo)
    (repo / "a.txt").write_text("a")
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-qm", "init"], cwd=repo)
    return repo


def _add_worktree(repo: Path, path: Path, branch: str) -> None:
    _git(["worktree", "add", "-b", branch, str(path), "main"], cwd=repo)


def test_removes_assignment_worktrees_default(tmp_path):
    repo = _make_repo(tmp_path)
    wt_a = tmp_path / "proj-baton-feat-tic-001-a01"
    wt_int = tmp_path / "proj-baton-feat-integration"
    _add_worktree(repo, wt_a, "baton/feat-tic-001-a01")
    _add_worktree(repo, wt_int, "baton/feat")

    res = cleanup_worktrees(str(repo), "feat", include_integration=False)

    removed_branches = {r["branch"] for r in res["removed"]}
    assert "baton/feat-tic-001-a01" in removed_branches
    assert not wt_a.exists()
    # integration worktree NOT removed without the flag
    assert wt_int.exists()
    assert res["integrationIncluded"] is False
    # main working tree untouched
    assert repo.exists()


def test_include_integration_removes_integration(tmp_path):
    repo = _make_repo(tmp_path)
    wt_int = tmp_path / "proj-baton-feat-integration"
    _add_worktree(repo, wt_int, "baton/feat")

    res = cleanup_worktrees(str(repo), "feat", include_integration=True)

    assert any(r["branch"] == "baton/feat" for r in res["removed"])
    assert not wt_int.exists()
    assert repo.exists()  # main working tree untouched


def test_dirty_worktree_skipped_not_discarded(tmp_path):
    repo = _make_repo(tmp_path)
    wt = tmp_path / "proj-baton-feat-tic-002-a01"
    _add_worktree(repo, wt, "baton/feat-tic-002-a01")
    # leave an uncommitted change in the worktree (dirty)
    (wt / "a.txt").write_text("dirty")
    subprocess.run(
        ["git", "add", "-A"], cwd=str(wt), check=True, capture_output=True
    )

    res = cleanup_worktrees(str(repo), "feat", include_integration=False)

    assert res["removed"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["branch"] == "baton/feat-tic-002-a01"
    # red line: the dirty worktree is still on disk, NOT discarded
    assert wt.exists()


def test_other_feature_worktrees_not_touched(tmp_path):
    repo = _make_repo(tmp_path)
    wt_other = tmp_path / "proj-baton-other-tic-001-a01"
    _add_worktree(repo, wt_other, "baton/other-tic-001-a01")
    # 'feat' ran (its integration branch exists, worktrees already cleaned):
    # empty result stays a success, not a no-trace error
    _git(["branch", "baton/feat"], cwd=repo)

    res = cleanup_worktrees(str(repo), "feat", include_integration=True)

    assert res["removed"] == []
    # a different feature's worktree is left alone
    assert wt_other.exists()


def test_typoed_feature_fails_loud_not_empty_success(tmp_path):
    repo = _make_repo(tmp_path)
    # a different feature really ran; the requested one left no trace
    _git(["branch", "baton/real-tic-001-a01"], cwd=repo)

    with pytest.raises(
        WorktreeError, match="no baton worktrees or branches found for 'typo'"
    ):
        cleanup_worktrees(str(repo), "typo")


def test_cleaned_feature_with_branch_keeps_empty_success(tmp_path):
    repo = _make_repo(tmp_path)
    # the run happened (integration branch); every worktree is already gone
    _git(["branch", "baton/feat"], cwd=repo)

    res = cleanup_worktrees(str(repo), "feat", include_integration=True)

    assert res == {
        "removed": [],
        "skipped": [],
        "integrationIncluded": True,
    }


def test_non_git_directory_fails_loud(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    with pytest.raises(WorktreeError, match="git worktree list failed"):
        cleanup_worktrees(str(plain), "feat")


def test_dry_run_previews_matches_and_removes_nothing(tmp_path):
    repo = _make_repo(tmp_path)
    wt_a = tmp_path / "proj-baton-feat-tic-001-a01"
    wt_int = tmp_path / "proj-baton-feat-integration"
    _add_worktree(repo, wt_a, "baton/feat-tic-001-a01")
    _add_worktree(repo, wt_int, "baton/feat")

    res = cleanup_worktrees(str(repo), "feat", dry_run=True)
    assert res["dryRun"] is True
    assert res["integrationIncluded"] is False
    assert [w["branch"] for w in res["wouldRemove"]] == ["baton/feat-tic-001-a01"]

    res_all = cleanup_worktrees(
        str(repo), "feat", include_integration=True, dry_run=True
    )
    assert sorted(w["branch"] for w in res_all["wouldRemove"]) == [
        "baton/feat",
        "baton/feat-tic-001-a01",
    ]
    # the preview removed nothing
    assert wt_a.exists() and wt_int.exists()

    # the real run after the preview still works
    res_real = cleanup_worktrees(str(repo), "feat")
    assert [r["branch"] for r in res_real["removed"]] == ["baton/feat-tic-001-a01"]


def test_dry_run_typoed_feature_also_fails_loud(tmp_path):
    repo = _make_repo(tmp_path)
    _git(["branch", "baton/feat"], cwd=repo)

    with pytest.raises(WorktreeError, match="no baton worktrees or branches"):
        cleanup_worktrees(str(repo), "typo", dry_run=True)
