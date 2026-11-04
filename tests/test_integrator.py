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
    assert result["error"] == "merge-conflict"
    assert result["conflicts"] == ["base.txt"]
    assert (integration.path / ".git").exists()


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
