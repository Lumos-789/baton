#!/usr/bin/env python3
"""Command-line entry to the coordination resource queue.

This is the universal surface for Baton's generic resource-scheduling kernel.
Any script, deploy step, hook, or operator command can wrap itself in a
resource gate via ``queue exec``, submit background jobs via ``queue submit``,
inspect state, cancel work, freeze/unfreeze resources, audit the event log and
reap stale leases — all without writing any Python.

The CLI is a thin wrapper over the three coordination modules
(:mod:`coordination.store`, :mod:`coordination.scheduler`,
:mod:`coordination.runner`) plus :mod:`coordination.model`. It holds no state
of its own: every invocation opens the shared
``~/.baton/coordination/queue.db`` (overridable via ``--db``), does its work
in one or two short transactions, and exits.

Subcommands (argparse subparsers, modeled on :mod:`amendment`):

* ``exec``    — synchronously run a command inside a resource gate, exit code
                propagated to the shell.
* ``submit``  — submit a background job from a JSON spec file.
* ``declare`` — explicitly annotate a resource's capacity / mode (the
                concurrency-blocking-item registry).
* ``gate``    — run a disruptive command behind a drain barrier:
                freeze → wait for in-flight leases (+ optional --drain-check)
                → command → unfreeze, audited either way.
* ``status``  — list jobs / drill into a resource / inspect one job.
* ``cancel``  — cancel a queued or (with ``--force``) a running job.
* ``freeze``  — freeze a resource so no new leases may be granted on it.
* ``unfreeze``— reverse a freeze.
* ``events``  — dump the audit log (JSON).
* ``reap``    — run the stale-lease scan / cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from coordination.model import (  # noqa: E402
    CANCELED,
    EXCLUSIVE,
    FAILED,
    LEASED,
    NORMAL,
    PRIORITIES,
    ResourceClaim,
    RUNNING,
    StageSpec,
    SUBMITTED,
    SUCCEEDED,
    WAITING_RESOURCE,
    Job,
)
from coordination.runner import JobRunner  # noqa: E402
from coordination.scheduler import Scheduler  # noqa: E402
from coordination.store import CoordinationStore  # noqa: E402


# Job statuses whose work has not yet started running. ``cancel`` may flip
# these to ``canceled`` directly — there is no live process to terminate and
# no side effect to undo. Anything else needs ``--force`` (see cmd_cancel).
_PRE_RUN_STATES: tuple[str, ...] = (SUBMITTED, WAITING_RESOURCE)

# Job statuses with a live leased process — ``cancel`` needs ``--force`` here,
# and even then the CLI only attempts a best-effort signal; the scheduler is
# the authority on releasing the lease.
_RUNNING_STATES: tuple[str, ...] = (LEASED, RUNNING)


# ---------------------------------------------------------------------------
# Small output helpers
# ---------------------------------------------------------------------------


def _json(data: Any) -> str:
    """Pretty, sorted JSON for machine-readable output (submit/events/etc.)."""
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _short(job_id: str, width: int = 8) -> str:
    """First ``width`` chars of a job id — enough to disambiguate in tables."""
    return job_id[:width]


def _age(created_at: str) -> str:
    """Human-friendly elapsed-since string for a job's ``created_at``."""
    if not created_at:
        return "?"
    try:
        from datetime import datetime

        created = datetime.fromisoformat(created_at)
    except (ValueError, TypeError):
        return "?"
    now = datetime.now(created.tzinfo) if created.tzinfo else datetime.now()
    secs = max(0, int((now - created).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _build_store(args: argparse.Namespace) -> CoordinationStore:
    """Construct a store bound to the requested DB path."""
    db_path = getattr(args, "db", None)
    return CoordinationStore(db_path=db_path) if db_path else CoordinationStore()


# ---------------------------------------------------------------------------
# exec — synchronous resource-gated command execution
# ---------------------------------------------------------------------------


def _make_idempotency_key(owner: str, command: list[str], resources: list[str]) -> str:
    """Deterministic key that prevents duplicate concurrent submissions.

    Two ``exec`` invocations with the same owner, the same command, and the same
    resource set collapse onto the same job — the second one sees the first's
    already-existing job row (via the store's idempotency guard) and just waits
    on the result rather than double-submitting.
    """
    cmd_hash = hashlib.sha1(
        "\x00".join(command).encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    res_hash = hashlib.sha1(
        "\x00".join(sorted(resources)).encode("utf-8", errors="replace")
    ).hexdigest()[:12]
    return f"exec:{owner}:{cmd_hash}:{res_hash}"


def _wait_for_lease(
    scheduler: Scheduler,
    store: CoordinationStore,
    job: Job,
    owner: str,
    poll_interval: float,
) -> Job:
    """Block until ``job`` is granted a lease (or the job becomes terminal).

    Polls the scheduler for the specific job id. While waiting the job sits in
    ``submitted`` (or ``waiting-resource``); once all its claims are
    satisfiable the scheduler atomically flips it to ``leased`` and we return
    the granted job. If the job ends up in any terminal state without us ever
    leasing it (e.g. a concurrent worker canceled it) we surface that as a
    ``RuntimeError`` so the CLI exits non-zero.
    """
    terminal = {SUCCEEDED, FAILED, CANCELED}
    while True:
        granted = scheduler.try_grant(job, owner=owner)
        if granted is not None:
            return granted
        current = store.get_job(job.job_id)
        if current is None:
            raise RuntimeError(f"job {job.job_id} disappeared while waiting for lease")
        if current.status in terminal:
            raise RuntimeError(
                f"job {job.job_id} ended in status {current.status!r} before lease"
            )
        time.sleep(poll_interval)


def cmd_exec(args: argparse.Namespace) -> int:
    """Run ``args.command`` inside a resource gate, propagate its exit code.

    Builds a single-stage job (``stage name="exec"``, ``runner="shell"``) from
    the ``--resource`` flags + the trailing command, submits it (idempotently),
    waits for the lease, runs the command via the runner, and exits with the
    command's exit code. Stdout/stderr of the child are streamed to the
    parent's stdout/stderr through :meth:`JobRunner.exec_sync`.
    """
    command = _extract_trailing_command(args)
    if not command:
        print("Error: exec needs a command to run (after --)", file=sys.stderr)
        return 2

    resources = list(args.resource or [])
    if not resources:
        print("Error: exec needs at least one --resource", file=sys.stderr)
        return 2

    owner = args.owner or f"queue-cli:{os.getpid()}"
    reason = args.reason or f"queue exec: {' '.join(command[:4])}"
    store = _build_store(args)
    scheduler = Scheduler(store=store)
    runner = JobRunner(scheduler=scheduler, store=store)

    claims = [ResourceClaim(resource_key=key, mode=EXCLUSIVE) for key in resources]
    stage = StageSpec(
        name="exec",
        runner="shell",
        claims=claims,
        command=command,
    )
    idempotency_key = _make_idempotency_key(owner, command, resources)
    job = Job(
        job_id=idempotency_key,
        idempotency_key=idempotency_key,
        submitter=owner,
        project=args.project or "cli",
        feature=args.feature or "exec",
        priority=args.priority,
        reason=reason,
        stages=[stage],
    )
    submitted = store.submit_job(job)

    # If the job already existed (idempotent hit) and someone else already ran
    # it to a terminal state, surface that instead of re-running. Otherwise
    # wait for our own lease and execute.
    if submitted.status in {SUCCEEDED, FAILED, CANCELED}:
        print(
            f"job {submitted.job_id} already {submitted.status} — not re-run",
            file=sys.stderr,
        )
        return 0 if submitted.status == SUCCEEDED else 1

    try:
        granted = _wait_for_lease(
            scheduler, store, submitted, owner=owner, poll_interval=args.poll_interval
        )
    except KeyboardInterrupt:
        # Interrupted while still queued: no child, no lease — cancel the
        # job instead of leaving a queued row whose owner will never claim.
        _flip_to_canceled(
            store, submitted, reason="interrupted while waiting for lease"
        )
        print(
            f"interrupted while waiting for lease: job {submitted.job_id} canceled",
            file=sys.stderr,
        )
        return 130
    try:
        exit_code, stdout, stderr = runner.exec_sync(
            granted, command=command, timeout=args.timeout
        )
    except KeyboardInterrupt:
        # exec_sync re-raises only after killing the child's process group
        # (the child's own session never saw the terminal's SIGINT) and
        # releasing the lease with final_status=failed. 130 = 128+SIGINT.
        print(
            "interrupted: child process group killed, lease released, "
            "job marked failed",
            file=sys.stderr,
        )
        return 130
    # The runner captures the child's stdout/stderr; surface them on the
    # parent's streams so ``queue exec`` behaves like a normal command from
    # the shell's point of view (exit code is still the propagated truth).
    if stdout:
        sys.stdout.write(stdout)
        if not stdout.endswith("\n"):
            sys.stdout.write("\n")
    if stderr:
        sys.stderr.write(stderr)
        if not stderr.endswith("\n"):
            sys.stderr.write("\n")
    return int(exit_code)


def _extract_trailing_command(args: argparse.Namespace) -> list[str]:
    """Recover the user's command argv from the parsed namespace.

    argparse leaves the trailing argv (everything after ``--`` or the first
    unknown token) in ``args.command`` as a ``REMAINDER`` list. When the user
    used the explicit ``--`` separator that token is the first element; we
    strip it. An empty/None remainder means the user forgot the command.
    """
    raw = getattr(args, "command", None) or []
    if raw and raw[0] == "--":
        raw = raw[1:]
    return list(raw)


# ---------------------------------------------------------------------------
# submit — background job from a JSON spec file
# ---------------------------------------------------------------------------


def cmd_submit(args: argparse.Namespace) -> int:
    """Submit a background job described by ``args.spec`` (path to JSON).

    The JSON shape mirrors :meth:`Job.from_dict` plus a ``stages`` list whose
    each element is :meth:`StageSpec.from_dict`. On success prints the
    persisted ``job_id`` (suitable for piping into ``status --job``).
    """
    spec_path = Path(args.spec)
    if not spec_path.is_file():
        print(f"Error: spec file does not exist: {spec_path}", file=sys.stderr)
        return 1
    try:
        data = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Error: spec is not valid JSON: {exc}", file=sys.stderr)
        return 1
    try:
        job = Job.from_dict(data)
    except (KeyError, ValueError, TypeError) as exc:
        print(f"Error: spec is missing required fields or has invalid fields: {exc}", file=sys.stderr)
        return 1

    store = _build_store(args)
    persisted = store.submit_job(job)
    print(_json({"job_id": persisted.job_id, "status": persisted.status}))
    return 0


# ---------------------------------------------------------------------------
# status — queue / resource / job inspection
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    """Print queue state, filtered by resource / job / status.

    * No filters  → one-line-per-job summary table.
    * ``--resource`` → that resource's lease holders + the jobs waiting on it.
    * ``--job``  → full job detail + its event history.
    """
    store = _build_store(args)

    if args.job:
        return _status_job(store, args.job)
    if args.resource:
        return _status_resource(store, args.resource)
    return _status_table(store, args.status)


def _status_table(store: CoordinationStore, status: str | None) -> int:
    jobs = store.list_jobs(status=status)
    if not jobs:
        print("(no jobs)")
        return 0
    print(
        f"{'JOB':<10} {'STATUS':<18} {'PRIORITY':<10} {'OWNER':<16} {'AGE':<6} {'RESOURCES'}"
    )
    for j in jobs:
        resources = _job_resource_summary(j)
        owner = j.owner or "-"
        print(
            f"{_short(j.job_id):<10} {j.status:<18} {j.priority:<10} "
            f"{owner:<16} {_age(j.created_at):<6} {resources}"
        )
    return 0


def _status_resource(store: CoordinationStore, resource_key: str) -> int:
    leases = _list_leases_for_resource(store, resource_key)
    res = store.get_resource(resource_key)
    if res is not None:
        print(
            f"resource: {res.resource_key}  capacity={res.capacity}  "
            f"mode={res.mode}  frozen={res.frozen}"
        )
    else:
        print(f"resource: {resource_key}  (undeclared → defaults: capacity=1 exclusive)")
    print()
    print("leases:")
    if not leases:
        print("  (none)")
    for row in leases:
        print(
            f"  job={_short(row['job_id'])}  owner={row['owner']}  "
            f"units={row['units']}  epoch={row['epoch']}  "
            f"heartbeat={row['heartbeat_at'] or '-'}"
        )
    print()
    print("waiting:")
    waiters = _jobs_waiting_for_resource(store, resource_key)
    if not waiters:
        print("  (none)")
    for j in waiters:
        print(f"  job={_short(j.job_id)}  status={j.status}  priority={j.priority}")
    return 0


def _status_job(store: CoordinationStore, job_id: str) -> int:
    job = store.get_job(job_id)
    if job is None:
        # Try prefix match — common case is passing a truncated job_id.
        matches = [j for j in store.list_jobs() if j.job_id.startswith(job_id)]
        if len(matches) == 1:
            job = matches[0]
        elif len(matches) > 1:
            print(
                f"Error: '{job_id}' matches multiple jobs: {[m.job_id for m in matches]}",
                file=sys.stderr,
            )
            return 1
    if job is None:
        print(f"Error: job not found: {job_id}", file=sys.stderr)
        return 1
    print(_json(job.to_dict()))
    print("\nevents:")
    events = store.list_events(job_id=job.job_id, limit=200)
    for e in events:
        print(
            f"  {e['created_at']}  {e['event_type']:<20}  "
            f"{json.dumps(e['payload'], ensure_ascii=False, sort_keys=True)}"
        )
    return 0


def _job_resource_summary(job: Job) -> str:
    """Comma-joined list of the resource keys claimed by the job's stages."""
    keys: list[str] = []
    for stage in job.stages:
        for claim in stage.claims:
            if claim.resource_key not in keys:
                keys.append(claim.resource_key)
    return ",".join(keys) if keys else "-"


def _list_leases_for_resource(
    store: CoordinationStore, resource_key: str
) -> list[dict[str, Any]]:
    """Live lease rows for ``resource_key`` (read-only query)."""
    conn = sqlite3.connect(str(store.db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM leases WHERE resource_key = ? ORDER BY epoch DESC",
            (resource_key,),
        ).fetchall()
        return [
            {
                "job_id": str(r["job_id"]),
                "owner": str(r["owner"]),
                "units": int(r["units"]),
                "epoch": int(r["epoch"]),
                "heartbeat_at": str(r["heartbeat_at"]),
            }
            for r in rows
        ]
    finally:
        conn.close()


def _jobs_waiting_for_resource(
    store: CoordinationStore, resource_key: str
) -> list[Job]:
    """Jobs whose current stage claims ``resource_key`` and aren't running yet."""
    waiting: list[Job] = []
    for job in store.list_jobs():
        if job.status in _RUNNING_STATES or job.status in {
            SUCCEEDED,
            FAILED,
            CANCELED,
        }:
            continue
        for stage in job.stages:
            if any(c.resource_key == resource_key for c in stage.claims):
                waiting.append(job)
                break
    return waiting


# ---------------------------------------------------------------------------
# cancel — terminate a queued or running job
# ---------------------------------------------------------------------------


def cmd_cancel(args: argparse.Namespace) -> int:
    """Cancel ``args.job_id``.

    Queued jobs (``submitted`` / ``waiting-resource``) flip to ``canceled``
    directly. Running jobs (``leased`` / ``running``) require ``--force``: the
    CLI then attempts to signal the live process group (best-effort) and
    releases the lease through the scheduler so capacity frees immediately.
    """
    store = _build_store(args)
    scheduler = Scheduler(store=store)
    job = _resolve_job(store, args.job_id)
    if job is None:
        print(f"Error: job not found: {args.job_id}", file=sys.stderr)
        return 1

    if job.status in {SUCCEEDED, FAILED, CANCELED}:
        print(f"job {job.job_id} is already in terminal state {job.status}, no need to cancel")
        return 0

    if job.status in _PRE_RUN_STATES:
        _flip_to_canceled(store, job, reason=args.reason or "canceled via CLI")
        print(f"job {job.job_id} canceled")
        return 0

    if job.status in _RUNNING_STATES:
        if not args.force:
            print(
                f"Error: job {job.job_id} is running (status={job.status}), "
                "--force is required to cancel",
                file=sys.stderr,
            )
            return 1
        return _cancel_running(store, scheduler, job, reason=args.reason or "")


def _resolve_job(store: CoordinationStore, job_id: str) -> Job | None:
    """Look up by exact id, falling back to a unique prefix match."""
    job = store.get_job(job_id)
    if job is not None:
        return job
    matches = [j for j in store.list_jobs() if j.job_id.startswith(job_id)]
    return matches[0] if len(matches) == 1 else None


def _flip_to_canceled(
    store: CoordinationStore, job: Job, reason: str
) -> None:
    """Stamp a queued job ``canceled`` + record the event (one transaction)."""
    conn = sqlite3.connect(str(store.db_path), timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        from orchestration.plan_model import utc_now

        now = utc_now()
        conn.execute(
            "UPDATE jobs SET status = ?, reason = ?, updated_at = ? WHERE job_id = ?",
            (CANCELED, reason, now, job.job_id),
        )
        conn.execute(
            "INSERT INTO events (event_id, job_id, event_type, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                _new_event_id(),
                job.job_id,
                "job.canceled",
                json.dumps({"reason": reason, "from_status": job.status}),
                now,
            ),
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


def _cancel_running(
    store: CoordinationStore,
    scheduler: Scheduler,
    job: Job,
    reason: str,
) -> int:
    """Force-cancel a leased/running job: signal the process group, then release."""
    signalled = _signal_owner(job, graceful=True)
    if signalled:
        # Give the process a moment to terminate on SIGTERM before escalating.
        deadline = time.time() + 10
        while time.time() < deadline:
            if not _owner_alive(job):
                break
            time.sleep(0.2)
        else:
            _signal_owner(job, graceful=False)
    released = scheduler.release(
        job.job_id, job.lease_token, final_status=CANCELED
    )
    if released is None:
        # Lease token no longer matched (concurrent scan already moved it) —
        # re-stamp status directly so the user sees ``canceled``.
        _stamp_canceled_direct(store, job, reason=reason)
    store.record_event(
        job.job_id,
        "job.canceled",
        {"reason": reason or "force-canceled via CLI", "from_status": job.status},
    )
    print(f"job {job.job_id} force-canceled")
    return 0


def _signal_owner(job: Job, graceful: bool) -> bool:
    """Best-effort signal the job's owner process group. Returns True if sent."""
    import signal as signal_mod

    pid = int(job.owner_pid or 0)
    if pid <= 0:
        return False
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return False
    sig = signal_mod.SIGTERM if graceful else signal_mod.SIGKILL
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _owner_alive(job: Job) -> bool:
    """Quick pid-existence check (used by the cancel grace window)."""
    pid = int(job.owner_pid or 0)
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError,):
        return False
    except (PermissionError,):
        return True
    return True


def _stamp_canceled_direct(
    store: CoordinationStore, job: Job, reason: str
) -> None:
    """Last-resort status update when the scheduler can't release the lease.

    Also deletes the job's lease rows — without this the job is marked
    ``canceled`` but its leases linger, permanently consuming resource
    capacity so later claims on the same resource can never be granted.
    """
    # Match the kernel's busy_timeout (30s) so a contended DB doesn't make
    # this cancel path bail earlier than the scheduler itself.
    conn = sqlite3.connect(str(store.db_path), timeout=30)
    try:
        from orchestration.plan_model import utc_now

        now = utc_now()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE jobs SET status = ?, reason = ?, updated_at = ? WHERE job_id = ?",
            (CANCELED, reason, now, job.job_id),
        )
        # Free the capacity held by this job's leases (scheduler.release does
        # this on the happy path; this fallback must match or resources leak).
        conn.execute("DELETE FROM leases WHERE job_id = ?", (job.job_id,))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def _new_event_id() -> str:
    import uuid

    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# freeze / unfreeze — resource levelers
# ---------------------------------------------------------------------------


def cmd_freeze(args: argparse.Namespace) -> int:
    store = _build_store(args)
    store.freeze_resource(
        args.resource_key,
        reason=args.reason or "frozen via CLI",
        operator=args.operator or os.environ.get("USER") or "cli",
    )
    print(f"resource {args.resource_key} frozen")
    return 0


def cmd_unfreeze(args: argparse.Namespace) -> int:
    store = _build_store(args)
    store.unfreeze_resource(
        args.resource_key,
        operator=args.operator or os.environ.get("USER") or "cli",
    )
    print(f"resource {args.resource_key} unfrozen")
    return 0


# ---------------------------------------------------------------------------
# declare — explicit resource annotation (concurrency-blocking items)
# ---------------------------------------------------------------------------


def cmd_declare(args: argparse.Namespace) -> int:
    """Declare (or re-declare) a resource's capacity / mode.

    This is the annotation half of the concurrency-blocking-item protocol: a
    flow states up front "this shared resource exists and holds N units", so
    every later ``exec`` claim and ``gate`` drain knows the contract without
    relying on the lazy capacity=1 default.
    """
    store = _build_store(args)
    res = store.declare_resource(
        args.resource_key,
        capacity=args.capacity,
        mode=args.mode,
        reason=args.reason or "",
        operator=args.operator or os.environ.get("USER") or "cli",
    )
    print(
        f"resource {res.resource_key} declared: capacity={res.capacity} "
        f"mode={res.mode} frozen={res.frozen}"
    )
    return 0


# ---------------------------------------------------------------------------
# gate — disruption barrier (freeze → drain → command → unfreeze)
# ---------------------------------------------------------------------------


def cmd_gate(args: argparse.Namespace) -> int:
    """Run a disruptive command behind a drain barrier.

    The protocol for concurrency-blocking items whose disruption cannot be
    expressed as a capacity claim (a deploy restarts a shared service and
    kills whatever is in flight on it):

    1. ``freeze`` every ``--resource`` — new claimants queue up (wait, not
       fail), in-flight leases are untouched.
    2. Poll until all unexpired leases on those resources are gone (the
       in-flight batch drained), plus an optional ``--drain-check`` command
       that must exit 0 (for truth sources the queue cannot see, e.g. a DB
       query for running jobs).
    3. Run the command. Exit code is propagated.
    4. ``unfreeze`` everything in a ``finally`` — waiters resume — and record
       the gate outcome in the audit log either way.

    Drain timeout exits 124 (``timeout``(1) convention) with the holders
    listed; the command is NOT run and the resources are unfrozen again.
    A SIGINT (Ctrl-C) during drain or during the command unfreezes, records
    ``drain-interrupted`` (command never ran) or ``command-interrupted``
    (command was started and killed) in the audit log, and exits 130.
    """
    command = _extract_trailing_command(args)
    if not command:
        print("Error: gate needs a command to run (after --)", file=sys.stderr)
        return 2
    resources = list(args.resource or [])
    if not resources:
        print("Error: gate needs at least one --resource", file=sys.stderr)
        return 2

    store = _build_store(args)
    operator = args.operator or os.environ.get("USER") or "cli"
    reason = args.reason or f"queue gate: {' '.join(command[:4])}"

    for key in resources:
        store.freeze_resource(key, reason=reason, operator=operator)
    store.record_resource_event(
        "resource.gate.opened",
        {"resources": resources, "reason": reason, "operator": operator},
    )
    print(f"gate open: froze {', '.join(resources)} — waiting for in-flight leases to drain")

    outcome = "drain-timeout"
    exit_code = 124
    phase = "drain"
    try:
        deadline = time.monotonic() + args.drain_timeout
        last_signature: str | None = None
        while True:
            live = store.list_live_leases(resources)
            signature = ",".join(
                sorted(f"{row['resource_key']}:{row['job_id']}" for row in live)
            )
            if signature != last_signature:
                if live:
                    holders = ", ".join(
                        f"{row['resource_key']}:{_short(str(row['job_id']))}({row['owner']})"
                        for row in live
                    )
                    print(f"  draining: {len(live)} lease(s) still live — {holders}")
                else:
                    print("  queue leases drained")
                last_signature = signature
            if not live:
                if not args.drain_check:
                    break
                check = subprocess.run(
                    args.drain_check, shell=True, capture_output=True, text=True
                )
                if check.returncode == 0:
                    print("  drain-check passed")
                    break
                detail = (check.stderr or check.stdout or "").strip().splitlines()
                if detail:
                    print(f"  drain-check busy — {detail[-1][:120]}")
                # fall through to the sleep/deadline check below
            if time.monotonic() >= deadline:
                print(
                    f"Error: drain timeout after {args.drain_timeout}s — "
                    "command NOT run, resources unfrozen",
                    file=sys.stderr,
                )
                for row in live:
                    print(
                        f"  holder: {row['resource_key']} job={_short(str(row['job_id']))} "
                        f"owner={row['owner']} expires_at={row['expires_at']}",
                        file=sys.stderr,
                    )
                if args.drain_check:
                    print(
                        f"  (drain-check still failing: {args.drain_check})",
                        file=sys.stderr,
                    )
                return 124
            time.sleep(args.poll_interval)

        print(f"drained — running: {' '.join(command)}")
        phase = "command"
        proc = subprocess.run(command)
        exit_code = int(proc.returncode)
        outcome = f"command-exit-{exit_code}"
        return exit_code
    except KeyboardInterrupt:
        # The gated command is already dead here (subprocess.run kills it on
        # KeyboardInterrupt), so the audit row must name the interruption
        # instead of the pre-initialized drain-timeout label — whose documented
        # meaning ("command NOT run") is false once the command had started.
        # 130 = 128+SIGINT, the same contract as exec's interrupt path.
        outcome = f"{phase}-interrupted"
        print(
            f"interrupted during {phase}: resources unfrozen, "
            f"outcome recorded as {outcome}",
            file=sys.stderr,
        )
        return 130
    finally:
        for key in resources:
            store.unfreeze_resource(key, operator=operator)
        store.record_resource_event(
            "resource.gate.closed",
            {"resources": resources, "operator": operator, "outcome": outcome},
        )
        print(f"gate closed: unfroze {', '.join(resources)} (outcome: {outcome})")


# ---------------------------------------------------------------------------
# events — audit log dump
# ---------------------------------------------------------------------------


def cmd_events(args: argparse.Namespace) -> int:
    store = _build_store(args)
    events = store.list_events(
        job_id=args.job or "",
        event_type=args.event_type or "",
        limit=args.limit,
    )
    print(_json(events))
    return 0


# ---------------------------------------------------------------------------
# reap — stale-lease cleanup
# ---------------------------------------------------------------------------


def cmd_reap(args: argparse.Namespace) -> int:
    """Run :meth:`JobRunner.check_stale_leases` and report touched jobs.

    With ``--dry-run`` the scan is not actually run; instead a snapshot of
    jobs currently in a lease-live state is printed so an operator can preview
    what the scan would consider. Without ``--dry-run`` the scan runs for real
    and each touched job is reported with its post-triage status.
    """
    store = _build_store(args)
    runner = JobRunner(scheduler=Scheduler(store=store), store=store)
    if args.dry_run:
        print("(dry-run — no mutations performed)")
        live = [j for j in store.list_jobs() if j.status in _RUNNING_STATES]
        if not live:
            print("(no live-leased jobs to scan)")
            return 0
        for j in live:
            print(
                f"  job={_short(j.job_id)}  status={j.status}  "
                f"owner_pid={j.owner_pid}  heartbeat={j.heartbeat_at or '-'}"
            )
        return 0
    touched = runner.check_stale_leases(lease_ttl=args.lease_ttl)
    if not touched:
        print("(no stale leases)")
        return 0
    for j in touched:
        print(f"  job={_short(j.job_id)}  status={j.status}  reason={j.reason}")
    return 0


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level parser with all ten subcommands registered."""
    parser = argparse.ArgumentParser(
        prog="queue",
        description=(
            "Baton coordination queue CLI. Wrap any command in a resource "
            "gate, submit/inspect/cancel background jobs, freeze resources, "
            "audit events, and reap stale leases."
        ),
    )
    parser.add_argument(
        "--db",
        help=(
            "Path to the coordination DB (default: "
            "~/.baton/coordination/queue.db)"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # exec ---------------------------------------------------------------
    exec_cmd = sub.add_parser(
        "exec",
        help="run a command inside a resource gate, exit code propagated",
        description=(
            "Wrap a command in a single-stage job that claims the given "
            "resources, wait for the lease, run the command, and exit with "
            "its exit code. Idempotent: identical owner+command+resource "
            "submissions collapse onto one job."
        ),
    )
    exec_cmd.add_argument(
        "--resource",
        action="append",
        required=True,
        help="resource key to claim (exclusive); repeatable for multi-resource",
    )
    exec_cmd.add_argument("--reason", help="human-readable reason for the job")
    exec_cmd.add_argument(
        "--priority", choices=sorted(PRIORITIES), default=NORMAL
    )
    exec_cmd.add_argument("--owner", help="owner id (default: queue-cli:<pid>)")
    exec_cmd.add_argument("--project", default="cli", help="project tag")
    exec_cmd.add_argument("--feature", default="exec", help="feature tag")
    exec_cmd.add_argument(
        "--timeout", type=int, help="per-exec timeout (seconds)"
    )
    exec_cmd.add_argument(
        "--poll-interval",
        type=float,
        default=0.2,
        help="seconds between grant attempts while waiting (default 0.2)",
    )
    exec_cmd.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="command to run (use -- to separate from flags)",
    )
    exec_cmd.set_defaults(func=cmd_exec)

    # submit -------------------------------------------------------------
    submit_cmd = sub.add_parser(
        "submit",
        help="submit a background job from a JSON spec file",
        description=(
            "Parse a job spec JSON file and submit it. The JSON shape mirrors "
            "Job.from_dict (job_id, idempotency_key, submitter, project, "
            "feature, priority, stages[]). Prints the persisted job_id."
        ),
    )
    submit_cmd.add_argument("spec", help="path to job spec JSON file")
    submit_cmd.set_defaults(func=cmd_submit)

    # status -------------------------------------------------------------
    status_cmd = sub.add_parser(
        "status",
        help="inspect jobs / a resource / one job",
        description=(
            "With no flags: one-line-per-job summary table. --resource: that "
            "resource's leases + waiters. --job: full job detail + events."
        ),
    )
    status_cmd.add_argument("--resource", help="resource key to drill into")
    status_cmd.add_argument("--job", help="job id (or unique prefix) to inspect")
    status_cmd.add_argument("--status", help="filter summary by status")
    status_cmd.set_defaults(func=cmd_status)

    # cancel -------------------------------------------------------------
    cancel_cmd = sub.add_parser(
        "cancel",
        help="cancel a queued or (with --force) running job",
        description=(
            "Queued jobs are canceled directly. Running jobs require --force "
            "(the CLI then signals the owner process group and releases the "
            "lease via the scheduler)."
        ),
    )
    cancel_cmd.add_argument("job_id", help="job id or unique prefix")
    cancel_cmd.add_argument(
        "--force",
        action="store_true",
        help="required to cancel a running (leased/running) job",
    )
    cancel_cmd.add_argument("--reason", help="reason recorded in the audit log")
    cancel_cmd.set_defaults(func=cmd_cancel)

    # freeze / unfreeze --------------------------------------------------
    freeze_cmd = sub.add_parser(
        "freeze",
        help="freeze a resource (no new leases granted on it)",
    )
    freeze_cmd.add_argument("resource_key")
    freeze_cmd.add_argument("--reason", help="why this resource is frozen")
    freeze_cmd.add_argument("--operator", help="operator name (default $USER)")
    freeze_cmd.set_defaults(func=cmd_freeze)

    unfreeze_cmd = sub.add_parser(
        "unfreeze",
        help="reverse a freeze",
    )
    unfreeze_cmd.add_argument("resource_key")
    unfreeze_cmd.add_argument("--operator", help="operator name (default $USER)")
    unfreeze_cmd.set_defaults(func=cmd_unfreeze)

    # declare ------------------------------------------------------------
    declare_cmd = sub.add_parser(
        "declare",
        help="explicitly annotate a resource's capacity / mode",
        description=(
            "Declare (or re-declare) a resource: capacity, mode, and the "
            "reason it exists. The annotation half of the "
            "concurrency-blocking-item protocol — flows state their shared "
            "resources up front instead of relying on the lazy capacity=1 "
            "default."
        ),
    )
    declare_cmd.add_argument("resource_key")
    declare_cmd.add_argument(
        "--capacity", type=int, help="capacity units (default: keep current / 1)"
    )
    declare_cmd.add_argument(
        "--mode",
        choices=["exclusive", "capacity", "shared"],
        help="claim mode the resource advertises (default: keep current / exclusive)",
    )
    declare_cmd.add_argument("--reason", help="why this resource exists")
    declare_cmd.add_argument("--operator", help="operator name (default $USER)")
    declare_cmd.set_defaults(func=cmd_declare)

    # gate ---------------------------------------------------------------
    gate_cmd = sub.add_parser(
        "gate",
        help="run a disruptive command behind a drain barrier",
        description=(
            "freeze every --resource (new claimants wait, in-flight leases "
            "finish) → poll until all unexpired leases are gone and the "
            "optional --drain-check exits 0 → run the command → unfreeze in "
            "a finally. Drain timeout exits 124 without running the command."
        ),
    )
    gate_cmd.add_argument(
        "--resource",
        action="append",
        required=True,
        help="resource key to freeze+drain; repeatable for multi-resource",
    )
    gate_cmd.add_argument(
        "--drain-timeout",
        type=int,
        default=1800,
        help="max seconds to wait for the drain before aborting (default 1800)",
    )
    gate_cmd.add_argument(
        "--drain-check",
        help=(
            "shell command that must exit 0 before the gated command runs — "
            "for truth sources the queue cannot see (e.g. a DB query for "
            "running jobs)"
        ),
    )
    gate_cmd.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="seconds between drain polls (default 1.0)",
    )
    gate_cmd.add_argument("--reason", help="human-readable reason recorded in the audit log")
    gate_cmd.add_argument("--operator", help="operator name (default $USER)")
    gate_cmd.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="disruptive command to run once drained (use -- to separate)",
    )
    gate_cmd.set_defaults(func=cmd_gate)

    # events -------------------------------------------------------------
    events_cmd = sub.add_parser(
        "events",
        help="dump the audit log (JSON)",
    )
    events_cmd.add_argument("--job", help="filter to events for this job id")
    events_cmd.add_argument(
        "--type", dest="event_type", help="filter to this event_type"
    )
    events_cmd.add_argument("--limit", type=int, default=50)
    events_cmd.set_defaults(func=cmd_events)

    # reap ---------------------------------------------------------------
    reap_cmd = sub.add_parser(
        "reap",
        help="run the stale-lease scan / cleanup",
        description=(
            "Runs JobRunner.check_stale_leases which routes heartbeat-expired "
            "jobs to suspect / unknown / requeued per the guardian contract. "
            "--dry-run previews the scan without mutating anything."
        ),
    )
    reap_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="preview candidates without running the scan",
    )
    reap_cmd.add_argument(
        "--lease-ttl",
        type=int,
        default=3600,
        help="heartbeat age (seconds) past which a job is considered stale",
    )
    reap_cmd.set_defaults(func=cmd_reap)

    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv[1:])
    try:
        return int(args.func(args))
    except (ValueError, FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
