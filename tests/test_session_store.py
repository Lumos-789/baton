"""Atomic session assignment persistence and claims."""

from __future__ import annotations

import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _ticket import parse_tasks_md  # noqa: E402
from orchestration.plan_store import (  # noqa: E402
    ExecutionAttemptError,
    PlanStore,
    PlanStoreError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sample-change"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    change = project / "openspec" / "changes" / "feature"
    change.mkdir(parents=True)
    for source in FIXTURES.glob("*.md"):
        shutil.copy(source, change / source.name)
    return project


def test_execution_schema_migrates_existing_queue_db_without_data_loss(tmp_path):
    project = _project(tmp_path)
    store = PlanStore(project, "feature")
    plan = store.ensure_initial_plan()

    attempts = store.claim_ready_assignments(
        session_id="session-a", capacity=1, base_sha="abc"
    )

    assert plan.revision == 1
    assert len(attempts) == 1
    assert store.current_plan().revision == 1


def test_two_sessions_cannot_claim_same_ticket(tmp_path):
    project = _project(tmp_path)

    def claim(session: str):
        return PlanStore(project, "feature").claim_ready_assignments(
            session_id=session, capacity=1, base_sha="abc"
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ["session-a", "session-b"]))

    claimed = [a.ticket_num for batch in results for a in batch]
    assert len(claimed) == len(set(claimed))
    assert set(claimed).issubset({201, 202})


def test_claim_respects_capacity_without_claiming_entire_frontier(tmp_path):
    project = _project(tmp_path)
    store = PlanStore(project, "feature")

    first = store.claim_ready_assignments(
        session_id="session-a", capacity=1, base_sha="abc"
    )

    assert [a.ticket_num for a in first] == [201]
    # The plan face is not mutated at claim time — the durable store owns
    # in-flight state; tasks.md still shows both tasks unchecked.
    statuses = {t.num: t.status for t in parse_tasks_md(store.tasks_path)}
    assert statuses[201] == "ready"
    assert statuses[202] == "ready"
    assert store.get_execution_attempt(first[0].assignment_id).state == "leased"


def test_claim_records_revision_packet_hash_and_token(tmp_path):
    store = PlanStore(_project(tmp_path), "feature")

    attempt = store.claim_ready_assignments(
        session_id="session-a", capacity=1, base_branch="main", base_sha="abc"
    )[0]
    persisted = store.get_execution_attempt(attempt.assignment_id)

    assert attempt.claim_token
    assert attempt.task_packet_hash
    assert persisted.claim_token == ""
    assert persisted.plan_revision == 1
    assert persisted.base_sha == "abc"


def test_bind_agent_requires_matching_claim_token(tmp_path):
    store = PlanStore(_project(tmp_path), "feature")
    attempt = store.claim_ready_assignments(
        session_id="session-a", capacity=1, base_sha="abc"
    )[0]

    with pytest.raises(ExecutionAttemptError):
        store.bind_attempt_agent(attempt.assignment_id, "wrong", "agent-1")

    bound = store.bind_attempt_agent(
        attempt.assignment_id, attempt.claim_token, "agent-1"
    )
    assert bound.state == "spawned"
    assert bound.agent_id == "agent-1"


def test_capacity_above_five_is_rejected(tmp_path):
    store = PlanStore(_project(tmp_path), "feature")
    with pytest.raises(ValueError, match="between 1 and 5"):
        store.claim_ready_assignments(
            session_id="session-a", capacity=6, base_sha="abc"
        )


@pytest.mark.parametrize("feature", ["", ".", "..", "../escape", "a/b", "a\\b"])
def test_plan_store_rejects_feature_path_traversal(tmp_path, feature):
    with pytest.raises(PlanStoreError, match="change id"):
        PlanStore(_project(tmp_path), feature)


def test_atomic_write_temp_names_are_unique_per_write(tmp_path, monkeypatch):
    """plan.json is shared by every concurrent CLI process.

    A fixed `plan.json.tmp` let two writers truncate each other's payload
    mid-write (one rename then installed mixed bytes); each writer now renames
    its own pid+uuid temp file.
    """
    import os

    from orchestration import store_base

    path = tmp_path / "plan.json"
    seen: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(Path(src).name)
        return real_replace(src, dst)

    monkeypatch.setattr(store_base.os, "replace", spy)

    store_base._atomic_write(path, '{"a": 1}\n')
    store_base._atomic_write(path, '{"a": 2}\n')

    assert len(seen) == 2
    assert len(set(seen)) == 2
    assert all(
        name.startswith("plan.json.") and name.endswith(".tmp") for name in seen
    )
    assert path.read_text(encoding="utf-8") == '{"a": 2}\n'
    assert not list(tmp_path.glob("*.tmp"))
