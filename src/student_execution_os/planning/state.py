from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from student_execution_os.domain.model import (
    Dependency,
    DependencySuccessorKind,
    Event,
    Milestone,
    MilestoneOwnerKind,
    MilestoneRole,
    MilestoneStatus,
    Task,
    UserTimeConstraint,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt
from student_execution_os.planning.model import CutoffReconciliationContext
from student_execution_os.reconciliation.repository import SQLiteReconciliationRepository
from student_execution_os.travel.projection import TravelProjectionBuilder
from student_execution_os.travel.repository import SQLiteTravelRepository


class PlanningStateSource(Protocol):
    def get_server_revision(self, account_id: str) -> int: ...
    def list_tasks(self, account_id: str) -> list[Task]: ...
    def list_events(self, account_id: str) -> list[Event]: ...
    def list_dependencies(self, account_id: str) -> list[Dependency]: ...
    def list_milestones(self, account_id: str) -> list[Milestone]: ...
    def list_time_constraints(self, account_id: str) -> list[UserTimeConstraint]: ...
    def list_cutoff_reconciliation(self, account_id: str) -> list[CutoffReconciliationContext]: ...
    def build_travel_projection(self, account_id: str, events: tuple[Event, ...], analysis_horizon_start, analysis_horizon_end): ...


@dataclass(frozen=True)
class SQLitePlanningStateSource:
    """Read-only planning projection over the canonical SQLite adapter.

    This keeps planning reads separate from the canonical mutation port while
    reusing the canonical entity decoders for Task/Event/UserTimeConstraint.
    """

    repository: SQLiteCanonicalRepository

    def get_server_revision(self, account_id: str) -> int:
        return self.repository.get_server_revision(account_id)

    def list_tasks(self, account_id: str) -> list[Task]:
        self.repository._require_account(account_id)
        rows = self.repository.connection.execute(
            "SELECT id FROM obligations WHERE account_id=? AND kind='TASK' ORDER BY id", (account_id,)
        ).fetchall()
        reconciliation = SQLiteReconciliationRepository(self.repository)
        return [
            reconciliation.apply_cutoff_projection(self.repository.get_task(account_id, row["id"]))
            for row in rows
        ]

    def list_cutoff_reconciliation(self, account_id: str) -> list[CutoffReconciliationContext]:
        self.repository._require_account(account_id)
        rows = self.repository.connection.execute(
            "SELECT id FROM obligations WHERE account_id=? AND kind='TASK' ORDER BY id", (account_id,)
        ).fetchall()
        reconciliation = SQLiteReconciliationRepository(self.repository)
        contexts: list[CutoffReconciliationContext] = []
        for row in rows:
            effective = reconciliation.get_effective_cutoff(account_id, row["id"])
            if effective is None:
                continue
            contexts.append(
                CutoffReconciliationContext(
                    task_id=row["id"],
                    truth_state=effective.state.value,
                    evidence_ids=effective.evidence_ids,
                    policy_version=effective.policy_version,
                    override_id=effective.override_id,
                    conflict_id=effective.conflict_id,
                    admissible_cutoffs=effective.admissible_cutoffs,
                    planning_projection=effective.planning_projection,
                    reason=effective.reason,
                )
            )
        return contexts

    def list_events(self, account_id: str) -> list[Event]:
        self.repository._require_account(account_id)
        rows = self.repository.connection.execute(
            "SELECT id FROM obligations WHERE account_id=? AND kind='EVENT' ORDER BY id", (account_id,)
        ).fetchall()
        return [self.repository.get_event(account_id, row["id"]) for row in rows]

    def build_travel_projection(
        self,
        account_id: str,
        events: tuple[Event, ...],
        analysis_horizon_start,
        analysis_horizon_end,
    ):
        travel = SQLiteTravelRepository(self.repository)
        return TravelProjectionBuilder(travel).build(
            account_id=account_id,
            events=events,
            current_location=travel.current_location(account_id),
            analysis_horizon_start=analysis_horizon_start,
            analysis_horizon_end=analysis_horizon_end,
        )

    def list_dependencies(self, account_id: str) -> list[Dependency]:
        self.repository._require_account(account_id)
        rows = self.repository.connection.execute(
            "SELECT id,account_id,predecessor_task_id,successor_kind,successor_id "
            "FROM dependencies WHERE account_id=? ORDER BY id", (account_id,)
        ).fetchall()
        return [
            Dependency(
                id=row["id"], account_id=row["account_id"],
                predecessor_task_id=row["predecessor_task_id"],
                successor_kind=DependencySuccessorKind(row["successor_kind"]),
                successor_id=row["successor_id"],
            )
            for row in rows
        ]

    def list_milestones(self, account_id: str) -> list[Milestone]:
        self.repository._require_account(account_id)
        rows = self.repository.connection.execute(
            "SELECT * FROM milestones WHERE account_id=? ORDER BY id", (account_id,)
        ).fetchall()
        return [
            Milestone(
                id=row["id"], account_id=row["account_id"],
                owner_kind=MilestoneOwnerKind(row["owner_kind"]), owner_id=row["owner_id"],
                title=row["title"], marker_at=_dt(row["marker_at"]),
                role=MilestoneRole(row["role"]), consequence=row["consequence"],
                hard_for_planning=bool(row["hard_for_planning"]),
                status=MilestoneStatus(row["status"]), version=int(row["version"]),
            )
            for row in rows
        ]

    def list_time_constraints(self, account_id: str) -> list[UserTimeConstraint]:
        self.repository._require_account(account_id)
        rows = self.repository.connection.execute(
            "SELECT id FROM user_time_constraints WHERE account_id=? ORDER BY id", (account_id,)
        ).fetchall()
        return [self.repository.get_time_constraint(account_id, row["id"]) for row in rows]