"""Deterministic session-subagent scheduling and reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from orchestration.integrator import integrate_assignment
from orchestration.model import ROLE_BUILDER, TaskPacket
from orchestration.plan_store import ExecutionAttemptError, PlanStore
from orchestration.result import ResultEnvelope
from orchestration.worktree import cleanup_feature_worktrees
from orchestration.session_model import (
    ActionBatch,
    BlockedAction,
    CompleteAction,
    DispatchAction,
    ExecutionAttempt,
    RecoverAction,
    ResolveMergeAction,
    WaitAction,
    finalize_task_packet,
)
from orchestration.worktree import (
    WorktreeError,
    prepare_assignment_worktree,
    prepare_integration_worktree,
)


class SessionControllerError(RuntimeError):
    """A session action cannot be produced from the durable state."""


class SessionController:
    """The sole deterministic control plane for native session subagents."""

    def __init__(
        self,
        project_path: str | Path,
        feature: str,
        *,
        verification_commands: list[str] | None = None,
        lease_seconds: int = 7200,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        self.feature = feature
        self.store = PlanStore(self.project_path, feature)
        self.verification_commands = list(
            verification_commands or ["git diff --check"]
        )
        self.lease_seconds = lease_seconds

    def actions(
        self,
        session_id: str,
        capacity: int = 1,
        base_branch: str = "main",
    ) -> ActionBatch:
        """Recover durable attempts, then claim and prepare remaining capacity."""
        if not session_id.strip():
            raise ValueError("session_id is required")
        if capacity < 1 or capacity > 5:
            raise ValueError("capacity must be between 1 and 5")
        self.store.ensure_initial_plan()

        try:
            integration = prepare_integration_worktree(
                self.project_path, self.feature, base_branch=base_branch
            )
            integration_sha = self._git(integration.path, "rev-parse", "HEAD")
        except WorktreeError as exc:
            return self._batch(
                "blocked",
                [BlockedAction(code="integration-worktree", reason=str(exc))],
            )

        produced: list[Any] = []
        for attempt in self.store.list_execution_attempts():
            if attempt.state == "succeeded":
                continue
            token = self._token_from_packet(attempt.packet_path)
            if attempt.receipt_path and Path(attempt.receipt_path).is_file() and token:
                action = self._reconcile_sealed(attempt, token)
                if action is not None:
                    produced.append(action)
                continue
            if attempt.state == "unknown":
                produced.append(
                    RecoverAction(
                        assignment_id=attempt.assignment_id,
                        ticket_num=attempt.ticket_num,
                        reason=attempt.failure_code or "attempt outcome is unknown",
                    )
                )
            elif attempt.state == "blocked-review":
                produced.append(self._merge_action(attempt, token))
            elif attempt.session_id == session_id and attempt.state == "leased" and token:
                produced.append(self._dispatch_from_attempt(attempt, token))

        if not any(isinstance(action, (RecoverAction, ResolveMergeAction)) for action in produced):
            claimed = self.store.claim_ready_assignments(
                session_id=session_id,
                capacity=capacity,
                base_branch=base_branch,
                base_sha=integration_sha,
                lease_seconds=self.lease_seconds,
            )
            for attempt in claimed:
                try:
                    produced.append(
                        self._prepare_dispatch(attempt, integration.path, integration.branch)
                    )
                except Exception as exc:
                    self.store.mark_execution_attempt_state(
                        attempt.assignment_id,
                        attempt.claim_token,
                        "unknown",
                        failure_code=f"prepare-failed:{exc}",
                        release_lease=True,
                    )
                    produced.append(
                        RecoverAction(
                            assignment_id=attempt.assignment_id,
                            ticket_num=attempt.ticket_num,
                            reason=f"assignment preparation failed: {exc}",
                        )
                    )

        if produced:
            state = "blocked" if any(
                isinstance(action, (RecoverAction, ResolveMergeAction, BlockedAction))
                for action in produced
            ) else "executing"
            return self._batch(state, produced)

        completion = self.store.latest_completion()
        if completion["complete"]:
            plan = self.store.current_plan()
            # A completed run's per-assignment worktrees (baton/<feature>-tic-*)
            # are stale — their work is already integrated into the integration
            # branch. Auto-remove them. The shared integration worktree stays so
            # the user can merge baton/<feature> to main, then `cleanup
            # --include-integration` removes it.
            cleanup = cleanup_feature_worktrees(self.project_path, self.feature)
            return self._batch(
                "complete",
                [
                    CompleteAction(
                        revision=int(completion["revision"]),
                        resolved_tickets=sorted(
                            ticket.num for ticket in plan.tickets if ticket.required
                        ),
                        worktrees_removed=cleanup["removed"],
                        worktrees_skipped=cleanup["skipped"],
                    )
                ],
            )

        active = [
            attempt.assignment_id
            for attempt in self.store.list_execution_attempts()
            if attempt.state in {
                "leased", "spawned", "running", "submitted", "verifying", "integrating"
            }
        ]
        return self._batch(
            "waiting",
            [
                WaitAction(
                    reason="no newly dispatchable ticket; required work remains",
                    active_assignments=active,
                )
            ],
        )

    def bind_agent(
        self, assignment_id: str, claim_token: str, agent_id: str
    ) -> ExecutionAttempt:
        return self.store.bind_attempt_agent(assignment_id, claim_token, agent_id)

    def status(self) -> dict[str, Any]:
        """Return a read-only snapshot from the durable session state."""
        plan = self.store.current_plan()
        completion = self.store.latest_completion()
        attempts = self.store.list_execution_attempts()
        return {
            "ok": True,
            "runId": f"RUN-{self.feature}",
            "feature": self.feature,
            "projectPath": str(self.project_path),
            "revision": plan.revision,
            "complete": bool(completion["complete"]),
            "missingRequiredTickets": [
                f"{num // 100}.{num % 100}"
                for num in completion["missingRequiredTickets"]
            ],
            "attempts": [attempt.to_dict() | {"claim_token": ""} for attempt in attempts],
        }

    def reconcile(
        self,
        assignment_id: str,
        claim_token: str,
        *,
        agent_terminal: str,
        session_id: str,
        capacity: int = 1,
        base_branch: str = "main",
        final_text: str = "",
    ) -> ActionBatch:
        """Consume a terminal lifecycle event; free text is deliberately ignored."""
        del final_text
        if agent_terminal not in {"completed", "failed", "canceled"}:
            raise ValueError("agent_terminal must be completed, failed, or canceled")
        attempt = self.store.get_execution_attempt(assignment_id)
        if attempt.state != "succeeded":
            if not attempt.receipt_path or not Path(attempt.receipt_path).is_file():
                self.store.mark_execution_attempt_state(
                    assignment_id,
                    claim_token,
                    "unknown",
                    failure_code=f"agent-{agent_terminal}-without-sealed-receipt",
                    release_lease=True,
                )
            else:
                self._reconcile_sealed(attempt, claim_token)
        return self.actions(session_id, capacity=capacity, base_branch=base_branch)

    def _prepare_dispatch(
        self, attempt: ExecutionAttempt, integration_path: Path, integration_branch: str
    ) -> DispatchAction:
        slot = prepare_assignment_worktree(
            self.project_path,
            self.feature,
            attempt.ticket_num,
            attempt.attempt,
        )
        plan = self.store.current_plan()
        ticket = next(t for t in plan.tickets if t.num == attempt.ticket_num)
        packet = finalize_task_packet(
            TaskPacket(
                task_id=f"{self.feature}:ticket-{ticket.num:03d}:build",
                role=ROLE_BUILDER,
                goal=ticket.what_to_build or ticket.title,
                acceptance_criteria=list(ticket.criteria),
                dependencies=[f"ticket-{num:03d}" for num in ticket.blocked_by],
                base_commit=attempt.base_sha,
                project_path=str(slot.path),
                resources=list(ticket.resources),
                verification_commands=list(ticket.verify) or list(self.verification_commands),
                output_contract=["sealed receipt"],
                plan_revision=attempt.plan_revision,
            )
        )
        run_dir = (
            self.project_path
            / ".dsh"
            / "baton"
            / "runs"
            / attempt.assignment_id
        )
        packet_path = run_dir / "packet.json"
        receipt_path = run_dir / "receipt.json"
        payload = {
            "assignmentId": attempt.assignment_id,
            "ticketNum": attempt.ticket_num,
            "attempt": attempt.attempt,
            "claimToken": attempt.claim_token,
            "worktreePath": str(slot.path),
            "worktreeBranch": slot.branch,
            "integrationPath": str(integration_path),
            "integrationBranch": integration_branch,
            "baseSha": attempt.base_sha,
            "verificationPath": str(run_dir / "verification.json"),
            "receiptPath": str(receipt_path),
            "taskPacket": packet.to_dict(),
        }
        self._write_immutable_json(packet_path, payload)
        configured = self.store.configure_execution_attempt(
            attempt.assignment_id,
            attempt.claim_token,
            task_packet_hash=packet.task_packet_hash,
            worktree_path=str(slot.path),
            worktree_branch=slot.branch,
            integration_path=str(integration_path),
            integration_branch=integration_branch,
            packet_path=str(packet_path),
            receipt_path=str(receipt_path),
        )
        return self._dispatch_from_attempt(configured, attempt.claim_token)

    def _dispatch_from_attempt(
        self, attempt: ExecutionAttempt, claim_token: str
    ) -> DispatchAction:
        if not attempt.packet_path:
            raise SessionControllerError(f"attempt has no packet: {attempt.assignment_id}")
        receipt_cli = Path(__file__).resolve().parents[1] / "receipt.py"
        prompt = (
            f"Implement Baton assignment {attempt.assignment_id} only.\n"
            f"Work exclusively in `{attempt.worktree_path}` on branch "
            f"`{attempt.worktree_branch}`. Do not change ticket or controller state.\n"
            f"For change context, read `openspec/changes/{self.feature}/proposal.md` "
            f"and `tasks.md` in the worktree.\n"
            f"Read immutable packet `{attempt.packet_path}`. Implement the ticket, then run:\n"
            f'`"{sys.executable}" "{receipt_cli}" verify '
            f'--packet "{attempt.packet_path}"`\n'
            "Commit all changes, then run the same command with `seal`. "
            "A sealed receipt is the only success evidence."
        )
        return DispatchAction(
            assignment_id=attempt.assignment_id,
            ticket_num=attempt.ticket_num,
            task_name=f"baton_ticket_{attempt.ticket_num:03d}",
            cwd=attempt.worktree_path,
            prompt=prompt,
            claim_token=claim_token,
            packet_path=attempt.packet_path,
            task_packet_hash=attempt.task_packet_hash,
        )

    def _reconcile_sealed(
        self, attempt: ExecutionAttempt, claim_token: str
    ) -> ResolveMergeAction | None:
        receipt_path = Path(attempt.receipt_path)
        try:
            payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionControllerError(f"cannot read sealed receipt: {exc}") from exc
        expected_token_hash = hashlib.sha256(claim_token.encode("utf-8")).hexdigest()
        if payload.get("claimTokenHash") != expected_token_hash:
            raise ExecutionAttemptError("sealed receipt claim token mismatch")
        envelope = ResultEnvelope.from_dict(payload)
        if envelope.task_packet_hash != attempt.task_packet_hash:
            raise ExecutionAttemptError("sealed receipt task packet hash mismatch")
        self.store.mark_execution_attempt_state(
            attempt.assignment_id, claim_token, "integrating"
        )
        result = integrate_assignment(attempt.integration_path, attempt.worktree_branch)
        if not result["ok"]:
            self.store.mark_execution_attempt_state(
                attempt.assignment_id,
                claim_token,
                "blocked-review",
                failure_code="merge-conflict",
            )
            return self._merge_action(attempt, claim_token, result.get("conflicts", []))
        self.store.finalize_execution_attempt(
            attempt.assignment_id,
            claim_token,
            envelope,
            str(result["integration_sha"]),
        )
        return None

    def _merge_action(
        self,
        attempt: ExecutionAttempt,
        claim_token: str,
        conflicts: list[str] | None = None,
    ) -> ResolveMergeAction:
        return ResolveMergeAction(
            assignment_id=attempt.assignment_id,
            ticket_num=attempt.ticket_num,
            cwd=attempt.integration_path,
            claim_token=claim_token,
            prompt=(
                f"Resolve the preserved merge conflict for {attempt.assignment_id} in "
                f"`{attempt.integration_path}`. Conflicts: {conflicts or 'inspect git status'}. "
                "Do not abort or discard the merge. Resolve, run the packet verification "
                "commands, and commit the merge; then the main session will reconcile again."
            ),
        )

    def _token_from_packet(self, packet_path: str) -> str:
        if not packet_path or not Path(packet_path).is_file():
            return ""
        try:
            payload = json.loads(Path(packet_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str(payload.get("claimToken", ""))

    def _batch(self, state: str, actions: list[Any]) -> ActionBatch:
        return ActionBatch(
            run_id=f"RUN-{self.feature}",
            generation=len(self.store.events()),
            state=state,
            actions=actions,
        )

    @staticmethod
    def _git(path: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "git command failed")
        return result.stdout.strip()

    @staticmethod
    def _write_immutable_json(path: Path, payload: dict[str, Any]) -> None:
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if path.exists():
            if path.read_text(encoding="utf-8") != text:
                raise SessionControllerError(f"immutable packet changed: {path}")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)


__all__ = ["SessionController", "SessionControllerError"]
