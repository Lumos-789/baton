"""Multi-agent orchestration data contracts.

These dataclasses describe the *agent* side of Baton's orchestration layer:
who an Agent is (``AgentProfile``), what work we hand them (``TaskPacket``),
and the running record of a dispatch (``Assignment``). They model *who* runs
the work; the resource queue that used to sit alongside them now lives
outside this repository and is not imported here.

The structured receipt returned by an agent is reused verbatim from
``orchestration/result.py`` (``ResultEnvelope``) — it is **not** redefined
here. ``Assignment.result_envelope`` therefore stores that envelope as a
plain ``dict`` (its serialized form) so callers can either keep the raw
payload or materialize it via ``ResultEnvelope.from_dict`` when needed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from orchestration.plan_model import utc_now

# ---------------------------------------------------------------------------
# Agent Profile
# ---------------------------------------------------------------------------

#: Runtimes recognized today. A runtime is the process that hosts an agent
#: (DSH, Codex CLI, ...).
RUNTIMES: tuple[str, ...] = ("dsh", "codex")

#: Model families recognized today.
MODELS: tuple[str, ...] = ("glm", "gpt")

#: Reasoning-effort buckets. ``xhigh`` is the strongest tier (used by
#: profiles like ``codex-gpt-xhigh``); it must appear here so the registry's
#: validation accepts it instead of silently treating it as unknown.
REASONING_EFFORTS: tuple[str, ...] = ("minimal", "low", "normal", "high", "xhigh")

#: Dispatchable lifecycle states for an Assignment.
ASSIGNMENT_STATES: tuple[str, ...] = (
    "assigned",
    "running",
    "collecting",
    "succeeded",
    "failed",
    "blocked-review",
)

#: Sensible default roles an AgentProfile may declare.
ROLE_PLANNER = "planner"
ROLE_BUILDER = "builder"
ROLE_REVIEWER = "reviewer"


@dataclass
class AgentProfile:
    """A dispatchable Agent Stack instance.

    A profile bundles an identity (``id``), the runtime/model stack it runs
    on (``runtime`` / ``model``), what it is allowed to do (``roles`` /
    ``capabilities`` / ``data_policy``), how it is isolated
    (``workspace_strategy``), how much parallelism it tolerates
    (``max_parallel``), how to drive it (``driver``) and how to health-check
    it (``probe_command`` / ``probe_interval_seconds``).
    """

    id: str
    runtime: str
    model: str
    reasoning_effort: str = "normal"
    roles: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    data_policy: str = "local-only"
    workspace_strategy: str = "isolated-clone"
    max_parallel: int = 1
    driver: str = ""
    enabled: bool = True
    probe_command: list[str] = field(default_factory=list)
    probe_interval_seconds: int = 300

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentProfile":
        return cls(
            id=str(data.get("id", "")),
            runtime=str(data.get("runtime", "")),
            model=str(data.get("model", "")),
            reasoning_effort=str(data.get("reasoning_effort", "normal")),
            roles=[str(x) for x in data.get("roles", [])],
            capabilities=[str(x) for x in data.get("capabilities", [])],
            data_policy=str(data.get("data_policy", "local-only")),
            workspace_strategy=str(data.get("workspace_strategy", "isolated-clone")),
            max_parallel=int(data.get("max_parallel", 1)),
            driver=str(data.get("driver", "")),
            enabled=bool(data.get("enabled", True)),
            probe_command=[str(x) for x in data.get("probe_command", [])],
            probe_interval_seconds=int(data.get("probe_interval_seconds", 300)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Task Packet — the immutable contract handed to a builder
# ---------------------------------------------------------------------------


@dataclass
class TaskPacket:
    """The immutable, hashable-by-construction work contract handed to a
    builder agent.

    A packet fully describes one unit of work: the goal, how success is
    judged (``acceptance_criteria`` / ``verification_commands`` /
    ``output_contract``), what it depends on (``dependencies`` /
    ``base_commit`` / ``artifacts``), where it may touch (``project_path`` /
    ``allowed_paths`` / ``resources``), and how the orchestrator should
    behave on ambiguity (``retry_policy``). ``plan_revision`` binds the
    packet to the Live Plan revision that produced it; ``task_packet_hash``
    is the canonical content hash callers use for idempotency.
    """

    task_id: str
    role: str
    goal: str
    acceptance_criteria: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    base_commit: str = ""
    artifacts: list[str] = field(default_factory=list)
    project_path: str = ""
    allowed_paths: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    verification_commands: list[str] = field(default_factory=list)
    output_contract: list[str] = field(default_factory=list)
    retry_policy: str = "manual-on-ambiguous"
    plan_revision: int = 0
    task_packet_hash: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskPacket":
        return cls(
            task_id=str(data.get("task_id", "")),
            role=str(data.get("role", "")),
            goal=str(data.get("goal", "")),
            acceptance_criteria=[str(x) for x in data.get("acceptance_criteria", [])],
            dependencies=[str(x) for x in data.get("dependencies", [])],
            base_commit=str(data.get("base_commit", "")),
            artifacts=[str(x) for x in data.get("artifacts", [])],
            project_path=str(data.get("project_path", "")),
            allowed_paths=[str(x) for x in data.get("allowed_paths", [])],
            resources=[str(x) for x in data.get("resources", [])],
            verification_commands=[str(x) for x in data.get("verification_commands", [])],
            output_contract=[str(x) for x in data.get("output_contract", [])],
            retry_policy=str(data.get("retry_policy", "manual-on-ambiguous")),
            plan_revision=int(data.get("plan_revision", 0)),
            task_packet_hash=str(data.get("task_packet_hash", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Assignment — a ticket→agent dispatch record
# ---------------------------------------------------------------------------


@dataclass
class Assignment:
    """A single dispatch of a ``TaskPacket`` to an ``AgentProfile``.

    The structured receipt produced by the agent lives in
    ``result_envelope`` as the *serialized* ``ResultEnvelope`` payload (a
    plain ``dict``, or ``None`` until the agent returns). Materialize it
    with ``ResultEnvelope.from_dict`` when typed access is needed. This
    keeps ``orchestration/result.py`` the single source of truth for the
    receipt shape.
    """

    assignment_id: str
    task_packet: TaskPacket
    profile_id: str
    state: str = "assigned"
    result_envelope: dict[str, Any] | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Assignment":
        raw_packet = data.get("task_packet") or {}
        task_packet = (
            raw_packet
            if isinstance(raw_packet, TaskPacket)
            else TaskPacket.from_dict(raw_packet if isinstance(raw_packet, dict) else {})
        )
        return cls(
            assignment_id=str(data.get("assignment_id", "")),
            task_packet=task_packet,
            profile_id=str(data.get("profile_id", "")),
            state=str(data.get("state", "assigned")),
            result_envelope=data.get("result_envelope"),
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
