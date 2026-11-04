"""SQLite persistence layer for the coordination kernel.

This is the truth source for the resource-scheduling kernel: jobs, resources,
leases, events and the resource freeze log. It is deliberately a generic
scheduling substrate (no knowledge of agents / ralph / tickets) so that the
orchestration plane can compose on top of it.

Design contract (pinned to ``docs/architecture.md``):

* Single database at ``~/.baton/coordination/queue.db`` (global, cross-project).
  Do not confuse with ``plan_store.py``'s per-project queue.db.
* All writes go through ``BEGIN IMMEDIATE`` so concurrent workers serialize on
  the database file — no last-writer-wins clobbering of lease / status state.
* All timestamps are produced via :func:`orchestration.plan_model.utc_now`
  (UTC, second-precision, ISO-8601) — never ``time.time()`` directly.
* Schema is created lazily and idempotently via ``_ensure_schema``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from orchestration.plan_model import utc_now

from coordination.model import (
    Job,
    ResourceDef,
    StageSpec,
)

# Default global location for the coordination database. Cross-project by
# design — every Baton-driven workspace on this machine shares one queue.
_DEFAULT_DB_PATH: Path = Path.home() / ".baton" / "coordination" / "queue.db"


class CoordinationStore:
    """Persistent truth source for the coordination kernel.

    Every mutating method opens its own short-lived connection wrapped in a
    ``BEGIN IMMEDIATE`` transaction. This keeps the lock window tight (one
    statement group) while still guaranteeing that two concurrent workers can
    never both observe the same pre-state and write conflicting updates.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path: Path = Path(db_path) if db_path is not None else _DEFAULT_DB_PATH

    # ------------------------------------------------------------------
    # Connection / schema bootstrap
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """Open a connection, ensure parent dirs + schema, set row factory."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        self._ensure_schema(conn)
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create all five tables + indexes if they don't yet exist.

        Idempotent — safe to call on every connection. The DDL below is the
        authoritative schema; field/column names match the design doc verbatim.
        """
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS jobs (
              job_id TEXT PRIMARY KEY,
              idempotency_key TEXT NOT NULL,
              submitter TEXT NOT NULL,
              project TEXT NOT NULL,
              feature TEXT NOT NULL,
              priority TEXT NOT NULL,
              priority_rank INTEGER NOT NULL,
              status TEXT NOT NULL,
              stages_json TEXT NOT NULL,
              current_stage_index INTEGER NOT NULL DEFAULT 0,
              lease_token TEXT NOT NULL DEFAULT '',
              lease_epoch INTEGER NOT NULL DEFAULT 0,
              owner TEXT NOT NULL DEFAULT '',
              owner_pid INTEGER NOT NULL DEFAULT 0,
              owner_start_time REAL NOT NULL DEFAULT 0,
              heartbeat_at TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              reason TEXT NOT NULL DEFAULT '',
              UNIQUE(idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority_rank DESC, created_at);
            CREATE INDEX IF NOT EXISTS idx_jobs_project_feature ON jobs(project, feature);

            CREATE TABLE IF NOT EXISTS resources (
              resource_key TEXT PRIMARY KEY,
              capacity INTEGER NOT NULL DEFAULT 1,
              mode TEXT NOT NULL DEFAULT 'exclusive',
              frozen INTEGER NOT NULL DEFAULT 0,
              frozen_reason TEXT NOT NULL DEFAULT '',
              last_epoch INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS leases (
              lease_id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL,
              resource_key TEXT NOT NULL,
              epoch INTEGER NOT NULL,
              owner TEXT NOT NULL,
              owner_pid INTEGER NOT NULL,
              owner_start_time REAL NOT NULL,
              units INTEGER NOT NULL DEFAULT 1,
              heartbeat_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              reason TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_leases_job ON leases(job_id);
            CREATE INDEX IF NOT EXISTS idx_leases_resource ON leases(resource_key);

            CREATE TABLE IF NOT EXISTS events (
              event_id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL DEFAULT '',
              event_type TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, created_at);

            CREATE TABLE IF NOT EXISTS freeze_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              resource_key TEXT NOT NULL,
              action TEXT NOT NULL,
              reason TEXT NOT NULL,
              operator TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            """
        )
        # Forward migration for databases created before ``resources.last_epoch``
        # existed. ``last_epoch`` is the per-resource high-water mark of granted
        # lease epochs — it survives lease release so epoch assignment stays
        # monotonic across a resource's lifetime (a released lease must not
        # cause the next grant to rewind to epoch 1). Inspect the live columns
        # rather than relying on the table existing, since CREATE TABLE IF NOT
        # EXISTS will have skipped a table that predates this column.
        cols = {
            str(r[1])
            for r in conn.execute("PRAGMA table_info(resources)").fetchall()
        }
        if "last_epoch" not in cols:
            conn.execute(
                "ALTER TABLE resources ADD COLUMN last_epoch INTEGER NOT NULL DEFAULT 0"
            )

    # ------------------------------------------------------------------
    # Job CRUD
    # ------------------------------------------------------------------
    def submit_job(self, job: Job) -> Job:
        """Persist a new job, honoring idempotency.

        If a job with the same ``idempotency_key`` already exists, the
        existing row is returned untouched (no error, no clobber). Otherwise
        the job is inserted and a ``job.submitted`` event is recorded.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?",
                (job.idempotency_key,),
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                return self._row_to_job(existing)
            now = utc_now()
            if not job.created_at:
                job.created_at = now
            job.updated_at = now
            conn.execute(
                """
                INSERT INTO jobs (
                  job_id, idempotency_key, submitter, project, feature,
                  priority, priority_rank, status, stages_json,
                  current_stage_index, lease_token, lease_epoch, owner,
                  owner_pid, owner_start_time, heartbeat_at,
                  created_at, updated_at, reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.idempotency_key,
                    job.submitter,
                    job.project,
                    job.feature,
                    job.priority,
                    int(job.priority_rank),
                    job.status,
                    json.dumps([s.to_dict() for s in job.stages]),
                    int(job.current_stage_index),
                    job.lease_token,
                    int(job.lease_epoch),
                    job.owner,
                    int(job.owner_pid),
                    float(job.owner_start_time),
                    job.heartbeat_at,
                    job.created_at,
                    job.updated_at,
                    job.reason,
                ),
            )
            self._record_event_locked(
                conn,
                job_id=job.job_id,
                event_type="job.submitted",
                payload={"priority": job.priority, "submitter": job.submitter},
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

    def get_job(self, job_id: str) -> Job | None:
        """Return the job with ``job_id`` or ``None`` if not present."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            return self._row_to_job(row) if row is not None else None
        finally:
            conn.close()

    def list_jobs(self, status: str | None = None) -> list[Job]:
        """List jobs, optionally filtered by status.

        Ordered so the scheduler's natural dequeue order falls out for free:
        highest priority_rank first, then FIFO by created_at.
        """
        conn = self._connect()
        try:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY priority_rank DESC, created_at ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY priority_rank DESC, created_at ASC",
                    (status,),
                ).fetchall()
            return [self._row_to_job(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Resources: freeze / unfreeze
    # ------------------------------------------------------------------
    def _upsert_resource_row(
        self,
        conn: sqlite3.Connection,
        resource_key: str,
        mode: str = "exclusive",
        capacity: int = 1,
    ) -> None:
        """Lazily create the resources row if missing (default unfrozen)."""
        now = utc_now()
        conn.execute(
            """
            INSERT INTO resources (resource_key, capacity, mode, frozen, frozen_reason, created_at, updated_at)
            VALUES (?, ?, ?, 0, '', ?, ?)
            ON CONFLICT(resource_key) DO NOTHING
            """,
            (resource_key, int(capacity), mode, now, now),
        )

    def freeze_resource(self, resource_key: str, reason: str, operator: str) -> None:
        """Freeze a resource (no new leases may be granted against it).

        Records the action in ``freeze_log`` and emits a ``resource.frozen``
        event. Creates the resources row lazily so callers don't have to
        pre-declare a resource to freeze it.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._upsert_resource_row(conn, resource_key)
            now = utc_now()
            conn.execute(
                "UPDATE resources SET frozen = 1, frozen_reason = ?, updated_at = ? WHERE resource_key = ?",
                (reason, now, resource_key),
            )
            conn.execute(
                """
                INSERT INTO freeze_log (resource_key, action, reason, operator, created_at)
                VALUES (?, 'freeze', ?, ?, ?)
                """,
                (resource_key, reason, operator, now),
            )
            self._record_event_locked(
                conn,
                job_id="",
                event_type="resource.frozen",
                payload={"resource_key": resource_key, "reason": reason, "operator": operator},
                now=now,
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def unfreeze_resource(self, resource_key: str, operator: str) -> None:
        """Reverse of :meth:`freeze_resource`. Records ``resource.unfrozen``."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._upsert_resource_row(conn, resource_key)
            now = utc_now()
            conn.execute(
                "UPDATE resources SET frozen = 0, frozen_reason = '', updated_at = ? WHERE resource_key = ?",
                (now, resource_key),
            )
            conn.execute(
                """
                INSERT INTO freeze_log (resource_key, action, reason, operator, created_at)
                VALUES (?, 'unfreeze', ?, ?, ?)
                """,
                (resource_key, "", operator, now),
            )
            self._record_event_locked(
                conn,
                job_id="",
                event_type="resource.unfrozen",
                payload={"resource_key": resource_key, "operator": operator},
                now=now,
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def is_frozen(self, resource_key: str) -> bool:
        """True if the resource is currently frozen. Missing resource → False."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT frozen FROM resources WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            return bool(row["frozen"]) if row is not None else False
        finally:
            conn.close()

    def declare_resource(
        self,
        resource_key: str,
        capacity: int | None = None,
        mode: str | None = None,
        reason: str = "",
        operator: str = "",
    ) -> ResourceDef:
        """Explicitly declare (or re-declare) a resource's capacity / mode.

        Unlike the lazy ``_upsert_resource_row`` (which only fills in defaults
        for a missing row), this is the deliberate "this flow's shared
        resource exists and holds N units" statement — the annotation half of
        the concurrency-blocking-item protocol. Existing values are left
        untouched for parameters that are ``None`` so a re-declare can change
        just the capacity. Records a ``declare`` row in ``freeze_log`` and a
        ``resource.declared`` event.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._upsert_resource_row(conn, resource_key)
            now = utc_now()
            if capacity is not None or mode is not None:
                if capacity is not None:
                    conn.execute(
                        "UPDATE resources SET capacity = ?, updated_at = ? WHERE resource_key = ?",
                        (int(capacity), now, resource_key),
                    )
                if mode is not None:
                    conn.execute(
                        "UPDATE resources SET mode = ?, updated_at = ? WHERE resource_key = ?",
                        (mode, now, resource_key),
                    )
            conn.execute(
                """
                INSERT INTO freeze_log (resource_key, action, reason, operator, created_at)
                VALUES (?, 'declare', ?, ?, ?)
                """,
                (resource_key, reason, operator, now),
            )
            self._record_event_locked(
                conn,
                job_id="",
                event_type="resource.declared",
                payload={
                    "resource_key": resource_key,
                    "capacity": capacity,
                    "mode": mode,
                    "reason": reason,
                    "operator": operator,
                },
                now=now,
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        res = self.get_resource(resource_key)
        assert res is not None  # just upserted
        return res

    def list_live_leases(self, resource_keys: list[str]) -> list[dict]:
        """Unexpired lease rows for the given resources (read-only query).

        A crashed holder's lease row lingers past its process death until the
        TTL expires / reap cleans it — expiry is therefore the truth for
        "is anything still holding", not row existence.
        """
        if not resource_keys:
            return []
        conn = self._connect()
        try:
            placeholders = ",".join("?" for _ in resource_keys)
            rows = conn.execute(
                f"""
                SELECT * FROM leases
                 WHERE resource_key IN ({placeholders})
                   AND expires_at > ?
                 ORDER BY resource_key, epoch DESC
                """,
                (*resource_keys, utc_now()),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def record_resource_event(self, event_type: str, payload: dict) -> None:
        """Append a resource-scoped audit event (job_id '')."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._record_event_locked(
                conn, job_id="", event_type=event_type, payload=payload
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_resource(self, resource_key: str) -> ResourceDef | None:
        """Return the resource definition or ``None`` if undeclared."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM resources WHERE resource_key = ?",
                (resource_key,),
            ).fetchone()
            if row is None:
                return None
            return ResourceDef(
                resource_key=row["resource_key"],
                capacity=int(row["capacity"]),
                mode=str(row["mode"]),
                frozen=bool(row["frozen"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
            )
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------
    def record_event(
        self, job_id: str, event_type: str, payload: dict[str, Any]
    ) -> str:
        """Append an event row. Returns the generated ``event_id``.

        Events are append-only and never updated — they form the audit log.
        Callers outside a transaction may use this; for writes that should be
        atomic with their event (e.g. submit_job) use the private
        ``_record_event_locked`` helper inside an already-open ``BEGIN
        IMMEDIATE`` block.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            event_id = self._record_event_locked(
                conn, job_id=job_id, event_type=event_type, payload=payload
            )
            conn.execute("COMMIT")
            return event_id
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _record_event_locked(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        event_type: str,
        payload: dict[str, Any],
        now: str | None = None,
    ) -> str:
        """Insert an event row inside an already-held write transaction."""
        event_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO events (event_id, job_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_id,
                job_id,
                event_type,
                json.dumps(payload, default=str),
                now or utc_now(),
            ),
        )
        return event_id

    def list_events(
        self,
        job_id: str = "",
        event_type: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query the event log with optional job_id / event_type filters.

        Returns rows newest-first (most recent event at index 0), each as a
        dict with ``event_id`` / ``job_id`` / ``event_type`` / ``payload`` /
        ``created_at``.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if job_id:
            clauses.append("job_id = ?")
            params.append(job_id)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            f"SELECT * FROM events{where} ORDER BY created_at DESC, event_id DESC LIMIT ?"
        )
        params.append(int(limit))
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            return [
                {
                    "event_id": row["event_id"],
                    "job_id": row["job_id"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Row → dataclass
    # ------------------------------------------------------------------
    def _row_to_job(self, row: sqlite3.Row) -> Job:
        """Materialize a ``Job`` dataclass from a SQLite row.

        ``stages_json`` is parsed back into :class:`StageSpec` objects. The
        stored ``priority_rank`` is carried through but Job.__post_init__ will
        reconcile it against ``priority`` (bucket always wins on conflict).
        """
        raw_stages = json.loads(row["stages_json"]) if row["stages_json"] else []
        stages = [StageSpec.from_dict(s) for s in raw_stages]
        return Job(
            job_id=str(row["job_id"]),
            idempotency_key=str(row["idempotency_key"]),
            submitter=str(row["submitter"]),
            project=str(row["project"]),
            feature=str(row["feature"]),
            priority=str(row["priority"]),
            priority_rank=int(row["priority_rank"]),
            status=str(row["status"]),
            stages=stages,
            current_stage_index=int(row["current_stage_index"]),
            lease_token=str(row["lease_token"]),
            lease_epoch=int(row["lease_epoch"]),
            owner=str(row["owner"]),
            owner_pid=int(row["owner_pid"]),
            owner_start_time=float(row["owner_start_time"]),
            heartbeat_at=str(row["heartbeat_at"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            reason=str(row["reason"]),
        )
