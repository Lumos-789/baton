"""Coordination kernel data-model tests.

Exercises the dataclasses backing Baton's resource-scheduling layer: status
constants, priority-rank mapping, and to_dict / from_dict round-trips for
Job / StageSpec / ResourceClaim / ResourceDef.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from coordination.model import (  # noqa: E402
    BACKGROUND,
    CLAIM_MODES,
    CAPACITY,
    CANCELED,
    EMERGENCY,
    EXCLUSIVE,
    FAILED,
    JOB_STATUSES,
    Job,
    JobStatus,
    LEASED,
    NORMAL,
    ORPHANED,
    PRIORITY_RANK,
    PRIORITIES,
    Priority,
    ClaimMode,
    ResourceClaim,
    ResourceDef,
    RUNNING,
    SHARED,
    StageSpec,
    SUBMITTED,
    SUCCEEDED,
    SUSPECT,
    UNKNOWN,
    VERIFYING,
    WAITING_DEPENDENCY,
    WAITING_RESOURCE,
    priority_rank_for,
)

# Sanity aliases so the type-style acceptance criterion (which references the
# string-constant "enum" names) is observably imported.
_STATUS_NAMES: tuple[JobStatus, ...] = JOB_STATUSES
_PRIORITY_NAMES: tuple[Priority, ...] = PRIORITIES
_CLAIM_NAMES: tuple[ClaimMode, ...] = CLAIM_MODES


def _sample_job(**overrides) -> Job:
    base = dict(
        job_id="job-1",
        idempotency_key="idem-1",
        submitter="baton",
        project="baton",
        feature="core-buildout",
    )
    base.update(overrides)
    return Job(**base)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_status_constants_match_design():
    assert JOB_STATUSES == (
        SUBMITTED,
        WAITING_DEPENDENCY,
        WAITING_RESOURCE,
        LEASED,
        RUNNING,
        VERIFYING,
        SUCCEEDED,
        FAILED,
        CANCELED,
        ORPHANED,
        UNKNOWN,
        SUSPECT,
    )
    # Each value is unique and a plain string.
    assert len(set(JOB_STATUSES)) == len(JOB_STATUSES)
    assert all(isinstance(v, str) for v in JOB_STATUSES)
    # Spot-check the exact spellings pinned by the design doc.
    assert SUBMITTED == "submitted"
    assert WAITING_DEPENDENCY == "waiting-dependency"
    assert WAITING_RESOURCE == "waiting-resource"
    assert LEASED == "leased"
    assert RUNNING == "running"
    assert VERIFYING == "verifying"
    assert SUCCEEDED == "succeeded"
    assert FAILED == "failed"
    assert CANCELED == "canceled"
    assert ORPHANED == "orphaned"
    assert UNKNOWN == "unknown"
    assert SUSPECT == "suspect"


def test_priority_constants_and_ranks():
    assert PRIORITIES == (EMERGENCY, NORMAL, BACKGROUND)
    assert EMERGENCY == "emergency"
    assert NORMAL == "normal"
    assert BACKGROUND == "background"
    assert PRIORITY_RANK == {EMERGENCY: 100, NORMAL: 50, BACKGROUND: 10}
    # priority_rank_for falls back to 0 for unknown buckets.
    assert priority_rank_for(EMERGENCY) == 100
    assert priority_rank_for(NORMAL) == 50
    assert priority_rank_for(BACKGROUND) == 10
    assert priority_rank_for("nope") == 0


def test_claim_mode_constants():
    assert CLAIM_MODES == (EXCLUSIVE, CAPACITY, SHARED)
    assert EXCLUSIVE == "exclusive"
    assert CAPACITY == "capacity"
    assert SHARED == "shared"


# ---------------------------------------------------------------------------
# Job state transitions + priority mapping (the named acceptance test)
# ---------------------------------------------------------------------------


def test_job_state_transitions():
    """A Job must accept every JobStatus value, and priority_rank must match
    the priority bucket at construction."""
    # Default job lands in submitted + normal.
    job = _sample_job()
    assert job.status == SUBMITTED
    assert job.priority == NORMAL
    assert job.priority_rank == PRIORITY_RANK[NORMAL] == 50

    # Every declared status value must be assignable to Job.status.
    for status in JOB_STATUSES:
        job.status = status
        assert job.status == status

    # Priority buckets map to the documented ranks.
    for priority, expected_rank in PRIORITY_RANK.items():
        ranked = _sample_job(priority=priority)
        assert ranked.priority == priority
        assert ranked.priority_rank == expected_rank


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_job_defaults():
    job = _sample_job()
    assert job.stages == []
    assert job.current_stage_index == 0
    assert job.lease_token == ""
    assert job.lease_epoch == 0
    assert job.owner == ""
    assert job.owner_pid == 0
    assert job.owner_start_time == 0.0
    assert job.heartbeat_at == ""
    assert job.reason == ""
    # created_at / updated_at are populated automatically.
    assert job.created_at
    assert job.updated_at


def test_stage_spec_defaults():
    stage = StageSpec(name="build")
    assert stage.runner == ""
    assert stage.claims == []
    assert stage.command == []
    assert stage.input_version == ""
    assert stage.timeout_seconds == 3600


def test_resource_claim_defaults():
    claim = ResourceClaim(resource_key="cpu")
    assert claim.mode == EXCLUSIVE
    assert claim.units == 1


def test_resource_def_defaults():
    res = ResourceDef(resource_key="cpu")
    assert res.capacity == 1
    assert res.mode == EXCLUSIVE
    assert res.frozen is False
    assert res.created_at
    assert res.updated_at


# ---------------------------------------------------------------------------
# Round-trip serialization
# ---------------------------------------------------------------------------


def test_resource_claim_roundtrip():
    claim = ResourceClaim(resource_key="gpu", mode=CAPACITY, units=4)
    out = ResourceClaim.from_dict(claim.to_dict())
    assert out == claim
    # Tolerant defaults when keys are missing.
    partial = ResourceClaim.from_dict({"resource_key": "gpu"})
    assert partial == ResourceClaim(resource_key="gpu")


def test_resource_def_roundtrip():
    res = ResourceDef(resource_key="cpu-pool", capacity=8, mode=CAPACITY, frozen=True)
    out = ResourceDef.from_dict(res.to_dict())
    assert out == res


def test_stage_spec_roundtrip():
    stage = StageSpec(
        name="test",
        runner="ralph",
        claims=[
            ResourceClaim(resource_key="ralph", mode=EXCLUSIVE),
            ResourceClaim(resource_key="cpu", mode=CAPACITY, units=2),
        ],
        command=["ralph.sh", "--prd", "prd-x.json"],
        input_version="abc123",
        timeout_seconds=900,
    )
    out = StageSpec.from_dict(stage.to_dict())
    assert out == stage


def test_job_roundtrip():
    job = _sample_job(
        priority=EMERGENCY,
        status=RUNNING,
        stages=[StageSpec(name="s1", runner="ralph")],
        current_stage_index=1,
        lease_token="tok-7",
        lease_epoch=7,
        owner="ralph-1",
        owner_pid=4242,
        owner_start_time=1700000000.0,
        heartbeat_at="2026-08-02T00:00:00+00:00",
        reason="claimed",
    )
    out = Job.from_dict(job.to_dict())
    assert out == job
    # priority_rank survives the round trip alongside priority.
    assert out.priority == EMERGENCY
    assert out.priority_rank == 100


def test_job_from_dict_derives_rank_when_missing():
    """When priority_rank is absent, from_dict must derive it from priority
    so the two fields never drift."""
    raw = {
        "job_id": "j2",
        "idempotency_key": "k2",
        "submitter": "baton",
        "project": "p",
        "feature": "f",
        "priority": EMERGENCY,
        # no priority_rank
    }
    job = Job.from_dict(raw)
    assert job.priority == EMERGENCY
    assert job.priority_rank == PRIORITY_RANK[EMERGENCY]


def test_job_from_dict_recomputes_inconsistent_rank():
    """priority_rank is a denormalization of priority. If a caller passes a
    rank that disagrees with the bucket, the bucket wins and the rank is
    recomputed — the two fields must never drift."""
    raw = {
        "job_id": "j3",
        "idempotency_key": "k3",
        "submitter": "baton",
        "project": "p",
        "feature": "f",
        "priority": BACKGROUND,
        "priority_rank": 999,  # inconsistent -> ignored, recomputed
    }
    job = Job.from_dict(raw)
    assert job.priority == BACKGROUND
    assert job.priority_rank == PRIORITY_RANK[BACKGROUND] == 10


def test_job_construction_keeps_rank_in_sync():
    """Direct construction must derive priority_rank from priority."""
    for priority, expected_rank in PRIORITY_RANK.items():
        job = _sample_job(priority=priority)
        assert job.priority_rank == expected_rank, priority
