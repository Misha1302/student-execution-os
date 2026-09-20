from __future__ import annotations

from typing import Protocol

from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    Dependency,
    DependencySuccessorKind,
    Event,
    HardCutoff,
    Importance,
    LocationEffect,
    Milestone,
    MilestoneOwnerKind,
    MilestoneRole,
    ObligationCategory,
    Project,
    Task,
    UserTimeConstraint,
    UserTimeConstraintType,
)


class CanonicalRepository(Protocol):
    """Port for account-scoped canonical local state used by later planning passes."""

    def create_account(self, account_id: str) -> None: ...

    def create_task(
        self,
        *,
        account_id: str,
        title: str,
        category: ObligationCategory,
        importance: Importance,
        estimated_total_effort_minutes: int,
        remaining_effort_minutes: int,
        splittable: bool,
        actual_cutoff: HardCutoff,
        actor: ActorCategory,
        description: str | None = None,
        min_chunk_minutes: int | None = None,
        max_chunk_minutes: int | None = None,
        actionable_from=None,
        target_at=None,
        obligation_id: str | None = None,
    ) -> Task: ...

    def get_task(self, account_id: str, obligation_id: str) -> Task: ...

    def create_fixed_event(
        self,
        *,
        account_id: str,
        title: str,
        starts_at,
        ends_at,
        actor: ActorCategory,
        category: ObligationCategory = ObligationCategory.GENERAL,
        importance: Importance = Importance.NORMAL,
        attendance_policy: AttendancePolicy = AttendancePolicy.REQUIRED,
        location_effect: LocationEffect | None = None,
        description: str | None = None,
        obligation_id: str | None = None,
    ) -> Event: ...

    def create_project(self, *, account_id: str, title: str, actor: ActorCategory, **kwargs) -> Project: ...
    def add_dependency(
        self,
        *,
        account_id: str,
        predecessor_task_id: str,
        successor_kind: DependencySuccessorKind,
        successor_id: str,
        actor: ActorCategory,
        dependency_id: str | None = None,
    ) -> Dependency: ...
    def create_milestone(
        self,
        *,
        account_id: str,
        owner_kind: MilestoneOwnerKind,
        owner_id: str,
        title: str,
        marker_at,
        role: MilestoneRole,
        actor: ActorCategory,
        **kwargs,
    ) -> Milestone: ...
    def create_time_constraint(
        self,
        *,
        account_id: str,
        type: UserTimeConstraintType,
        starts_at,
        ends_at,
        actor: ActorCategory,
        **kwargs,
    ) -> UserTimeConstraint: ...
