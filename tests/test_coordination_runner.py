"""JobRunner — process-group exec and stale-lease triage tests.

Pins the two guardian contracts from US-004:

1. **Never release a lease when the owner's fate is ambiguous.** A dead owner
   whose command may still be running elsewhere (or whose side effects are
   unknown) must land in ``unknown`` with its lease rows intact — never
   ``succeeded``/``submitted``.

2. **PID + start_time double check.** ``is_owner_alive`` must detect a recycled
   pid (same number, different create_time) as dead, and must treat
   ``owner_pid == 0`` as dead by definition.

Plus the exec path: ``exec_sync`` injects ``BATON_LEASE_TOKEN`` and
``BATON_JOB_ID`` into the child environment and releases the lease with a
status derived from the exit code.

Every test uses an isolated temp DB; the global
``~/.baton/coordination/queue.db`` is never touched.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from coordination.model import (  # noqa: E402
    FAILED,
    Job,
    LEASED,
    NORMAL,
    ResourceClaim,
    RUNNING,
    StageSpec,
    SUBMITTED,
    SUCCEEDED,
    UNKNOWN,
)
from coordination.runner import JobRunner  # noqa: E402
from coordination.scheduler import Scheduler  # noqa: E402
from coordination.store import CoordinationStore  # noqa: E402


# ---------------------------------------------------------------------------
# Factories / fixtures
# ---------------------------------------------------------------------------


def _job(
    *,
    job_id: str,
    idempotency_key: str | None = None,
    priority: str = NORMAL,
    claims: list[ResourceClaim] | None = None,
    command: list[str] | None = None,
    stage_name: str = "build",
    reason: str = "",
    project: str = "baton",
    feature: str = "core-buildout",
    submitter: str = "tester",
) -> Job:
    if idempotency_key is None:
        idempotency_key = f"idem-{job_id}"
    if claims is None:
        claims = [ResourceClaim(resource_key="builder-slot", mode="exclusive")]
    if command is None:
        command = ["true"]
    return Job(
        job_id=job_id,
        idempotency_key=idempotency_key,
        submitter=submitter,
        project=project,
        feature=feature,
        priority=priority,
        reason=reason,
        stages=[
            StageSpec(
                name=stage_name,
                runner="ralph",
                claims=claims,
                command=command,
            )
        ],
    )


@pytest.fixture()
def store(tmp_path: Path) -> CoordinationStore:
    return CoordinationStore(db_path=tmp_path / "test-runner.db")


@pytest.fixture()
def scheduler(store: CoordinationStore) -> Scheduler:
    return Scheduler(store=store)


@pytest.fixture()
def runner(scheduler: Scheduler, store: CoordinationStore) -> JobRunner:
    return JobRunner(scheduler=scheduler, store=store)


def _count_leases(store: CoordinationStore, job_id: str) -> int:
    conn = sqlite3.connect(str(store.db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM leases WHERE job_id = ?", (job_id,)
        ).fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


def _force_heartbeat_past(store: CoordinationStore, job_id: str, seconds_ago: int = 7200) -> None:
    """Push a job's heartbeat_at into the distant past so it trips the stale scan."""
    conn = sqlite3.connect(str(store.db_path))
    try:
        # SQLite can't subtract from an ISO string, so recompute it in Python.
        from datetime import datetime, timedelta, timezone

        old = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).replace(
            microsecond=0
        ).isoformat()
        conn.execute(
            "UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ?",
            (old, old, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def _backdate_owner(store: CoordinationStore, job_id: str, pid: int, start_time: float) -> None:
    """Stamp a job's owner_pid / owner_start_time directly (test seam)."""
    conn = sqlite3.connect(str(store.db_path))
    try:
        conn.execute(
            "UPDATE jobs SET owner_pid = ?, owner_start_time = ? WHERE job_id = ?",
            (int(pid), float(start_time), job_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# exec_sync
# ---------------------------------------------------------------------------


class TestExecSync:
    def test_requires_leased_job(self, runner, store, scheduler):
        job = store.submit_job(_job(job_id="j", command=["true"]))
        # status is 'submitted' here — exec_sync must refuse.
        with pytest.raises(ValueError):
            runner.exec_sync(job, command=["true"])

    def test_injects_lease_token_and_job_id_env(self, runner, store, scheduler):
        """Required by spec: BATON_LEASE_TOKEN and BATON_JOB_ID are injected."""
        job = store.submit_job(_job(job_id="j-env", command=["printenv"]))
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        # A command that prints the injected vars so we can assert them.
        script = (
            "import os; "
            "print(os.environ.get('BATON_LEASE_TOKEN')); "
            "print(os.environ.get('BATON_JOB_ID'))"
        )
        exit_code, stdout, _ = runner.exec_sync(
            granted, command=[sys.executable, "-c", script]
        )
        assert exit_code == 0
        lines = [ln for ln in stdout.splitlines() if ln]
        assert lines[0] == granted.lease_token
        assert lines[1] == "j-env"

    def test_exit_zero_releases_succeeded(self, runner, store, scheduler):
        job = store.submit_job(_job(job_id="j-ok", command=["true"]))
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        exit_code, _, _ = runner.exec_sync(granted, command=["true"])
        assert exit_code == 0
        fetched = store.get_job("j-ok")
        assert fetched is not None
        assert fetched.status == SUCCEEDED
        assert fetched.lease_token == ""
        assert _count_leases(store, "j-ok") == 0

    def test_nonzero_exit_releases_failed(self, runner, store, scheduler):
        job = store.submit_job(_job(job_id="j-fail", command=["false"]))
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        exit_code, _, stderr = runner.exec_sync(granted, command=["sh", "-c", "echo boom 1>&2; exit 7"])
        assert exit_code == 7
        assert "boom" in stderr
        fetched = store.get_job("j-fail")
        assert fetched is not None
        assert fetched.status == FAILED
        assert _count_leases(store, "j-fail") == 0

    def test_release_emits_terminal_status_event(self, runner, store, scheduler):
        job = store.submit_job(_job(job_id="j-evt", command=["true"]))
        granted = scheduler.try_grant(job, owner="w")
        runner.exec_sync(granted, command=["true"])
        evts = [e for e in store.list_events(job_id="j-evt") if e["event_type"] == "job.released"]
        assert len(evts) == 1
        assert evts[0]["payload"]["final_status"] == SUCCEEDED

    def test_spawn_failure_releases_the_lease(self, runner, store, scheduler):
        """A command that cannot be spawned must not wedge the resource.

        ``queue exec -- no-such-binary`` never starts a child, so the lease has
        to be released here; keeping it turns the resource into a permanent
        blocker (every later ``gate`` waits on a dead owner forever).
        """
        job = store.submit_job(_job(job_id="j-nospawn", command=["no-such-binary-xyz"]))
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None

        with pytest.raises(OSError):
            runner.exec_sync(granted, command=["no-such-binary-xyz"])

        fetched = store.get_job("j-nospawn")
        assert fetched is not None
        assert fetched.status == FAILED
        assert _count_leases(store, "j-nospawn") == 0
        # The freed capacity is immediately re-grantable to another worker.
        retry = store.submit_job(_job(job_id="j-nospawn-next", command=["true"]))
        assert scheduler.try_grant(retry, owner="w") is not None


# ---------------------------------------------------------------------------
# is_owner_alive — PID + start_time double check
# ---------------------------------------------------------------------------


class TestIsOwnerAlive:
    def test_pid_zero_is_dead(self, runner):
        job = _job(job_id="j")
        job.owner_pid = 0
        assert runner.is_owner_alive(job) is False

    def test_dead_pid_is_dead(self, runner):
        # Pick a pid that is essentially guaranteed to not exist.
        job = _job(job_id="j")
        job.owner_pid = 999_999
        job.owner_start_time = 0.0
        assert runner.is_owner_alive(job) is False

    def test_live_self_process_is_alive_without_start_time(self, runner):
        """A live pid with no recorded start_time is trusted by existence alone."""
        job = _job(job_id="j")
        job.owner_pid = os.getpid()
        job.owner_start_time = 0.0
        assert runner.is_owner_alive(job) is True

    def test_live_self_process_is_alive_with_matching_start_time(self, runner):
        job = _job(job_id="j")
        job.owner_pid = os.getpid()
        # Use the real create_time if psutil is available, else 0 (existence path).
        try:
            import psutil  # noqa: F401

            job.owner_start_time = float(__import__("psutil").Process(os.getpid()).create_time())
        except Exception:
            pytest.skip("psutil not installed; start_time-match path untestable here")
        assert runner.is_owner_alive(job) is True

    def test_recycled_pid_detected_via_mismatched_start_time(self, runner):
        """Same pid number, different create_time → reported dead (PID reuse)."""
        job = _job(job_id="j")
        job.owner_pid = os.getpid()
        # A start_time far in the past — a pid recycled into our number would
        # have a current create_time, not one from years ago.
        job.owner_start_time = 1.0  # epoch 1970-01-01
        # Without psutil we can't compute create_time, so the check degrades to
        # existence-only and would (wrongly) report alive. Guard the assertion.
        try:
            import psutil  # noqa: F401
        except Exception:
            pytest.skip("psutil not installed; PID-reuse detection degrades to existence-only")
        assert runner.is_owner_alive(job) is False


# ---------------------------------------------------------------------------
# check_stale_leases — the guardian
# ---------------------------------------------------------------------------


class TestCheckStaleLeases:
    def test_owner_alive_goes_suspect_not_released(self, runner, store, scheduler):
        """Stale heartbeat but live owner → suspect, lease retained."""
        job = store.submit_job(_job(job_id="j-live", command=["sleep", "60"]))
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        # The owner is THIS process — alive.
        _backdate_owner(store, "j-live", pid=os.getpid(), start_time=0.0)
        _force_heartbeat_past(store, "j-live")
        touched = runner.check_stale_leases(lease_ttl=60)
        assert len(touched) == 1
        fetched = store.get_job("j-live")
        assert fetched is not None
        assert fetched.status == "suspect"
        # Lease NOT released.
        assert _count_leases(store, "j-live") == 1

    def test_dead_owner_does_not_release_live_child(self, runner, store, scheduler):
        """Required by spec: owner dead, lease retained → unknown.

        Even though the owner process is gone, the runner must NOT release the
        lease — the work might still be running in a detached child, or its
        side effects are unobservable. The job lands in ``unknown`` with its
        lease rows intact, waiting for a human.
        """
        job = store.submit_job(
            _job(job_id="j-dead", command=["sh", "-c", "echo side-effect"], reason="")
        )
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        # Owner pid that does not exist.
        _backdate_owner(store, "j-dead", pid=999_999, start_time=0.0)
        _force_heartbeat_past(store, "j-dead")
        touched = runner.check_stale_leases(lease_ttl=60)
        assert len(touched) == 1
        fetched = store.get_job("j-dead")
        assert fetched is not None
        assert fetched.status == UNKNOWN
        # The whole point: lease rows are STILL THERE.
        assert _count_leases(store, "j-dead") == 1

    def test_ambiguous_side_effect_is_not_retried(self, runner, store, scheduler):
        """Required by spec: non-idempotent dead owner → unknown, never requeued.

        A job whose command has external side effects (no idempotency marker)
        must not be flipped back to ``submitted`` for another worker to retry
        — that could duplicate the side effect. It lands in ``unknown``.
        """
        job = store.submit_job(
            _job(
                job_id="j-charge",
                command=["charge-credit-card", "--amount", "100"],
                reason="",
            )
        )
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        _backdate_owner(store, "j-charge", pid=999_999, start_time=0.0)
        _force_heartbeat_past(store, "j-charge")
        touched = runner.check_stale_leases(lease_ttl=60)
        assert len(touched) == 1
        fetched = store.get_job("j-charge")
        assert fetched is not None
        assert fetched.status == UNKNOWN
        assert _count_leases(store, "j-charge") == 1
        # And it was definitely NOT requeued.
        assert fetched.status != SUBMITTED

    def test_dead_owner_idempotent_is_requeued(self, runner, store, scheduler):
        """Dead owner + idempotent → release back to submitted, capacity freed."""
        job = store.submit_job(
            _job(
                job_id="j-rebuild",
                command=["make", "build"],
                stage_name="build (idempotent)",
            )
        )
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        _backdate_owner(store, "j-rebuild", pid=999_999, start_time=0.0)
        _force_heartbeat_past(store, "j-rebuild")
        touched = runner.check_stale_leases(lease_ttl=60)
        assert len(touched) == 1
        fetched = store.get_job("j-rebuild")
        assert fetched is not None
        assert fetched.status == SUBMITTED
        # Lease freed so another worker can pick it up.
        assert _count_leases(store, "j-rebuild") == 0
        # And it IS pickable again.
        regranted = scheduler.pick_next_runnable(owner="w2")
        assert regranted is not None
        assert regranted.job_id == "j-rebuild"

    def test_fresh_heartbeat_not_touched(self, runner, store, scheduler):
        job = store.submit_job(_job(job_id="j-fresh", command=["true"]))
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        # heartbeat is fresh (just granted). Stale scan must skip it.
        touched = runner.check_stale_leases(lease_ttl=3600)
        assert touched == []
        fetched = store.get_job("j-fresh")
        assert fetched is not None
        assert fetched.status == LEASED

    def test_terminal_jobs_skipped(self, runner, store, scheduler):
        """A succeeded/failed job is not in a lease-live state → skipped."""
        job = store.submit_job(_job(job_id="j-done", command=["true"]))
        granted = scheduler.try_grant(job, owner="w")
        scheduler.release("j-done", granted.lease_token, final_status=SUCCEEDED)
        touched = runner.check_stale_leases(lease_ttl=1)
        assert touched == []

    def test_idempotent_via_reason_marker(self, runner, store, scheduler):
        """The 'idempotent' marker on job.reason also routes to requeue."""
        job = store.submit_job(
            _job(
                job_id="j-reason",
                command=["deploy-static"],
                reason="idempotent: pure static sync, safe to retry",
            )
        )
        granted = scheduler.try_grant(job, owner="w")
        _backdate_owner(store, "j-reason", pid=999_999, start_time=0.0)
        _force_heartbeat_past(store, "j-reason")
        runner.check_stale_leases(lease_ttl=60)
        fetched = store.get_job("j-reason")
        assert fetched is not None
        assert fetched.status == SUBMITTED

    def test_idempotent_via_command_token(self, runner, store, scheduler):
        """An 'idempotent' token anywhere in the command also routes to requeue."""
        job = store.submit_job(
            _job(
                job_id="j-token",
                command=["run-tool", "--mode=idempotent"],
            )
        )
        granted = scheduler.try_grant(job, owner="w")
        _backdate_owner(store, "j-token", pid=999_999, start_time=0.0)
        _force_heartbeat_past(store, "j-token")
        runner.check_stale_leases(lease_ttl=60)
        fetched = store.get_job("j-token")
        assert fetched is not None
        assert fetched.status == SUBMITTED

    def test_empty_heartbeat_treated_as_stale(self, runner, store, scheduler):
        """A leased job that never heartbeated is itself suspect — scan catches it."""
        job = store.submit_job(_job(job_id="j-nohb", command=["true"]))
        granted = scheduler.try_grant(job, owner="w")
        assert granted is not None
        # Force updated_at (the fallback clock) into the past.
        conn = sqlite3.connect(str(store.db_path))
        try:
            from datetime import datetime, timedelta, timezone

            old = (datetime.now(timezone.utc) - timedelta(seconds=7200)).replace(
                microsecond=0
            ).isoformat()
            # heartbeat_at stays empty; updated_at is the fallback reference.
            conn.execute(
                "UPDATE jobs SET heartbeat_at = '', updated_at = ? WHERE job_id = ?",
                (old, "j-nohb"),
            )
            conn.commit()
        finally:
            conn.close()
        _backdate_owner(store, "j-nohb", pid=999_999, start_time=0.0)
        touched = runner.check_stale_leases(lease_ttl=60)
        assert len(touched) == 1
        fetched = store.get_job("j-nohb")
        assert fetched is not None
        assert fetched.status == UNKNOWN


# ---------------------------------------------------------------------------
# Idempotency heuristic
# ---------------------------------------------------------------------------


class TestIdempotencyHeuristic:
    def test_default_is_non_idempotent(self, runner):
        job = _job(job_id="j", command=["charge-card"])
        assert runner._is_idempotent(job) is False

    def test_reason_marker(self, runner):
        job = _job(job_id="j", command=["x"], reason="idempotent rebuild")
        assert runner._is_idempotent(job) is True

    def test_stage_name_marker(self, runner):
        job = _job(job_id="j", command=["x"], stage_name="build (idempotent)")
        assert runner._is_idempotent(job) is True

    def test_command_token_marker(self, runner):
        job = _job(job_id="j", command=["tool", "--flag=idempotent"])
        assert runner._is_idempotent(job) is True

    def test_case_insensitive(self, runner):
        job = _job(job_id="j", command=["x"], reason="IDEMPOTENT")
        assert runner._is_idempotent(job) is True
