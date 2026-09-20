from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from student_execution_os.domain.model import (
    Dependency,
    Event,
    HardCutoff,
    Importance,
    Milestone,
    Task,
    UserTimeConstraint,
    require_aware,
)


@dataclass(frozen=True)
class PlanningPolicy:
    version: str = "pass3-v1"
    minute_grid: int = 1
    start_soon_lead_minutes: int = 120
    max_next_actions: int = 5

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("policy version is required")
        if self.minute_grid != 1:
            raise ValueError("current exact model supports a one-minute grid only")
        if self.start_soon_lead_minutes < 0:
            raise ValueError("start_soon_lead_minutes cannot be negative")
        if not 1 <= self.max_next_actions <= 5:
            raise ValueError("max_next_actions must be in [1,5]")


@dataclass(frozen=True)
class CutoffReconciliationContext:
    task_id: str
    truth_state: str
    evidence_ids: tuple[str, ...]
    policy_version: str
    override_id: str | None
    conflict_id: str | None
    admissible_cutoffs: tuple[HardCutoff, ...]
    planning_projection: HardCutoff | None
    reason: str | None

    def __post_init__(self) -> None:
        if not self.task_id or not self.truth_state or not self.policy_version:
            raise ValueError("cutoff reconciliation context identity is required")


@dataclass(frozen=True)
class PlanningSnapshot:
    account_id: str
    input_server_revision: int
    input_hash: str
    analysis_horizon_start: datetime
    analysis_horizon_end: datetime
    plan_output_horizon_start: datetime
    plan_output_horizon_end: datetime
    tasks: tuple[Task, ...]
    events: tuple[Event, ...]
    constraints: tuple[UserTimeConstraint, ...]
    dependencies: tuple[Dependency, ...]
    milestones: tuple[Milestone, ...]
    policy: PlanningPolicy
    cutoff_reconciliation: tuple[CutoffReconciliationContext, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "analysis_horizon_start",
            "analysis_horizon_end",
            "plan_output_horizon_start",
            "plan_output_horizon_end",
        ):
            require_aware(getattr(self, name), name)
        if not self.account_id or not self.input_hash:
            raise ValueError("snapshot identity is required")
        if self.input_server_revision < 0:
            raise ValueError("input_server_revision cannot be negative")
        if not self.analysis_horizon_start < self.analysis_horizon_end:
            raise ValueError("analysis horizon must be non-empty")
        if not self.plan_output_horizon_start < self.plan_output_horizon_end:
            raise ValueError("plan output horizon must be non-empty")
        if self.plan_output_horizon_start < self.analysis_horizon_start:
            raise ValueError("display horizon cannot start before analysis horizon")
        if self.plan_output_horizon_end > self.analysis_horizon_end:
            raise ValueError("display horizon cannot extend beyond analysis horizon")


class FeasibilityStatus(StrEnum):
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, order=True)
class WorkPlacement:
    starts_at: datetime
    ends_at: datetime
    task_id: str
    source: str = "SEARCH"

    def __post_init__(self) -> None:
        require_aware(self.starts_at, "starts_at")
        require_aware(self.ends_at, "ends_at")
        if not self.task_id or self.starts_at >= self.ends_at:
            raise ValueError("placement requires task id and positive interval")

    @property
    def duration_minutes(self) -> int:
        return int((self.ends_at - self.starts_at).total_seconds() // 60)


@dataclass(frozen=True)
class FeasibilityResult:
    status: FeasibilityStatus
    planning_snapshot_hash: str
    witness: tuple[WorkPlacement, ...] = ()
    reasons: tuple[str, ...] = ()
    explored_nodes: int = 0

    def __post_init__(self) -> None:
        if self.status is FeasibilityStatus.FEASIBLE and not self.witness and not any(
            reason == "NO_REMAINING_HARD_WORK" for reason in self.reasons
        ):
            raise ValueError("FEASIBLE requires a concrete witness unless no hard work remains")
        if self.status is not FeasibilityStatus.FEASIBLE and self.witness:
            raise ValueError("non-feasible result cannot carry a witness")


class PlanBlockType(StrEnum):
    WORK = "WORK"
    EVENT_PROJECTION = "EVENT_PROJECTION"


@dataclass(frozen=True, order=True)
class PlanBlock:
    starts_at: datetime
    ends_at: datetime
    id: str
    type: PlanBlockType
    obligation_id: str | None = None
    source_constraint_ids: tuple[str, ...] = ()
    source_event_id: str | None = None
    explanation: str = ""

    def __post_init__(self) -> None:
        require_aware(self.starts_at, "starts_at")
        require_aware(self.ends_at, "ends_at")
        if not self.id or self.starts_at >= self.ends_at:
            raise ValueError("PlanBlock requires identity and positive interval")
        if self.type is PlanBlockType.WORK and not self.obligation_id:
            raise ValueError("WORK PlanBlock requires obligation_id")
        if self.type is PlanBlockType.EVENT_PROJECTION and not self.source_event_id:
            raise ValueError("EVENT_PROJECTION requires source_event_id")

    @property
    def duration_minutes(self) -> int:
        return int((self.ends_at - self.starts_at).total_seconds() // 60)


@dataclass(frozen=True)
class PlanSnapshot:
    id: str
    account_id: str
    plan_revision: str
    input_server_revision: int
    input_hash: str
    horizon_start: datetime
    horizon_end: datetime
    feasibility_status: FeasibilityStatus
    generated_at: datetime
    blocks: tuple[PlanBlock, ...]
    explanations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("horizon_start", "horizon_end", "generated_at"):
            require_aware(getattr(self, name), name)
        if not self.id or not self.account_id or not self.plan_revision or not self.input_hash:
            raise ValueError("plan identity is required")
        if not self.horizon_start < self.horizon_end:
            raise ValueError("plan horizon must be non-empty")

    def is_current_for(self, snapshot: PlanningSnapshot) -> bool:
        return (
            self.account_id == snapshot.account_id
            and self.input_server_revision == snapshot.input_server_revision
            and self.input_hash == snapshot.input_hash
            and self.horizon_start == snapshot.plan_output_horizon_start
            and self.horizon_end == snapshot.plan_output_horizon_end
        )


class RiskState(StrEnum):
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    SAFE = "SAFE"
    START_SOON = "START_SOON"
    AT_RISK = "AT_RISK"
    CRITICAL = "CRITICAL"
    IMPOSSIBLE = "IMPOSSIBLE"
    OVERDUE = "OVERDUE"


class RiskBasis(StrEnum):
    RESOLVED_FACTS = "RESOLVED_FACTS"
    CONSERVATIVE_CONFLICT_PROJECTION = "CONSERVATIVE_CONFLICT_PROJECTION"
    NO_HARD_CUTOFF = "NO_HARD_CUTOFF"


@dataclass(frozen=True)
class RiskResult:
    task_id: str
    state: RiskState
    basis: RiskBasis
    reasons: tuple[str, ...]
    planning_snapshot_hash: str
    policy_version: str
    latest_safe_start: datetime | None = None

    def __post_init__(self) -> None:
        require_aware(self.latest_safe_start, "latest_safe_start")
        if not self.task_id:
            raise ValueError("risk result requires task_id")


class DisplayColour(StrEnum):
    TRANSPARENT = "TRANSPARENT"
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    BURNING = "BURNING"


@dataclass(frozen=True)
class DisplayProjection:
    task_id: str
    importance: Importance
    computed_risk: RiskState
    colour: DisplayColour


@dataclass(frozen=True)
class NextAction:
    task_id: str
    what: str
    recommended_duration_minutes: int
    why_now: str
    risk_if_skipped: str
    relevant_at: datetime | None
    plan_revision: str

    def __post_init__(self) -> None:
        require_aware(self.relevant_at, "relevant_at")
        if not self.task_id or not self.what or self.recommended_duration_minutes <= 0:
            raise ValueError("next action requires task, text, and positive duration")
