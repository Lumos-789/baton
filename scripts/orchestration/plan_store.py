"""Versioned local storage for Baton live plans."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from _frontier import compute_frontier, topo_sort
from _ticket import (
    Ticket,
    TasksMarkdownError,
    check_task,
    parse_tasks_md,
    validate_change_id,
)

from orchestration.plan_model import (
    AssignmentSnapshot,
    PlanRevision,
    PlanTicket,
    stable_hash,
    utc_now,
)
from orchestration.result import ResultEnvelope
from orchestration.session_model import ExecutionAttempt


class PlanStoreError(RuntimeError):
    """Base class for plan-store failures."""






class QueueTaskNotFoundError(PlanStoreError):
    """Raised when a queue task id is unknown."""


class QueueLeaseError(PlanStoreError):
    """Raised when a queue lease cannot be updated or released."""


class ExecutionAttemptError(PlanStoreError):
    """Raised when a session execution attempt transition is invalid."""


PRIORITY_RANK = {
    "normal": 10,
    "high": 20,
    "emergency": 30,
}


def _pretty_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (write-temp + os.replace).

    A crash mid-write (SIGKILL, disk full, power loss) must not leave a
    truncated file at ``path`` — readers would hit JSONDecodeError with no
    recovery path. We write to a sibling temp file then ``os.replace`` (POSIX
    atomic rename) so ``path`` is either the old content or the complete new
    content, never a partial write. ``fsync`` before rename persists the bytes
    so a crash after rename still has the file on disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            # fsync may be unavailable/forbidden on some filesystems; the
            # rename is still atomic and the OS page cache is good enough in
            # the common case. Don't let fsync failure abort the write.
            pass
    os.replace(tmp, path)


def _utc_after(seconds: int) -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        + timedelta(seconds=seconds)
    ).isoformat()


class PlanStore:
    """Persist plan revisions, assignments, and audit events.

    The plan face is an OpenSpec change: ``openspec/changes/<feature>/tasks.md``
    is re-read on every sync — edits (including baton's own ``[x]`` write-backs)
    produce a new plan revision. `queue.db` lives at
    `<project>/.dsh/baton/queue.db`; runtime snapshots live under
    `<project>/.dsh/baton/<feature>/`.
    """

    def __init__(self, project_path: str | Path, feature: str):
        self.project_path = Path(project_path).resolve()
        try:
            self.feature = validate_change_id(feature)
        except ValueError as exc:
            raise PlanStoreError(str(exc)) from exc
        self.job_id = self.feature
        self.baton_dir = self.project_path / ".dsh" / "baton"
        self.feature_dir = self.baton_dir / self.feature
        self.change_dir = self.project_path / "openspec" / "changes" / self.feature
        self.tasks_path = self.change_dir / "tasks.md"
        self.results_dir = self.feature_dir / "results"
        self.plan_path = self.feature_dir / "plan.json"
        self.db_path = self.baton_dir / "queue.db"

    def ensure_initial_plan(self, approved_by: str = "baton") -> PlanRevision:
        """Create revision 1 from existing Markdown tickets if needed."""
        self._ensure_dirs()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = self._ensure_initial_plan_locked(conn, approved_by)
            conn.commit()
        finally:
            conn.close()

        self._write_plan_json(plan)
        return plan

    def current_plan(self) -> PlanRevision:
        """Load the latest approved plan revision with current assignments."""
        self.ensure_initial_plan()
        conn = self._connect()
        try:
            plan = self._load_current_plan_locked(conn)
            if plan is None:
                raise PlanStoreError("initial plan was not created")
            return plan
        finally:
            conn.close()



    def plan_history(self) -> list[PlanRevision]:
        self.ensure_initial_plan()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT snapshot_json FROM plan_revisions "
                "WHERE job_id = ? ORDER BY revision",
                (self.job_id,),
            ).fetchall()
            return [
                # Historical snapshots use each revision's assignments as they
                # were at its approval time, without injecting the current live
                # assignments (otherwise every historical revision would show
                # the same current snapshot, distorting audit/replay).
                # live_assignments=False makes _plan_from_snapshot_json use the
                # assignments baked into snapshot_json directly.
                self._plan_from_snapshot_json(
                    row["snapshot_json"], conn, live_assignments=False
                )
                for row in rows
            ]
        finally:
            conn.close()

    def record_assignment(self, assignment: AssignmentSnapshot) -> None:
        """Record runtime assignment state without creating a semantic revision."""
        self.ensure_initial_plan()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._record_assignment_locked(conn, assignment)
            conn.commit()
        finally:
            conn.close()
        self._write_plan_json(self.current_plan())

    def claim_ready_assignments(
        self,
        *,
        session_id: str,
        capacity: int,
        base_branch: str = "main",
        base_sha: str = "",
        lease_seconds: int = 7200,
    ) -> list[ExecutionAttempt]:
        """Atomically claim up to ``capacity`` distinct ready tickets.

        This is the session-subagent claim primitive. It never claims the whole
        frontier pre-emptively and never uses a shared workspace resource. Each
        returned attempt receives a one-time claim token; only its hash is
        persisted.
        """
        if not session_id.strip():
            raise ValueError("session_id is required")
        if capacity < 1 or capacity > 5:
            raise ValueError("capacity must be between 1 and 5")

        conn = self._connect()
        claimed: list[ExecutionAttempt] = []
        touched_files: list[Path] = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = self._ensure_initial_plan_locked(conn)
            self._expire_execution_attempts_locked(conn)
            active_states = ("leased", "spawned", "running", "submitted", "verifying", "integrating", "blocked-review")
            active_rows = conn.execute(
                "SELECT ticket_num FROM execution_attempts "
                "WHERE job_id = ? AND state IN (?, ?, ?, ?, ?, ?, ?)",
                (self.job_id, *active_states),
            ).fetchall()
            active_tickets = {int(row["ticket_num"]) for row in active_rows}
            available = max(0, capacity - len(active_rows))
            if available == 0:
                conn.commit()
                return []

            ticket_files = {
                ticket.num: ticket for ticket in self._read_source_tickets()
            }
            resolved = {
                num for num, ticket in ticket_files.items() if ticket.status == "resolved"
            }
            result_rows = conn.execute(
                "SELECT ticket_num FROM result_envelopes "
                "WHERE job_id = ? AND status = 'succeeded'",
                (self.job_id,),
            ).fetchall()
            resolved.update(int(row["ticket_num"]) for row in result_rows)

            rank = {"emergency": 30, "high": 20, "normal": 10}
            eligible = [
                ticket
                for ticket in plan.tickets
                if ticket.required
                and ticket.num not in active_tickets
                and ticket.num not in resolved
                and ticket_files.get(ticket.num) is not None
                and ticket_files[ticket.num].status == "ready"
                and all(blocker in resolved for blocker in ticket.blocked_by)
            ]
            eligible.sort(key=lambda ticket: (-rank.get(ticket.priority, 10), ticket.num))

            for ticket in eligible:
                if len(claimed) >= available:
                    break
                resources = list(dict.fromkeys(ticket.resources))
                if self._attempt_resources_locked(conn, resources):
                    continue
                row = conn.execute(
                    "SELECT COALESCE(MAX(attempt), 0) AS n FROM execution_attempts "
                    "WHERE job_id = ? AND ticket_num = ?",
                    (self.job_id, ticket.num),
                ).fetchone()
                attempt_no = int(row["n"]) + 1
                assignment_id = (
                    f"ASG-{self.feature}-{ticket.num:03d}-r{plan.revision}-a{attempt_no}"
                )
                claim_token = uuid.uuid4().hex
                claim_token_hash = self._claim_token_hash(claim_token)
                packet_hash = stable_hash(
                    {
                        "planRevision": plan.revision,
                        "ticket": ticket.to_dict(),
                        "baseBranch": base_branch,
                        "baseSha": base_sha,
                    }
                )
                now = utc_now()
                expires_at = _utc_after(lease_seconds)
                run_id = f"RUN-{self.feature}"
                conn.execute(
                    "INSERT INTO execution_attempts "
                    "(job_id, run_id, assignment_id, ticket_num, attempt, state, "
                    "session_id, agent_id, plan_revision, task_packet_hash, "
                    "claim_token_hash, lease_expires_at, base_branch, base_sha, "
                    "worktree_path, worktree_branch, integration_path, "
                    "integration_branch, packet_path, receipt_path, commit_sha, "
                    "integration_sha, failure_code, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'leased', ?, '', ?, ?, ?, ?, ?, ?, "
                    "'', '', '', '', '', '', '', '', '', ?, ?)",
                    (
                        self.job_id, run_id, assignment_id, ticket.num, attempt_no,
                        session_id, plan.revision, packet_hash, claim_token_hash,
                        expires_at, base_branch, base_sha, now, now,
                    ),
                )
                for resource in resources:
                    conn.execute(
                        "INSERT INTO attempt_resource_leases "
                        "(resource_key, assignment_id, job_id, claim_token_hash, "
                        "session_id, expires_at, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            resource, assignment_id, self.job_id, claim_token_hash,
                            session_id, expires_at, now, now,
                        ),
                    )
                self._record_assignment_locked(
                    conn,
                    AssignmentSnapshot(
                        assignment_id=assignment_id,
                        ticket_num=ticket.num,
                        state="leased",
                        plan_revision=plan.revision,
                        task_packet_hash=packet_hash,
                        profile=ticket.agent_profile,
                        resources=resources,
                    ),
                )
                self._event_locked(
                    conn,
                    "execution.attempt.claimed",
                    {
                        "assignmentId": assignment_id,
                        "ticket": ticket.num,
                        "attempt": attempt_no,
                        "sessionId": session_id,
                        "planRevision": plan.revision,
                        "taskPacketHash": packet_hash,
                        "resources": resources,
                        "leaseExpiresAt": expires_at,
                    },
                )
                claimed.append(
                    ExecutionAttempt(
                        assignment_id=assignment_id,
                        run_id=run_id,
                        ticket_num=ticket.num,
                        attempt=attempt_no,
                        state="leased",
                        session_id=session_id,
                        plan_revision=plan.revision,
                        task_packet_hash=packet_hash,
                        claim_token=claim_token,
                        lease_expires_at=expires_at,
                        base_branch=base_branch,
                        base_sha=base_sha,
                    )
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if claimed:
            self._write_plan_json(self.current_plan())
        return claimed

    def list_execution_attempts(self) -> list[ExecutionAttempt]:
        self.ensure_initial_plan()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM execution_attempts WHERE job_id = ? "
                "ORDER BY ticket_num, attempt",
                (self.job_id,),
            ).fetchall()
            return [self._execution_attempt_from_row(row) for row in rows]
        finally:
            conn.close()

    def get_execution_attempt(self, assignment_id: str) -> ExecutionAttempt:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM execution_attempts WHERE job_id = ? AND assignment_id = ?",
                (self.job_id, assignment_id),
            ).fetchone()
            if row is None:
                raise ExecutionAttemptError(f"unknown assignment: {assignment_id}")
            return self._execution_attempt_from_row(row)
        finally:
            conn.close()

    def bind_attempt_agent(
        self, assignment_id: str, claim_token: str, agent_id: str
    ) -> ExecutionAttempt:
        if not agent_id.strip():
            raise ValueError("agent_id is required")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load_execution_attempt_locked(conn, assignment_id)
            self._require_claim_token(row, claim_token)
            if row["state"] not in {"leased", "spawned"}:
                raise ExecutionAttemptError(
                    f"cannot bind agent while {assignment_id} is {row['state']}"
                )
            if row["state"] == "spawned":
                if row["agent_id"] != agent_id:
                    raise ExecutionAttemptError(
                        f"assignment {assignment_id} is already bound to {row['agent_id']}"
                    )
                conn.commit()
                return self.get_execution_attempt(assignment_id)
            now = utc_now()
            conn.execute(
                "UPDATE execution_attempts SET state = 'spawned', agent_id = ?, "
                "updated_at = ? WHERE job_id = ? AND assignment_id = ?",
                (agent_id, now, self.job_id, assignment_id),
            )
            self._event_locked(
                conn,
                "execution.agent.bound",
                {"assignmentId": assignment_id, "agentId": agent_id},
            )
            conn.commit()
            return self.get_execution_attempt(assignment_id)
        finally:
            conn.close()

    def heartbeat_execution_attempt(
        self, assignment_id: str, claim_token: str, lease_seconds: int = 7200
    ) -> ExecutionAttempt:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load_execution_attempt_locked(conn, assignment_id)
            self._require_claim_token(row, claim_token)
            if row["state"] in {"succeeded", "failed", "canceled"}:
                raise ExecutionAttemptError(f"attempt is terminal: {assignment_id}")
            expires_at = _utc_after(lease_seconds)
            now = utc_now()
            conn.execute(
                "UPDATE execution_attempts SET lease_expires_at = ?, updated_at = ? "
                "WHERE job_id = ? AND assignment_id = ?",
                (expires_at, now, self.job_id, assignment_id),
            )
            conn.execute(
                "UPDATE attempt_resource_leases SET expires_at = ?, updated_at = ? "
                "WHERE job_id = ? AND assignment_id = ?",
                (expires_at, now, self.job_id, assignment_id),
            )
            conn.commit()
            return self.get_execution_attempt(assignment_id)
        finally:
            conn.close()

    def configure_execution_attempt(
        self,
        assignment_id: str,
        claim_token: str,
        *,
        task_packet_hash: str,
        worktree_path: str,
        worktree_branch: str,
        integration_path: str,
        integration_branch: str,
        packet_path: str,
        receipt_path: str,
    ) -> ExecutionAttempt:
        """Persist the immutable packet and workspace coordinates for a claim."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load_execution_attempt_locked(conn, assignment_id)
            self._require_claim_token(row, claim_token)
            if row["state"] not in {"leased", "spawned"}:
                raise ExecutionAttemptError(
                    f"cannot configure {assignment_id} while it is {row['state']}"
                )
            now = utc_now()
            conn.execute(
                "UPDATE execution_attempts SET task_packet_hash = ?, "
                "worktree_path = ?, worktree_branch = ?, integration_path = ?, "
                "integration_branch = ?, packet_path = ?, receipt_path = ?, "
                "updated_at = ? WHERE job_id = ? AND assignment_id = ?",
                (
                    task_packet_hash,
                    worktree_path,
                    worktree_branch,
                    integration_path,
                    integration_branch,
                    packet_path,
                    receipt_path,
                    now,
                    self.job_id,
                    assignment_id,
                ),
            )
            conn.execute(
                "UPDATE assignments SET task_packet_hash = ?, updated_at = ? "
                "WHERE job_id = ? AND assignment_id = ?",
                (task_packet_hash, now, self.job_id, assignment_id),
            )
            self._event_locked(
                conn,
                "execution.attempt.configured",
                {
                    "assignmentId": assignment_id,
                    "taskPacketHash": task_packet_hash,
                    "worktreePath": worktree_path,
                    "packetPath": packet_path,
                },
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_execution_attempt(assignment_id)

    def mark_execution_attempt_state(
        self,
        assignment_id: str,
        claim_token: str,
        state: str,
        *,
        failure_code: str = "",
        release_lease: bool = False,
    ) -> ExecutionAttempt:
        """Move an attempt to a controller-owned lifecycle state."""
        allowed = {"running", "submitted", "verifying", "integrating", "blocked-review", "unknown"}
        if state not in allowed:
            raise ValueError(f"unsupported execution attempt state: {state}")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load_execution_attempt_locked(conn, assignment_id)
            self._require_claim_token(row, claim_token)
            if row["state"] in {"succeeded", "failed", "canceled"}:
                raise ExecutionAttemptError(f"attempt is terminal: {assignment_id}")
            now = utc_now()
            conn.execute(
                "UPDATE execution_attempts SET state = ?, failure_code = ?, "
                "updated_at = ? WHERE job_id = ? AND assignment_id = ?",
                (state, failure_code, now, self.job_id, assignment_id),
            )
            conn.execute(
                "UPDATE assignments SET state = ?, updated_at = ? "
                "WHERE job_id = ? AND assignment_id = ?",
                (state, now, self.job_id, assignment_id),
            )
            if release_lease:
                conn.execute(
                    "DELETE FROM attempt_resource_leases "
                    "WHERE job_id = ? AND assignment_id = ?",
                    (self.job_id, assignment_id),
                )
            self._event_locked(
                conn,
                f"execution.attempt.{state}",
                {
                    "assignmentId": assignment_id,
                    "failureCode": failure_code,
                    "leaseReleased": release_lease,
                },
            )
            conn.commit()
        finally:
            conn.close()
        return self.get_execution_attempt(assignment_id)

    def finalize_execution_attempt(
        self,
        assignment_id: str,
        claim_token: str,
        envelope: ResultEnvelope,
        integration_sha: str,
    ) -> ExecutionAttempt:
        """Persist one sealed, integrated result and release its attempt lease."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._load_execution_attempt_locked(conn, assignment_id)
            self._require_claim_token(row, claim_token)
            if row["state"] == "succeeded":
                conn.commit()
                return self.get_execution_attempt(assignment_id)
            if envelope.status != "succeeded":
                raise ExecutionAttemptError("only succeeded sealed receipts can finalize")
            expected = {
                "assignment_id": assignment_id,
                "ticket_num": int(row["ticket_num"]),
                "plan_revision": int(row["plan_revision"]),
                "task_packet_hash": str(row["task_packet_hash"]),
                "attempt": int(row["attempt"]),
            }
            for field, value in expected.items():
                if getattr(envelope, field) != value:
                    raise ExecutionAttemptError(
                        f"receipt {field} mismatch for {assignment_id}"
                    )
            now = utc_now()
            envelope.integration_sha = integration_sha
            conn.execute(
                "UPDATE execution_attempts SET state = 'succeeded', commit_sha = ?, "
                "integration_sha = ?, failure_code = '', updated_at = ? "
                "WHERE job_id = ? AND assignment_id = ?",
                (envelope.commit_sha, integration_sha, now, self.job_id, assignment_id),
            )
            conn.execute(
                "DELETE FROM attempt_resource_leases WHERE job_id = ? AND assignment_id = ?",
                (self.job_id, assignment_id),
            )
            conn.execute(
                "UPDATE assignments SET state = 'completed', updated_at = ? "
                "WHERE job_id = ? AND assignment_id = ?",
                (now, self.job_id, assignment_id),
            )
            envelope_id = self._result_envelope_id(envelope)
            conn.execute(
                "INSERT OR REPLACE INTO result_envelopes "
                "(envelope_id, job_id, feature, ticket_num, assignment_id, "
                "plan_revision, task_packet_hash, status, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    envelope_id,
                    self.job_id,
                    self.feature,
                    envelope.ticket_num,
                    envelope.assignment_id,
                    envelope.plan_revision,
                    envelope.task_packet_hash,
                    envelope.status,
                    json.dumps(envelope.to_dict(), ensure_ascii=False, sort_keys=True),
                    envelope.created_at,
                ),
            )
            self._event_locked(
                conn,
                "execution.attempt.succeeded",
                {
                    "assignmentId": assignment_id,
                    "ticket": envelope.ticket_num,
                    "commitSha": envelope.commit_sha,
                    "integrationSha": integration_sha,
                },
            )
            conn.commit()
        finally:
            conn.close()

        try:
            check_task(self.tasks_path, envelope.ticket_num)
        except TasksMarkdownError as exc:
            raise PlanStoreError(str(exc)) from exc
        self._write_result_envelope_json(self._result_envelope_id(envelope), envelope)
        self._write_plan_json(self.current_plan())
        return self.get_execution_attempt(assignment_id)

    def enqueue_frontier(self, ticket_nums: list[int]) -> list[dict[str, Any]]:
        """Add current-revision frontier tickets to the local resource queue."""
        plan = self.current_plan()
        tickets = {ticket.num: ticket for ticket in plan.tickets}
        enqueued: list[dict[str, Any]] = []
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for num in ticket_nums:
                ticket = tickets.get(num)
                if ticket is None or not ticket.required:
                    continue
                task = self._upsert_queue_task_locked(conn, plan, ticket)
                if task is not None:
                    enqueued.append(task)
            if enqueued:
                self._event_locked(
                    conn,
                    "queue.frontier.enqueued",
                    {
                        "revision": plan.revision,
                        "tasks": enqueued,
                    },
                )
            conn.commit()
        finally:
            conn.close()
        return enqueued

    def list_queue(self, status: str | None = None) -> list[dict[str, Any]]:
        """Return queue tasks for the current job."""
        self.ensure_initial_plan()
        conn = self._connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM queue_tasks WHERE job_id = ? AND status = ? "
                    "ORDER BY priority_rank DESC, created_at, queue_id",
                    (self.job_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM queue_tasks WHERE job_id = ? "
                    "ORDER BY priority_rank DESC, created_at, queue_id",
                    (self.job_id,),
                ).fetchall()
            return [self._queue_row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def claim_next_queue_task(
        self,
        owner: str,
        agent_profile: str = "",
        lease_seconds: int = 3600,
    ) -> dict[str, Any] | None:
        """Claim the next queued task whose resources are available."""
        self.ensure_initial_plan()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._delete_expired_leases_locked(conn)
            rows = conn.execute(
                "SELECT * FROM queue_tasks "
                "WHERE job_id = ? AND status = 'queued' "
                "ORDER BY priority_rank DESC, created_at, queue_id",
                (self.job_id,),
            ).fetchall()
            for row in rows:
                task = self._queue_row_to_dict(row, include_leases=False)
                if agent_profile and task["agentProfile"] not in {"", agent_profile}:
                    continue
                resources = task["resources"]
                if self._resources_locked(conn, resources):
                    continue
                token = uuid.uuid4().hex
                expires_at = _utc_after(lease_seconds)
                now = utc_now()
                for resource in resources:
                    conn.execute(
                        "INSERT INTO resource_leases "
                        "(resource_key, queue_id, job_id, lease_token, owner, "
                        "expires_at, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            resource,
                            task["queueId"],
                            self.job_id,
                            token,
                            owner,
                            expires_at,
                            now,
                            now,
                        ),
                    )
                conn.execute(
                    "UPDATE queue_tasks SET status = 'running', owner = ?, "
                    "lease_token = ?, updated_at = ? WHERE queue_id = ?",
                    (owner, token, now, task["queueId"]),
                )
                self._event_locked(
                    conn,
                    "queue.task.claimed",
                    {
                        "queueId": task["queueId"],
                        "ticket": task["ticket"],
                        "owner": owner,
                        "leaseToken": token,
                        "resources": resources,
                        "expiresAt": expires_at,
                    },
                )
                conn.commit()
                task["status"] = "running"
                task["owner"] = owner
                task["leaseToken"] = token
                task["leaseExpiresAt"] = expires_at
                return task
            conn.commit()
            return None
        finally:
            conn.close()

    def heartbeat_queue_task(
        self,
        queue_id: str,
        lease_token: str,
        lease_seconds: int = 3600,
    ) -> dict[str, Any]:
        """Extend a running task's resource leases."""
        self.ensure_initial_plan()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            task = self._load_queue_task_locked(conn, queue_id)
            if task["leaseToken"] != lease_token or task["status"] != "running":
                raise QueueLeaseError(f"invalid lease for queue task {queue_id}")
            expires_at = _utc_after(lease_seconds)
            now = utc_now()
            conn.execute(
                "UPDATE resource_leases SET expires_at = ?, updated_at = ? "
                "WHERE queue_id = ? AND lease_token = ?",
                (expires_at, now, queue_id, lease_token),
            )
            conn.execute(
                "UPDATE queue_tasks SET updated_at = ? WHERE queue_id = ?",
                (now, queue_id),
            )
            self._event_locked(
                conn,
                "queue.task.heartbeat",
                {"queueId": queue_id, "leaseToken": lease_token, "expiresAt": expires_at},
            )
            conn.commit()
            task["leaseExpiresAt"] = expires_at
            return task
        finally:
            conn.close()

    def complete_queue_task(
        self,
        queue_id: str,
        lease_token: str,
        status: str = "completed",
    ) -> dict[str, Any]:
        """Complete a claimed task and release its resource leases."""
        if status not in {"completed", "failed", "canceled"}:
            raise ValueError("status must be completed, failed, or canceled")
        self.ensure_initial_plan()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            task = self._load_queue_task_locked(conn, queue_id)
            if task["leaseToken"] != lease_token:
                raise QueueLeaseError(f"invalid lease for queue task {queue_id}")
            now = utc_now()
            conn.execute(
                "DELETE FROM resource_leases WHERE queue_id = ? AND lease_token = ?",
                (queue_id, lease_token),
            )
            conn.execute(
                "UPDATE queue_tasks SET status = ?, lease_token = '', "
                "updated_at = ? WHERE queue_id = ?",
                (status, now, queue_id),
            )
            self._event_locked(
                conn,
                "queue.task.completed",
                {
                    "queueId": queue_id,
                    "ticket": task["ticket"],
                    "status": status,
                    "releasedResources": task["resources"],
                },
            )
            conn.commit()
            task["status"] = status
            task["leaseToken"] = ""
            return task
        finally:
            conn.close()

    def complete_queue_task_for_ticket(
        self,
        ticket_num: int,
        status: str = "completed",
    ) -> dict[str, Any] | None:
        """Complete a running queue task by ticket number, if one exists."""
        self.ensure_initial_plan()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM queue_tasks "
                "WHERE job_id = ? AND ticket_num = ? AND status = 'running'",
                (self.job_id, ticket_num),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            task = self._queue_row_to_dict(row, include_leases=False)
            now = utc_now()
            conn.execute(
                "DELETE FROM resource_leases WHERE queue_id = ? AND lease_token = ?",
                (task["queueId"], task["leaseToken"]),
            )
            conn.execute(
                "UPDATE queue_tasks SET status = ?, lease_token = '', "
                "updated_at = ? WHERE queue_id = ?",
                (status, now, task["queueId"]),
            )
            self._event_locked(
                conn,
                "queue.task.completed",
                {
                    "queueId": task["queueId"],
                    "ticket": ticket_num,
                    "status": status,
                    "releasedResources": task["resources"],
                },
            )
            conn.commit()
            task["status"] = status
            task["leaseToken"] = ""
            return task
        finally:
            conn.close()

    def record_result_envelope(self, envelope: ResultEnvelope) -> ResultEnvelope:
        """Persist a structured Result Envelope for audit and handoff."""
        self.ensure_initial_plan()
        envelope_id = self._result_envelope_id(envelope)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO result_envelopes "
                "(envelope_id, job_id, feature, ticket_num, assignment_id, "
                "plan_revision, task_packet_hash, status, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    envelope_id,
                    self.job_id,
                    self.feature,
                    envelope.ticket_num,
                    envelope.assignment_id,
                    envelope.plan_revision,
                    envelope.task_packet_hash,
                    envelope.status,
                    json.dumps(envelope.to_dict(), ensure_ascii=False, sort_keys=True),
                    envelope.created_at,
                ),
            )
            self._event_locked(
                conn,
                "result.envelope.recorded",
                {
                    "envelopeId": envelope_id,
                    "ticket": envelope.ticket_num,
                    "assignmentId": envelope.assignment_id,
                    "status": envelope.status,
                    "commitSha": envelope.commit_sha,
                },
            )
            conn.commit()
        finally:
            conn.close()
        self._write_result_envelope_json(envelope_id, envelope)
        return envelope

    def result_envelopes(self) -> list[ResultEnvelope]:
        """Return persisted Result Envelopes for the current job."""
        self.ensure_initial_plan()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT payload_json FROM result_envelopes "
                "WHERE job_id = ? ORDER BY created_at, envelope_id",
                (self.job_id,),
            ).fetchall()
            return [
                ResultEnvelope.from_dict(json.loads(row["payload_json"]))
                for row in rows
            ]
        finally:
            conn.close()

    def latest_completion(self) -> dict[str, Any]:
        """Check completion against the latest approved revision's required tickets."""
        plan = self.current_plan()
        required = {t.num for t in plan.tickets if t.required}
        status_by_num = {t.num: t.status for t in self._read_source_tickets()}
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT ticket_num FROM result_envelopes "
                "WHERE job_id = ? AND status = 'succeeded'",
                (self.job_id,),
            ).fetchall()
            for row in rows:
                status_by_num[int(row["ticket_num"])] = "resolved"
        finally:
            conn.close()
        missing = sorted(
            n for n in required if status_by_num.get(n) != "resolved"
        )
        return {
            "jobId": self.job_id,
            "revision": plan.revision,
            "complete": not missing,
            "missingRequiredTickets": missing,
        }

    def events(self) -> list[dict[str, Any]]:
        self.ensure_initial_plan()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT event_type, created_at, payload_json FROM events "
                "WHERE job_id = ? ORDER BY id",
                (self.job_id,),
            ).fetchall()
            return [
                {
                    "eventType": row["event_type"],
                    "createdAt": row["created_at"],
                    "payload": json.loads(row["payload_json"]),
                }
                for row in rows
            ]
        finally:
            conn.close()

    def _ensure_dirs(self) -> None:
        self.baton_dir.mkdir(parents=True, exist_ok=True)
        self.feature_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        self._ensure_dirs()
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        self._init_db(conn)
        return conn

    def _read_source_tickets(self) -> list[Ticket]:
        """Parse the change's tasks.md — the single ticket source."""
        try:
            return parse_tasks_md(self.tasks_path)
        except TasksMarkdownError as exc:
            raise PlanStoreError(str(exc)) from exc

    @staticmethod
    def _tickets_hash(tickets: list[PlanTicket]) -> str:
        """Hash the ticket set for change detection.

        ``source_revision`` is projected out: candidates are parsed with the
        *next* revision number stamped in, so including it would make every
        sync look like a change (runaway revision creep).
        """
        def projection(ticket: PlanTicket) -> dict[str, Any]:
            data = ticket.to_dict()
            data["source_revision"] = 0
            return data

        return stable_hash([projection(t) for t in tickets])

    def _plan_tickets_from_source(self, revision: int) -> list[PlanTicket]:
        return [
            PlanTicket(
                num=parsed.num,
                title=parsed.title,
                what_to_build=parsed.what_to_build,
                blocked_by=parsed.blocked_by,
                status=parsed.status,
                criteria=[text for _, text in parsed.criteria],
                verify=list(parsed.verify),
                source_revision=revision,
            )
            for parsed in self._read_source_tickets()
        ]

    def _ensure_initial_plan_locked(
        self,
        conn: sqlite3.Connection,
        approved_by: str = "openspec/tasks.md",
    ) -> PlanRevision:
        """Sync the durable plan with the OpenSpec change's tasks.md.

        No plan yet → revision 1. tasks.md changed since the latest revision
        (human edits, added tasks, or baton's own ``[x]`` write-backs) → a
        fresh revision, so the next frontier reflects the current file. In-flight
        attempts keep their immutable packets and are never rewritten.
        """
        current = self._load_current_plan_locked(conn)
        candidate_revision = 1 if current is None else current.revision + 1
        tickets = self._plan_tickets_from_source(candidate_revision)
        if (
            current is not None
            and self._tickets_hash(tickets) == self._tickets_hash(current.tickets)
        ):
            return current

        now = utc_now()
        plan = PlanRevision(
            job_id=self.job_id,
            feature=self.feature,
            revision=candidate_revision,
            parent_revision=None if current is None else current.revision,
            plan_hash="",
            approved_by=approved_by,
            approved_at=now,
            created_at=now,
            tickets=tickets,
        )
        plan.plan_hash = stable_hash(plan.hashable_dict())
        self._insert_revision_locked(conn, plan)
        self._event_locked(
            conn,
            "plan.revision.initialized" if current is None else "plan.revision.updated",
            {
                "revision": plan.revision,
                "parentRevision": plan.parent_revision,
                "planHash": plan.plan_hash,
                "source": f"openspec/changes/{self.feature}/tasks.md",
            },
        )
        return plan

    def _init_db(self, conn: sqlite3.Connection) -> None:
        """Create all tables/indexes idempotently and enable WAL.

        Wrapped in a short busy-retry loop: ``PRAGMA journal_mode=WAL`` and the
        schema DDL run inside ``executescript``, which does **not** honor the
        connection's ``timeout`` busy-wait. When two threads connect to a
        freshly-created database simultaneously (e.g. the parallel-claim test,
        or two ralph workers starting at once), the second thread's pragma
        collides with the first's lock and raises ``database is locked``
        immediately rather than waiting. We retry on that specific transient
        error so schema bootstrap serializes cleanly instead of crashing the
        loser. The retry is bounded and short — once WAL + tables exist the
        script is a no-op and the loop exits on the first attempt.
        """
        sql = """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS plan_revisions (
              job_id TEXT NOT NULL,
              feature TEXT NOT NULL,
              revision INTEGER NOT NULL,
              parent_revision INTEGER,
              plan_hash TEXT NOT NULL,
              approved_by TEXT NOT NULL,
              approved_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              snapshot_json TEXT NOT NULL,
              PRIMARY KEY (job_id, revision)
            );
            CREATE TABLE IF NOT EXISTS amendments (
              amendment_id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL,
              feature TEXT NOT NULL,
              base_revision INTEGER NOT NULL,
              state TEXT NOT NULL,
              classification TEXT NOT NULL,
              request TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              applied_revision INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_amendments_job
              ON amendments(job_id, created_at);
            CREATE TABLE IF NOT EXISTS assignments (
              job_id TEXT NOT NULL,
              assignment_id TEXT NOT NULL,
              ticket_num INTEGER NOT NULL,
              state TEXT NOT NULL,
              plan_revision INTEGER NOT NULL,
              task_packet_hash TEXT NOT NULL,
              profile TEXT NOT NULL,
              resources_json TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (job_id, assignment_id)
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id TEXT NOT NULL,
              feature TEXT NOT NULL,
              event_type TEXT NOT NULL,
              created_at TEXT NOT NULL,
              payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_job
              ON events(job_id, id);
            CREATE TABLE IF NOT EXISTS queue_tasks (
              queue_id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL,
              feature TEXT NOT NULL,
              ticket_num INTEGER NOT NULL,
              plan_revision INTEGER NOT NULL,
              task_packet_hash TEXT NOT NULL,
              agent_profile TEXT NOT NULL,
              priority TEXT NOT NULL,
              priority_rank INTEGER NOT NULL,
              status TEXT NOT NULL,
              resources_json TEXT NOT NULL,
              owner TEXT NOT NULL,
              lease_token TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              UNIQUE(job_id, ticket_num)
            );
            CREATE INDEX IF NOT EXISTS idx_queue_tasks_job_status
              ON queue_tasks(job_id, status, priority_rank, created_at);
            CREATE TABLE IF NOT EXISTS resource_leases (
              resource_key TEXT PRIMARY KEY,
              queue_id TEXT NOT NULL,
              job_id TEXT NOT NULL,
              lease_token TEXT NOT NULL,
              owner TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_resource_leases_job
              ON resource_leases(job_id, queue_id);
            CREATE TABLE IF NOT EXISTS result_envelopes (
              envelope_id TEXT PRIMARY KEY,
              job_id TEXT NOT NULL,
              feature TEXT NOT NULL,
              ticket_num INTEGER NOT NULL,
              assignment_id TEXT NOT NULL,
              plan_revision INTEGER NOT NULL,
              task_packet_hash TEXT NOT NULL,
              status TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_attempts (
              job_id TEXT NOT NULL,
              run_id TEXT NOT NULL,
              assignment_id TEXT NOT NULL,
              ticket_num INTEGER NOT NULL,
              attempt INTEGER NOT NULL,
              state TEXT NOT NULL,
              session_id TEXT NOT NULL,
              agent_id TEXT NOT NULL,
              plan_revision INTEGER NOT NULL,
              task_packet_hash TEXT NOT NULL,
              claim_token_hash TEXT NOT NULL,
              lease_expires_at TEXT NOT NULL,
              base_branch TEXT NOT NULL,
              base_sha TEXT NOT NULL,
              worktree_path TEXT NOT NULL,
              worktree_branch TEXT NOT NULL,
              integration_path TEXT NOT NULL,
              integration_branch TEXT NOT NULL,
              packet_path TEXT NOT NULL,
              receipt_path TEXT NOT NULL,
              commit_sha TEXT NOT NULL,
              integration_sha TEXT NOT NULL,
              failure_code TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              PRIMARY KEY (job_id, assignment_id),
              UNIQUE(job_id, ticket_num, attempt)
            );
            CREATE INDEX IF NOT EXISTS idx_execution_attempts_job_state
              ON execution_attempts(job_id, state, ticket_num);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_attempts_one_active_ticket
              ON execution_attempts(job_id, ticket_num)
              WHERE state IN ('leased', 'spawned', 'running', 'submitted',
                              'verifying', 'integrating', 'blocked-review');
            CREATE TABLE IF NOT EXISTS attempt_resource_leases (
              resource_key TEXT PRIMARY KEY,
              assignment_id TEXT NOT NULL,
              job_id TEXT NOT NULL,
              claim_token_hash TEXT NOT NULL,
              session_id TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_attempt_resource_leases_job
              ON attempt_resource_leases(job_id, assignment_id);
            CREATE INDEX IF NOT EXISTS idx_result_envelopes_job
              ON result_envelopes(job_id, ticket_num);
            """
        # ``executescript`` ignores the connection busy-timeout, so a concurrent
        # first-connect races on ``PRAGMA journal_mode=WAL`` (which needs an
        # exclusive lock) and raises SQLITE_BUSY. Retry briefly — schema init is
        # idempotent, so the loser just waits for the winner's COMMIT and the
        # script becomes a no-op on the second attempt.
        import time as _time

        last_err: Exception | None = None
        for _ in range(50):
            try:
                conn.executescript(sql)
                return
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    last_err = exc
                    _time.sleep(0.02)
                    continue
                raise
        # Exhausted retries — surface the original locked error.
        raise last_err  # type: ignore[misc]

    def _load_current_plan_locked(
        self,
        conn: sqlite3.Connection,
    ) -> PlanRevision | None:
        row = conn.execute(
            "SELECT snapshot_json FROM plan_revisions "
            "WHERE job_id = ? ORDER BY revision DESC LIMIT 1",
            (self.job_id,),
        ).fetchone()
        if row is None:
            return None
        return self._plan_from_snapshot_json(row["snapshot_json"], conn)

    def _plan_from_snapshot_json(
        self,
        snapshot_json: str,
        conn: sqlite3.Connection | None = None,
        *,
        live_assignments: bool = True,
    ) -> PlanRevision:
        """Materialize a PlanRevision from a snapshot_json blob.

        ``live_assignments=True`` (default, used for the *current* plan) reads
        the live ``assignments`` table and injects it — the current plan should
        reflect real-time assignment state. ``live_assignments=False`` (used
        for ``plan_history``) keeps the assignments that were frozen into the
        snapshot at approval time — historical revisions must show their own
        assignments, not today's, or audit/replay is distorted.
        """
        plan = PlanRevision.from_dict(json.loads(snapshot_json))
        if not live_assignments:
            # Keep the assignments already parsed from the snapshot by
            # from_dict — they are the ones frozen at approval time.
            return plan
        close_conn = False
        if conn is None:
            conn = self._connect()
            close_conn = True
        try:
            rows = conn.execute(
                "SELECT assignment_id, ticket_num, state, plan_revision, "
                "task_packet_hash, profile, resources_json "
                "FROM assignments WHERE job_id = ? ORDER BY assignment_id",
                (self.job_id,),
            ).fetchall()
            plan.assignments = [
                AssignmentSnapshot(
                    assignment_id=row["assignment_id"],
                    ticket_num=row["ticket_num"],
                    state=row["state"],
                    plan_revision=row["plan_revision"],
                    task_packet_hash=row["task_packet_hash"],
                    profile=row["profile"],
                    resources=json.loads(row["resources_json"]),
                )
                for row in rows
            ]
            return plan
        finally:
            if close_conn:
                conn.close()

    def _insert_revision_locked(
        self,
        conn: sqlite3.Connection,
        plan: PlanRevision,
    ) -> None:
        conn.execute(
            "INSERT INTO plan_revisions "
            "(job_id, feature, revision, parent_revision, plan_hash, approved_by, "
            "approved_at, created_at, snapshot_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan.job_id,
                plan.feature,
                plan.revision,
                plan.parent_revision,
                plan.plan_hash,
                plan.approved_by,
                plan.approved_at,
                plan.created_at,
                json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True),
            ),
        )


    def _event_locked(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO events "
            "(job_id, feature, event_type, created_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                self.job_id,
                self.feature,
                event_type,
                utc_now(),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _record_assignment_locked(
        self,
        conn: sqlite3.Connection,
        assignment: AssignmentSnapshot,
    ) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO assignments "
            "(job_id, assignment_id, ticket_num, state, plan_revision, "
            "task_packet_hash, profile, resources_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.job_id,
                assignment.assignment_id,
                assignment.ticket_num,
                assignment.state,
                assignment.plan_revision,
                assignment.task_packet_hash,
                assignment.profile,
                json.dumps(assignment.resources, ensure_ascii=False),
                utc_now(),
            ),
        )
        self._event_locked(
            conn,
            "assignment.recorded",
            assignment.to_dict(),
        )

    def _default_workspace_resource(self) -> str:
        return f"workspace:{self.project_path}"

    def _task_resources(self, ticket: PlanTicket) -> list[str]:
        resources = [self._default_workspace_resource()]
        for resource in ticket.resources:
            if resource not in resources:
                resources.append(resource)
        return resources

    def _queue_task_hash(self, plan: PlanRevision, ticket: PlanTicket) -> str:
        return stable_hash(
            {
                "planRevision": plan.revision,
                "ticket": ticket.to_dict(),
                "resources": self._task_resources(ticket),
            }
        )

    def _upsert_queue_task_locked(
        self,
        conn: sqlite3.Connection,
        plan: PlanRevision,
        ticket: PlanTicket,
    ) -> dict[str, Any] | None:
        queue_id = f"Q-{self.feature}-{ticket.num:03d}"
        resources = self._task_resources(ticket)
        now = utc_now()
        row = conn.execute(
            "SELECT * FROM queue_tasks WHERE job_id = ? AND ticket_num = ?",
            (self.job_id, ticket.num),
        ).fetchone()
        payload = {
            "queueId": queue_id,
            "jobId": self.job_id,
            "feature": self.feature,
            "ticket": ticket.num,
            "planRevision": plan.revision,
            "taskPacketHash": self._queue_task_hash(plan, ticket),
            "agentProfile": ticket.agent_profile,
            "priority": ticket.priority,
            "priorityRank": PRIORITY_RANK.get(ticket.priority, PRIORITY_RANK["normal"]),
            "status": "queued",
            "resources": resources,
            "owner": "",
            "leaseToken": "",
        }
        if row is None:
            conn.execute(
                "INSERT INTO queue_tasks "
                "(queue_id, job_id, feature, ticket_num, plan_revision, "
                "task_packet_hash, agent_profile, priority, priority_rank, "
                "status, resources_json, owner, lease_token, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    queue_id,
                    self.job_id,
                    self.feature,
                    ticket.num,
                    plan.revision,
                    payload["taskPacketHash"],
                    ticket.agent_profile,
                    ticket.priority,
                    payload["priorityRank"],
                    "queued",
                    json.dumps(resources, ensure_ascii=False),
                    "",
                    "",
                    now,
                    now,
                ),
            )
            self._event_locked(conn, "queue.task.enqueued", payload)
            return payload

        existing = self._queue_row_to_dict(row)
        if existing["status"] != "queued":
            return None
        conn.execute(
            "UPDATE queue_tasks SET plan_revision = ?, task_packet_hash = ?, "
            "agent_profile = ?, priority = ?, priority_rank = ?, "
            "resources_json = ?, updated_at = ? WHERE queue_id = ?",
            (
                plan.revision,
                payload["taskPacketHash"],
                ticket.agent_profile,
                ticket.priority,
                payload["priorityRank"],
                json.dumps(resources, ensure_ascii=False),
                now,
                queue_id,
            ),
        )
        self._event_locked(conn, "queue.task.updated", payload)
        return payload

    def _queue_row_to_dict(
        self,
        row: sqlite3.Row,
        include_leases: bool = True,
    ) -> dict[str, Any]:
        leases = self._leases_for_queue(row["queue_id"]) if include_leases else []
        return {
            "queueId": row["queue_id"],
            "jobId": row["job_id"],
            "feature": row["feature"],
            "ticket": row["ticket_num"],
            "planRevision": row["plan_revision"],
            "taskPacketHash": row["task_packet_hash"],
            "agentProfile": row["agent_profile"],
            "priority": row["priority"],
            "priorityRank": row["priority_rank"],
            "status": row["status"],
            "resources": json.loads(row["resources_json"]),
            "owner": row["owner"],
            "leaseToken": row["lease_token"],
            "leases": leases,
        }

    def _leases_for_queue(self, queue_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT resource_key, owner, expires_at, lease_token "
                "FROM resource_leases WHERE queue_id = ? ORDER BY resource_key",
                (queue_id,),
            ).fetchall()
            return [
                {
                    "resource": row["resource_key"],
                    "owner": row["owner"],
                    "expiresAt": row["expires_at"],
                    "leaseToken": row["lease_token"],
                }
                for row in rows
            ]
        finally:
            conn.close()

    def _load_queue_task_locked(
        self,
        conn: sqlite3.Connection,
        queue_id: str,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM queue_tasks WHERE job_id = ? AND queue_id = ?",
            (self.job_id, queue_id),
        ).fetchone()
        if row is None:
            raise QueueTaskNotFoundError(queue_id)
        return self._queue_row_to_dict(row, include_leases=False)

    def _delete_expired_leases_locked(self, conn: sqlite3.Connection) -> None:
        now = utc_now()
        rows = conn.execute(
            "SELECT queue_id, lease_token FROM resource_leases WHERE expires_at <= ?",
            (now,),
        ).fetchall()
        expired = {(row["queue_id"], row["lease_token"]) for row in rows}
        if not expired:
            return
        conn.execute("DELETE FROM resource_leases WHERE expires_at <= ?", (now,))
        for queue_id, lease_token in expired:
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM resource_leases "
                "WHERE queue_id = ? AND lease_token = ?",
                (queue_id, lease_token),
            ).fetchone()
            if int(remaining["c"]) == 0:
                conn.execute(
                    "UPDATE queue_tasks SET status = 'queued', owner = '', "
                    "lease_token = '', updated_at = ? "
                    "WHERE queue_id = ? AND lease_token = ? AND status = 'running'",
                    (now, queue_id, lease_token),
                )
                self._event_locked(
                    conn,
                    "queue.task.lease-expired",
                    {"queueId": queue_id, "leaseToken": lease_token},
                )

    def _resources_locked(
        self,
        conn: sqlite3.Connection,
        resources: list[str],
    ) -> bool:
        if not resources:
            return False
        now = utc_now()
        placeholders = ",".join("?" for _ in resources)
        rows = conn.execute(
            f"SELECT resource_key FROM resource_leases "
            f"WHERE resource_key IN ({placeholders}) AND expires_at > ?",
            (*resources, now),
        ).fetchall()
        return bool(rows)

    @staticmethod
    def _claim_token_hash(claim_token: str) -> str:
        return hashlib.sha256(claim_token.encode("utf-8")).hexdigest()

    def _load_execution_attempt_locked(
        self, conn: sqlite3.Connection, assignment_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM execution_attempts WHERE job_id = ? AND assignment_id = ?",
            (self.job_id, assignment_id),
        ).fetchone()
        if row is None:
            raise ExecutionAttemptError(f"unknown assignment: {assignment_id}")
        return row

    def _require_claim_token(self, row: sqlite3.Row, claim_token: str) -> None:
        if self._claim_token_hash(claim_token) != row["claim_token_hash"]:
            raise ExecutionAttemptError(
                f"invalid claim token for assignment {row['assignment_id']}"
            )

    def _execution_attempt_from_row(self, row: sqlite3.Row) -> ExecutionAttempt:
        return ExecutionAttempt(
            assignment_id=str(row["assignment_id"]),
            run_id=str(row["run_id"]),
            ticket_num=int(row["ticket_num"]),
            attempt=int(row["attempt"]),
            state=str(row["state"]),
            session_id=str(row["session_id"]),
            plan_revision=int(row["plan_revision"]),
            task_packet_hash=str(row["task_packet_hash"]),
            claim_token="",
            lease_expires_at=str(row["lease_expires_at"]),
            base_branch=str(row["base_branch"]),
            base_sha=str(row["base_sha"]),
            worktree_path=str(row["worktree_path"]),
            worktree_branch=str(row["worktree_branch"]),
            integration_path=str(row["integration_path"]),
            integration_branch=str(row["integration_branch"]),
            packet_path=str(row["packet_path"]),
            receipt_path=str(row["receipt_path"]),
            agent_id=str(row["agent_id"]),
            commit_sha=str(row["commit_sha"]),
            integration_sha=str(row["integration_sha"]),
            failure_code=str(row["failure_code"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _attempt_resources_locked(
        self, conn: sqlite3.Connection, resources: list[str]
    ) -> bool:
        if not resources:
            return False
        placeholders = ",".join("?" for _ in resources)
        rows = conn.execute(
            f"SELECT resource_key FROM attempt_resource_leases "
            f"WHERE resource_key IN ({placeholders}) AND expires_at > ?",
            (*resources, utc_now()),
        ).fetchall()
        return bool(rows)

    def _expire_execution_attempts_locked(self, conn: sqlite3.Connection) -> None:
        now = utc_now()
        rows = conn.execute(
            "SELECT assignment_id FROM execution_attempts "
            "WHERE job_id = ? AND lease_expires_at <= ? "
            "AND state IN ('leased', 'spawned', 'running', 'submitted', 'verifying', 'integrating')",
            (self.job_id, now),
        ).fetchall()
        for row in rows:
            assignment_id = str(row["assignment_id"])
            conn.execute(
                "UPDATE execution_attempts SET state = 'unknown', failure_code = 'lease-expired', "
                "updated_at = ? WHERE job_id = ? AND assignment_id = ?",
                (now, self.job_id, assignment_id),
            )
            conn.execute(
                "DELETE FROM attempt_resource_leases WHERE job_id = ? AND assignment_id = ?",
                (self.job_id, assignment_id),
            )
            self._event_locked(
                conn,
                "execution.attempt.unknown",
                {"assignmentId": assignment_id, "reason": "lease-expired"},
            )









    def _write_plan_json(self, plan: PlanRevision) -> None:
        _atomic_write(self.plan_path, _pretty_json(plan.to_dict()))


    def _result_envelope_id(self, envelope: ResultEnvelope) -> str:
        if envelope.assignment_id:
            return f"RE-{envelope.assignment_id}"
        return f"RE-{self.feature}-{envelope.ticket_num:03d}"

    def _write_result_envelope_json(
        self,
        envelope_id: str,
        envelope: ResultEnvelope,
    ) -> None:
        path = self.results_dir / f"{envelope_id}.json"
        _atomic_write(path, _pretty_json(envelope.to_dict()))
