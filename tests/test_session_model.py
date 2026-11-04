"""Contracts for session-driven Baton actions and attempts."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from orchestration.model import TaskPacket  # noqa: E402
from orchestration.result import ResultEnvelope  # noqa: E402
from orchestration.session_model import (  # noqa: E402
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


def test_action_batch_round_trips_every_action_kind():
    batch = ActionBatch(
        run_id="RUN-feature",
        generation=7,
        state="executing",
        actions=[
            DispatchAction("ASG-1", 1, "baton_ticket_001", "/tmp/w1", "do it", "tok", "/tmp/p.json", "hash"),
            ResolveMergeAction("ASG-2", 2, "/tmp/integration", "resolve", "tok2"),
            RecoverAction("ASG-3", 3, "receipt exists"),
            WaitAction("agents running", ["ASG-1"]),
            BlockedAction("missing-authority", "deploy not approved", 4),
            CompleteAction(3, [1, 2, 3]),
        ],
    )

    restored = ActionBatch.from_dict(batch.to_dict())

    assert restored.to_dict() == batch.to_dict()
    assert [a.kind for a in restored.actions] == [
        "dispatch", "resolve_merge", "recover", "wait", "blocked", "complete"
    ]


def test_dispatch_action_contains_host_neutral_prompt_and_cwd():
    action = DispatchAction(
        assignment_id="ASG-feature-002-r1-a1",
        ticket_num=2,
        task_name="baton_feature_002_a1",
        cwd="/abs/project-baton-feature-tic-002-a01",
        prompt="Implement ticket 02",
        claim_token="token",
        packet_path="/abs/packet.json",
        task_packet_hash="abc",
        model="gpt-5.6-terra",
        reasoning_effort="high",
    )

    data = action.to_dict()
    assert data["kind"] == "dispatch"
    assert data["cwd"].startswith("/")
    assert "spawn_agent" not in data
    assert "zcode" not in data["prompt"].lower()


def test_task_packet_hash_is_stable():
    p1 = TaskPacket(task_id="t1", role="builder", goal="do it")
    p2 = TaskPacket(task_id="t1", role="builder", goal="do it")

    assert finalize_task_packet(p1).task_packet_hash
    assert finalize_task_packet(p1).task_packet_hash == finalize_task_packet(p2).task_packet_hash


def test_result_envelope_reads_legacy_payload_without_session_fields():
    envelope = ResultEnvelope.from_dict(
        {
            "task_id": "feature:ticket-001:build",
            "profile_id": "legacy",
            "status": "succeeded",
            "ticket_num": 1,
            "assignment_id": "ASG-1",
            "plan_revision": 1,
            "task_packet_hash": "hash",
            "commit_sha": "abc123",
        }
    )

    assert envelope.attempt == 0
    assert envelope.receipt_path == ""
    assert envelope.integration_sha == ""


def test_execution_attempt_round_trip():
    attempt = ExecutionAttempt(
        assignment_id="ASG-feature-001-r1-a1",
        run_id="RUN-feature",
        ticket_num=1,
        attempt=1,
        state="leased",
        session_id="session-1",
        plan_revision=1,
        task_packet_hash="hash",
        claim_token="token",
        lease_expires_at="2026-08-08T10:00:00+00:00",
        base_branch="main",
        base_sha="abc",
    )

    assert ExecutionAttempt.from_dict(attempt.to_dict()) == attempt
