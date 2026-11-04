"""Deterministic assignment verification and receipt sealing."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from orchestration.model import TaskPacket  # noqa: E402
from orchestration.receipt import (  # noqa: E402
    ReceiptError,
    seal_receipt,
    shell_command,
    verify_packet,
)
from orchestration.session_model import finalize_task_packet  # noqa: E402


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=path, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _setup(tmp_path: Path, *, commands: list[str] | None = None) -> tuple[Path, Path, dict]:
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-b", "main")
    _git(project, "config", "user.email", "t@t.t")
    _git(project, "config", "user.name", "test")
    (project / "base.txt").write_text("base\n")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "init")
    base = _git(project, "rev-parse", "HEAD")
    _git(project, "switch", "-c", "baton/feat-tic-001-a01")
    task = finalize_task_packet(
        TaskPacket(
            task_id="feat:ticket-001:build",
            role="builder",
            goal="change base",
            project_path=str(project),
            base_commit=base,
            verification_commands=commands if commands is not None else ["test -f base.txt"],
            plan_revision=1,
        )
    )
    runtime = tmp_path / "runtime"
    packet_path = runtime / "packet.json"
    payload = {
        "assignmentId": "ASG-feat-001-r1-a1",
        "ticketNum": 1,
        "attempt": 1,
        "claimToken": "secret-token",
        "worktreePath": str(project),
        "worktreeBranch": "baton/feat-tic-001-a01",
        "baseSha": base,
        "verificationPath": str(runtime / "verification.json"),
        "receiptPath": str(runtime / "receipt.json"),
        "taskPacket": task.to_dict(),
    }
    runtime.mkdir()
    packet_path.write_text(json.dumps(payload), encoding="utf-8")
    return project, packet_path, payload


def test_shell_command_uses_posix_sh():
    assert shell_command("echo hi", platform="posix") == ["/bin/sh", "-lc", "echo hi"]


def test_shell_command_uses_powershell_on_windows():
    assert shell_command("echo hi", platform="nt") == [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "echo hi",
    ]


def test_verify_runs_only_declared_commands(tmp_path):
    project, packet, _ = _setup(
        tmp_path, commands=["printf verified > verification-marker.txt"]
    )

    report = verify_packet(packet)

    assert (project / "verification-marker.txt").read_text() == "verified"
    assert report["verification"][0]["exitCode"] == 0


def test_verify_fails_when_commands_are_missing(tmp_path):
    _, packet, _ = _setup(tmp_path, commands=[])
    with pytest.raises(ReceiptError, match="no verification commands"):
        verify_packet(packet)


def test_seal_rejects_unchanged_head(tmp_path):
    _, packet, _ = _setup(tmp_path)
    with pytest.raises(ReceiptError, match="did not advance"):
        seal_receipt(packet)


def test_seal_rejects_dirty_workspace(tmp_path):
    project, packet, _ = _setup(tmp_path)
    (project / "changed.txt").write_text("dirty")
    with pytest.raises(ReceiptError, match="dirty"):
        seal_receipt(packet)


def test_seal_rejects_wrong_branch(tmp_path):
    project, packet, _ = _setup(tmp_path)
    (project / "changed.txt").write_text("done")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "done")
    _git(project, "switch", "-c", "wrong")
    with pytest.raises(ReceiptError, match="wrong branch"):
        seal_receipt(packet)


def test_seal_rejects_failed_verification(tmp_path):
    project, packet, _ = _setup(tmp_path, commands=["exit 7"])
    (project / "changed.txt").write_text("done")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "done")
    with pytest.raises(ReceiptError, match="verification failed"):
        seal_receipt(packet)


def test_receipt_write_is_atomic_and_idempotent(tmp_path):
    project, packet, payload = _setup(tmp_path)
    (project / "changed.txt").write_text("done")
    _git(project, "add", "-A")
    _git(project, "commit", "-m", "done")

    first = seal_receipt(packet)
    second = seal_receipt(packet)
    on_disk = json.loads(Path(payload["receiptPath"]).read_text())

    assert first.commit_sha == second.commit_sha
    assert on_disk["status"] == "succeeded"
    assert on_disk["commit_sha"] == first.commit_sha
    assert not Path(payload["receiptPath"] + ".tmp").exists()
