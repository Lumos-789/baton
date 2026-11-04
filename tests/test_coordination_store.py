"""CoordinationStore — SQLite persistence layer tests.

Exercises: idempotent submit_job, get/list, freeze/unfreeze/is_frozen,
event recording/listing, and the row→Job round-trip. Each test uses an
isolated temp DB so the global ``~/.baton/coordination/queue.db`` is never
touched.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from coordination.model import (  # noqa: E402
    EMERGENCY,
    Job,
    NORMAL,
    PRIORITY_RANK,
    ResourceClaim,
    StageSpec,
    SUBMITTED,
)
from coordination.store import CoordinationStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_job(
    *,
    job_id: str = "job-1",
    idempotency_key: str = "idem-1",
    priority: str = NORMAL,
    project: str = "baton",
    feature: str = "core-buildout",
    submitter: str = "tester",
) -> Job:
    return Job(
        job_id=job_id,
        idempotency_key=idempotency_key,
        submitter=submitter,
        project=project,
        feature=feature,
        priority=priority,
        stages=[
            StageSpec(
                name="build",
                runner="ralph",
                claims=[ResourceClaim(resource_key="builder-slot", mode="exclusive")],
                command=["ralph.sh", "run"],
            )
        ],
    )


@pytest.fixture()
def store(tmp_path: Path) -> CoordinationStore:
    """A CoordinationStore backed by an isolated temp DB file."""
    return CoordinationStore(db_path=tmp_path / "test-queue.db")


# ---------------------------------------------------------------------------
# submit_job / idempotency
# ---------------------------------------------------------------------------


class TestSubmitJob:
    def test_submit_returns_job_with_persisted_fields(self, store):
        job = _make_job()
        out = store.submit_job(job)
        assert out.job_id == "job-1"
        assert out.status == SUBMITTED
        # priority_rank derived from NORMAL bucket
        assert out.priority_rank == PRIORITY_RANK[NORMAL]
        # created_at / updated_at stamped
        assert out.created_at
        assert out.updated_at

    def test_submit_persists_to_db(self, store):
        job = _make_job()
        store.submit_job(job)
        # open a raw connection to confirm the row is real
        conn = sqlite3.connect(str(store.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", ("job-1",)).fetchone()
            assert row is not None
            assert row["idempotency_key"] == "idem-1"
            assert row["priority"] == NORMAL
            assert row["priority_rank"] == PRIORITY_RANK[NORMAL]
            # stages_json is valid JSON
            parsed = json.loads(row["stages_json"])
            assert parsed[0]["name"] == "build"
        finally:
            conn.close()

    def test_duplicate_idempotency_key_returns_existing_job(self, store):
        """Required-by-spec: same idempotency_key → existing job, no error, no clobber."""
        first = store.submit_job(_make_job(job_id="job-A", idempotency_key="dup-key"))
        # second submit with same idempotency_key but different job_id must NOT clobber
        second = store.submit_job(
            _make_job(job_id="job-B", idempotency_key="dup-key")
        )
        assert second.job_id == "job-A"  # original returned, not job-B
        assert first.job_id == second.job_id
        assert first.created_at == second.created_at
        # only one row in the table
        conn = sqlite3.connect(str(store.db_path))
        try:
            count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            assert count == 1
        finally:
            conn.close()

    def test_submit_emits_submitted_event(self, store):
        job = store.submit_job(_make_job())
        events = store.list_events(job_id=job.job_id)
        assert any(e["event_type"] == "job.submitted" for e in events)

    def test_submit_priority_rank_for_emergency(self, store):
        job = store.submit_job(_make_job(priority=EMERGENCY))
        fetched = store.get_job(job.job_id)
        assert fetched is not None
        assert fetched.priority_rank == PRIORITY_RANK[EMERGENCY]


# ---------------------------------------------------------------------------
# get_job / list_jobs
# ---------------------------------------------------------------------------


class TestGetListJobs:
    def test_get_job_missing_returns_none(self, store):
        assert store.get_job("nope") is None

    def test_get_job_round_trip(self, store):
        original = _make_job(stages_kwargs=None) if False else _make_job()
        store.submit_job(original)
        fetched = store.get_job(original.job_id)
        assert fetched is not None
        assert fetched.job_id == original.job_id
        assert fetched.idempotency_key == original.idempotency_key
        assert fetched.priority == original.priority
        assert fetched.priority_rank == original.priority_rank
        # stages survive the JSON round-trip
        assert len(fetched.stages) == 1
        assert fetched.stages[0].name == "build"
        assert fetched.stages[0].claims[0].resource_key == "builder-slot"

    def test_list_jobs_no_filter_orders_by_priority_then_created(self, store):
        # submit in interleaved priority order; expect emergency first
        a = store.submit_job(_make_job(job_id="A", idempotency_key="k-A", priority=NORMAL))
        b = store.submit_job(_make_job(job_id="B", idempotency_key="k-B", priority=EMERGENCY))
        listed = store.list_jobs()
        ids = [j.job_id for j in listed]
        assert ids[0] == "B"  # emergency (rank 100) before normal (rank 50)
        assert set(ids) == {"A", "B"}

    def test_list_jobs_filter_by_status(self, store):
        store.submit_job(_make_job(job_id="A", idempotency_key="k-A"))
        listed = store.list_jobs(status=SUBMITTED)
        assert len(listed) == 1
        assert listed[0].job_id == "A"
        # a status nobody has → empty
        assert store.list_jobs(status="succeeded") == []


# ---------------------------------------------------------------------------
# Resource freeze / unfreeze
# ---------------------------------------------------------------------------


class TestFreeze:
    def test_freeze_then_is_frozen(self, store):
        assert store.is_frozen("builder-slot") is False
        store.freeze_resource("builder-slot", reason="maintenance", operator="ops")
        assert store.is_frozen("builder-slot") is True
        # resource row materialized lazily
        res = store.get_resource("builder-slot")
        assert res is not None
        assert res.frozen is True

    def test_unfreeze_clears_frozen_flag(self, store):
        store.freeze_resource("builder-slot", reason="x", operator="ops")
        assert store.is_frozen("builder-slot") is True
        store.unfreeze_resource("builder-slot", operator="ops")
        assert store.is_frozen("builder-slot") is False
        res = store.get_resource("builder-slot")
        assert res is not None
        assert res.frozen is False

    def test_freeze_logs_action(self, store):
        store.freeze_resource("builder-slot", reason="maintenance", operator="ops")
        store.unfreeze_resource("builder-slot", operator="ops2")
        conn = sqlite3.connect(str(store.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT action, reason, operator FROM freeze_log ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        actions = [r["action"] for r in rows]
        assert actions == ["freeze", "unfreeze"]
        assert rows[0]["reason"] == "maintenance"
        assert rows[1]["operator"] == "ops2"

    def test_freeze_emits_event(self, store):
        store.freeze_resource("builder-slot", reason="x", operator="ops")
        evts = [e for e in store.list_events() if e["event_type"] == "resource.frozen"]
        assert len(evts) == 1
        assert evts[0]["payload"]["resource_key"] == "builder-slot"

    def test_unfreeze_emits_event(self, store):
        store.unfreeze_resource("builder-slot", operator="ops")
        evts = [e for e in store.list_events() if e["event_type"] == "resource.unfrozen"]
        assert len(evts) == 1


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


class TestEvents:
    def test_record_event_returns_id(self, store):
        eid = store.record_event("job-1", "test.ping", {"k": "v"})
        assert isinstance(eid, str) and len(eid) > 0

    def test_list_events_filter_by_job(self, store):
        store.record_event("job-A", "x", {})
        store.record_event("job-B", "x", {})
        store.record_event("job-A", "y", {})
        a_events = store.list_events(job_id="job-A")
        assert all(e["job_id"] == "job-A" for e in a_events)
        assert len(a_events) == 2

    def test_list_events_filter_by_type(self, store):
        store.record_event("job-1", "alpha", {})
        store.record_event("job-1", "beta", {})
        store.record_event("job-1", "alpha", {})
        alphas = store.list_events(event_type="alpha")
        assert all(e["event_type"] == "alpha" for e in alphas)
        assert len(alphas) == 2

    def test_list_events_payload_parsed_back_to_dict(self, store):
        store.record_event("j", "evt", {"n": 7, "s": "hi", "nested": {"x": 1}})
        events = store.list_events(job_id="j")
        assert events[0]["payload"] == {"n": 7, "s": "hi", "nested": {"x": 1}}

    def test_list_events_limit(self, store):
        for i in range(10):
            store.record_event("j", "evt", {"i": i})
        assert len(store.list_events(job_id="j", limit=3)) == 3


# ---------------------------------------------------------------------------
# Schema presence (defensive — pin the 5 tables by name)
# ---------------------------------------------------------------------------


def _table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


class TestSchema:
    def test_all_five_tables_created(self, store):
        # instantiate + force schema creation
        store._connect().close()
        names = _table_names(store.db_path)
        for t in ("jobs", "resources", "leases", "events", "freeze_log"):
            assert t in names, f"missing table: {t}"

    def test_jobs_unique_constraint_on_idempotency_key(self, store):
        """Verify the UNIQUE(idempotency_key) constraint exists at the DB level.

        SQLite names table-level UNIQUE constraints as ``sqlite_autoindex_<t>_<n>``,
        so we verify via PRAGMA index_info which column the unique index covers.
        """
        store._connect().close()
        conn = sqlite3.connect(str(store.db_path))
        try:
            indexes = conn.execute("PRAGMA index_list(jobs)").fetchall()
            # Find unique indexes and check their covered columns
            found = False
            for idx in indexes:
                if idx[2] == 1:  # "unique" flag
                    idx_name = idx[1]
                    cols = conn.execute(
                        f"PRAGMA index_info({idx_name})"
                    ).fetchall()
                    covered = {col[2] for col in cols}  # column name at position 2
                    if "idempotency_key" in covered:
                        found = True
                        break
        finally:
            conn.close()
        assert found, "expected a unique index covering idempotency_key"

    def test_jobs_status_index_exists(self, store):
        store._connect().close()
        conn = sqlite3.connect(str(store.db_path))
        try:
            rows = conn.execute("PRAGMA index_list(jobs)").fetchall()
            names = [row[1] for row in rows]
        finally:
            conn.close()
        assert "idx_jobs_status" in names
        assert "idx_jobs_project_feature" in names

    def test_leases_indexes_exist(self, store):
        store._connect().close()
        conn = sqlite3.connect(str(store.db_path))
        try:
            rows = conn.execute("PRAGMA index_list(leases)").fetchall()
            names = [row[1] for row in rows]
        finally:
            conn.close()
        assert "idx_leases_job" in names
        assert "idx_leases_resource" in names

    def test_events_index_exists(self, store):
        store._connect().close()
        conn = sqlite3.connect(str(store.db_path))
        try:
            rows = conn.execute("PRAGMA index_list(events)").fetchall()
            names = [row[1] for row in rows]
        finally:
            conn.close()
        assert "idx_events_job" in names


# ---------------------------------------------------------------------------
# Default path / construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_default_db_path_is_global(self):
        s = CoordinationStore()
        expected = Path.home() / ".baton" / "coordination" / "queue.db"
        assert s.db_path == expected

    def test_accepts_str_or_path(self, tmp_path):
        s1 = CoordinationStore(db_path=str(tmp_path / "a.db"))
        s2 = CoordinationStore(db_path=tmp_path / "b.db")
        assert isinstance(s1.db_path, Path)
        assert isinstance(s2.db_path, Path)
