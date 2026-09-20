from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from datetime import datetime

from student_execution_os.domain.model import (
    AttendancePolicy,
    CutoffBoundary,
    CutoffState,
    DependencySuccessorKind,
    HalfOpenInterval,
    LifecycleStatus,
    MilestoneStatus,
    UserTimeConstraintType,
)
from student_execution_os.planning.model import (
    FeasibilityResult,
    FeasibilityStatus,
    PlanningSnapshot,
    WorkPlacement,
)
from student_execution_os.planning.search import BoundedSearch, merge_intervals
from student_execution_os.planning.witness import validate_witness

@dataclass(frozen=True)
class FeasibilityEngine:
    exact_search_enabled: bool = True
    node_limit: int = 200_000
    timeout_seconds: float = 2.0
    task_tie_break: Callable[[object], tuple] | None = None

    def evaluate(self, snapshot: PlanningSnapshot) -> FeasibilityResult:
        if self.node_limit < 0 or self.timeout_seconds < 0:
            raise ValueError("search budgets cannot be negative")
        unsupported = self._unsupported_reason(snapshot)
        if unsupported:
            return self._unknown(snapshot, unsupported)

        required_events = [
            e for e in snapshot.events
            if e.attendance_policy is AttendancePolicy.REQUIRED
            and e.obligation.lifecycle_status is LifecycleStatus.ACTIVE
            and self._overlaps_horizon(e.interval, snapshot)
        ]
        constraints = [c for c in snapshot.constraints if self._overlaps_horizon(c.interval, snapshot)]

        conflict = self._fixed_conflict(required_events, constraints)
        if conflict:
            return FeasibilityResult(
                FeasibilityStatus.INFEASIBLE, snapshot.input_hash, reasons=(conflict,), explored_nodes=0
            )

        tasks = [t for t in snapshot.tasks if t.remaining_effort_minutes > 0]
        if not tasks:
            return FeasibilityResult(
                FeasibilityStatus.FEASIBLE,
                snapshot.input_hash,
                reasons=("NO_REMAINING_HARD_WORK",),
                explored_nodes=0,
            )

        unknown_cutoff = next((t for t in tasks if t.actual_cutoff.state is CutoffState.UNKNOWN), None)
        if unknown_cutoff is not None:
            return self._unknown(snapshot, f"UNKNOWN_HARD_CUTOFF:{unknown_cutoff.obligation.id}")

        pin_state = self._pinned_state(snapshot, tasks)
        if isinstance(pin_state, str):
            if pin_state.startswith("UNSUPPORTED"):
                return self._unknown(snapshot, pin_state)
            return FeasibilityResult(
                FeasibilityStatus.INFEASIBLE, snapshot.input_hash, reasons=(pin_state,), explored_nodes=0
            )
        pinned_by_task, occupied = pin_state
        occupied.extend(e.interval for e in required_events)
        occupied.extend(c.interval for c in constraints if c.type is not UserTimeConstraintType.PINNED_WORK)
        occupied = merge_intervals(occupied)

        task_map = {t.obligation.id: t for t in tasks}
        milestone_map = {m.id: m for m in snapshot.milestones if m.status is MilestoneStatus.ACTIVE}
        event_map = {e.obligation.id: e for e in required_events}
        deps = tuple(snapshot.dependencies)

        effective_deadlines: dict[str, tuple[datetime, CutoffBoundary | None]] = {}
        for task in tasks:
            cutoff = task.actual_cutoff
            if cutoff.state is CutoffState.KNOWN:
                assert cutoff.at is not None
                effective_deadlines[task.obligation.id] = (cutoff.at, cutoff.boundary)
            else:
                effective_deadlines[task.obligation.id] = (snapshot.analysis_horizon_end, CutoffBoundary.INCLUSIVE)
        for dep in deps:
            if dep.predecessor_task_id not in task_map:
                continue
            bound: datetime | None = None
            if dep.successor_kind is DependencySuccessorKind.EVENT and dep.successor_id in event_map:
                bound = event_map[dep.successor_id].interval.starts_at
            elif dep.successor_kind is DependencySuccessorKind.MILESTONE:
                m = milestone_map.get(dep.successor_id)
                if m is not None and m.hard_for_planning:
                    bound = m.marker_at
            if bound is not None:
                old, old_boundary = effective_deadlines[dep.predecessor_task_id]
                if bound < old:
                    effective_deadlines[dep.predecessor_task_id] = (bound, CutoffBoundary.INCLUSIVE)

        order_or_error = self._task_order(tasks, deps)
        if isinstance(order_or_error, str):
            return self._unknown(snapshot, order_or_error)
        ordered_tasks = order_or_error

        search = BoundedSearch(self.node_limit, self.timeout_seconds)
        greedy = search.greedy(
            snapshot, ordered_tasks, deps, effective_deadlines, occupied, pinned_by_task
        )
        if greedy is not None:
            candidate = tuple(sorted(greedy))
            validation = validate_witness(snapshot, candidate)
            if validation:
                return self._unknown(snapshot, "INTERNAL_WITNESS_VALIDATION_FAILED:" + validation[0])
            return FeasibilityResult(
                FeasibilityStatus.FEASIBLE, snapshot.input_hash, witness=candidate,
                reasons=("CONSTRUCTIVE_WITNESS",), explored_nodes=0,
            )
        if not self.exact_search_enabled:
            return self._unknown(snapshot, "CONSTRUCTIVE_SEARCH_FAILED_WITHOUT_PROOF")

        witness, state = search.exact(
            snapshot, ordered_tasks, deps, effective_deadlines, occupied, pinned_by_task
        )
        if witness is not None:
            candidate = tuple(sorted(witness))
            validation = validate_witness(snapshot, candidate)
            if validation:
                return self._unknown(
                    snapshot, "INTERNAL_WITNESS_VALIDATION_FAILED:" + validation[0], state.nodes
                )
            return FeasibilityResult(
                FeasibilityStatus.FEASIBLE,
                snapshot.input_hash,
                witness=candidate,
                reasons=("EXACT_WITNESS",),
                explored_nodes=state.nodes,
            )
        if state.timed_out:
            return self._unknown(snapshot, "EXACT_SEARCH_BUDGET_EXHAUSTED", state.nodes)
        if any(task.actual_cutoff.state is CutoffState.ABSENT for task in ordered_tasks):
            return self._unknown(
                snapshot, "NO_HARD_CUTOFF_EXHAUSTED_DISPLAYED_ANALYSIS_HORIZON", state.nodes
            )
        return FeasibilityResult(
            FeasibilityStatus.INFEASIBLE,
            snapshot.input_hash,
            reasons=("EXACT_SEARCH_EXHAUSTED_NO_LEGAL_WITNESS",),
            explored_nodes=state.nodes,
        )

    def _unsupported_reason(self, snapshot: PlanningSnapshot) -> str | None:
        for event in snapshot.events:
            if (
                event.obligation.lifecycle_status is LifecycleStatus.ACTIVE
                and event.attendance_policy is not AttendancePolicy.REQUIRED
                and self._overlaps_horizon(event.interval, snapshot)
            ):
                return f"UNSUPPORTED_OPTIONAL_EVENT_POLICY:{event.obligation.id}"
        values = [
            snapshot.analysis_horizon_start,
            snapshot.analysis_horizon_end,
            snapshot.plan_output_horizon_start,
            snapshot.plan_output_horizon_end,
        ]
        for task in snapshot.tasks:
            values.extend([task.actionable_from, task.actual_cutoff.at])
        for event in snapshot.events:
            values.extend([event.interval.starts_at, event.interval.ends_at])
        for constraint in snapshot.constraints:
            values.extend([constraint.interval.starts_at, constraint.interval.ends_at])
        for milestone in snapshot.milestones:
            values.append(milestone.marker_at)
        for value in values:
            if value is not None and (value.second != 0 or value.microsecond != 0):
                return "UNSUPPORTED_SUB_MINUTE_TIME"
        return None

    def _fixed_conflict(self, events, constraints) -> str | None:
        for i, left in enumerate(events):
            for right in events[i + 1 :]:
                if left.interval.overlaps(right.interval):
                    return f"REQUIRED_EVENT_CONFLICT:{left.obligation.id}:{right.obligation.id}"
        for event in events:
            for constraint in constraints:
                if event.interval.overlaps(constraint.interval):
                    return f"REQUIRED_EVENT_CONSTRAINT_CONFLICT:{event.obligation.id}:{constraint.id}"
        pins = [c for c in constraints if c.type is UserTimeConstraintType.PINNED_WORK]
        for i, left in enumerate(pins):
            for right in pins[i + 1 :]:
                if left.interval.overlaps(right.interval):
                    return f"PINNED_WORK_CONFLICT:{left.id}:{right.id}"
        return None

    def _pinned_state(self, snapshot, tasks):
        task_map = {t.obligation.id: t for t in tasks}
        pins: dict[str, list] = {}
        for c in snapshot.constraints:
            if c.type is UserTimeConstraintType.PINNED_WORK:
                pins.setdefault(c.obligation_id or "", []).append(c)
        pinned_by_task: dict[str, list[WorkPlacement]] = {}
        occupied: list[HalfOpenInterval] = []
        for task_id, task_pins in pins.items():
            if len(task_pins) > 1:
                return f"UNSUPPORTED_MULTIPLE_PINNED_WORK:{task_id}"
            task = task_map.get(task_id)
            if task is None:
                occupied.extend(c.interval for c in task_pins)
                continue
            pin = task_pins[0]
            duration = int((pin.interval.ends_at - pin.interval.starts_at).total_seconds() // 60)
            if task.actionable_from and pin.interval.starts_at < task.actionable_from:
                return f"PIN_BEFORE_ACTIONABLE:{task_id}"
            if not self._ends_before_cutoff(task, pin.interval.ends_at):
                return f"PIN_AFTER_CUTOFF:{task_id}"
            if not task.splittable and duration != task.remaining_effort_minutes:
                return f"UNSUPPORTED_PARTIAL_NON_SPLITTABLE_PIN:{task_id}"
            if task.max_chunk_minutes is not None and duration > task.max_chunk_minutes:
                return f"UNSUPPORTED_PIN_EXCEEDS_MAX_CHUNK:{task_id}"
            if task.splittable and task.min_chunk_minutes is not None and duration < task.min_chunk_minutes and duration < task.remaining_effort_minutes:
                return f"UNSUPPORTED_PIN_BELOW_MIN_CHUNK_WITH_RESIDUAL:{task_id}"
            pinned_by_task[task_id] = [WorkPlacement(pin.interval.starts_at, pin.interval.ends_at, task_id, "PINNED")]
            occupied.append(pin.interval)
        return pinned_by_task, occupied

    def _task_order(self, tasks, deps):
        ids = {t.obligation.id for t in tasks}
        incoming = {i: set() for i in ids}
        outgoing = {i: set() for i in ids}
        for d in deps:
            if d.successor_kind is DependencySuccessorKind.TASK and d.predecessor_task_id in ids and d.successor_id in ids:
                outgoing[d.predecessor_task_id].add(d.successor_id)
                incoming[d.successor_id].add(d.predecessor_task_id)
        by_id = {t.obligation.id: t for t in tasks}
        def sort_key(task_id):
            return self.task_tie_break(by_id[task_id]) if self.task_tie_break is not None else (task_id,)
        ready = sorted((i for i in ids if not incoming[i]), key=sort_key)
        ordered_ids = []
        while ready:
            node = ready.pop(0)
            ordered_ids.append(node)
            for nxt in sorted(outgoing[node]):
                incoming[nxt].remove(node)
                if not incoming[nxt]:
                    ready.append(nxt)
                    ready.sort(key=sort_key)
        if len(ordered_ids) != len(ids):
            return "DEPENDENCY_GRAPH_NOT_ACYCLIC"
        return [by_id[i] for i in ordered_ids]
    @staticmethod
    def _ends_before_cutoff(task, end) -> bool:
        cutoff = task.actual_cutoff
        if cutoff.state is not CutoffState.KNOWN:
            return True
        assert cutoff.at is not None
        return end <= cutoff.at if cutoff.boundary is CutoffBoundary.INCLUSIVE else end < cutoff.at

    @staticmethod
    def _overlaps_horizon(interval, snapshot) -> bool:
        return (
            interval.starts_at < snapshot.analysis_horizon_end
            and snapshot.analysis_horizon_start < interval.ends_at
        )

    @staticmethod
    def _unknown(snapshot, reason, nodes=0):
        return FeasibilityResult(
            FeasibilityStatus.UNKNOWN, snapshot.input_hash, reasons=(reason,), explored_nodes=nodes
        )

