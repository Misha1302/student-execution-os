from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from student_execution_os.domain.model import CutoffBoundary, CutoffState, Task, require_aware
from student_execution_os.planning.feasibility import FeasibilityEngine
from student_execution_os.planning.model import (
    FeasibilityStatus,
    PlanningSnapshot,
    RiskBasis,
    RiskResult,
    RiskState,
)


class Scenario(StrEnum):
    OPTIMISTIC = "OPTIMISTIC"
    EXPECTED = "EXPECTED"
    SAFE = "SAFE"


def _scenario_effort(task: Task, scenario: Scenario) -> int:
    if scenario is Scenario.OPTIMISTIC:
        return task.remaining_effort_low_minutes if task.remaining_effort_low_minutes is not None else task.remaining_effort_minutes
    if scenario is Scenario.SAFE:
        return task.remaining_effort_high_minutes if task.remaining_effort_high_minutes is not None else task.remaining_effort_minutes
    return task.remaining_effort_minutes


def _derived_hash(snapshot: PlanningSnapshot, label: str, tasks: tuple[Task, ...]) -> str:
    body = [snapshot.input_hash, label]
    body.extend(f"{t.obligation.id}:{t.remaining_effort_minutes}:{t.actionable_from}" for t in tasks)
    return hashlib.sha256("|".join(body).encode("utf-8")).hexdigest()


def scenario_snapshot(snapshot: PlanningSnapshot, scenario: Scenario) -> PlanningSnapshot | None:
    tasks: list[Task] = []
    try:
        for task in snapshot.tasks:
            tasks.append(replace(task, remaining_effort_minutes=_scenario_effort(task, scenario)))
    except Exception:
        return None
    task_tuple = tuple(tasks)
    return replace(snapshot, tasks=task_tuple, input_hash=_derived_hash(snapshot, scenario.value, task_tuple))


def _past_hard_cutoff(cutoff, now: datetime) -> bool:
    assert cutoff.state is CutoffState.KNOWN and cutoff.at is not None
    if cutoff.boundary is CutoffBoundary.EXCLUSIVE:
        return now >= cutoff.at
    return now > cutoff.at


def _past_cutoff(task: Task, now: datetime) -> bool:
    return _past_hard_cutoff(task.actual_cutoff, now)


@dataclass(frozen=True)
class RiskEngine:
    node_limit: int = 200_000
    timeout_seconds: float = 2.0

    def evaluate(self, snapshot: PlanningSnapshot, now: datetime) -> dict[str, RiskResult]:
        require_aware(now, "now")
        scenario_snaps = {scenario: scenario_snapshot(snapshot, scenario) for scenario in Scenario}
        scenario_results = {}
        for scenario, scenario_snap in scenario_snaps.items():
            scenario_results[scenario] = (
                None if scenario_snap is None
                else FeasibilityEngine(node_limit=self.node_limit, timeout_seconds=self.timeout_seconds).evaluate(scenario_snap)
            )

        results: dict[str, RiskResult] = {}
        reconciliation = {item.task_id: item for item in snapshot.cutoff_reconciliation}
        for task in snapshot.tasks:
            if task.remaining_effort_minutes <= 0:
                continue
            task_id = task.obligation.id
            context = reconciliation.get(task_id)
            if context is not None and context.truth_state == "CONFLICT":
                exact_conflict = (
                    context.reason == "EXACT_CUTOFF_VALUES_CONFLICT"
                    and bool(context.admissible_cutoffs)
                )
                passed = [
                    _past_hard_cutoff(cutoff, now)
                    for cutoff in context.admissible_cutoffs
                ]
                if exact_conflict and passed and all(passed):
                    results[task_id] = self._result(
                        snapshot,
                        task_id,
                        RiskState.OVERDUE,
                        RiskBasis.CONSERVATIVE_CONFLICT_PROJECTION,
                        ("ALL_ADMISSIBLE_CONFLICTING_CUTOFFS_PASSED",),
                    )
                else:
                    straddles_now = bool(passed) and any(passed) and not all(passed)
                    results[task_id] = self._result(
                        snapshot,
                        task_id,
                        RiskState.UNKNOWN,
                        RiskBasis.CONSERVATIVE_CONFLICT_PROJECTION,
                        (
                            "UNRESOLVED_CUTOFF_CONFLICT_STRADDLES_NOW"
                            if straddles_now
                            else "UNRESOLVED_CUTOFF_CONFLICT"
                        ,),
                    )
                continue
            if context is not None and context.truth_state == "UNKNOWN":
                results[task_id] = self._result(
                    snapshot,
                    task_id,
                    RiskState.UNKNOWN,
                    RiskBasis.RESOLVED_FACTS,
                    ("UNRESOLVED_HARD_CUTOFF",),
                )
                continue
            if context is not None and context.truth_state == "ABSENT":
                results[task_id] = self._result(
                    snapshot,
                    task_id,
                    RiskState.NOT_APPLICABLE,
                    RiskBasis.NO_HARD_CUTOFF,
                    ("NO_HARD_CUTOFF",),
                )
                continue

            cutoff = task.actual_cutoff
            if cutoff.state is CutoffState.ABSENT:
                results[task_id] = self._result(snapshot, task_id, RiskState.NOT_APPLICABLE, RiskBasis.NO_HARD_CUTOFF, ("NO_HARD_CUTOFF",))
                continue
            if cutoff.state is CutoffState.UNKNOWN:
                results[task_id] = self._result(snapshot, task_id, RiskState.UNKNOWN, RiskBasis.RESOLVED_FACTS, ("UNKNOWN_HARD_CUTOFF",))
                continue
            if _past_cutoff(task, now):
                results[task_id] = self._result(snapshot, task_id, RiskState.OVERDUE, RiskBasis.RESOLVED_FACTS, ("HARD_CUTOFF_PASSED",))
                continue

            optimistic = scenario_results[Scenario.OPTIMISTIC]
            expected = scenario_results[Scenario.EXPECTED]
            safe = scenario_results[Scenario.SAFE]
            if optimistic is None or expected is None or safe is None:
                results[task_id] = self._result(snapshot, task_id, RiskState.UNKNOWN, RiskBasis.RESOLVED_FACTS, ("INVALID_SCENARIO_BOUNDS",))
                continue
            if optimistic.status is FeasibilityStatus.INFEASIBLE:
                results[task_id] = self._result(snapshot, task_id, RiskState.IMPOSSIBLE, RiskBasis.RESOLVED_FACTS, ("OPTIMISTIC_INFEASIBLE",))
                continue
            if optimistic.status is FeasibilityStatus.UNKNOWN:
                results[task_id] = self._result(snapshot, task_id, RiskState.UNKNOWN, RiskBasis.RESOLVED_FACTS, ("OPTIMISTIC_UNKNOWN",))
                continue
            if expected.status is FeasibilityStatus.INFEASIBLE:
                results[task_id] = self._result(snapshot, task_id, RiskState.CRITICAL, RiskBasis.RESOLVED_FACTS, ("EXPECTED_INFEASIBLE",))
                continue
            if expected.status is FeasibilityStatus.UNKNOWN:
                results[task_id] = self._result(snapshot, task_id, RiskState.UNKNOWN, RiskBasis.RESOLVED_FACTS, ("EXPECTED_UNKNOWN",))
                continue
            if safe.status is FeasibilityStatus.INFEASIBLE:
                results[task_id] = self._result(snapshot, task_id, RiskState.AT_RISK, RiskBasis.RESOLVED_FACTS, ("SAFE_INFEASIBLE",))
                continue
            if safe.status is FeasibilityStatus.UNKNOWN:
                results[task_id] = self._result(snapshot, task_id, RiskState.UNKNOWN, RiskBasis.RESOLVED_FACTS, ("SAFE_UNKNOWN",))
                continue

            safe_snapshot = scenario_snaps[Scenario.SAFE]
            assert safe_snapshot is not None
            latest = self._latest_safe_start(safe_snapshot, task_id, now)
            if latest is None:
                results[task_id] = self._result(snapshot, task_id, RiskState.UNKNOWN, RiskBasis.RESOLVED_FACTS, ("LATEST_SAFE_START_UNKNOWN",))
                continue
            lead = now + timedelta(minutes=snapshot.policy.start_soon_lead_minutes)
            state = RiskState.START_SOON if latest <= lead else RiskState.SAFE
            results[task_id] = self._result(snapshot, task_id, state, RiskBasis.RESOLVED_FACTS, ("SAFE_SCENARIO_FEASIBLE",), latest)
        return results

    def _latest_safe_start(self, snapshot: PlanningSnapshot, task_id: str, now: datetime) -> datetime | None:
        task = next((t for t in snapshot.tasks if t.obligation.id == task_id), None)
        if task is None or task.actual_cutoff.state is not CutoffState.KNOWN or task.actual_cutoff.at is None:
            return None
        lower = max(now, task.actionable_from or now, snapshot.analysis_horizon_start)
        cutoff = task.actual_cutoff.at
        if lower >= cutoff and task.remaining_effort_minutes > 0:
            return None
        max_minutes = max(0, int((cutoff - lower).total_seconds() // 60))
        lo, hi = 0, max_minutes
        best: datetime | None = None
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = lower + timedelta(minutes=mid)
            changed_tasks = tuple(
                replace(t, actionable_from=max(t.actionable_from or snapshot.analysis_horizon_start, candidate))
                if t.obligation.id == task_id else t
                for t in snapshot.tasks
            )
            candidate_snapshot = replace(
                snapshot,
                tasks=changed_tasks,
                input_hash=_derived_hash(snapshot, f"LATEST:{task_id}:{candidate.isoformat()}", changed_tasks),
            )
            result = FeasibilityEngine(node_limit=self.node_limit, timeout_seconds=self.timeout_seconds).evaluate(candidate_snapshot)
            if result.status is FeasibilityStatus.UNKNOWN:
                return None
            if result.status is FeasibilityStatus.FEASIBLE:
                best = candidate
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    @staticmethod
    def _result(snapshot, task_id, state, basis, reasons, latest=None):
        return RiskResult(
            task_id=task_id,
            state=state,
            basis=basis,
            reasons=tuple(reasons),
            planning_snapshot_hash=snapshot.input_hash,
            policy_version=snapshot.policy.version,
            latest_safe_start=latest,
        )
