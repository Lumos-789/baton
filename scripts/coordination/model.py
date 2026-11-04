"""Resource-scheduling kernel data model.

This module defines the dataclasses that back Baton's coordination layer —
the generic queueing and serial-execution substrate used by the agent
orchestration plane. It is intentionally pure standard-library and free of
I/O so that store/scheduler/runner modules can compose on top of it.

Field names and defaults are pinned to the design contract in
``docs/architecture.md`` and the durable plan store under
``.dsh/baton/<feature>/``. Do not rename fields without updating both the
design doc and the tests.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from orchestration.plan_model import utc_now

# Sentinel that distinguishes "caller did not pass priority_rank" from "caller
# passed 0". Used by Job.__post_init__ to derive priority_rank from priority so
# the two fields never drift (priority_rank is a SQLite-sort denormalization of
# priority, not an independent knob).
_UNSET: Any = object()


# ---------------------------------------------------------------------------
# Status / priority / claim-mode string constants
#
# These are deliberately plain ``str`` constants (not ``enum.Enum``) so they
# round-trip cleanly through JSON storage and SQLite columns. Mirror the
# values used in docs/architecture.md verbatim.
# ---------------------------------------------------------------------------

# Job lifecycle states. A job flows through these in roughly:
# submitted -> waiting-dependency -> waiting-resource -> leased -> running
#   -> verifying -> succeeded | failed, with canceled / orphaned / suspect /
#   unknown as terminal or observability states.
JobStatus = str
SUBMITTED = "submitted"
WAITING_DEPENDENCY = "waiting-dependency"
WAITING_RESOURCE = "waiting-resource"
LEASED = "leased"
RUNNING = "running"
VERIFYING = "verifying"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELED = "canceled"
ORPHANED = "orphaned"
UNKNOWN = "unknown"
SUSPECT = "suspect"

JOB_STATUSES: tuple[str, ...] = (
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


# Priority buckets. priority_rank is the numeric value used for SQLite ORDER BY;
# higher wins. ``PRIORITY_RANK`` maps the string constants to those ranks.
Priority = str
EMERGENCY = "emergency"
NORMAL = "normal"
BACKGROUND = "background"

PRIORITIES: tuple[str, ...] = (EMERGENCY, NORMAL, BACKGROUND)
PRIORITY_RANK: dict[str, int] = {
    EMERGENCY: 100,
    NORMAL: 50,
    BACKGROUND: 10,
}


def priority_rank_for(priority: str) -> int:
    """Return the SQLite sort rank for a priority bucket (unknown -> 0)."""
    return PRIORITY_RANK.get(priority, 0)


# How a resource claim consumes capacity.
ClaimMode = str
EXCLUSIVE = "exclusive"
CAPACITY = "capacity"
SHARED = "shared"

CLAIM_MODES: tuple[str, ...] = (EXCLUSIVE, CAPACITY, SHARED)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ResourceClaim:
    """A job stage's demand on one resource.

    For ``exclusive`` mode only one job may hold the resource at a time
    (``units`` is effectively 1). ``capacity`` mode reserves ``units`` of the
    resource's declared ``capacity``. ``shared`` reads the resource without
    consuming capacity (read-only co-tenancy).
    """

    resource_key: str
    mode: str = EXCLUSIVE
    units: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResourceClaim":
        return cls(
            resource_key=str(data["resource_key"]),
            mode=str(data.get("mode", EXCLUSIVE)),
            units=int(data.get("units", 1)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ResourceDef:
    """Definition of a schedulable resource (a unit of capacity or a lock)."""

    resource_key: str
    capacity: int = 1
    mode: str = EXCLUSIVE
    frozen: bool = False
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResourceDef":
        now = utc_now()
        return cls(
            resource_key=str(data["resource_key"]),
            capacity=int(data.get("capacity", 1)),
            mode=str(data.get("mode", EXCLUSIVE)),
            frozen=bool(data.get("frozen", False)),
            created_at=str(data.get("created_at", now)),
            updated_at=str(data.get("updated_at", now)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StageSpec:
    """One execution step of a job.

    A job is a sequence of stages; each stage claims resources, runs a
    command (e.g. launching a ralph run), and is pinned to an immutable
    ``input_version`` (a commit SHA or artifact id) so results are auditable.
    """

    name: str
    runner: str = ""
    claims: list[ResourceClaim] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    input_version: str = ""
    timeout_seconds: int = 3600

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StageSpec":
        raw_claims = data.get("claims", []) or []
        return cls(
            name=str(data["name"]),
            runner=str(data.get("runner", "")),
            claims=[ResourceClaim.from_dict(c) for c in raw_claims],
            command=[str(x) for x in data.get("command", [])],
            input_version=str(data.get("input_version", "")),
            timeout_seconds=int(data.get("timeout_seconds", 3600)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Job:
    """A unit of scheduled work in the coordination kernel.

    A job owns an ordered list of :class:`StageSpec`. ``current_stage_index``
    tracks progress through the stages; the lease/owner fields describe the
    worker currently holding the job (or the last known holder when an
    orphan/suspect check runs).
    """

    job_id: str
    idempotency_key: str
    submitter: str
    project: str
    feature: str
    priority: str = NORMAL
    # priority_rank is a derived denormalization of ``priority`` used for
    # SQLite ORDER BY. Use the _UNSET sentinel default so __post_init__ can
    # detect "not provided by caller" and derive the rank from ``priority``.
    # If a caller explicitly passes a rank it is honored (e.g. deserialization
    # of a foreign bucket), but it must agree with the bucket at construction
    # time — otherwise it is recomputed.
    priority_rank: int = _UNSET
    status: str = SUBMITTED
    stages: list[StageSpec] = field(default_factory=list)
    current_stage_index: int = 0
    lease_token: str = ""
    lease_epoch: int = 0
    owner: str = ""
    owner_pid: int = 0
    owner_start_time: float = 0.0
    heartbeat_at: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    reason: str = ""

    def __post_init__(self) -> None:
        # Keep priority_rank in sync with priority. The caller may omit it
        # (derive) or pass it explicitly (honor, but only if consistent).
        derived = priority_rank_for(self.priority)
        if self.priority_rank is _UNSET:
            self.priority_rank = derived
        elif int(self.priority_rank) != derived:
            # Inconsistent rank/bucket — the bucket wins, rank is recomputed.
            self.priority_rank = derived
        else:
            self.priority_rank = int(self.priority_rank)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        now = utc_now()
        raw_stages = data.get("stages", []) or []
        priority = str(data.get("priority", NORMAL))
        kwargs: dict[str, Any] = dict(
            job_id=str(data["job_id"]),
            idempotency_key=str(data["idempotency_key"]),
            submitter=str(data["submitter"]),
            project=str(data["project"]),
            feature=str(data["feature"]),
            priority=priority,
            status=str(data.get("status", SUBMITTED)),
            stages=[StageSpec.from_dict(s) for s in raw_stages],
            current_stage_index=int(data.get("current_stage_index", 0)),
            lease_token=str(data.get("lease_token", "")),
            lease_epoch=int(data.get("lease_epoch", 0)),
            owner=str(data.get("owner", "")),
            owner_pid=int(data.get("owner_pid", 0)),
            owner_start_time=float(data.get("owner_start_time", 0.0)),
            heartbeat_at=str(data.get("heartbeat_at", "")),
            created_at=str(data.get("created_at", now)),
            updated_at=str(data.get("updated_at", now)),
            reason=str(data.get("reason", "")),
        )
        # Pass priority_rank through so a caller-supplied override survives,
        # but __post_init__ will still reconcile it against the bucket.
        if "priority_rank" in data and data["priority_rank"] is not None:
            kwargs["priority_rank"] = int(data["priority_rank"])
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
