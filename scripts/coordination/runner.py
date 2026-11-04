"""Process-group runner and fault-state guardian.

The :class:`JobRunner` is the layer that turns a leased job into a real
running process, then watches over it. Two concerns live here:

* **Exec.** :meth:`JobRunner.exec_sync` spawns the job's command in its own
  process group (``start_new_session=True``), injects the lease token and job
  id into the child environment so the workload can self-identify, waits for
  it, and then releases the lease via the scheduler with a status derived from
  the exit code.

* **Stale-lease triage.** :meth:`JobRunner.check_stale_leases` is the guardian
  that the coordination kernel runs periodically. It finds jobs whose
  heartbeat has expired *without* the owner ever calling
  :meth:`Scheduler.release`, and decides — per job — what to do. The cardinal
  rule is **never silently release**: the runner never deletes lease rows or
  flips a job to ``succeeded``/``failed`` on its own. Every state transition
  goes through the scheduler (``mark_suspect`` / ``mark_unknown`` /
  ``release``), and a release only happens when it is provably safe (the owner
  process is dead *and* the work is idempotent).

This is what makes "the heartbeat expired but the process is still alive" a
non-catastrophic event: a long GC pause or a slow heartbeat loop will trip the
stale-lease scan, but :meth:`is_owner_alive` will still see the live process
and the job is merely flagged ``suspect`` — its lease is retained, the owner
keeps running, and no second worker is granted the same resource.

PID reuse defense: :meth:`is_owner_alive` does not trust the pid alone. When
the job recorded an ``owner_start_time`` (the process create_time captured at
grant), the runner re-reads the live process's create_time and compares them.
A recycled pid will have a different create_time → reported dead. psutil is
the preferred source; when it is unavailable the runner falls back to
``os.kill(pid, 0)`` and a best-effort ``ps``-based create_time probe, matching
the design contract's "psutil is optional" clause.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta
from typing import Any

from orchestration.plan_model import utc_now

from coordination.model import (
    FAILED,
    Job,
    LEASED,
    RUNNING,
    SUBMITTED,
    SUCCEEDED,
)
from coordination.scheduler import Scheduler
from coordination.store import CoordinationStore

# psutil is an optional dependency. When present we get a reliable, portable
# create_time() (the foundation of the PID-reuse check). When absent we
# degrade gracefully: os.kill(pid, 0) for existence plus a best-effort `ps`
# probe for create_time. The tests do not assume psutil is installed.
try:  # pragma: no cover - import-time branch depends on the host
    import psutil  # type: ignore[import-untyped]

    _HAS_PSUTIL = True
except Exception:  # pragma: no cover - exercised only on psutil-less hosts
    psutil = None  # type: ignore[assignment]
    _HAS_PSUTIL = False

# A pid's create_time must agree with the recorded owner_start_time to within
# this tolerance (seconds). Sub-process accounting and clock tick boundaries
# make exact equality brittle; a second of slack is plenty for reuse defense
# (a recycled pid's create_time differs by minutes-to-hours, never by < 1s).
_START_TIME_TOLERANCE_SECONDS = 1.0

# The set of job statuses whose lease is still considered "live" by the stale
# scan. Jobs in any other status have already shed their lease (released,
# failed, canceled) or were never leased, so the scan skips them.
_LEASE_LIVE_STATES: tuple[str, ...] = (LEASED, RUNNING)


class JobRunner:
    """Executes jobs and triages stale leases without ever releasing unsafely.

    The runner is constructed with the scheduler and store it drives. It is a
    *peer* of the scheduler, not its caller: it asks the scheduler to grant /
    release / mark, and it reads job state from the store, but it never opens
    a write transaction against the coordination DB itself.
    """

    def __init__(self, scheduler: Scheduler, store: CoordinationStore) -> None:
        self.scheduler = scheduler
        self.store = store

    # ------------------------------------------------------------------
    # Exec
    # ------------------------------------------------------------------
    def exec_sync(
        self,
        job: Job,
        command: list[str],
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> tuple[int, str, str]:
        """Run ``command`` for ``job`` synchronously, then release the lease.

        The job **must** already be ``leased`` (the caller obtained it via
        :meth:`Scheduler.pick_next_runnable` / :meth:`Scheduler.try_grant`).
        The command is launched as the leader of a new process group
        (``start_new_session=True``) so the whole group can be signaled
        together if needed, and ``BATON_LEASE_TOKEN`` / ``BATON_JOB_ID`` are
        injected into the child environment so the workload can self-identify
        and heartbeat using the right token.

        After the process exits the lease is released via
        :meth:`Scheduler.release`: exit 0 → ``succeeded``, anything else →
        ``failed``. Returns ``(exit_code, stdout, stderr)``. The release is
        best-effort against the *original* lease token; if the lease was
        already invalidated (e.g. by a concurrent stale-lease scan marking
        the job suspect/unknown) the release is a no-op but the exit tuple is
        still returned — the workload already ran, its result is the truth.

        A ``KeyboardInterrupt`` while the command runs (Ctrl-C in the calling
        terminal; the child's own session never sees that signal) gets the
        timeout treatment — child group killed, lease released with
        ``failed`` — and is then re-raised so the caller can exit with its
        own interrupt convention.
        """
        if job.status != LEASED:
            raise ValueError(
                f"exec_sync requires a leased job; job {job.job_id} is {job.status!r}"
            )
        if not command:
            raise ValueError("exec_sync requires a non-empty command")

        child_env = dict(os.environ if env is None else env)
        # Inject coordination identity so the workload can heartbeat / log
        # under the right lease. These override anything the caller passed in
        # env — the runner is the authority on which lease the child runs under.
        child_env["BATON_LEASE_TOKEN"] = job.lease_token
        child_env["BATON_JOB_ID"] = job.job_id

        try:
            proc = subprocess.Popen(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
                # Own process group → the leader and all its descendants can be
                # signaled as a unit (used by a future kill-on-cancel path).
                start_new_session=True,
                text=True,
            )
        except OSError:
            # The command never started (missing binary, not executable), so no
            # child exists to own the lease. Release it here or the resource
            # stays held by a dead owner and every later gate waits on it.
            self.scheduler.release(
                job.job_id, job.lease_token, final_status=FAILED
            )
            raise
        interrupted = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Timed out: reap what we can and surface a non-zero exit. The
            # lease is still released below so the resource frees for the next
            # job; the partial stdout/stderr collected so far is returned.
            self._terminate_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = ("", "")
            exit_code = proc.returncode if proc.returncode is not None else -1
        except KeyboardInterrupt:
            # Ctrl-C reached this parent only — the child runs in its own
            # session (start_new_session=True above) and never saw the
            # terminal's SIGINT. Without this branch the parent dies, the
            # command keeps running invisible, and the lease stays held.
            # Kill the whole group (same treatment as the timeout path), let
            # the normal release below free the lease, then re-raise so the
            # caller can exit with the interrupt convention.
            interrupted = True
            self._terminate_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                stdout, stderr = ("", "")
            exit_code = proc.returncode if proc.returncode is not None else -1
        else:
            exit_code = int(proc.returncode)

        final_status = SUCCEEDED if exit_code == 0 else FAILED
        # Best-effort release: if the lease token no longer matches (the job
        # was flagged suspect/unknown by a concurrent scan, or already
        # released), scheduler.release returns None and we accept that — the
        # workload already ran, its exit code is the source of truth.
        self.scheduler.release(
            job.job_id, job.lease_token, final_status=final_status
        )
        if interrupted:
            raise KeyboardInterrupt
        return exit_code, stdout or "", stderr or ""

    # ------------------------------------------------------------------
    # Owner liveness (PID + create_time double check)
    # ------------------------------------------------------------------
    def is_owner_alive(self, job: Job) -> bool:
        """True iff the job's owner process is still running and not recycled.

        ``owner_pid == 0`` means no owner was ever recorded → the job was
        never leased by a real process → not alive. Otherwise the pid is
        checked for existence and, when an ``owner_start_time`` was recorded,
        the live process's create_time is compared to it: a mismatch means the
        pid was recycled to a different process → reported dead. This is the
        defense that makes "the owner died and the OS reused its pid for an
        unrelated process" detectable.
        """
        pid = int(job.owner_pid)
        if pid == 0:
            return False
        if not self._pid_exists(pid):
            return False
        recorded_start = float(job.owner_start_time or 0.0)
        if recorded_start <= 0.0:
            # No start_time was recorded (older grant path / caller skipped
            # it). Trust the existence check alone — there's nothing to
            # compare against. This is the documented degradation mode.
            return True
        live_start = self._process_create_time(pid)
        if live_start is None:
            # psutil absent and ps probe failed: cannot verify start_time.
            # Fall back to existence-only (PRD's documented os.kill fallback).
            return True
        # PID-reuse guard: a recycled pid's create_time will differ from the
        # recorded owner_start_time by far more than the tolerance.
        return abs(live_start - recorded_start) <= _START_TIME_TOLERANCE_SECONDS

    # ------------------------------------------------------------------
    # Stale-lease triage
    # ------------------------------------------------------------------
    def check_stale_leases(
        self, lease_ttl: int = 3600
    ) -> list[Job]:
        """Find heartbeat-expired jobs and route each one to a safe state.

        Scans every job in a lease-live status (``leased`` / ``running``)
        whose ``heartbeat_at + lease_ttl`` is in the past, and for each:

        * **Owner alive** → :meth:`Scheduler.mark_suspect`. The lease is kept;
          the owner may still finish. A second worker is *not* granted the
          same resource. This is the "slow heartbeat, live process" case.
        * **Owner dead + idempotent** → :meth:`Scheduler.release` with
          ``final_status = submitted``. The work is safe to redo, so the lease
          is freed and the job returns to the queue for another worker to
          pick up.
        * **Owner dead + non-idempotent** → :meth:`Scheduler.mark_unknown`.
          Re-running could double-charge a credit card / send a duplicate
          email / etc., so the lease is retained and the job is flagged for a
          human to resolve. Nothing is released.

        Returns the list of jobs that were touched (suspect / unknown /
        requeued), each reflected to its post-triage state. Jobs not stale,
        or stale but already terminal, are skipped.

        The runner never deletes lease rows directly — every release goes
        through the scheduler, so the all-or-nothing lease accounting stays
        intact.
        """
        now = datetime.fromisoformat(utc_now())
        cutoff = timedelta(seconds=int(lease_ttl))
        touched: list[Job] = []

        for job in self.store.list_jobs():
            if job.status not in _LEASE_LIVE_STATES:
                continue
            if not job.heartbeat_at:
                # Never heartbeaten — treat submission time as the clock. A
                # leased/running job with an empty heartbeat is itself a sign
                # of a crashed-early owner; include it in the scan.
                reference = job.updated_at or job.created_at
            else:
                reference = job.heartbeat_at
            try:
                ref_dt = datetime.fromisoformat(reference)
            except ValueError:
                # Unparseable timestamp — can't prove it's stale, skip.
                continue
            if ref_dt + cutoff >= now:
                continue  # still within its lease window

            touched.append(self._triage_stale_job(job))

        return touched

    # ------------------------------------------------------------------
    # Internal: per-job triage
    # ------------------------------------------------------------------
    def _triage_stale_job(self, job: Job) -> Job:
        """Apply the suspect / requeue / unknown decision to one stale job."""
        if self.is_owner_alive(job):
            return self.scheduler.mark_suspect(
                job.job_id,
                reason="heartbeat stale; owner process still alive",
            )
        if self._is_idempotent(job):
            # Safe to redo: release the lease and flip back to submitted so
            # pick_next_runnable re-grants it to another worker. Releasing via
            # the scheduler keeps lease accounting consistent (capacity frees,
            # epoch high-water mark is preserved).
            requeued = self.scheduler.release(
                job.job_id,
                job.lease_token,
                final_status=SUBMITTED,
            )
            if requeued is None:
                # Token no longer matches (concurrent scan already moved it).
                # Re-fetch so the caller sees the current state.
                current = self.store.get_job(job.job_id)
                return current if current is not None else job
            return requeued
        return self.scheduler.mark_unknown(
            job.job_id,
            reason=(
                "heartbeat stale; owner dead and command is non-idempotent "
                "— manual resolution required"
            ),
        )

    @staticmethod
    def _is_idempotent(job: Job) -> bool:
        """Heuristic: is this job safe to re-run after an owner crash?

        Per the design contract, idempotency is signalled by markers on the
        job's reason or its current stage (name / command tokens). A job is
        considered idempotent if any of:

        * ``job.reason`` contains the substring ``"idempotent"`` (case-
          insensitive), e.g. ``"idempotent: pure build, safe to retry"``;
        * the current stage's ``name`` contains ``"idempotent"``;
        * any token of the current stage's ``command`` contains
          ``"idempotent"``.

        Anything else is treated as non-idempotent — the safe default when
        re-running could duplicate an external side effect.
        """
        marker = "idempotent"
        if marker in (job.reason or "").lower():
            return True
        if job.stages:
            stage = job.stages[min(int(job.current_stage_index), len(job.stages) - 1)]
            if marker in (stage.name or "").lower():
                return True
            for token in stage.command:
                if marker in str(token).lower():
                    return True
        return False

    # ------------------------------------------------------------------
    # Internal: process liveness primitives
    # ------------------------------------------------------------------
    @staticmethod
    def _pid_exists(pid: int) -> bool:
        """True iff ``pid`` currently identifies a live process.

        Uses psutil's reliable check when available; otherwise falls back to
        ``os.kill(pid, 0)``, which raises ``ProcessLookupError`` for a dead
        pid and ``PermissionError`` for a live pid owned by another user
        (treated as alive — the process exists, we just can't signal it).
        """
        if _HAS_PSUTIL:
            try:
                return bool(psutil.pid_exists(pid))  # type: ignore[union-attr]
            except Exception:
                # psutil itself misbehaving — fall through to os.kill.
                pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Process exists but isn't ours — still alive.
            return True
        return True

    @staticmethod
    def _process_create_time(pid: int) -> float | None:
        """Return the create_time (epoch seconds) of ``pid``, or None.

        psutil is the authoritative source. When it is absent we try a
        best-effort ``ps -p <pid> -o lstart=`` parse; on any failure we
        return None, which the caller treats as "could not verify start_time
        → fall back to existence-only checking".
        """
        if _HAS_PSUTIL:
            try:
                p = psutil.Process(pid)  # type: ignore[union-attr]
                return float(p.create_time())
            except Exception:
                return None
        # Best-effort cross-platform fallback (macOS/Linux `ps`).
        try:
            out = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return None
        line = (out.stdout or "").strip()
        if not line:
            return None
        try:
            # ps lstart format e.g. "Sun Aug  2 01:00:00 2026"
            dt = datetime.strptime(line, "%a %b %d %H:%M:%S %Y")
        except ValueError:
            return None
        return dt.timestamp()

    # ------------------------------------------------------------------
    # Internal: process-group teardown on timeout
    # ------------------------------------------------------------------
    @staticmethod
    def _terminate_group(proc: subprocess.Popen[Any]) -> None:
        """Best-effort kill of the whole process group led by ``proc``.

        ``start_new_session=True`` made the child a session leader, so its pgid
        equals its pid. Sending SIGKILL to the negative pgid reaches every
        descendant. Any failure is swallowed — this is a cleanup path, not the
        source of truth for the exit code.
        """
        import signal

        pgid: int | None = None
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pgid = None
        if pgid is not None and pgid > 0:
            try:
                os.killpg(pgid, signal.SIGKILL)
                return
            except Exception:
                pass
        # Fallback: just kill the leader.
        try:
            proc.kill()
        except Exception:
            pass
