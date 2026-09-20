from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic

from student_execution_os.domain.model import (
    CutoffBoundary,
    DependencySuccessorKind,
    HalfOpenInterval,
)
from student_execution_os.planning.model import PlanningSnapshot, WorkPlacement

_MINUTE = timedelta(minutes=1)


@dataclass
class SearchState:
    nodes: int = 0
    timed_out: bool = False


@dataclass(frozen=True)
class BoundedSearch:
    """Deterministic constructive + complete minute-grid search for the supported Pass 2 model."""

    node_limit: int
    timeout_seconds: float

    @staticmethod
    def predecessor_floor(task_id, deps, completion_by_task, default):
        floor = default
        for dependency in deps:
            if dependency.successor_kind is DependencySuccessorKind.TASK and dependency.successor_id == task_id:
                end = completion_by_task.get(dependency.predecessor_task_id)
                if end is not None and end > floor:
                    floor = end
        return floor

    def greedy(self, snapshot, tasks, deps, deadlines, occupied, pinned_by_task):
        placements = {key: list(value) for key, value in pinned_by_task.items()}
        completion = {
            task_id: max(placement.ends_at for placement in task_placements)
            for task_id, task_placements in placements.items()
            if task_placements
        }
        occupancy = list(occupied)
        for task in tasks:
            task_id = task.obligation.id
            pinned = placements.get(task_id, [])
            remaining = max(0, task.remaining_effort_minutes - sum(p.duration_minutes for p in pinned))
            if remaining == 0:
                if pinned:
                    completion[task_id] = max(p.ends_at for p in pinned)
                else:
                    completion[task_id] = self.predecessor_floor(
                        task_id, deps, completion, snapshot.analysis_horizon_start
                    )
                continue

            earliest = max(
                snapshot.analysis_horizon_start,
                task.actionable_from or snapshot.analysis_horizon_start,
            )
            earliest = self.predecessor_floor(task_id, deps, completion, earliest)
            deadline, boundary = deadlines[task_id]
            chosen = []
            for duration in self.greedy_chunks(task, remaining):
                candidate = next(
                    self.candidate_blocks(task, duration, earliest, deadline, boundary, occupancy),
                    None,
                )
                if candidate is None:
                    return None
                chosen.append(candidate)
                occupancy.append(HalfOpenInterval(candidate.starts_at, candidate.ends_at))
                occupancy = merge_intervals(occupancy)
                earliest = candidate.ends_at
            placements.setdefault(task_id, []).extend(chosen)
            completion[task_id] = max(p.ends_at for p in placements[task_id])
        return [placement for task_placements in placements.values() for placement in task_placements]

    @staticmethod
    def greedy_chunks(task, remaining):
        if not task.splittable:
            return [remaining]
        min_chunk = task.min_chunk_minutes or 1
        max_chunk = task.max_chunk_minutes or remaining
        result = []
        while remaining > 0:
            if remaining < min_chunk:
                result.append(remaining)
                break
            take = min(max_chunk, remaining)
            result.append(take)
            remaining -= take
        return result

    def exact(self, snapshot, tasks, deps, deadlines, occupied, pinned_by_task):
        deadline_clock = monotonic() + self.timeout_seconds
        state = SearchState()
        witness = self._search_tasks(
            snapshot,
            tasks,
            deps,
            deadlines,
            list(occupied),
            {key: list(value) for key, value in pinned_by_task.items()},
            {},
            0,
            deadline_clock,
            state,
        )
        return witness, state

    def _search_tasks(
        self,
        snapshot,
        tasks,
        deps,
        deadlines,
        occupied,
        placements,
        completion,
        index,
        deadline_clock,
        state,
    ):
        if monotonic() > deadline_clock or state.nodes >= self.node_limit:
            state.timed_out = True
            return None
        if index >= len(tasks):
            return [placement for task_placements in placements.values() for placement in task_placements]

        task = tasks[index]
        task_id = task.obligation.id
        pinned = list(placements.get(task_id, []))
        remaining = max(0, task.remaining_effort_minutes - sum(p.duration_minutes for p in pinned))
        earliest = max(
            snapshot.analysis_horizon_start,
            task.actionable_from or snapshot.analysis_horizon_start,
        )
        earliest = self.predecessor_floor(task_id, deps, completion, earliest)
        task_deadline, boundary = deadlines[task_id]

        if remaining == 0:
            completion_next = dict(completion)
            completion_next[task_id] = max((p.ends_at for p in pinned), default=earliest)
            return self._search_tasks(
                snapshot,
                tasks,
                deps,
                deadlines,
                occupied,
                placements,
                completion_next,
                index + 1,
                deadline_clock,
                state,
            )

        for task_placements, next_occupancy in self._enumerate_task_placements(
            task,
            remaining,
            earliest,
            task_deadline,
            boundary,
            occupied,
            deadline_clock,
            state,
        ):
            placements_next = {key: list(value) for key, value in placements.items()}
            placements_next.setdefault(task_id, []).extend(task_placements)
            completion_next = dict(completion)
            completion_next[task_id] = max(p.ends_at for p in placements_next[task_id])
            found = self._search_tasks(
                snapshot,
                tasks,
                deps,
                deadlines,
                next_occupancy,
                placements_next,
                completion_next,
                index + 1,
                deadline_clock,
                state,
            )
            if found is not None:
                return found
            if state.timed_out:
                return None
        return None

    def _enumerate_task_placements(
        self,
        task,
        remaining,
        earliest,
        deadline,
        boundary,
        occupied,
        deadline_clock,
        state,
    ):
        if monotonic() > deadline_clock or state.nodes >= self.node_limit:
            state.timed_out = True
            return
        if remaining == 0:
            yield [], occupied
            return

        for duration in self.chunk_lengths(task, remaining):
            for candidate in self.candidate_blocks(task, duration, earliest, deadline, boundary, occupied):
                state.nodes += 1
                if monotonic() > deadline_clock or state.nodes > self.node_limit:
                    state.timed_out = True
                    return
                next_occupancy = merge_intervals(
                    [*occupied, HalfOpenInterval(candidate.starts_at, candidate.ends_at)]
                )
                if task.splittable:
                    for rest, final_occupancy in self._enumerate_task_placements(
                        task,
                        remaining - duration,
                        candidate.ends_at,
                        deadline,
                        boundary,
                        next_occupancy,
                        deadline_clock,
                        state,
                    ):
                        yield [candidate, *rest], final_occupancy
                        if state.timed_out:
                            return
                elif duration == remaining:
                    yield [candidate], next_occupancy

    @staticmethod
    def chunk_lengths(task, remaining):
        if not task.splittable:
            return [remaining]
        min_chunk = task.min_chunk_minutes or 1
        max_chunk = min(task.max_chunk_minutes or remaining, remaining)
        if remaining < min_chunk:
            return [remaining]
        return list(range(max_chunk, min_chunk - 1, -1))

    @staticmethod
    def candidate_blocks(task, duration, earliest, deadline, boundary, occupied):
        latest_end = min(deadline, task.actual_cutoff.at or deadline)
        cursor = earliest
        while cursor + timedelta(minutes=duration) <= latest_end:
            end = cursor + timedelta(minutes=duration)
            if boundary is CutoffBoundary.EXCLUSIVE and end >= deadline:
                cursor += _MINUTE
                continue
            interval = HalfOpenInterval(cursor, end)
            if not any(interval.overlaps(existing) for existing in occupied):
                yield WorkPlacement(cursor, end, task.obligation.id)
            cursor += _MINUTE


def merge_intervals(intervals):
    if not intervals:
        return []
    items = sorted(intervals, key=lambda interval: (interval.starts_at, interval.ends_at))
    merged = [items[0]]
    for current in items[1:]:
        last = merged[-1]
        if current.starts_at <= last.ends_at:
            if current.ends_at > last.ends_at:
                merged[-1] = HalfOpenInterval(last.starts_at, current.ends_at)
        else:
            merged.append(current)
    return merged
