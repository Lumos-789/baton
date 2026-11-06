"""Relay judgments: pure wait/abandon/adopt verdicts from attempt rows.

Replays the 2026-11-06 fusion-s1 incident parameters exactly: a spawned
attempt 14 minutes into a 2-hour lease (106 minutes remaining) with no
receipt must read as alive ("wait"), and abandoning it must be refused.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from orchestration.relay import (  # noqa: E402
    ABANDON_GUARD_FRACTION,
    abandon_refusal,
    relay_facts,
    suggest_action,
)
from orchestration.session_model import ExecutionAttempt  # noqa: E402


def _attempt(**overrides) -> ExecutionAttempt:
    defaults = dict(
        assignment_id="ASG-feature-203-r5-a2",
        run_id="RUN-feature",
        ticket_num=203,
        attempt=2,
        state="spawned",
        session_id="session-a",
        plan_revision=5,
        task_packet_hash="abc",
        claim_token="",
        lease_expires_at="",
        base_branch="main",
        base_sha="0" * 40,
    )
    defaults.update(overrides)
    return ExecutionAttempt(**defaults)


def _timestamps(elapsed: timedelta, lease: timedelta) -> dict[str, str]:
    """created_at `elapsed` ago, lease_expires_at `lease - elapsed` from now.

    Both stamps derive from one clock reading so the fraction arithmetic in
    the assertions stays exact.
    """
    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "created_at": (now - elapsed).isoformat(),
        "lease_expires_at": (now + (lease - elapsed)).isoformat(),
    }


def test_incident_parameters_read_as_alive_and_guard_abandon(tmp_path):
    """spawned, 14 min elapsed, 106 min remaining, no receipt → wait + refusal."""
    packet = tmp_path / "packet.json"
    packet.write_text("{}", encoding="utf-8")
    stamps = _timestamps(timedelta(minutes=14), timedelta(hours=2))
    attempt = _attempt(packet_path=str(packet), **stamps)

    facts = relay_facts(attempt)
    assert facts["receiptExists"] is False
    assert facts["elapsedSec"] == 840
    assert facts["leaseRemainingSec"] == 6360
    assert facts["leaseElapsedFraction"] == round(840 / 7200, 3)
    assert facts["worktree"]["exists"] is False  # no worktree_path → no crash
    assert suggest_action(attempt, facts) == "wait"

    refusal = abandon_refusal(attempt, facts)
    assert refusal is not None
    assert refusal["state"] == "spawned"
    assert refusal["suggestedAction"] == "wait"
    assert "--force" in refusal["force"]
    assert "clean worktree" in refusal["advice"]


def test_guard_releases_when_lease_mostly_spent_or_expired():
    spent = _timestamps(timedelta(minutes=115), timedelta(hours=2))
    attempt = _attempt(**spent)
    refusal = abandon_refusal(attempt, relay_facts(attempt))
    assert refusal is None, "past the guard fraction the hatch must be cheap"

    expired = _timestamps(timedelta(minutes=130), timedelta(hours=2))
    attempt = _attempt(**expired)
    facts = relay_facts(attempt)
    assert facts["leaseRemainingSec"] <= 0
    assert abandon_refusal(attempt, facts) is None
    assert suggest_action(attempt, facts) == "await-expiry"


def test_guard_releases_on_failure_evidence_or_terminal_states(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    stamps = _timestamps(timedelta(minutes=1), timedelta(hours=2))

    unknown = _attempt(state="unknown", failure_code="lease-expired", **stamps)
    assert abandon_refusal(unknown, relay_facts(unknown)) is None
    assert suggest_action(unknown, relay_facts(unknown)) == "decide"

    blocked = _attempt(state="blocked-review", failure_code="merge-conflict", **stamps)
    assert abandon_refusal(blocked, relay_facts(blocked)) is None
    assert suggest_action(blocked, relay_facts(blocked)) == "resolve-or-abandon"

    # Terminal attempts need no guard at all — except the adopt hint.
    abandoned_bare = _attempt(state="abandoned", **stamps)
    assert abandon_refusal(abandoned_bare, relay_facts(abandoned_bare)) is None
    assert suggest_action(abandoned_bare, relay_facts(abandoned_bare)) == "none"

    packet = tmp_path / "packet.json"
    packet.write_text("{}", encoding="utf-8")
    abandoned_late = _attempt(
        state="abandoned", packet_path=str(packet), receipt_path=str(receipt), **stamps
    )
    facts = relay_facts(abandoned_late)
    assert facts["receiptExists"] is True
    assert suggest_action(abandoned_late, facts) == "adopt"


def test_sealed_receipt_on_active_attempt_says_reconcile_not_abandon(tmp_path):
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    stamps = _timestamps(timedelta(minutes=30), timedelta(hours=2))
    attempt = _attempt(receipt_path=str(receipt), **stamps)

    facts = relay_facts(attempt)
    assert facts["receiptExists"] is True
    assert suggest_action(attempt, facts) == "reconcile"
    refusal = abandon_refusal(attempt, facts)
    assert refusal is not None
    assert "sealed receipt" in refusal["reason"]


def test_guard_fraction_boundary_is_inclusive_release():
    # fraction == ABANDON_GUARD_FRACTION must NOT refuse (>= releases).
    assert ABANDON_GUARD_FRACTION == 0.8
    stamps = _timestamps(
        timedelta(minutes=96), timedelta(minutes=120)
    )  # exactly 80% spent
    attempt = _attempt(**stamps)
    facts = relay_facts(attempt)
    assert facts["leaseElapsedFraction"] == 0.8
    assert abandon_refusal(attempt, facts) is None
    assert suggest_action(attempt, facts) == "wait-lease-nearly-spent"


def test_unparseable_budget_never_blocks_the_hatch():
    attempt = _attempt(created_at="not-a-date", lease_expires_at="")
    facts = relay_facts(attempt)
    assert facts["elapsedSec"] is None
    assert facts["leaseRemainingSec"] is None
    assert abandon_refusal(attempt, facts) is None
