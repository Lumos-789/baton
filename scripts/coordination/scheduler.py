"""Fair scheduler with all-or-nothing resource leases.

The :class:`Scheduler` is the heart of the coordination kernel. Given a
:class:`~coordination.model.Job`, it decides atomically whether all of the
job's current-stage claims can be granted *simultaneously* and, if so, issues
exactly one ``lease_token`` and stamps the job as ``leased``.

Design contract (pinned to ``docs/architecture.md`` and the US-003 ticket):

* **All-or-nothing.** A job that declares 3 claims gets all 3 or none. There is
  no partial grant — a multi-resource job never finds itself holding one
  resource while blocked on another (the classic distributed deadlock source).
* **Single ``BEGIN IMMEDIATE`` transaction.** The whole grant decision
  (read claims → check capacity → insert leases → update job → record event)
  happens inside one write transaction so two concurrent workers can never
  both observe the same free capacity and double-book it. SQLite's
  ``BEGIN IMMEDIATE`` acquires a RESERVED lock up front, serializing writers.
* **Oldest-eligible fairness.** ``pick_next_runnable`` walks submitted jobs
  ordered by ``priority_rank DESC, created_at ASC`` and grants the first one
  whose claims are all satisfiable. Ties at the same priority are FIFO.
* **No silent fallback.** If nothing can be granted the scheduler returns
  ``None``; the caller decides whether to block, retry, or surface the
  conflict — never the scheduler.

The scheduler never owns a connection long-term: every public method opens a
short-lived connection via ``store._connect()`` (which lazily ensures the
schema) and closes it in ``finally``. This mirrors :class:`CoordinationStore`
and keeps the lock window to one transaction.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta
from typing import Any

from orchestration.plan_model import utc_now

from coordination.model import (
    FAILED,
    Job,
    LEASED,
    ResourceClaim,
    RUNNING,
    SUBMITTED,
    SUCCEEDED,
    SUSPECT,
    UNKNOWN,
    ORPHANED,
)
from coordination.store import CoordinationStore

# Statuses under which a held lease is still considered "live" for the
# purposes of release / heartbeat. Anything else means the job already shed
# its lease (or never had one) and the call is a no-op returning None.
_LEASED_STATES: tuple[str, ...] = (LEASED, RUNNING)

# psutil is an optional dependency (mirrors coordination/runner.py). When
# present we capture a reliable process create_time at grant time so the
# stale-lease scan's PID-reuse check has a real baseline to compare against.
try:  # pragma: no cover - import-time branch depends on the host
    import psutil  # type: ignore[import-untyped]

    _HAS_PSUTIL = True
except Exception:  # pragma: no cover - exercised only on psutil-less hosts
    psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False


def _process_create_time(pid: int) -> float:
    """Return the create_time (epoch seconds) of ``pid``, or 0.0 if unknown.

    Captured at grant time and stored as the job/lease ``owner_start_time`` so
    :meth:`JobRunner.is_owner_alive` can detect PID reuse later. 0.0 means
    "could not determine" — the runner degrades to existence-only checking in
    that case (its documented fallback), so a 0 baseline never produces a
    false "dead" verdict on a live owner.
    """
    if _HAS_PSUTIL:
        try:
            return float(psutil.Process(pid).create_time())  # type: ignore[union-attr]
        except Exception:
            return 0.0
    # Best-effort cross-platform fallback (macOS/Linux `ps`).
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=5, check=False,
        )
        line = (out.stdout or "").strip()
        if not line:
            return 0.0
        dt = datetime.strptime(line, "%a %b %d %H:%M:%S %Y")
        return dt.timestamp()
    except Exception:
        return 0.0


class Scheduler:
    """Atomically grants / releases resource leases for jobs.

    The scheduler is the only component that mutates the ``leases`` table.
    Workers obtain a job via :meth:`pick_next_runnable` (or
    :meth:`try_grant` for a specific job), do their work, then call
    :meth:`release` with the final status. Long-running work should call
    :meth:`heartbeat` periodically to push out the lease expiry.
    """

    def __init__(self, store: CoordinationStore) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # Internal: connection helpers
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """Open a schema-ready connection through the store.

        We reach into ``store._connect()`` deliberately: it owns schema
        bootstrap and the row factory, so the scheduler never has to
        duplicate DDL. The scheduler is the store's peer, not its caller.
        """
        return self.store._connect()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def try_grant(
        self,
        job: Job,
        owner: str,
        lease_seconds: int = 3600,
    ) -> Job | None:
        """Attempt to lease ``job``'s current stage, all-or-nothing.

        Returns the updated :class:`Job` (status=``leased``) on success, or
        ``None`` if any of the stage's claims cannot be satisfied (a
        resource is frozen or out of capacity). The whole decision +
        bookkeeping is one atomic ``BEGIN IMMEDIATE`` transaction: either
        every claim gets a lease row and the job flips to ``leased``, or
        nothing is written.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            granted = self._try_grant_locked(conn, job, owner, lease_seconds)
            if granted is None:
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
            return granted
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def release(
        self,
        job_id: str,
        lease_token: str,
        final_status: str = SUCCEEDED,
    ) -> Job | None:
        """Release a job's lease and stamp a terminal status.

        Validates that ``lease_token`` matches the stored job and that the
        job is still in an active leased/running state. Deletes every lease
        row owned by the job (so capacity frees up immediately for the next
        waiter) and records a ``job.released`` event.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            job = self.store._row_to_job(row)
            if job.lease_token != lease_token or job.status not in _LEASED_STATES:
                conn.execute("ROLLBACK")
                return None
            now = utc_now()
            conn.execute("DELETE FROM leases WHERE job_id = ?", (job_id,))
            conn.execute(
                """
                UPDATE jobs
                   SET status = ?, lease_token = '', updated_at = ?
                 WHERE job_id = ?
                """,
                (final_status, now, job_id),
            )
            job.status = final_status
            job.lease_token = ""
            job.updated_at = now
            self.store._record_event_locked(
                conn,
                job_id=job_id,
                event_type="job.released",
                payload={"final_status": final_status},
                now=now,
            )
            conn.execute("COMMIT")
            return job
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def heartbeat(
        self,
        job_id: str,
        lease_token: str,
        lease_seconds: int = 3600,
    ) -> Job | None:
        """Push the lease expiry out by ``lease_seconds`` and stamp heartbeat.

        Updates every lease row owned by the job plus the job row itself.
        A missing token or a job no longer in a leased/running state is a
        no-op returning ``None`` (the worker's lease has already lapsed or
        been released; it cannot be revived).
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            job = self.store._row_to_job(row)
            if job.lease_token != lease_token or job.status not in _LEASED_STATES:
                conn.execute("ROLLBACK")
                return None
            now = utc_now()
            # Compute expiry as an ISO-8601 string to match the column type
            # used everywhere else in the kernel (leases.expires_at is TEXT).
            expires_at = (
                _parse_iso(now) + timedelta(seconds=int(lease_seconds))
            ).replace(microsecond=0).isoformat()
            conn.execute(
                """
                UPDATE leases
                   SET heartbeat_at = ?, expires_at = ?
                 WHERE job_id = ?
                """,
                (now, expires_at, job_id),
            )
            conn.execute(
                "UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ?",
                (now, now, job_id),
            )
            job.heartbeat_at = now
            job.updated_at = now
            conn.execute("COMMIT")
            return job
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def pick_next_runnable(
        self,
        owner: str,
        lease_seconds: int = 3600,
    ) -> Job | None:
        """Worker poll entry: grant the oldest eligible runnable job.

        Walks jobs in ``priority_rank DESC, created_at ASC`` order and tries
        to grant each one's current stage. The first whose claims are all
        satisfiable is leased and returned. Everything happens in one
        ``BEGIN IMMEDIATE`` transaction so a concurrent worker cannot race
        between the "find next" and "grant it" steps.

        v0 does not resolve ticket-DAG dependencies — every job in
        ``submitted`` status is considered runnable. Dependency gating will
        be layered in when the orchestration plane wires stage advancement.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM jobs
                 WHERE status = ?
                 ORDER BY priority_rank DESC, created_at ASC
                """,
                (SUBMITTED,),
            ).fetchall()
            for row in rows:
                job = self.store._row_to_job(row)
                granted = self._try_grant_locked(conn, job, owner, lease_seconds)
                if granted is not None:
                    conn.execute("COMMIT")
                    return granted
            # Nothing runnable — commit the empty transaction (no writes).
            conn.execute("COMMIT")
            return None
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Observability status mutators (do NOT release the lease)
    # ------------------------------------------------------------------
    def mark_unknown(self, job_id: str, reason: str) -> Job | None:
        """Flag a job whose state cannot be determined (e.g. orphan scan)."""
        return self._set_status_no_release(job_id, UNKNOWN, reason, "job.unknown")

    def mark_orphaned(self, job_id: str, reason: str) -> Job | None:
        """Flag a job whose owner process has vanished."""
        return self._set_status_no_release(job_id, ORPHANED, reason, "job.orphaned")

    def mark_suspect(self, job_id: str, reason: str) -> Job | None:
        """Flag a job that may be in a bad state but whose lease is retained."""
        return self._set_status_no_release(job_id, SUSPECT, reason, "job.suspect")

    # ------------------------------------------------------------------
    # Capacity check (exposed for testing / introspection)
    # ------------------------------------------------------------------
    def _resources_available(
        self, conn: sqlite3.Connection, claims: list[ResourceClaim]
    ) -> bool:
        """True iff every claim can be satisfied against current capacity.

        ``shared`` claims never consume capacity (read-only co-tenancy) and
        are always satisfiable unless the resource is frozen. ``exclusive``
        and ``capacity`` claims sum the units of live leases on the resource
        and check ``occupied + units <= capacity``. A resource row may be
        absent (never declared) — it is then treated as a default
        capacity-1 exclusive lock, which matches :meth:`CoordinationStore`'s
        lazy ``_upsert_resource_row`` semantics.
        """
        for claim in claims:
            res = conn.execute(
                "SELECT * FROM resources WHERE resource_key = ?",
                (claim.resource_key,),
            ).fetchone()
            if res is not None and bool(res["frozen"]):
                return False
            if str(claim.mode) == "shared":
                continue
            capacity = int(res["capacity"]) if res is not None else 1
            occupied_row = conn.execute(
                "SELECT COALESCE(SUM(units), 0) AS s FROM leases WHERE resource_key = ?",
                (claim.resource_key,),
            ).fetchone()
            occupied = int(occupied_row["s"]) if occupied_row is not None else 0
            if occupied + int(claim.units) > capacity:
                return False
        return True

    # ------------------------------------------------------------------
    # Internal: the all-or-nothing grant, inside an open write transaction
    # ------------------------------------------------------------------
    def _try_grant_locked(
        self,
        conn: sqlite3.Connection,
        job: Job,
        owner: str,
        lease_seconds: int,
    ) -> Job | None:
        """Grant ``job``'s current stage within the caller's transaction.

        Pre: caller already holds ``BEGIN IMMEDIATE`` and will COMMIT/ROLLBACK.
        On failure (any claim unsatisfiable) returns ``None`` without writing
        — the caller rolls back. On success every claim gets a lease row, the
        job row is stamped ``leased``, and a ``job.leased`` event is recorded;
        the caller commits.
        """
        stage = self._current_stage(job)
        if stage is None:
            return None
        claims = list(stage.claims)
        if not self._resources_available(conn, claims):
            return None

        now = utc_now()
        lease_token = uuid.uuid4().hex
        pid = os.getpid()
        # Capture the granting process's real create_time so the stale-lease
        # scan can later detect PID reuse (a recycled pid has a different
        # create_time). 0.0 means "unknown" → runner degrades to existence
        # check, never a false-dead verdict.
        start_time = _process_create_time(pid) or float(job.owner_start_time or 0.0)
        expires_at = (
            _parse_iso(now) + timedelta(seconds=int(lease_seconds))
        ).replace(microsecond=0).isoformat()

        granted_claims: list[dict[str, Any]] = []
        for claim in claims:
            if str(claim.mode) == "shared":
                # Shared claims don't consume capacity; still record a lease
                # row so the holder is visible in the audit trail (units=0).
                units = 0
            else:
                units = int(claim.units)
            # epoch = max(active-lease epoch, persisted high-water mark) + 1.
            # The high-water mark (resources.last_epoch) survives lease release,
            # so epoch assignment is monotonic across a resource's lifetime — a
            # released lease never causes the next grant to rewind to epoch 1.
            # This lets later observers treat epoch as a strict grant ordering.
            epoch = self._next_epoch_locked(conn, claim.resource_key)
            lease_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO leases (
                  lease_id, job_id, resource_key, epoch, owner, owner_pid,
                  owner_start_time, units, heartbeat_at, created_at,
                  expires_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    job.job_id,
                    claim.resource_key,
                    epoch,
                    owner,
                    pid,
                    start_time,
                    units,
                    now,
                    now,
                    expires_at,
                    "",
                ),
            )
            granted_claims.append(
                {
                    "resource_key": claim.resource_key,
                    "mode": claim.mode,
                    "units": units,
                    "epoch": epoch,
                }
            )

        # The job's own lease_epoch is the max epoch across its claims — a
        # monotonic grant counter useful for orphan scans / ordering.
        job_epoch = max((c["epoch"] for c in granted_claims), default=0)
        conn.execute(
            """
            UPDATE jobs
               SET status = ?, lease_token = ?, lease_epoch = ?, owner = ?,
                   owner_pid = ?, owner_start_time = ?, heartbeat_at = ?,
                   updated_at = ?
             WHERE job_id = ?
            """,
            (
                LEASED,
                lease_token,
                job_epoch,
                owner,
                pid,
                start_time,
                now,
                now,
                job.job_id,
            ),
        )
        # Reflect the grant on the in-memory object the caller sees.
        job.status = LEASED
        job.lease_token = lease_token
        job.lease_epoch = job_epoch
        job.owner = owner
        job.owner_pid = pid
        job.owner_start_time = start_time
        job.heartbeat_at = now
        job.updated_at = now

        self.store._record_event_locked(
            conn,
            job_id=job.job_id,
            event_type="job.leased",
            payload={
                "owner": owner,
                "lease_token": lease_token,
                "lease_seconds": int(lease_seconds),
                "granted_claims": granted_claims,
            },
            now=now,
        )
        return job

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _current_stage(job: Job):
        """Return the job's current stage or ``None`` if past the last."""
        if not job.stages:
            return None
        idx = int(job.current_stage_index)
        if idx < 0 or idx >= len(job.stages):
            return None
        return job.stages[idx]

    def _next_epoch_locked(
        self, conn: sqlite3.Connection, resource_key: str
    ) -> int:
        """Next monotonic lease epoch for a resource, then persist the bump.

        epoch = max(active-lease epoch on this resource,
                    persisted high-water mark in resources.last_epoch) + 1.

        The high-water mark is what makes epoch survive release: once a lease
        is deleted its row no longer contributes to ``MAX(epoch)``, but the
        resource's ``last_epoch`` keeps the counter advancing so the *next*
        grant gets a strictly higher epoch than any prior grant. We both read
        and bump the mark here, in the caller's already-open transaction, so
        concurrent grants on the same resource serialize on ``BEGIN IMMEDIATE``
        and never hand out the same epoch.

        Returns the assigned epoch (>= 1).
        """
        # Ensure the resources row exists so the UPDATE below hits a row.
        self.store._upsert_resource_row(conn, resource_key)
        row = conn.execute(
            """
            SELECT COALESCE(MAX(l.epoch), 0) AS lease_max,
                   COALESCE(r.last_epoch, 0) AS res_max
              FROM resources r
              LEFT JOIN leases l ON l.resource_key = r.resource_key
             WHERE r.resource_key = ?
            """,
            (resource_key,),
        ).fetchone()
        lease_max = int(row["lease_max"]) if row is not None else 0
        res_max = int(row["res_max"]) if row is not None else 0
        epoch = max(lease_max, res_max) + 1
        conn.execute(
            "UPDATE resources SET last_epoch = ?, updated_at = ? WHERE resource_key = ?",
            (epoch, utc_now(), resource_key),
        )
        return epoch

    def _set_status_no_release(
        self,
        job_id: str,
        status: str,
        reason: str,
        event_type: str,
    ) -> Job | None:
        """Stamp an observability status without touching the lease.

        ``unknown`` / ``orphaned`` / ``suspect`` are advisory flags: they
        mark a job for human/operator attention but do not release its
        resources (the owner may still be alive and able to finish). The
        lease rows are intentionally left in place.

        Guard: the stamp only applies while the job is still in a lease-live
        status (``leased`` / ``running``). A job that was released / failed /
        succeeded between the stale scan's snapshot read and this call is left
        untouched — we never clobber a terminal state with an advisory flag.
        This closes the TOCTOU window where the scan reads "stale" but the
        owner releases (succeeded) in the gap.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            job = self.store._row_to_job(row)
            # Only stamp lease-live jobs. A terminal/unknown job here means the
            # owner (or another scan) already resolved it — don't clobber.
            if job.status not in _LEASED_STATES:
                conn.execute("ROLLBACK")
                return None
            now = utc_now()
            # Guarded UPDATE: WHERE status IN _LEASED_STATES makes this a no-op
            # if a concurrent transaction flipped the job to terminal between
            # the SELECT above and now (defensive; same BEGIN IMMEDIATE window).
            cur = conn.execute(
                "UPDATE jobs SET status = ?, reason = ?, updated_at = ? "
                "WHERE job_id = ? AND status IN (?, ?)",
                (status, reason, now, job_id, LEASED, RUNNING),
            )
            if cur.rowcount == 0:
                # Race lost — job is no longer lease-live. Return current state.
                conn.execute("ROLLBACK")
                fresh = conn.execute(
                    "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                return self.store._row_to_job(fresh) if fresh is not None else None
            job.status = status
            job.reason = reason
            job.updated_at = now
            self.store._record_event_locked(
                conn,
                job_id=job_id,
                event_type=event_type,
                payload={"reason": reason, "status": status},
                now=now,
            )
            conn.execute("COMMIT")
            return job
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Module-private helpers
# ---------------------------------------------------------------------------


def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp from :func:`utc_now` into a datetime.

    ``utc_now`` emits second-precision ``datetime.isoformat()`` output with a
    trailing ``+00:00`` offset, which ``datetime.fromisoformat`` accepts
    directly on Python 3.11+ (and 3.7+ for the no-microsecond case).
    """
    return datetime.fromisoformat(ts)
