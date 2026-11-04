"""Deterministic verification and sealed receipts for session assignments."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from orchestration.model import TaskPacket
from orchestration.result import ResultEnvelope
from orchestration.session_model import finalize_task_packet


class ReceiptError(RuntimeError):
    """The assignment cannot produce a trusted receipt."""


def shell_command(command: str, *, platform: str | None = None) -> list[str]:
    """Wrap a verification command in the platform's bootstrap shell."""
    name = os.name if platform is None else platform
    if name == "nt":
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/sh", "-lc", command]


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _load_packet(packet_path: str | Path) -> tuple[Path, dict[str, Any], TaskPacket]:
    path = Path(packet_path).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReceiptError(f"cannot read packet {path}: {exc}") from exc
    raw_task = payload.get("taskPacket")
    if not isinstance(raw_task, dict):
        raise ReceiptError("packet is missing taskPacket")
    task = TaskPacket.from_dict(raw_task)
    expected = task.task_packet_hash
    actual = finalize_task_packet(TaskPacket.from_dict(raw_task)).task_packet_hash
    if not expected or expected != actual:
        raise ReceiptError("task packet hash mismatch")
    if not payload.get("assignmentId") or not payload.get("claimToken"):
        raise ReceiptError("packet is missing assignment identity or claim token")
    return path, payload, task


def _run_git(worktree: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise ReceiptError(
            result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed"
        )
    return result.stdout.strip()


def _verification_path(payload: dict[str, Any], packet_path: Path) -> Path:
    raw = payload.get("verificationPath")
    return Path(raw).resolve() if raw else packet_path.with_name("verification.json")


def _receipt_path(payload: dict[str, Any], packet_path: Path) -> Path:
    raw = payload.get("receiptPath")
    return Path(raw).resolve() if raw else packet_path.with_name("receipt.json")


def _run_verification_commands(worktree: Path, commands: list[str]) -> list[dict[str, Any]]:
    if not commands:
        raise ReceiptError("task packet has no verification commands")
    results: list[dict[str, Any]] = []
    for command in commands:
        completed = subprocess.run(
            shell_command(command),
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
        results.append(
            {
                "command": command,
                "exitCode": completed.returncode,
                "outputSha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
                "outputTail": output[-2000:],
            }
        )
        if completed.returncode != 0:
            raise ReceiptError(f"verification failed: {command}")
    return results


def verify_packet(packet_path: str | Path) -> dict[str, Any]:
    """Run the packet's declared checks as a pre-commit feedback loop."""
    path, payload, task = _load_packet(packet_path)
    worktree = Path(payload["worktreePath"]).resolve()
    results = _run_verification_commands(worktree, task.verification_commands)
    report = {
        "assignmentId": payload["assignmentId"],
        "taskPacketHash": task.task_packet_hash,
        "worktreePath": str(worktree),
        "verification": results,
    }
    _atomic_json(_verification_path(payload, path), report)
    return report


def seal_receipt(packet_path: str | Path) -> ResultEnvelope:
    """Re-run checks on committed code and atomically seal a ResultEnvelope."""
    path, payload, task = _load_packet(packet_path)
    worktree = Path(payload["worktreePath"]).resolve()
    expected_branch = str(payload["worktreeBranch"])
    branch = _run_git(worktree, ["branch", "--show-current"])
    if branch != expected_branch:
        raise ReceiptError(f"wrong branch: expected {expected_branch}, got {branch}")
    status = _run_git(worktree, ["status", "--porcelain"])
    if status:
        raise ReceiptError("worktree is dirty; commit all changes before sealing")
    head = _run_git(worktree, ["rev-parse", "HEAD"])
    base = str(payload.get("baseSha") or task.base_commit)
    if not base:
        raise ReceiptError("packet has no base SHA")
    if head == base:
        raise ReceiptError("HEAD did not advance from the assignment baseline")
    _run_git(worktree, ["merge-base", "--is-ancestor", base, head])
    verification = _run_verification_commands(worktree, task.verification_commands)
    changed = [
        line.strip()
        for line in _run_git(worktree, ["diff", "--name-only", f"{base}..{head}"]).splitlines()
        if line.strip()
    ]
    if task.allowed_paths:
        disallowed = [
            file
            for file in changed
            if not any(file == allowed or file.startswith(allowed.rstrip("/") + "/") for allowed in task.allowed_paths)
        ]
        if disallowed:
            raise ReceiptError(f"changed files outside allowed paths: {disallowed}")
    envelope = ResultEnvelope(
        task_id=task.task_id,
        profile_id=str(payload.get("profileId", "session-subagent")),
        status="succeeded",
        ticket_num=int(payload["ticketNum"]),
        assignment_id=str(payload["assignmentId"]),
        plan_revision=task.plan_revision,
        task_packet_hash=task.task_packet_hash,
        attempt=int(payload.get("attempt", 0)),
        commit_sha=head,
        receipt_path=str(_receipt_path(payload, path)),
        changed_files=changed,
        verification=verification,
    )
    receipt = envelope.to_dict()
    receipt["claimTokenHash"] = hashlib.sha256(
        str(payload["claimToken"]).encode("utf-8")
    ).hexdigest()
    _atomic_json(_receipt_path(payload, path), receipt)
    return envelope


__all__ = ["ReceiptError", "seal_receipt", "verify_packet"]
