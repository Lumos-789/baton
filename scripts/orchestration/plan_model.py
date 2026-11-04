"""Shared plan data model and stable-hash helpers.

Split out of the retired amendment engine: these contracts are the durable
plan-store schema (``queue.db`` snapshots, ``plan.json``) and are used by the
plan store, the session model, and result envelopes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    """Return a stable UTC timestamp for persisted audit records."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_json(data: Any) -> str:
    """Serialize data with deterministic key ordering."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(data: Any) -> str:
    """Compute a short, stable hash for plan snapshots and task packets."""
    return hashlib.sha256(stable_json(data).encode("utf-8")).hexdigest()[:16]


@dataclass
class PlanTicket:
    """Machine-readable snapshot of one OpenSpec task in a plan revision."""

    num: int
    title: str
    what_to_build: str
    blocked_by: list[int] = field(default_factory=list)
    status: str = "ready"
    criteria: list[str] = field(default_factory=list)
    required: bool = True
    source_revision: int = 1
    source_amendment: str = ""
    successor: int | None = None
    priority: str = "normal"
    priority_reason: str = ""
    agent_profile: str = ""
    resources: list[str] = field(default_factory=list)
    authority: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanTicket":
        return cls(
            num=int(data["num"]),
            title=str(data["title"]),
            what_to_build=str(data.get("what_to_build", "")),
            blocked_by=[int(x) for x in data.get("blocked_by", [])],
            status=str(data.get("status", "ready")),
            criteria=[str(x) for x in data.get("criteria", [])],
            required=bool(data.get("required", True)),
            source_revision=int(data.get("source_revision", 1)),
            source_amendment=str(data.get("source_amendment", "")),
            successor=(
                int(data["successor"])
                if data.get("successor") is not None
                else None
            ),
            priority=str(data.get("priority", "normal")),
            priority_reason=str(data.get("priority_reason", "")),
            agent_profile=str(data.get("agent_profile", "")),
            resources=[str(x) for x in data.get("resources", [])],
            authority=[str(x) for x in data.get("authority", [])],
            verify=[str(x) for x in data.get("verify", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AssignmentSnapshot:
    """Immutable assignment metadata captured by a plan revision."""

    assignment_id: str
    ticket_num: int
    state: str
    plan_revision: int
    task_packet_hash: str
    profile: str = ""
    resources: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AssignmentSnapshot":
        return cls(
            assignment_id=str(data["assignment_id"]),
            ticket_num=int(data["ticket_num"]),
            state=str(data["state"]),
            plan_revision=int(data["plan_revision"]),
            task_packet_hash=str(data["task_packet_hash"]),
            profile=str(data.get("profile", "")),
            resources=[str(x) for x in data.get("resources", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlanRevision:
    """A complete approved plan snapshot."""

    job_id: str
    feature: str
    revision: int
    parent_revision: int | None
    plan_hash: str
    approved_by: str
    approved_at: str
    created_at: str
    tickets: list[PlanTicket] = field(default_factory=list)
    assignments: list[AssignmentSnapshot] = field(default_factory=list)
    completion_state: str = "active"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlanRevision":
        return cls(
            job_id=str(data["job_id"]),
            feature=str(data["feature"]),
            revision=int(data["revision"]),
            parent_revision=(
                int(data["parent_revision"])
                if data.get("parent_revision") is not None
                else None
            ),
            plan_hash=str(data["plan_hash"]),
            approved_by=str(data.get("approved_by", "")),
            approved_at=str(data.get("approved_at", "")),
            created_at=str(data.get("created_at", "")),
            tickets=[PlanTicket.from_dict(x) for x in data.get("tickets", [])],
            assignments=[
                AssignmentSnapshot.from_dict(x)
                for x in data.get("assignments", [])
            ],
            completion_state=str(data.get("completion_state", "active")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "feature": self.feature,
            "revision": self.revision,
            "parent_revision": self.parent_revision,
            "plan_hash": self.plan_hash,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "created_at": self.created_at,
            "tickets": [t.to_dict() for t in self.tickets],
            "assignments": [a.to_dict() for a in self.assignments],
            "completion_state": self.completion_state,
        }

    def hashable_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data["plan_hash"] = ""
        data["assignments"] = []
        return data


__all__ = [
    "AssignmentSnapshot",
    "PlanRevision",
    "PlanTicket",
    "stable_hash",
    "stable_json",
    "utc_now",
]
