"""Actual work sessions (start, pause, resume, finish, cancel)."""
from __future__ import annotations

from typing import Any

from student_execution_os.domain.errors import ValidationError, VersionConflict

from ..primitives import APPLIED, NOOP, CONFLICT, OPEN, _ID, Outcome, _minutes

from .base import CommandHandler, Handler


class ExecutionCommandHandler(CommandHandler):
    """Actual work sessions (start, pause, resume, finish, cancel)."""

    def operations(self) -> dict[str, Handler]:
        return {
            "execution.start": self.execution_start,
            "execution.pause": self.execution_pause,
            "execution.resume": self.execution_resume,
            "execution.finish": self.execution_finish,
            "execution.cancel": self.execution_cancel,
        }

    def execution_start(self, session_id: str, payload: dict[str, Any]) -> Outcome:
        task_id = str(payload.get("task_id") or "")
        if not _ID.match(session_id) or not _ID.match(task_id):
            raise ValidationError("execution start requires safe session_id and task_id")
        allowed = {"task_id", "planning_snapshot_id", "source_plan_block_id", "occurred_at"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("execution start fields are not supported: " + ", ".join(sorted(unknown)))
        active = self._execution().active(self.account_id, self.now)
        if active is not None and active["id"] != session_id:
            return Outcome(
                CONFLICT, active, "EXECUTION_ACTIVE",
                f"already working on {active.get('task_title') or active['task_id']}",
            )
        occurred_at = self._execution_moment(payload)
        entity, changed = self._execution().start(
            self.account_id, session_id, task_id, occurred_at, self.actor,
            planning_snapshot_id=str(payload.get("planning_snapshot_id") or "") or None,
            source_plan_block_id=str(payload.get("source_plan_block_id") or "") or None,
        )
        if changed:
            task = self._task(task_id)
            fields = {"last_progress_at": occurred_at}
            if task.started_at is None:
                fields["started_at"] = occurred_at
            self._update(task_id, **fields)
            self._touch(task_id)
        return Outcome(APPLIED if changed else NOOP, entity, None if changed else "ALREADY_STARTED")

    def execution_pause(self, session_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"occurred_at"}:
            raise ValidationError("execution.pause only accepts occurred_at")
        entity, changed = self._execution().pause(
            self.account_id, session_id, self._execution_moment(payload), self.actor
        )
        return Outcome(APPLIED if changed else NOOP, entity, None if changed else "ALREADY_PAUSED")

    def execution_resume(self, session_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"occurred_at"}:
            raise ValidationError("execution.resume only accepts occurred_at")
        entity, changed = self._execution().resume(
            self.account_id, session_id, self._execution_moment(payload), self.actor
        )
        return Outcome(APPLIED if changed else NOOP, entity, None if changed else "ALREADY_ACTIVE")

    def execution_finish(self, session_id: str, payload: dict[str, Any]) -> Outcome:
        allowed = {"outcome", "remaining_effort_minutes", "task_id", "occurred_at"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("execution finish fields are not supported: " + ", ".join(sorted(unknown)))
        outcome = str(payload.get("outcome") or "KEEP_REMAINING")
        if outcome not in {"KEEP_REMAINING", "CONTINUE_LATER", "UPDATE_REMAINING", "COMPLETE"}:
            raise ValidationError("invalid execution finish outcome")
        before = self._execution().payload(self.account_id, session_id, self.now)
        task_id = str(before["task_id"])
        supplied_task_id = str(payload.get("task_id") or "")
        if supplied_task_id and supplied_task_id != task_id:
            raise ValidationError("execution finish task_id does not match the session")
        task = self._task(task_id)
        if outcome in {"UPDATE_REMAINING", "COMPLETE"} and task.obligation.lifecycle_status not in OPEN:
            if outcome == "COMPLETE" and task.obligation.completed_at is not None:
                pass
            else:
                raise VersionConflict("task changed while the execution session was active")
        remaining = None
        if outcome == "UPDATE_REMAINING":
            remaining = _minutes(payload.get("remaining_effort_minutes"), "remaining_effort_minutes", allow_zero=True)
        occurred_at = self._execution_moment(payload)
        entity, changed = self._execution().finish(
            self.account_id, session_id, occurred_at, self.actor
        )
        if outcome == "COMPLETE":
            current = self._task(task_id).obligation
            if current.completed_at is None:
                self._transition(task_id, "complete")
            self._touch(task_id)
        elif self._task(task_id).obligation.lifecycle_status in OPEN:
            fields: dict[str, Any] = {"last_progress_at": occurred_at}
            if outcome == "UPDATE_REMAINING":
                fields["remaining_effort_minutes"] = remaining
                fields["remaining_effort_low_minutes"] = None
                fields["remaining_effort_high_minutes"] = None
                current = self._task(task_id)
                if current.estimated_total_effort_minutes is None:
                    if remaining == 0:
                        raise ValidationError("complete the task instead of setting unknown-effort draft remaining to zero")
                    fields["estimated_total_effort_minutes"] = remaining
                elif remaining is not None and remaining > current.estimated_total_effort_minutes:
                    fields["estimated_total_effort_minutes"] = remaining
            self._update(task_id, **fields)
            self._touch(task_id)
        return Outcome(APPLIED if changed else NOOP, entity, None if changed else "ALREADY_FINISHED")

    def execution_cancel(self, session_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"occurred_at"}:
            raise ValidationError("execution.cancel only accepts occurred_at")
        entity, changed = self._execution().cancel(
            self.account_id, session_id, self._execution_moment(payload), self.actor
        )
        return Outcome(APPLIED if changed else NOOP, entity, None if changed else "ALREADY_CANCELLED")
