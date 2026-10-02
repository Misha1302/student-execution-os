"""Task lifecycle, effort, progress and deadlines."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import Importance, LifecycleStatus, ObligationCategory
from student_execution_os.persistence import extras

from ..serialize import task_payload

from ..primitives import APPLIED, NOOP, CONFLICT, REJECTED, OPEN, _ID, Outcome, parse_instant, parse_cutoff, _minutes, _title, _description

from .base import CommandHandler, Handler


class TaskCommandHandler(CommandHandler):
    """Task lifecycle, effort, progress and deadlines."""

    def operations(self) -> dict[str, Handler]:
        return {
            "task.create": self.task_create,
            "task.update": self.task_update,
            "task.activate": self.task_activate,
            "task.progress": self.task_progress,
            "task.start": self.task_start,
            "task.complete": self.task_complete,
            "task.cancel": self.task_cancel,
            "task.reopen": self.task_reopen,
            "task.defer": self.task_defer,
            "task.archive": self.task_archive,
            "task.unarchive": self.task_unarchive,
            "task.restore": self.task_restore,
            "task.delete": self.task_delete,
        }

    def _count(self, payload: dict[str, Any], current: dict[str, Any] | None) -> tuple[bool, dict[str, Any] | None]:
        """Counted progress from create/update payload fields; (changed, new value)."""
        if not any(key in payload for key in ("count_total", "count_done", "count_unit")):
            return False, current
        total = current["total"] if current else None
        if "count_total" in payload:
            raw = payload["count_total"]
            total = None if raw in (None, "", 0) else _minutes(raw, "count_total")
        if total is None:
            return True, None
        done = current["done"] if current else 0
        if "count_done" in payload:
            done = _minutes(payload["count_done"], "count_done", allow_zero=True)
        unit = extras.clean_unit(payload["count_unit"]) if "count_unit" in payload else (current or {}).get("unit")
        return True, {"total": total, "done": min(done, total), "unit": unit}

    def _save_count(self, task_id: str, value: dict[str, Any] | None) -> None:
        extras.set_progress_count(self.repo, self.account_id, task_id, total=None if value is None else value["total"],
                                  done=0 if value is None else value["done"], unit=None if value is None else value["unit"],
                                  now=self.now)

    def _remind_at(self, payload: dict[str, Any]) -> datetime | None:
        remind = parse_instant(payload.get("remind_at"), "remind_at")
        if remind is not None and remind <= self.now:
            raise ValidationError("remind_at must be in the future")
        return remind

    def task_create(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(task_id):
            raise ValidationError("task id must be a client-generated identifier (8-128 safe characters)")
        exists = self.repo.connection.execute(
            "SELECT account_id FROM obligations WHERE id=?", (task_id,)
        ).fetchone()
        if exists is not None:
            if exists["account_id"] != self.account_id:
                raise ValidationError("task id is already in use")
            return Outcome(NOOP, task_payload(self._task(task_id)), "ALREADY_EXISTS", "task already exists")
        effort = _minutes(payload.get("estimated_total_effort_minutes"), "estimated_total_effort_minutes", allow_none=True)
        provisional = payload.get("provisional_effort", "estimated_total_effort_minutes" not in payload)
        if not isinstance(provisional, bool):
            raise ValidationError("provisional_effort must be a boolean")
        low = high = None
        if effort is None and provisional:
            effort, low, high = 30, 15, 60
        splittable = bool(payload.get("splittable", False))
        remind = self._remind_at(payload)
        task = self.repo.create_task(
            account_id=self.account_id, obligation_id=task_id,
            title=_title(payload.get("title")), description=_description(payload.get("description")),
            category=ObligationCategory(payload.get("category") or ObligationCategory.GENERAL.value),
            importance=Importance(payload.get("importance") or Importance.NORMAL.value),
            estimated_total_effort_minutes=effort, remaining_effort_minutes=effort,
            estimated_total_effort_low_minutes=low, estimated_total_effort_high_minutes=high,
            remaining_effort_low_minutes=low, remaining_effort_high_minutes=high,
            effort_estimate_source='SYSTEM_PROVISIONAL' if low is not None else None,
            splittable=splittable,
            min_chunk_minutes=_minutes(payload.get("min_chunk_minutes"), "min_chunk_minutes", allow_none=True) if splittable else None,
            max_chunk_minutes=_minutes(payload.get("max_chunk_minutes"), "max_chunk_minutes", allow_none=True) if splittable else None,
            actionable_from=parse_instant(payload.get("actionable_from"), "actionable_from"),
            target_at=parse_instant(payload.get("target_at"), "target_at"),
            actual_cutoff=parse_cutoff(payload.get("actual_cutoff", {"state": "ABSENT"} if low is not None else None)),
            actor=self._capture_actor(payload),
        )
        changed, count = self._count(payload, None)
        if changed and count is not None:
            self._save_count(task_id, count)
        self._touch(task_id, remind_at=remind)
        return self._task_out(task.obligation.id)

    _EDITABLE = {
        "title", "description", "importance", "category", "estimated_total_effort_minutes",
        "remaining_effort_minutes", "actual_cutoff", "target_at", "actionable_from", "splittable",
        "min_chunk_minutes", "max_chunk_minutes", "remind_at", "count_total", "count_done", "count_unit",
    }

    def task_update(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        unknown = set(payload) - self._EDITABLE
        if unknown:
            raise ValidationError("fields cannot be edited: " + ", ".join(sorted(unknown)))
        current = self._task(task_id)
        fields: dict[str, Any] = {}
        if "title" in payload:
            fields["title"] = _title(payload["title"])
        if "description" in payload:
            fields["description"] = _description(payload["description"])
        if "importance" in payload:
            fields["importance"] = Importance(payload["importance"])
        if "category" in payload:
            fields["category"] = ObligationCategory(payload["category"])
        if "actual_cutoff" in payload:
            fields["actual_cutoff"] = parse_cutoff(payload["actual_cutoff"])
        for key in ("target_at", "actionable_from"):
            if key in payload:
                fields[key] = parse_instant(payload[key], key)
        if "splittable" in payload:
            fields["splittable"] = bool(payload["splittable"])
        for key in ("min_chunk_minutes", "max_chunk_minutes"):
            if key in payload:
                fields[key] = _minutes(payload[key], key, allow_none=True)
        estimate = current.estimated_total_effort_minutes
        if "estimated_total_effort_minutes" in payload:
            estimate = _minutes(payload["estimated_total_effort_minutes"], "estimated_total_effort_minutes",
                                allow_none=current.obligation.lifecycle_status is LifecycleStatus.DRAFT)
            fields["estimated_total_effort_minutes"] = estimate
            fields["remaining_effort_low_minutes"] = None
            fields["remaining_effort_high_minutes"] = None
            if estimate is None:
                fields["remaining_effort_minutes"] = None
        if "remaining_effort_minutes" in payload and estimate is not None:
            fields["remaining_effort_minutes"] = _minutes(payload["remaining_effort_minutes"], "remaining_effort_minutes", allow_zero=True)
        if estimate is not None and "remaining_effort_minutes" not in fields:
            remaining = current.remaining_effort_minutes
            if remaining is None or remaining > estimate:
                fields["remaining_effort_minutes"] = estimate
        remind_changed = "remind_at" in payload
        if remind_changed:
            from student_execution_os.reminders.store import ReminderStore
            ReminderStore(self.repo).set_remind_at(self.account_id, task_id, self._remind_at(payload), self.now)
        count_changed, count = self._count(payload, extras.progress_counts_for(self.repo, self.account_id, [task_id]).get(task_id))
        if count_changed:
            self._save_count(task_id, count)
            remind_changed = True
        if not fields:
            return self._task_out(task_id, APPLIED if remind_changed else NOOP, None if remind_changed else "NOTHING_TO_CHANGE")
        activate = current.obligation.lifecycle_status is LifecycleStatus.DRAFT and estimate is not None
        self.repo.update_task(account_id=self.account_id, obligation_id=task_id,
                              expected_version=current.obligation.version, actor=self.actor, activate=activate, **fields)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_progress(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        """Worked time ("minutes", a delta) and/or counted progress ("count", a delta)."""
        count_delta = _minutes(payload.get("count"), "count", allow_none=True)
        minutes = _minutes(payload.get("minutes"), "minutes", allow_none=count_delta is not None) or 0
        current = self._task(task_id)
        if current.obligation.lifecycle_status not in OPEN:
            return self._task_out(task_id, NOOP, "TASK_CLOSED", "task is already closed; progress not recorded")
        fields: dict[str, Any] = {"last_progress_at": self.now}
        if current.started_at is None:
            fields["started_at"] = self.now
        count = extras.progress_counts_for(self.repo, self.account_id, [task_id]).get(task_id)
        if count_delta is not None:
            if count is None:
                return self._task_out(task_id, REJECTED, "NO_COUNT", "the task has no counted progress")
            count = {**count, "done": min(count["total"], count["done"] + count_delta)}
            self._save_count(task_id, count)
            if not minutes and current.estimated_total_effort_minutes and current.remaining_effort_minutes is not None:
                # Nothing about time was said: the share of units left is the best estimate.
                left = -(-current.estimated_total_effort_minutes * (count["total"] - count["done"]) // count["total"])
                fields["remaining_effort_minutes"] = min(current.remaining_effort_minutes, left)
        if minutes and current.remaining_effort_minutes is not None:
            fields["remaining_effort_minutes"] = max(0, current.remaining_effort_minutes - minutes)
            if count is not None and count["done"] < count["total"]:
                # Units are still left: time alone does not finish the task.
                fields["remaining_effort_minutes"] = max(fields["remaining_effort_minutes"], 5)
            if current.remaining_effort_high_minutes is not None:
                fields["remaining_effort_high_minutes"] = max(fields["remaining_effort_minutes"], current.remaining_effort_high_minutes - minutes)
            if current.remaining_effort_low_minutes is not None:
                fields["remaining_effort_low_minutes"] = min(fields["remaining_effort_minutes"], max(0, current.remaining_effort_low_minutes - minutes))
        self._update(task_id, **fields)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_activate(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._task(task_id)
        if current.obligation.lifecycle_status is LifecycleStatus.ACTIVE:
            return self._task_out(task_id, NOOP, "ALREADY_ACTIVE")
        if current.obligation.lifecycle_status is not LifecycleStatus.DRAFT:
            return self._task_out(task_id, CONFLICT, "TASK_CLOSED")
        if current.estimated_total_effort_minutes is None:
            return self._task_out(task_id, REJECTED, "EFFORT_REQUIRED", "refine effort before activation")
        self.repo.activate_task(account_id=self.account_id, obligation_id=task_id,
                                expected_version=current.obligation.version, actor=self.actor)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_start(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._task(task_id)
        if current.obligation.lifecycle_status not in OPEN:
            if current.obligation.completed_at is not None:
                # Completion subsumes a delayed "I started": nothing is left to apply
                # and nothing should be offered for a retry.
                return self._task_out(task_id, NOOP, "SUPERSEDED")
            # Starting work that was cancelled elsewhere is a real disagreement.
            return self._task_out(task_id, CONFLICT, "TASK_CANCELLED", "task was cancelled; reopen it first")
        if current.started_at is not None:
            self._touch(task_id)
            return self._task_out(task_id, NOOP, "ALREADY_STARTED")
        self._update(task_id, started_at=self.now, last_progress_at=self.now)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_complete(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"occurred_at"}:
            raise ValidationError("task.complete only accepts occurred_at")
        current = self._task(task_id).obligation
        # An archived task keeps completed_at: archived-after-done is still done.
        if current.completed_at is not None:
            return self._task_out(task_id, NOOP, "ALREADY_COMPLETED")
        if current.lifecycle_status not in OPEN:
            return self._task_out(task_id, CONFLICT, "TASK_CANCELLED", "task was cancelled; reopen it first")
        execution_time = self._execution_moment(payload)
        self._execution().finish_active_for_task(self.account_id, task_id, execution_time, self.actor)
        self._transition(task_id, "complete")
        self._touch(task_id)
        return self._task_out(task_id)

    def task_cancel(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"occurred_at"}:
            raise ValidationError("task.cancel only accepts occurred_at")
        current = self._task(task_id).obligation
        if current.completed_at is not None:
            return self._task_out(task_id, CONFLICT, "TASK_COMPLETED", "task was completed; reopen it first")
        if current.lifecycle_status not in OPEN:  # cancelled, or archived after cancelling
            return self._task_out(task_id, NOOP, "ALREADY_CANCELLED")
        occurred_at = self._execution_moment(payload)
        active_execution = self._execution().active(self.account_id, self.now)
        if active_execution is not None and active_execution["task_id"] == task_id:
            self._execution().cancel(self.account_id, active_execution["id"], occurred_at, self.actor)
        self._transition(task_id, "cancel")
        self._touch(task_id)
        return self._task_out(task_id)

    def task_reopen(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._task(task_id)
        if current.obligation.lifecycle_status in OPEN:
            return self._task_out(task_id, NOOP, "ALREADY_OPEN")
        self._transition(task_id, "reopen")
        count = extras.progress_counts_for(self.repo, self.account_id, [task_id]).get(task_id)
        if count is not None and count["done"] >= count["total"] and payload.get("remaining_effort_minutes") is None:
            self._save_count(task_id, {**count, "done": max(0, count["total"] - 1)})
        reopened = self._task(task_id)
        remaining = _minutes(payload.get("remaining_effort_minutes"), "remaining_effort_minutes", allow_none=True)
        if reopened.estimated_total_effort_minutes is not None:
            if remaining is None and not reopened.remaining_effort_minutes:
                # "Not done after all" with nothing left on record: plan a modest block.
                remaining = min(reopened.estimated_total_effort_minutes, 30)
                if not reopened.splittable and reopened.min_chunk_minutes:
                    remaining = max(remaining, reopened.min_chunk_minutes)
            if remaining is not None:
                fields: dict[str, Any] = {"remaining_effort_minutes": remaining,
                                          "remaining_effort_low_minutes": None, "remaining_effort_high_minutes": None}
                if remaining > reopened.estimated_total_effort_minutes:
                    fields["estimated_total_effort_minutes"] = remaining
                self._update(task_id, **fields)
        self._touch(task_id)
        return self._task_out(task_id)

    def task_archive(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"occurred_at"}:
            raise ValidationError("task.archive only accepts occurred_at")
        status = self._task(task_id).obligation.lifecycle_status
        if status is LifecycleStatus.ARCHIVED:
            return self._task_out(task_id, NOOP, "ALREADY_ARCHIVED")
        if status in OPEN:
            # Put away something still open: stop actual execution before closing it.
            occurred_at = self._execution_moment(payload)
            active_execution = self._execution().active(self.account_id, self.now)
            if active_execution is not None and active_execution["task_id"] == task_id:
                self._execution().cancel(self.account_id, active_execution["id"], occurred_at, self.actor)
            self._transition(task_id, "cancel")
        self._transition(task_id, "archive")
        return self._task_out(task_id)

    def task_unarchive(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        if self._task(task_id).obligation.lifecycle_status is not LifecycleStatus.ARCHIVED:
            return self._task_out(task_id, NOOP, "NOT_ARCHIVED")
        self._transition(task_id, "unarchive")
        return self._task_out(task_id)

    def task_restore(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        """Take a task out of the archive in one step.

        For the user the archive is one place, whether they said «не буду делать»
        (CANCELLED) or put a finished task away (ARCHIVED). Restoring returns a done
        task to «Выполнено» and anything else back to work — never into another
        archived state that would still show under «Архив».
        """
        status = self._task(task_id).obligation.lifecycle_status
        if status in OPEN:
            return self._task_out(task_id, NOOP, "ALREADY_OPEN")
        if status is LifecycleStatus.COMPLETED:
            return self._task_out(task_id, NOOP, "NOT_ARCHIVED")
        if status is LifecycleStatus.ARCHIVED:
            self._transition(task_id, "unarchive")
            if self._task(task_id).obligation.lifecycle_status is LifecycleStatus.COMPLETED:
                return self._task_out(task_id)
        return self.task_reopen(task_id, payload)

    def task_delete(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        current = self._task(task_id)
        self.repo.delete_obligation(account_id=self.account_id, obligation_id=task_id,
                                    expected_version=current.obligation.version, actor=self.actor)
        return Outcome(APPLIED, {"kind": "TASK", "id": task_id, "deleted": True})

    def task_defer(self, task_id: str, payload: dict[str, Any]) -> Outcome:
        until = parse_instant(payload.get("until"), "until")
        if until is None:
            raise ValidationError("until is required")
        current = self._task(task_id)
        if current.obligation.lifecycle_status not in OPEN:
            return self._task_out(task_id, NOOP, "TASK_CLOSED")
        if until <= self.now:
            return self._task_out(task_id, NOOP, "DEFER_EXPIRED", "the postponement time has already passed")
        self._update(task_id, actionable_from=until)
        # "Not now": quiet until then, and come back with a prompt at that moment.
        self._touch(task_id, snooze_until=until, remind_at=until)
        return self._task_out(task_id)
