"""Queue CLI (scripts/queue.py) — coverage of all ten subcommands.

Pins the acceptance criteria from US-005:

1. ``exec`` wraps any command in a resource gate and propagates the exit code.
2. Two ``exec`` calls on the same exclusive resource serialize (the second
   waits for the first to finish).
3. ``freeze`` blocks a new ``exec`` (the job sits in ``submitted`` and never
   runs while the resource is frozen).
4. ``submit`` accepts a job-spec JSON file.
5. ``status`` / ``cancel`` / ``freeze`` / ``unfreeze`` / ``events`` / ``reap``
   all behave per their docstrings.
6. ``--help`` works on the top-level parser and every subcommand.

Every test uses an isolated temp DB; the global
``~/.baton/coordination/queue.db`` is never touched.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

# The script module is named ``queue`` which collides with the Python standard
# library's ``queue`` module. pytest's own plugin imports pull in the stdlib
# ``queue`` early, so a plain ``import queue`` would resolve to the cached
# stdlib module and shadow our script. Load our script explicitly by file path
# under a distinct alias so there is no ambiguity.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location(
    "baton_queue_cli", _SCRIPTS_DIR / "queue.py"
)
queue_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(queue_cli)

from coordination.model import (  # noqa: E402
    CANCELED,
    EXCLUSIVE,
    FAILED,
    LEASED,
    NORMAL,
    ResourceClaim,
    StageSpec,
    SUBMITTED,
    SUCCEEDED,
)
from coordination.runner import JobRunner  # noqa: E402
from coordination.scheduler import Scheduler  # noqa: E402
from coordination.store import CoordinationStore  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


QUEUE_SCRIPT = str(Path(__file__).parent.parent / "scripts" / "queue.py")


def _run_cli(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Invoke ``queue.py`` against ``db_path`` and capture the result."""
    return subprocess.run(
        [sys.executable, QUEUE_SCRIPT, "--db", str(db_path), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Path to an isolated temp coordination DB (file need not pre-exist)."""
    return tmp_path / "queue-cli.db"


@pytest.fixture()
def store(db_path: Path) -> CoordinationStore:
    return CoordinationStore(db_path=db_path)


# ---------------------------------------------------------------------------
# --help works (acceptance: "python3 scripts/queue.py --help and every subcommand --help works")
# ---------------------------------------------------------------------------


class TestHelp:
    def test_top_level_help(self):
        proc = _run_cli(Path("/tmp/nonexistent-help-test.db"), "--help")
        # --help exits 0 (argparse prints help and raises SystemExit(0)).
        assert proc.returncode == 0
        assert "exec" in proc.stdout
        for sub in (
            "exec",
            "submit",
            "declare",
            "gate",
            "status",
            "cancel",
            "freeze",
            "unfreeze",
            "events",
            "reap",
        ):
            assert sub in proc.stdout

    @pytest.mark.parametrize(
        "sub",
        [
            "exec",
            "submit",
            "declare",
            "gate",
            "status",
            "cancel",
            "freeze",
            "unfreeze",
            "events",
            "reap",
        ],
    )
    def test_subcommand_help(self, sub, tmp_path):
        proc = _run_cli(tmp_path / "h.db", sub, "--help")
        assert proc.returncode == 0
        # Subcommand help names the subcommand somewhere.
        assert sub in proc.stdout

    def test_missing_subcommand_errors(self):
        proc = _run_cli(Path("/tmp/none.db"))
        # argparse with required=True subparsers exits 2 on missing command.
        assert proc.returncode == 2


# ---------------------------------------------------------------------------
# exec — synchronous resource-gated execution (acceptance: exec + exit code)
# ---------------------------------------------------------------------------


class TestExec:
    def test_exec_runs_command_and_propagates_zero_exit(self, db_path):
        """``queue exec -- echo hello`` exits 0 and prints ``hello``."""
        proc = _run_cli(
            db_path,
            "exec",
            "--resource",
            "workspace:/tmp/ok",
            "--",
            "echo",
            "hello",
        )
        assert proc.returncode == 0
        assert "hello" in proc.stdout
        # The job should now be persisted as succeeded.
        store = CoordinationStore(db_path=db_path)
        jobs = store.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].status == SUCCEEDED

    def test_exec_propagates_nonzero_exit(self, db_path):
        """A failing command surfaces its exit code (here: 7)."""
        proc = _run_cli(
            db_path,
            "exec",
            "--resource",
            "workspace:/tmp/fail",
            "--",
            "sh",
            "-c",
            "exit 7",
        )
        assert proc.returncode == 7
        store = CoordinationStore(db_path=db_path)
        jobs = store.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].status == FAILED

    def test_exec_requires_resource(self, db_path):
        """No --resource → user error, exit 2."""
        proc = _run_cli(db_path, "exec", "--", "echo", "hi")
        assert proc.returncode == 2

    def test_exec_requires_command(self, db_path):
        """No command → user error, exit 2."""
        proc = _run_cli(
            db_path, "exec", "--resource", "workspace:/tmp/none"
        )
        assert proc.returncode == 2

    def test_exec_stage_is_named_exec_with_shell_runner(self, db_path):
        """The job built by exec has stage.name='exec' and runner='shell'."""
        _run_cli(
            db_path,
            "exec",
            "--resource",
            "workspace:/tmp/shape",
            "--",
            "true",
        )
        store = CoordinationStore(db_path=db_path)
        job = store.list_jobs()[0]
        assert job.stages[0].name == "exec"
        assert job.stages[0].runner == "shell"
        assert job.stages[0].claims[0].resource_key == "workspace:/tmp/shape"
        assert job.stages[0].claims[0].mode == EXCLUSIVE

    @staticmethod
    def _wait_until(condition, timeout: float = 15.0, what: str = "condition") -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.1)
        pytest.fail(f"timed out waiting for {what}")

    @staticmethod
    def _pid_dead(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return False
        except OSError:
            return True

    def test_exec_sigint_kills_child_releases_lease_exits_130(self, tmp_path):
        """Ctrl-C during exec: child killed, lease released, exit 130.

        The exec child runs in its own session (``start_new_session=True``),
        so a terminal SIGINT reaches only the queue CLI. The interrupt must
        not orphan the child nor leak the lease: the CLI kills the child's
        process group, releases the lease with ``failed`` and exits 130
        (128+SIGINT), leaving the resource immediately claimable again.
        """
        db_path = tmp_path / "ki-exec.db"
        pid_file = tmp_path / "child.pid"
        child_code = (
            "import os,time\n"
            f"open({str(pid_file)!r},'w').write(str(os.getpid()))\n"
            "time.sleep(60)\n"
        )
        proc = subprocess.Popen(
            [
                sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
                "exec", "--resource", "ki:exec", "--",
                sys.executable, "-c", child_code,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self._wait_until(pid_file.exists, what="exec child to start")
            child_pid = int(pid_file.read_text().strip())

            proc.send_signal(signal.SIGINT)
            out, err = proc.communicate(timeout=20)

            assert proc.returncode == 130
            assert "lease released" in err
            self._wait_until(
                lambda: self._pid_dead(child_pid),
                timeout=10,
                what="interrupted child to be killed",
            )
            store = CoordinationStore(db_path=db_path)
            jobs = store.list_jobs()
            assert len(jobs) == 1
            assert jobs[0].status == FAILED
            # The lease is really gone: a fresh exec on the same resource
            # acquires it immediately instead of queueing behind a ghost.
            follow = _run_cli(db_path, "exec", "--resource", "ki:exec", "--", "true")
            assert follow.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_exec_sigint_while_queued_cancels_job_exits_130(self, tmp_path):
        """Ctrl-C while still waiting for the lease: queued job canceled.

        Without the cancel, the interrupted waiter left a queued job whose
        owner pid would never claim it — a zombie row in every later
        ``status`` output.
        """
        db_path = tmp_path / "ki-wait.db"
        holder = subprocess.Popen(
            [
                sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
                "exec", "--resource", "ki:wait", "--",
                sys.executable, "-c", "import time; time.sleep(60)",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            store = CoordinationStore(db_path=db_path)
            self._wait_until(
                lambda: bool(store.list_jobs())
                and store.list_jobs()[0].status == LEASED,
                what="holder to acquire the lease",
            )
            waiter = subprocess.Popen(
                [
                    sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
                    "exec", "--resource", "ki:wait", "--", "true",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self._wait_until(
                lambda: len(store.list_jobs()) == 2,
                what="waiter job to be submitted",
            )
            # Let the waiter reach its lease-poll loop before interrupting.
            time.sleep(0.5)

            waiter.send_signal(signal.SIGINT)
            out, err = waiter.communicate(timeout=20)

            assert waiter.returncode == 130
            assert "canceled" in err
            statuses = {job.job_id: job.status for job in store.list_jobs()}
            assert CANCELED in statuses.values()
        finally:
            holder.kill()
            holder.communicate()


# ---------------------------------------------------------------------------
# exec serialization (acceptance: "two exec calls contending for the same exclusive resource serialize")
# ---------------------------------------------------------------------------


class TestExecSerialization:
    def test_two_execs_on_same_exclusive_resource_serialize(self, db_path):
        """Second exec must wait until the first releases the resource.

        Uses a long sleep as the first command; the second ``echo`` invocation
        cannot run until the sleep finishes. We assert on the *ordering* of
        completion: the marker file written by job 2 cannot appear before job
        1 has finished (which it signals by writing its own marker first).
        """
        marker_dir = db_path.parent / "markers"
        marker_dir.mkdir(parents=True, exist_ok=True)
        m1 = marker_dir / "first.done"
        m2 = marker_dir / "second.done"
        if m1.exists():
            m1.unlink()
        if m2.exists():
            m2.unlink()

        # Job 1 holds the resource for ~1.2s, then writes its marker.
        first_cmd = (
            f"sleep 1.2 && touch {m1}"
            if _has_sleep_touch()
            else _python_sleep_then_touch(1.2, m1)
        )
        # Job 2 writes its marker as soon as it gets to run.
        second_cmd = (
            f"touch {m2}"
            if _has_sleep_touch()
            else _python_touch(m2)
        )

        # Launch job 1 first.
        p1 = subprocess.Popen(
            [sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
             "exec", "--resource", "workspace:/tmp/serial",
             "--owner", "w1", "--", "sh", "-c", first_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        # Give the first job a head start so it grabs the lease first.
        time.sleep(0.4)
        # Launch job 2 — it should block until job 1 releases.
        p2 = subprocess.Popen(
            [sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
             "exec", "--resource", "workspace:/tmp/serial",
             "--owner", "w2", "--", "sh", "-c", second_cmd],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

        # While both are in flight, job 2 should NOT have produced its marker
        # (the resource is held by job 1).
        time.sleep(0.5)
        assert not m2.exists(), (
            "second exec produced its marker before the first released the "
            "resource — serialization broken"
        )

        out1 = p1.wait(timeout=30)
        out2 = p2.wait(timeout=30)
        assert out1 == 0
        assert out2 == 0

        # Both markers exist now, and m1 was written first (mtime m1 <= m2).
        assert m1.exists()
        assert m2.exists()
        assert m1.stat().st_mtime <= m2.stat().st_mtime + 0.01


def _has_sleep_touch() -> bool:
    """True iff ``sh -c 'touch X'`` is available on this host (POSIX)."""
    return True  # tests run on macOS/Linux; sh+touch+sleep are present


def _python_sleep_then_touch(secs: float, path: Path) -> str:
    py = (
        f"import time, pathlib; time.sleep({secs}); "
        f"pathlib.Path({str(path)!r}).touch()"
    )
    return f"{sys.executable} -c {shell_quote(py)}"


def _python_touch(path: Path) -> str:
    py = f"import pathlib; pathlib.Path({str(path)!r}).touch()"
    return f"{sys.executable} -c {shell_quote(py)}"


def shell_quote(s: str) -> str:
    if not s:
        return "''"
    if all(c.isalnum() or c in "-_./=" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# freeze blocks exec (acceptance: "after freeze, a new exec sits in the queue without running")
# ---------------------------------------------------------------------------


class TestFreezeBlocksExec:
    def test_freeze_keeps_new_exec_in_submitted(self, db_path, tmp_path):
        """A frozen resource must hold a new exec job in ``submitted``.

        Launches exec in a background process, lets it spin, and asserts that
        the job never leaves ``submitted`` and the command's side effect never
        happens. Then unfreezes and confirms the job proceeds.
        """
        marker = tmp_path / "ran.flag"
        if marker.exists():
            marker.unlink()

        # Freeze the resource first.
        freeze_proc = _run_cli(
            db_path, "freeze", "workspace:/tmp/frozen", "--reason", "test"
        )
        assert freeze_proc.returncode == 0
        assert CoordinationStore(db_path=db_path).is_frozen(
            "workspace:/tmp/frozen"
        )

        # Launch exec in background — it should block.
        proc = subprocess.Popen(
            [sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
             "exec", "--resource", "workspace:/tmp/frozen",
             "--owner", "blocked", "--", "sh", "-c", f"touch {marker}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            # Give it a moment, then verify it's still running and the marker
            # hasn't appeared.
            time.sleep(1.0)
            assert proc.poll() is None, "exec should still be blocked on frozen resource"
            assert not marker.exists(), "command must not run while resource is frozen"

            store = CoordinationStore(db_path=db_path)
            # The job is submitted with our --owner as submitter (the lease
            # grant hasn't happened yet, so Job.owner is still empty — we
            # match on submitter / resource instead).
            jobs = [
                j
                for j in store.list_jobs()
                if j.submitter == "blocked"
                or any(
                    c.resource_key == "workspace:/tmp/frozen"
                    for s in j.stages
                    for c in s.claims
                )
            ]
            assert len(jobs) >= 1
            assert all(j.status == SUBMITTED for j in jobs)
        finally:
            # Unfreeze so the background process can finish and we can reap it.
            _run_cli(db_path, "unfreeze", "workspace:/tmp/frozen")
            proc.wait(timeout=30)

        # After unfreeze the job should run and produce the marker.
        assert proc.returncode == 0
        assert marker.exists()


# ---------------------------------------------------------------------------
# submit — background job from JSON spec
# ---------------------------------------------------------------------------


class TestSubmit:
    def test_submit_persists_job_from_spec_file(self, db_path, tmp_path):
        spec = {
            "job_id": "bg-1",
            "idempotency_key": "idem-bg-1",
            "submitter": "tester",
            "project": "baton",
            "feature": "core-buildout",
            "priority": "normal",
            "stages": [
                {
                    "name": "build",
                    "runner": "shell",
                    "claims": [
                        {"resource_key": "workspace:/tmp/bg", "mode": "exclusive"}
                    ],
                    "command": ["echo", "from-spec"],
                }
            ],
        }
        spec_path = tmp_path / "job.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")

        proc = _run_cli(db_path, "submit", str(spec_path))
        assert proc.returncode == 0
        out = json.loads(proc.stdout)
        assert out["job_id"] == "bg-1"
        assert out["status"] == SUBMITTED

        store = CoordinationStore(db_path=db_path)
        job = store.get_job("bg-1")
        assert job is not None
        assert job.submitter == "tester"
        assert job.stages[0].claims[0].resource_key == "workspace:/tmp/bg"

    def test_submit_missing_file_errors(self, db_path, tmp_path):
        proc = _run_cli(db_path, "submit", str(tmp_path / "nope.json"))
        assert proc.returncode == 1

    def test_submit_bad_json_errors(self, db_path, tmp_path):
        spec = tmp_path / "bad.json"
        spec.write_text("{not valid json", encoding="utf-8")
        proc = _run_cli(db_path, "submit", str(spec))
        assert proc.returncode == 1

    def test_submit_missing_required_field_errors(self, db_path, tmp_path):
        spec = tmp_path / "partial.json"
        # Missing submitter/project/feature → Job.from_dict raises KeyError.
        spec.write_text(
            json.dumps(
                {
                    "job_id": "p",
                    "idempotency_key": "i",
                    "stages": [{"name": "s"}],
                }
            ),
            encoding="utf-8",
        )
        proc = _run_cli(db_path, "submit", str(spec))
        assert proc.returncode == 1


# ---------------------------------------------------------------------------
# status — table / resource / job
# ---------------------------------------------------------------------------


class TestStatus:
    def _seed(self, store: CoordinationStore) -> None:
        store.submit_job(
            _job_factory(job_id="job-alpha", resource="workspace:/tmp/a")
        )
        store.submit_job(
            _job_factory(job_id="job-beta", resource="workspace:/tmp/b")
        )

    def test_status_table_lists_all_jobs(self, db_path):
        store = CoordinationStore(db_path=db_path)
        self._seed(store)
        proc = _run_cli(db_path, "status")
        assert proc.returncode == 0
        # The table truncates job ids to 8 chars for column width, so match
        # the truncated prefix.
        assert "job-alph" in proc.stdout
        assert "job-beta" in proc.stdout
        assert "submitted" in proc.stdout

    def test_status_filter_by_status(self, db_path):
        store = CoordinationStore(db_path=db_path)
        self._seed(store)
        proc = _run_cli(db_path, "status", "--status", SUBMITTED)
        assert proc.returncode == 0
        assert "job-alph" in proc.stdout

    def test_status_resource_shows_leases_and_waiters(self, db_path):
        store = CoordinationStore(db_path=db_path)
        # One leased job holding the resource, one waiting on it.
        holding = store.submit_job(
            _job_factory(job_id="job-hold", resource="workspace:/tmp/r")
        )
        scheduler = Scheduler(store=store)
        granted = scheduler.try_grant(holding, owner="w")
        assert granted is not None
        store.submit_job(
            _job_factory(job_id="job-wait", resource="workspace:/tmp/r")
        )

        proc = _run_cli(db_path, "status", "--resource", "workspace:/tmp/r")
        assert proc.returncode == 0
        assert "workspace:/tmp/r" in proc.stdout
        assert "job-hold" in proc.stdout
        assert "job-wait" in proc.stdout

    def test_status_job_shows_detail_and_events(self, db_path):
        store = CoordinationStore(db_path=db_path)
        store.submit_job(
            _job_factory(job_id="job-detail", resource="workspace:/tmp/d")
        )
        proc = _run_cli(db_path, "status", "--job", "job-detail")
        assert proc.returncode == 0
        out = json.loads(proc.stdout.split("\nevents:")[0])
        assert out["job_id"] == "job-detail"
        # The submit event should be visible in the events section.
        assert "job.submitted" in proc.stdout

    def test_status_job_prefix_match(self, db_path):
        store = CoordinationStore(db_path=db_path)
        store.submit_job(
            _job_factory(job_id="job-prefix-123", resource="workspace:/tmp/p")
        )
        proc = _run_cli(db_path, "status", "--job", "job-prefix")
        assert proc.returncode == 0
        assert "job-prefix-123" in proc.stdout

    def test_status_job_not_found(self, db_path):
        proc = _run_cli(db_path, "status", "--job", "nope")
        assert proc.returncode == 1

    def test_status_empty_queue(self, db_path):
        proc = _run_cli(db_path, "status")
        assert proc.returncode == 0
        assert "no jobs" in proc.stdout


# ---------------------------------------------------------------------------
# cancel — queued vs running
# ---------------------------------------------------------------------------


class TestCancel:
    def test_cancel_queued_job(self, db_path):
        store = CoordinationStore(db_path=db_path)
        store.submit_job(
            _job_factory(job_id="job-q", resource="workspace:/tmp/q")
        )
        proc = _run_cli(db_path, "cancel", "job-q", "--reason", "test")
        assert proc.returncode == 0
        assert CANCELED == store.get_job("job-q").status

    def test_cancel_already_terminal_is_noop(self, db_path):
        store = CoordinationStore(db_path=db_path)
        store.submit_job(
            _job_factory(job_id="job-t", resource="workspace:/tmp/t")
        )
        # Force it to succeeded directly.
        store.record_event("job-t", "test.setup", {"note": "force succeeded"})
        _force_status(store, "job-t", SUCCEEDED)
        proc = _run_cli(db_path, "cancel", "job-t")
        assert proc.returncode == 0
        assert "terminal" in proc.stdout.lower()

    def test_cancel_running_requires_force(self, db_path):
        store = CoordinationStore(db_path=db_path)
        job = store.submit_job(
            _job_factory(job_id="job-run", resource="workspace:/tmp/run")
        )
        granted = Scheduler(store=store).try_grant(job, owner="w")
        assert granted is not None
        proc = _run_cli(db_path, "cancel", "job-run")
        assert proc.returncode == 1
        assert "--force" in proc.stderr
        # Job still leased (untouched).
        assert store.get_job("job-run").status == LEASED

    def test_cancel_not_found(self, db_path):
        proc = _run_cli(db_path, "cancel", "ghost")
        assert proc.returncode == 1


# ---------------------------------------------------------------------------
# freeze / unfreeze
# ---------------------------------------------------------------------------


class TestFreezeUnfreeze:
    def test_freeze_marks_resource_frozen(self, db_path):
        proc = _run_cli(
            db_path, "freeze", "workspace:/tmp/fz", "--reason", "maintenance"
        )
        assert proc.returncode == 0
        store = CoordinationStore(db_path=db_path)
        assert store.is_frozen("workspace:/tmp/fz")
        res = store.get_resource("workspace:/tmp/fz")
        assert res is not None
        assert res.frozen is True

    def test_unfreeze_clears_frozen(self, db_path):
        store = CoordinationStore(db_path=db_path)
        store.freeze_resource("workspace:/tmp/uf", reason="x", operator="t")
        proc = _run_cli(db_path, "unfreeze", "workspace:/tmp/uf")
        assert proc.returncode == 0
        assert store.is_frozen("workspace:/tmp/uf") is False

    def test_freeze_blocks_scheduler_grant(self, db_path):
        """End-to-end: frozen resource → try_grant returns None."""
        _run_cli(db_path, "freeze", "workspace:/tmp/blk", "--reason", "x")
        store = CoordinationStore(db_path=db_path)
        job = store.submit_job(
            _job_factory(job_id="job-blk", resource="workspace:/tmp/blk")
        )
        granted = Scheduler(store=store).try_grant(job, owner="w")
        assert granted is None
        assert store.get_job("job-blk").status == SUBMITTED


# ---------------------------------------------------------------------------
# events — audit log
# ---------------------------------------------------------------------------


class TestEvents:
    def test_events_dumps_json(self, db_path):
        store = CoordinationStore(db_path=db_path)
        store.submit_job(
            _job_factory(job_id="job-ev", resource="workspace:/tmp/ev")
        )
        proc = _run_cli(db_path, "events")
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert any(e["event_type"] == "job.submitted" for e in data)

    def test_events_filter_by_job(self, db_path):
        store = CoordinationStore(db_path=db_path)
        store.submit_job(
            _job_factory(job_id="job-a", resource="workspace:/tmp/a")
        )
        store.submit_job(
            _job_factory(job_id="job-b", resource="workspace:/tmp/b")
        )
        proc = _run_cli(db_path, "events", "--job", "job-a")
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert all(e["job_id"] == "job-a" for e in data)

    def test_events_filter_by_type(self, db_path):
        store = CoordinationStore(db_path=db_path)
        store.submit_job(
            _job_factory(job_id="job-t", resource="workspace:/tmp/t")
        )
        proc = _run_cli(db_path, "events", "--type", "job.submitted")
        assert proc.returncode == 0
        data = json.loads(proc.stdout)
        assert all(e["event_type"] == "job.submitted" for e in data)


# ---------------------------------------------------------------------------
# reap — stale-lease scan
# ---------------------------------------------------------------------------


class TestReap:
    def test_reap_dry_run_no_mutation(self, db_path):
        store = CoordinationStore(db_path=db_path)
        job = store.submit_job(
            _job_factory(job_id="job-live", resource="workspace:/tmp/reap")
        )
        Scheduler(store=store).try_grant(job, owner="w")
        proc = _run_cli(db_path, "reap", "--dry-run")
        assert proc.returncode == 0
        assert "dry-run" in proc.stdout
        # Status untouched.
        assert store.get_job("job-live").status == LEASED

    def test_reap_no_stale_reports_none(self, db_path):
        store = CoordinationStore(db_path=db_path)
        job = store.submit_job(
            _job_factory(job_id="job-fresh", resource="workspace:/tmp/fresh")
        )
        Scheduler(store=store).try_grant(job, owner="w")
        proc = _run_cli(db_path, "reap", "--lease-ttl", "3600")
        assert proc.returncode == 0
        assert "no stale leases" in proc.stdout

    def test_reap_handles_empty_queue(self, db_path):
        proc = _run_cli(db_path, "reap")
        assert proc.returncode == 0
        assert "no stale leases" in proc.stdout


# ---------------------------------------------------------------------------
# Programmatic API smoke (main() returns int)
# ---------------------------------------------------------------------------


class TestProgrammaticApi:
    def test_main_returns_int_for_exec(self, db_path, tmp_path, capsys):
        """Calling main([...]) directly works the same as the subprocess form."""
        rc = queue_cli.main(
            [
                "queue.py",
                "--db",
                str(db_path),
                "exec",
                "--resource",
                "workspace:/tmp/api",
                "--",
                "echo",
                "api",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert "api" in captured.out

    def test_main_returns_int_for_status_empty(self, db_path, capsys):
        rc = queue_cli.main(
            ["queue.py", "--db", str(db_path), "status"]
        )
        assert rc == 0


# ---------------------------------------------------------------------------
# Idempotency key helper
# ---------------------------------------------------------------------------


class TestIdempotencyKey:
    def test_same_inputs_same_key(self):
        a = queue_cli._make_idempotency_key("w", ["echo", "hi"], ["r1", "r2"])
        b = queue_cli._make_idempotency_key("w", ["echo", "hi"], ["r1", "r2"])
        assert a == b

    def test_different_owner_different_key(self):
        a = queue_cli._make_idempotency_key("w1", ["echo", "hi"], ["r1"])
        b = queue_cli._make_idempotency_key("w2", ["echo", "hi"], ["r1"])
        assert a != b

    def test_different_command_different_key(self):
        a = queue_cli._make_idempotency_key("w", ["echo", "hi"], ["r1"])
        b = queue_cli._make_idempotency_key("w", ["echo", "bye"], ["r1"])
        assert a != b

    def test_resource_order_invariant(self):
        """The resource hash is order-independent (sorted before hashing)."""
        a = queue_cli._make_idempotency_key("w", ["echo"], ["r1", "r2"])
        b = queue_cli._make_idempotency_key("w", ["echo"], ["r2", "r1"])
        assert a == b


# ---------------------------------------------------------------------------
# Internal helpers / factories used by the tests above
# ---------------------------------------------------------------------------


def _job_factory(
    *,
    job_id: str,
    resource: str = "workspace:/tmp/test",
    priority: str = NORMAL,
    command: list[str] | None = None,
    project: str = "baton",
    feature: str = "core-buildout",
    submitter: str = "tester",
) -> "Job":
    from coordination.model import Job

    if command is None:
        command = ["true"]
    return Job(
        job_id=job_id,
        idempotency_key=f"idem-{job_id}",
        submitter=submitter,
        project=project,
        feature=feature,
        priority=priority,
        stages=[
            StageSpec(
                name="exec",
                runner="shell",
                claims=[ResourceClaim(resource_key=resource, mode=EXCLUSIVE)],
                command=command,
            )
        ],
    )


def _force_status(store: CoordinationStore, job_id: str, status: str) -> None:
    """Test seam: directly UPDATE a job's status (bypasses the scheduler)."""
    import sqlite3

    from orchestration.plan_model import utc_now

    conn = sqlite3.connect(str(store.db_path))
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, updated_at = ? WHERE job_id = ?",
            (status, utc_now(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# declare — explicit resource annotation
# ---------------------------------------------------------------------------


class TestDeclare:
    def test_declare_sets_capacity_and_mode(self, db_path):
        proc = _run_cli(
            db_path,
            "declare",
            "replay-suite",
            "--capacity",
            "5",
            "--reason",
            "SUT LLM concurrency ceiling",
        )
        assert proc.returncode == 0
        assert "capacity=5" in proc.stdout
        store = CoordinationStore(db_path=db_path)
        res = store.get_resource("replay-suite")
        assert res is not None
        assert res.capacity == 5
        assert not res.frozen

    def test_redeclare_updates_only_given_fields(self, db_path):
        _run_cli(db_path, "declare", "r1", "--capacity", "3", "--mode", "capacity")
        _run_cli(db_path, "declare", "r1", "--capacity", "7")
        res = CoordinationStore(db_path=db_path).get_resource("r1")
        assert res.capacity == 7
        assert res.mode == "capacity"

    def test_declare_records_audit_event(self, db_path):
        _run_cli(db_path, "declare", "r2", "--capacity", "2")
        events = CoordinationStore(db_path=db_path).list_events(limit=10)
        assert any(e["event_type"] == "resource.declared" for e in events)

    def test_declared_capacity_governs_parallel_exec(self, db_path, tmp_path):
        """capacity=2 must let two execs overlap but hold the third."""
        _run_cli(db_path, "declare", "pair", "--capacity", "2")
        procs = [
            subprocess.Popen(
                [sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
                 "exec", "--resource", "pair", "--owner", f"p{i}",
                 "--", "sleep", "1.5"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for i in range(3)
        ]
        try:
            time.sleep(1.0)
            store = CoordinationStore(db_path=db_path)
            leased = [j for j in store.list_jobs() if j.status in queue_cli._RUNNING_STATES]
            assert len(leased) == 2, "capacity=2 should lease exactly two"
        finally:
            for p in procs:
                p.wait(timeout=30)


# ---------------------------------------------------------------------------
# gate — disruption barrier (freeze → drain → command → unfreeze)
# ---------------------------------------------------------------------------


class TestGate:
    def test_gate_runs_immediately_when_no_holders(self, db_path):
        proc = _run_cli(
            db_path, "gate", "--resource", "svc", "--", "echo", "deployed"
        )
        assert proc.returncode == 0
        assert "deployed" in proc.stdout
        assert "gate closed" in proc.stdout
        store = CoordinationStore(db_path=db_path)
        assert not store.is_frozen("svc")

    def test_gate_waits_for_inflight_lease_then_runs(self, db_path, tmp_path):
        """The gated command must not run until the in-flight lease drains."""
        holder_flag = tmp_path / "holder.done"
        gate_flag = tmp_path / "gate.ran"

        holder = subprocess.Popen(
            [sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
             "exec", "--resource", "svc", "--owner", "holder",
             "--", "sh", "-c", f"sleep 2 && touch {holder_flag}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(0.8)  # let the holder obtain its lease

        started = time.monotonic()
        gate = subprocess.Popen(
            [sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
             "gate", "--resource", "svc", "--", "sh", "-c", f"touch {gate_flag}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            time.sleep(0.7)
            assert not gate_flag.exists(), "gated command ran before drain"
            gate.wait(timeout=30)
            holder.wait(timeout=30)
        finally:
            if holder.poll() is None:
                holder.wait(timeout=30)

        assert gate.returncode == 0
        assert holder_flag.exists()
        assert gate_flag.exists()
        # The gate must have waited for the ~2s holder, not run instantly.
        assert time.monotonic() - started >= 1.5
        assert not CoordinationStore(db_path=db_path).is_frozen("svc")
        events = CoordinationStore(db_path=db_path).list_events(limit=50)
        types = [e["event_type"] for e in events]
        assert "resource.gate.opened" in types
        assert "resource.gate.closed" in types

    def test_gate_blocks_new_claims_while_open(self, db_path, tmp_path):
        """While the gate holds the resource frozen, a new exec waits."""
        release = tmp_path / "release.flag"
        gate = subprocess.Popen(
            [sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
             "gate", "--resource", "svc",
             "--", "sh", "-c", f"sleep 1.5 && touch {release}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(0.8)  # gate is now frozen+drained+running its command
        store = CoordinationStore(db_path=db_path)
        assert store.is_frozen("svc"), "resource must stay frozen while gated command runs"
        gate.wait(timeout=30)
        assert release.exists()
        assert not store.is_frozen("svc")

    def test_gate_drain_timeout_aborts_without_running(self, db_path, tmp_path):
        marker = tmp_path / "never.ran"
        holder = subprocess.Popen(
            [sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
             "exec", "--resource", "svc", "--owner", "holder", "--", "sleep", "5"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(0.8)
        try:
            proc = _run_cli(
                db_path, "gate", "--resource", "svc",
                "--drain-timeout", "1", "--", "sh", "-c", f"touch {marker}",
            )
            assert proc.returncode == 124
            assert not marker.exists(), "command must not run on drain timeout"
            assert "drain timeout" in proc.stderr
            # resources are restored even on abort
            assert not CoordinationStore(db_path=db_path).is_frozen("svc")
        finally:
            holder.wait(timeout=30)

    def test_gate_propagates_command_failure_and_unfreezes(self, db_path):
        proc = _run_cli(db_path, "gate", "--resource", "svc", "--", "sh", "-c", "exit 3")
        assert proc.returncode == 3
        assert "outcome: command-exit-3" in proc.stdout
        assert not CoordinationStore(db_path=db_path).is_frozen("svc")

    def test_gate_drain_check_must_pass(self, db_path, tmp_path):
        """A failing --drain-check keeps the gate waiting (no queue holders)."""
        marker = tmp_path / "never.ran"
        proc = _run_cli(
            db_path, "gate", "--resource", "svc",
            "--drain-check", "exit 1",
            "--drain-timeout", "2", "--", "sh", "-c", f"touch {marker}",
        )
        assert proc.returncode == 124
        assert not marker.exists()
        assert "drain-check still failing" in proc.stderr
        assert not CoordinationStore(db_path=db_path).is_frozen("svc")

    def test_gate_requires_command(self, db_path):
        proc = _run_cli(db_path, "gate", "--resource", "svc")
        assert proc.returncode == 2

    def test_gate_interrupt_during_command_records_truthful_outcome(
        self, db_path, tmp_path
    ):
        """Ctrl-C mid-command must not leave a lying audit row.

        ``outcome`` is pre-initialized to ``drain-timeout`` whose documented
        meaning is "command NOT run"; before the interrupt handler existed,
        a SIGINT during the command unfroze correctly but recorded
        ``drain-timeout`` although the command had started, and died from
        the signal with a 25-line traceback.
        """
        ran = tmp_path / "ran"
        gate = subprocess.Popen(
            [sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
             "gate", "--resource", "svc",
             "--", "sh", "-c", f"sleep 30 && touch {ran}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(1.5)  # frozen + drained + command running
        gate.send_signal(signal.SIGINT)
        _, err = gate.communicate(timeout=30)
        assert gate.returncode == 130
        assert "Traceback" not in err
        stderr_lines = [line for line in err.strip().splitlines() if line]
        assert len(stderr_lines) == 1
        assert "interrupted during command" in stderr_lines[0]
        assert not ran.exists(), "the gated command must be killed by the interrupt"
        store = CoordinationStore(db_path=db_path)
        assert not store.is_frozen("svc")
        closed = [
            e for e in store.list_events(limit=50)
            if e["event_type"] == "resource.gate.closed"
        ]
        assert closed[0]["payload"]["outcome"] == "command-interrupted"

    def test_gate_interrupt_during_drain_command_never_ran(self, db_path, tmp_path):
        """Ctrl-C while waiting for holders: command never started, audit says so."""
        marker = tmp_path / "never.ran"
        holder = subprocess.Popen(
            [sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
             "exec", "--resource", "svc", "--owner", "holder",
             "--", "sleep", "5"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        time.sleep(0.8)
        try:
            gate = subprocess.Popen(
                [sys.executable, QUEUE_SCRIPT, "--db", str(db_path),
                 "gate", "--resource", "svc",
                 "--", "sh", "-c", f"touch {marker}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            time.sleep(0.8)  # gate is frozen, waiting for the holder's lease
            gate.send_signal(signal.SIGINT)
            _, err = gate.communicate(timeout=30)
            assert gate.returncode == 130
            assert "interrupted during drain" in err
            assert "Traceback" not in err
            assert not marker.exists(), "command must not run when drain never finished"
            store = CoordinationStore(db_path=db_path)
            assert not store.is_frozen("svc")
            closed = [
                e for e in store.list_events(limit=50)
                if e["event_type"] == "resource.gate.closed"
            ]
            assert closed[0]["payload"]["outcome"] == "drain-interrupted"
        finally:
            holder.wait(timeout=30)
