"""Deterministic integration of assignment branches into a Baton feature branch."""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from orchestration.worktree import WorktreeError, branch_is_merged

_LOCK_TIMEOUT_SECONDS = 120.0
_LOCK_STALE_SECONDS = 3600


def _git(path: Path, args: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"git {' '.join(args)} failed: {exc}") from exc


def _lock_owner_alive(lock_path: Path) -> bool:
    """Best-effort liveness check so a crashed merge cannot wedge the repo."""
    try:
        pid = int(lock_path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return True
    if os.name == "nt" or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def _integration_lock(path: Path) -> Iterator[None]:
    """Serialize merges into the shared integration worktree across processes.

    Git's own ``index.lock`` makes concurrent merges fail rather than corrupt,
    but a retry loop here turns a parallel reconcile into a wait. The lock file
    lives in the repository's common git dir (shared by linked worktrees) and
    is removed on every exit path.
    """
    common = _git(path, ["rev-parse", "--git-common-dir"])
    if common.returncode != 0:
        raise WorktreeError(common.stderr.strip() or "cannot resolve git common dir")
    git_dir = Path(common.stdout.strip())
    if not git_dir.is_absolute():
        git_dir = (path / git_dir).resolve()
    lock_path = git_dir / "baton-integration.lock"
    deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > _LOCK_STALE_SECONDS or not _lock_owner_alive(lock_path):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise WorktreeError(f"integration lock busy: {lock_path}")
            time.sleep(0.05)
            continue
        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def integrate_assignment(
    integration_path: str | Path,
    assignment_branch: str,
) -> dict[str, Any]:
    """Merge ``assignment_branch`` and preserve conflicts for a repair action."""
    path = Path(integration_path).resolve()
    with _integration_lock(path):
        return _integrate_locked(path, assignment_branch)


def _integrate_locked(path: Path, assignment_branch: str) -> dict[str, Any]:
    if branch_is_merged(path, assignment_branch, "HEAD"):
        head = _git(path, ["rev-parse", "HEAD"])
        return {
            "ok": True,
            "already_merged": True,
            "integration_sha": head.stdout.strip(),
            "conflicts": [],
        }

    result = _git(path, ["merge", "--no-ff", "--no-edit", assignment_branch])
    if result.returncode == 0:
        head = _git(path, ["rev-parse", "HEAD"])
        if head.returncode != 0:
            raise WorktreeError(head.stderr.strip() or "cannot read integration HEAD")
        return {
            "ok": True,
            "already_merged": False,
            "integration_sha": head.stdout.strip(),
            "conflicts": [],
        }

    conflicts = _git(path, ["diff", "--name-only", "--diff-filter=U"])
    conflict_files = [
        line.strip() for line in conflicts.stdout.splitlines() if line.strip()
    ]
    if conflict_files:
        return {
            "ok": False,
            "already_merged": False,
            "integration_sha": "",
            "conflicts": conflict_files,
            "error": "merge-conflict",
        }
    raise WorktreeError(
        result.stderr.strip() or result.stdout.strip() or "git merge failed"
    )


__all__ = ["integrate_assignment"]
