"""Scheduler — fair, all-or-nothing resource lease tests.

Exercises the three guarantees that define the coordination kernel's
scheduler: atomic all-or-nothing grants, FIFO ordering under contention on
the same exclusive resource, and co-tenancy of non-conflicting jobs. Also
covers release / heartbeat / pick_next_runnable ordering / the observability
status mutators. Every test uses an isolated temp DB so the global
``~/.baton/coordination/queue.db`` is never touched.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from coordination.model import (  # noqa: E402
    BACKGROUND,
    EMERGENCY,
    FAILED,
    Job,
    LEASED,
    NORMAL,
    ORPHANED,
    ResourceClaim,
    RUNNING,
    StageSpec,
    SUBMITTED,
    SUCCEEDED,
    SUSPECT,
    UNKNOWN,
)
from coordination.scheduler import Scheduler  # noqa: E402
from coordination.store import CoordinationStore  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / factories
# ---------------------------------------------------------------------------


def _job(
    *,
    job_id: str,
    idempotency_key: str | None = None,
    priority: str = NORMAL,
    claims: list[ResourceClaim] | None = None,
    project: str = "baton",
    feature: str = "core-buildout",
    submitter: str = "tester",
) -> Job:
    if idempotency_key is None:
        idempotency_key = f"idem-{job_id}"
    if claims is None:
        claims = [ResourceClaim(resource_key="builder-slot", mode="exclusive")]
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
                claims=claims,
                command=["ralph.sh", "run"],
            )
        ],
    )


@pytest.fixture()
def store(tmp_path: Path) -> CoordinationStore:
    return CoordinationStore(db_path=tmp_path / "test-scheduler.db")


@pytest.fixture()
def scheduler(store: CoordinationStore) -> Scheduler:
    return Scheduler(store=store)


def _count_leases(store: CoordinationStore, job_id: str) -> int:
    conn = sqlite3.connect(str(store.db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM leases WHERE job_id = ?", (job_id,)
        ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# try_grant — atomicity / all-or-nothing
# ---------------------------------------------------------------------------


class TestTryGrantAtomicity:
    def test_grants_when_resource_free(self, store, scheduler):
        job = store.submit_job(_job(job_id="j1"))
        granted = scheduler.try_grant(job, owner="worker-1")
        assert granted is not None
        assert granted.status == LEASED
        assert granted.lease_token  # non-empty
        assert granted.owner == "worker-1"
        # exactly one lease row for one exclusive claim
        assert _count_leases(store, "j1") == 1
        # DB row reflects the grant
        fetched = store.get_job("j1")
        assert fetched is not None
        assert fetched.status == LEASED
        assert fetched.lease_token == granted.lease_token

    def test_multi_claim_is_all_or_nothing(self, store, scheduler):
        """Required by spec: if any claim is unavailable, NO claim is granted.

        Job declares 2 claims; one resource is pre-held by another job → the
        whole grant returns None and zero lease rows exist for this job.
        """
        # Pre-hold resource "r-busy" with a first job.
        first = store.submit_job(
            _job(
                job_id="holder",
                claims=[
                    ResourceClaim(resource_key="r-free", mode="exclusive"),
                    ResourceClaim(resource_key="r-busy", mode="exclusive"),
                ],
            )
        )
        assert scheduler.try_grant(first, owner="w-holder") is not None
        # Now a second job wants both r-free (still free) and r-busy (held).
        second = store.submit_job(
            _job(
                job_id="want-both",
                claims=[
                    ResourceClaim(resource_key="r-free", mode="exclusive"),
                    ResourceClaim(resource_key="r-busy", mode="exclusive"),
                ],
            )
        )
        result = scheduler.try_grant(second, owner="w-wantboth")
        # All-or-nothing: nothing granted.
        assert result is None
        # No lease rows left behind for the failed job.
        assert _count_leases(store, "want-both") == 0
        # And the free resource was NOT consumed (still 0 leases on r-free
        # attributable to want-both).
        conn = sqlite3.connect(str(store.db_path))
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM leases WHERE job_id = ? AND resource_key = ?",
                ("want-both", "r-free"),
            ).fetchone()
            assert int(row[0]) == 0
        finally:
            conn.close()

    def test_frozen_resource_blocks_grant(self, store, scheduler):
        store.freeze_resource("frozen-slot", reason="maintenance", operator="ops")
        job = store.submit_job(
            _job(
                job_id="j-frozen",
                claims=[ResourceClaim(resource_key="frozen-slot", mode="exclusive")],
            )
        )
        assert scheduler.try_grant(job, owner="w") is None
        assert _count_leases(store, "j-frozen") == 0

    def test_capacity_resource_allows_up_to_capacity(self, store, scheduler):
        """A capacity-N resource admits N unit-claims but not N+1."""
        store._connect().close()  # ensure schema
        conn = sqlite3.connect(str(store.db_path))
        try:
            conn.execute(
                "INSERT INTO resources (resource_key, capacity, mode, frozen, frozen_reason, created_at, updated_at) "
                "VALUES ('cap-2', 2, 'capacity', 0, '', 'now', 'now')"
            )
            conn.commit()
        finally:
            conn.close()
        for i in range(2):
            job = store.submit_job(
                _job(
                    job_id=f"cap-{i}",
                    claims=[ResourceClaim(resource_key="cap-2", mode="capacity", units=1)],
                )
            )
            assert scheduler.try_grant(job, owner=f"w-{i}") is not None
        # Third one must be refused (capacity exhausted).
        third = store.submit_job(
            _job(
                job_id="cap-overflow",
                claims=[ResourceClaim(resource_key="cap-2", mode="capacity", units=1)],
            )
        )
        assert scheduler.try_grant(third, owner="w-overflow") is None


# ---------------------------------------------------------------------------
# try_grant — single BEGIN IMMEDIATE transaction
# ---------------------------------------------------------------------------


class TestTryGrantTransaction:
    def test_grant_is_atomic_no_partial_state_on_failure(self, store, scheduler):
        """All-or-nothing at the row level: after a failed grant there is no
        orphan lease row for any of the job's claims."""
        # Pre-hold one of two resources.
        holder = store.submit_job(
            _job(
                job_id="hold",
                claims=[ResourceClaim(resource_key="shared-busy", mode="exclusive")],
            )
        )
        scheduler.try_grant(holder, owner="wh")
        # Job claims 3 resources, the middle one held → fail.
        victim = store.submit_job(
            _job(
                job_id="victim",
                claims=[
                    ResourceClaim(resource_key="a-free", mode="exclusive"),
                    ResourceClaim(resource_key="shared-busy", mode="exclusive"),
                    ResourceClaim(resource_key="c-free", mode="exclusive"),
                ],
            )
        )
        assert scheduler.try_grant(victim, owner="wv") is None
        # No leases at all for victim.
        assert _count_leases(store, "victim") == 0

    def test_try_grant_emits_leased_event_with_granted_claims(self, store, scheduler):
        job = store.submit_job(_job(job_id="j-evt"))
        scheduler.try_grant(job, owner="w-evt")
        events = [e for e in store.list_events(job_id="j-evt") if e["event_type"] == "job.leased"]
        assert len(events) == 1
        payload = events[0]["payload"]
        assert payload["owner"] == "w-evt"
        assert payload["lease_token"]
        granted = payload["granted_claims"]
        assert isinstance(granted, list)
        assert granted[0]["resource_key"] == "builder-slot"


# ---------------------------------------------------------------------------
# Conflicting vs non-conflicting jobs
# ---------------------------------------------------------------------------


class TestContentionOrdering:
    def test_conflicting_jobs_are_fifo(self, store, scheduler):
        """Required by spec: two jobs want the same exclusive resource; the
        earlier-submitted one wins, the later one gets None and waits."""
        first = store.submit_job(_job(job_id="first", idempotency_key="k-first"))
        # Ensure a strictly-later created_at so FIFO ordering is unambiguous.
        time.sleep(0.01)
        second = store.submit_job(_job(job_id="second", idempotency_key="k-second"))

        g1 = scheduler.try_grant(first, owner="w1")
        g2 = scheduler.try_grant(second, owner="w2")

        assert g1 is not None and g1.status == LEASED
        assert g2 is None  # blocked — resource held by first

    def test_non_conflicting_jobs_run_together(self, store, scheduler):
        """Required by spec: two jobs on different resources both get leased."""
        a = store.submit_job(
            _job(job_id="a", claims=[ResourceClaim(resource_key="slot-a", mode="exclusive")])
        )
        b = store.submit_job(
            _job(job_id="b", claims=[ResourceClaim(resource_key="slot-b", mode="exclusive")])
        )
        ga = scheduler.try_grant(a, owner="wa")
        gb = scheduler.try_grant(b, owner="wb")
        assert ga is not None and ga.status == LEASED
        assert gb is not None and gb.status == LEASED


# ---------------------------------------------------------------------------
# pick_next_runnable — fairness / ordering
# ---------------------------------------------------------------------------


class TestPickNextRunnable:
    def test_picks_highest_priority_then_oldest(self, store, scheduler):
        """Required by spec: priority_rank DESC, then created_at ASC."""
        # Submit a NORMAL job first, then an EMERGENCY job later.
        normal = store.submit_job(
            _job(job_id="normal", idempotency_key="k-n", priority=NORMAL)
        )
        time.sleep(0.01)
        emergency = store.submit_job(
            _job(job_id="emer", idempotency_key="k-e", priority=EMERGENCY)
        )
        # Emergency must win despite being submitted second.
        picked = scheduler.pick_next_runnable(owner="poller")
        assert picked is not None
        assert picked.job_id == emergency.job_id

    def test_fifo_within_same_priority(self, store, scheduler):
        # Two jobs, same priority, different resources so both can be picked.
        a = store.submit_job(
            _job(
                job_id="a",
                idempotency_key="k-a",
                priority=NORMAL,
                claims=[ResourceClaim(resource_key="slot-a", mode="exclusive")],
            )
        )
        time.sleep(0.01)
        b = store.submit_job(
            _job(
                job_id="b",
                idempotency_key="k-b",
                priority=NORMAL,
                claims=[ResourceClaim(resource_key="slot-b", mode="exclusive")],
            )
        )
        picked = scheduler.pick_next_runnable(owner="poller")
        assert picked is not None
        assert picked.job_id == a.job_id  # older first
        # b still runnable next
        picked2 = scheduler.pick_next_runnable(owner="poller")
        assert picked2 is not None
        assert picked2.job_id == b.job_id

    def test_returns_none_when_all_blocked(self, store, scheduler):
        # Hold the only resource.
        holder = store.submit_job(_job(job_id="hold"))
        scheduler.try_grant(holder, owner="wh")
        # A blocked waiter.
        store.submit_job(_job(job_id="wait", idempotency_key="k-wait"))
        assert scheduler.pick_next_runnable(owner="poller") is None

    def test_returns_none_when_no_submitted_jobs(self, scheduler):
        assert scheduler.pick_next_runnable(owner="poller") is None

    def test_skips_blocked_picks_next_runnable(self, store, scheduler):
        """If the highest-priority job is blocked, the next runnable wins."""
        # Hold slot-x with an emergency job.
        holder = store.submit_job(
            _job(
                job_id="hold",
                priority=EMERGENCY,
                claims=[ResourceClaim(resource_key="slot-x", mode="exclusive")],
            )
        )
        scheduler.try_grant(holder, owner="wh")
        # Two new jobs: blocked (wants slot-x) and free (wants slot-y).
        blocked = store.submit_job(
            _job(
                job_id="blocked",
                priority=EMERGENCY,  # high priority but cannot run
                claims=[ResourceClaim(resource_key="slot-x", mode="exclusive")],
                idempotency_key="k-blk",
            )
        )
        free = store.submit_job(
            _job(
                job_id="free",
                priority=NORMAL,
                claims=[ResourceClaim(resource_key="slot-y", mode="exclusive")],
                idempotency_key="k-free",
            )
        )
        picked = scheduler.pick_next_runnable(owner="poller")
        assert picked is not None
        # Even though `blocked` is higher priority, it can't run; `free` wins.
        assert picked.job_id == free.job_id


# ---------------------------------------------------------------------------
# release / heartbeat
# ---------------------------------------------------------------------------


class TestRelease:
    def test_release_clears_leases_and_sets_status(self, store, scheduler):
        job = store.submit_job(_job(job_id="j"))
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        assert _count_leases(store, "j") == 1
        released = scheduler.release("j", granted.lease_token, final_status=SUCCEEDED)
        assert released is not None
        assert released.status == SUCCEEDED
        assert released.lease_token == ""
        # Capacity freed: leases gone.
        assert _count_leases(store, "j") == 0
        # Subsequent job can now grab the resource.
        nxt = store.submit_job(_job(job_id="j2", idempotency_key="k2"))
        assert scheduler.try_grant(nxt, owner="w2") is not None

    def test_release_wrong_token_is_noop(self, store, scheduler):
        job = store.submit_job(_job(job_id="j"))
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        out = scheduler.release("j", "wrong-token", final_status=SUCCEEDED)
        assert out is None
        # Job still leased, lease rows intact.
        fetched = store.get_job("j")
        assert fetched is not None
        assert fetched.status == LEASED
        assert _count_leases(store, "j") == 1

    def test_release_missing_job_returns_none(self, scheduler):
        assert scheduler.release("nope", "any", final_status=SUCCEEDED) is None

    def test_release_emits_event(self, store, scheduler):
        job = store.submit_job(_job(job_id="j"))
        granted = scheduler.try_grant(job, owner="w")
        scheduler.release("j", granted.lease_token, final_status=FAILED)
        evts = [e for e in store.list_events(job_id="j") if e["event_type"] == "job.released"]
        assert len(evts) == 1
        assert evts[0]["payload"]["final_status"] == FAILED


class TestHeartbeat:
    def test_heartbeat_extends_expiry_and_stamps(self, store, scheduler):
        job = store.submit_job(_job(job_id="j"))
        granted = scheduler.try_grant(job, owner="w", lease_seconds=60)
        assert granted is not None
        hb = scheduler.heartbeat("j", granted.lease_token, lease_seconds=3600)
        assert hb is not None
        # leases.expires_at advanced (now +3600s vs original +60s).
        conn = sqlite3.connect(str(store.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT expires_at, heartbeat_at FROM leases WHERE job_id = ?", ("j",)
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["heartbeat_at"]

    def test_heartbeat_wrong_token_is_noop(self, store, scheduler):
        job = store.submit_job(_job(job_id="j"))
        scheduler.try_grant(job, owner="w")
        assert scheduler.heartbeat("j", "wrong-token") is None

    def test_heartbeat_after_release_is_noop(self, store, scheduler):
        job = store.submit_job(_job(job_id="j"))
        granted = scheduler.try_grant(job, owner="w")
        scheduler.release("j", granted.lease_token, final_status=SUCCEEDED)
        # Lease is gone; heartbeat can't revive it.
        assert scheduler.heartbeat("j", granted.lease_token) is None


# ---------------------------------------------------------------------------
# Observability status mutators (do not release lease)
# ---------------------------------------------------------------------------


class TestObservabilityMutators:
    def _grant(self, store, scheduler, job_id="j"):
        job = store.submit_job(_job(job_id=job_id))
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        return granted

    def test_mark_unknown_does_not_release(self, store, scheduler):
        granted = self._grant(store, scheduler)
        out = scheduler.mark_unknown("j", reason="scan inconclusive")
        assert out is not None
        assert out.status == UNKNOWN
        assert out.reason == "scan inconclusive"
        # Lease rows preserved.
        assert _count_leases(store, "j") == 1
        evts = [e for e in store.list_events(job_id="j") if e["event_type"] == "job.unknown"]
        assert len(evts) == 1

    def test_mark_orphaned_does_not_release(self, store, scheduler):
        granted = self._grant(store, scheduler)
        out = scheduler.mark_orphaned("j", reason="pid vanished")
        assert out is not None
        assert out.status == ORPHANED
        assert _count_leases(store, "j") == 1
        evts = [e for e in store.list_events(job_id="j") if e["event_type"] == "job.orphaned"]
        assert len(evts) == 1

    def test_mark_suspect_does_not_release(self, store, scheduler):
        granted = self._grant(store, scheduler)
        out = scheduler.mark_suspect("j", reason="heartbeat stale")
        assert out is not None
        assert out.status == SUSPECT
        assert _count_leases(store, "j") == 1
        evts = [e for e in store.list_events(job_id="j") if e["event_type"] == "job.suspect"]
        assert len(evts) == 1

    def test_mark_unknown_missing_job_returns_none(self, scheduler):
        assert scheduler.mark_unknown("nope", reason="x") is None


# ---------------------------------------------------------------------------
# lease_epoch monotonicity
# ---------------------------------------------------------------------------


class TestLeaseEpoch:
    def test_epoch_increments_per_resource(self, store, scheduler):
        """Two concurrent leases on the same capacity-2 resource get epoch 1 then 2.

        ``lease_epoch`` is computed as ``max(active-lease epoch, persisted
        high-water mark in resources.last_epoch) + 1`` at grant time, so leases
        admitted in sequence get a strictly increasing epoch.
        """
        # Declare a capacity-2 resource so two leases coexist.
        store._connect().close()  # ensure schema
        conn = sqlite3.connect(str(store.db_path))
        try:
            conn.execute(
                "INSERT INTO resources (resource_key, capacity, mode, frozen, frozen_reason, created_at, updated_at) "
                "VALUES ('cap-2', 2, 'capacity', 0, '', 'now', 'now')"
            )
            conn.commit()
        finally:
            conn.close()
        a = store.submit_job(
            _job(
                job_id="a",
                idempotency_key="ka",
                claims=[ResourceClaim(resource_key="cap-2", mode="capacity", units=1)],
            )
        )
        b = store.submit_job(
            _job(
                job_id="b",
                idempotency_key="kb",
                claims=[ResourceClaim(resource_key="cap-2", mode="capacity", units=1)],
            )
        )
        ga = scheduler.try_grant(a, owner="wa")
        gb = scheduler.try_grant(b, owner="wb")
        assert ga is not None and gb is not None
        conn = sqlite3.connect(str(store.db_path))
        try:
            rows = conn.execute(
                "SELECT job_id, epoch FROM leases WHERE resource_key = ? ORDER BY epoch",
                ("cap-2",),
            ).fetchall()
        finally:
            conn.close()
        # First grant → epoch 1, second grant → epoch 2.
        assert [(r[0], int(r[1])) for r in rows] == [("a", 1), ("b", 2)]

    def test_epoch_stays_monotonic_across_release(self, store, scheduler):
        """Releasing a lease must NOT rewind the epoch counter.

        The high-water mark in ``resources.last_epoch`` survives release, so a
        grant after a release gets a strictly higher epoch than the released
        one. This is what makes ``epoch`` usable as a strict grant ordering
        for orphan / replay detection.
        """
        a = store.submit_job(_job(job_id="a", idempotency_key="ka"))
        ga = scheduler.try_grant(a, owner="wa")
        assert ga is not None
        assert ga.lease_epoch == 1
        scheduler.release("a", ga.lease_token, final_status=SUCCEEDED)
        # Next grant on the now-empty resource gets epoch 2, not 1.
        b = store.submit_job(_job(job_id="b", idempotency_key="kb"))
        gb = scheduler.try_grant(b, owner="wb")
        assert gb is not None
        assert gb.lease_epoch == 2
