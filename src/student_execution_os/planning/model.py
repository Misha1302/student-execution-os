from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from student_execution_os.domain.model import Dependency, Event, Milestone, Task, UserTimeConstraint, require_aware


@dataclass(frozen=True)
class PlanningPolicy:
    version: str = "pass2-v1"
    minute_grid: int = 1

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("policy version is required")
        if self.minute_grid != 1:
            raise ValueError("Pass 2 supports a one-minute exact grid only")


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
