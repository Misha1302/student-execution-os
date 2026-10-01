"""Recurring work routines."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from student_execution_os.sync.serialize import routine_occurrence_payload, routine_payload
from student_execution_os.work_routines import SQLiteWorkRoutineRepository

from .common import _jsonify

from .base import ApplicationService


class RoutineQueries(ApplicationService):
    """Recurring work routines."""

    def work_routines(self) -> dict[str, Any]:
        with self._repo() as repo:
            store = SQLiteWorkRoutineRepository(repo)
            now = self._now()
            store.ensure_horizon(self.account_id, now, now + timedelta(days=28))
            templates = []
            for template in store.list_templates(self.account_id):
                occurrences = []
                for occurrence in store.list_occurrences(self.account_id, template.id):
                    task = repo.get_task(self.account_id, occurrence.task_id)
                    if task.target_at is not None and task.target_at < now - timedelta(days=7):
                        continue
                    occurrences.append({
                        **routine_occurrence_payload(occurrence),
                        "title": task.obligation.title,
                        "target_at": _jsonify(task.target_at),
                        "effort_minutes": task.estimated_total_effort_minutes,
                        "remaining_effort_minutes": task.remaining_effort_minutes,
                        "task_status": task.obligation.lifecycle_status.value,
                        "task_version": task.obligation.version,
                    })
                occurrences.sort(key=lambda item: (item["target_at"] or "", item["original_recurrence_id"]))
                templates.append({
                    **routine_payload(template),
                    "occurrences": occurrences,
                })
            return {"now": _jsonify(now), "routines": templates}
