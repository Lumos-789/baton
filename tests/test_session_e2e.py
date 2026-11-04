"""End-to-end session scheduling over the OpenSpec change fixture.

Fixture (tests/fixtures/sample-change/tasks.md):
    section 1: 1.1 resolved seed
    section 2: 2.1 / 2.2 parallel pair   ← frontier
    section 3: 3.1 blocked by sections 1+2
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _ticket import parse_tasks_md  # noqa: E402
from orchestration.receipt import seal_receipt  # noqa: E402
from orchestration.session_controller import SessionController  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "sample-change"


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "t@t.t")
    _git(project, "config", "user.name", "test")
    (project / ".gitignore").write_text(".dsh/\n", encoding="utf-8")
    (project / "base.txt").write_text("base\n", encoding="utf-8")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "init")
    change = project / "openspec" / "changes" / "feature"
    change.mkdir(parents=True)
    for source in FIXTURES.glob("*.md"):
        shutil.copy(source, change / source.name)
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "openspec change")
    return project


def _dispatches(batch):
    return [action for action in batch.actions if action.kind == "dispatch"]


def _finish(action, marker: str) -> None:
    worktree = Path(action.cwd)
    (worktree / marker).write_text("done\n", encoding="utf-8")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-m", f"finish {marker}")
    seal_receipt(action.packet_path)


def test_parallel_pair_then_final_completes_without_lost_task(tmp_path):
    project = _project(tmp_path)
    controller = SessionController(
        project, "feature", verification_commands=["test -f base.txt"]
    )
    first = controller.actions("session-a", capacity=2)
    by_ticket = {action.ticket_num: action for action in _dispatches(first)}
    assert set(by_ticket) == {201, 202}

    # Finishing 2.1 alone must NOT dispatch 3.1 — 2.2 is still running and
    # 3.1 is blocked by the whole section 2. The still-leased 2.2 attempt is
    # re-issued idempotently (same assignment id — resume semantics).
    _finish(by_ticket[201], "task-2.1.txt")
    after_first = controller.reconcile(
        by_ticket[201].assignment_id,
        by_ticket[201].claim_token,
        agent_terminal="completed",
        session_id="session-a",
        capacity=2,
    )
    assert [(a.ticket_num, a.assignment_id) for a in _dispatches(after_first)] == [
        (202, by_ticket[202].assignment_id)
    ]
    assert controller.store.get_execution_attempt(by_ticket[202].assignment_id).state == "leased"

    # Finishing 2.2 unblocks 3.1.
    _finish(by_ticket[202], "task-2.2.txt")
    after_second = controller.reconcile(
        by_ticket[202].assignment_id,
        by_ticket[202].claim_token,
        agent_terminal="completed",
        session_id="session-a",
        capacity=2,
    )
    final = next(a for a in _dispatches(after_second) if a.ticket_num == 301)

    _finish(final, "task-3.1.txt")
    after_final = controller.reconcile(
        final.assignment_id,
        final.claim_token,
        agent_terminal="completed",
        session_id="session-a",
        capacity=2,
    )

    attempts = {a.ticket_num: a for a in controller.store.list_execution_attempts()}
    assert attempts[201].state == "succeeded"
    assert attempts[202].state == "succeeded"
    assert attempts[301].state == "succeeded"
    assert {e.ticket_num for e in controller.store.result_envelopes()} >= {201, 202, 301}
    assert controller.store.latest_completion()["missingRequiredTickets"] == []
    assert [a.kind for a in after_final.actions] == ["complete"]
    # tasks.md write-back: every task checked.
    statuses = {t.num: t.status for t in parse_tasks_md(
        project / "openspec" / "changes" / "feature" / "tasks.md")}
    assert set(statuses.values()) == {"resolved"}
    # No PRD file ever appears on the active path.
    assert not (project / "prd-feature.json").exists()
