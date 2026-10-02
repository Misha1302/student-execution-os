"""Tasks, their lifecycle, saved views and attachments."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from student_execution_os.domain.errors import EntityNotFound
from student_execution_os.domain.model import ActorCategory, Importance, ObligationCategory
from student_execution_os.persistence import extras
from student_execution_os.execution import SQLiteExecutionStore
from student_execution_os.persistence.product import SQLiteAttachmentRepository, SQLiteSavedViewRepository
from student_execution_os.reminders import ReminderStore
from student_execution_os.planning import PlanningService, SQLitePlanningStateSource
from student_execution_os.reconciliation import SQLiteReconciliationRepository
from student_execution_os.sync.commands import SyncService

from .common import _dt, _cutoff

from .base import ApplicationService


class TaskService(ApplicationService):
    """Tasks, their lifecycle, saved views and attachments."""

    def tasks(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            source = SQLitePlanningStateSource(repo)
            reconciliation = SQLiteReconciliationRepository(repo)
            try:
                snapshot = self._snapshot(repo, hours=36)
                risks = {r.task_id: r for r in PlanningService().build(snapshot, now=self._now()).risks}
            except Exception:
                risks = {}
            reminders = ReminderStore(repo).pending_reminders(self.account_id)
            counts = extras.progress_counts(repo, self.account_id)
            return [
                self._task(
                    task,
                    risk=risks.get(task.obligation.id),
                    effective=reconciliation.get_effective_cutoff(self.account_id, task.obligation.id),
                    remind_at=reminders.get(task.obligation.id),
                    count=counts.get(task.obligation.id),
                )
                for task in source.list_tasks(self.account_id)
            ]

    def task(self, task_id: str) -> dict[str, Any]:
        """One task in any lifecycle state (completed/cancelled tasks stay openable)."""
        for item in self.tasks():
            if item["id"] == task_id:
                return item
        raise EntityNotFound("task not found")

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_effort = payload.get("estimated_total_effort_minutes")
        effort = None if raw_effort in (None, "") else int(raw_effort)
        raw_remaining = payload.get("remaining_effort_minutes", effort)
        remaining = None if raw_remaining in (None, "") else int(raw_remaining)
        provisional = payload.get("provisional_effort", "estimated_total_effort_minutes" not in payload)
        if not isinstance(provisional, bool):
            raise ValueError("provisional_effort must be a boolean")
        provisional = effort is None and provisional
        if provisional:
            effort = remaining = 30
        with self._repo() as repo:
            task = repo.create_task(
                account_id=self.account_id,
                title=str(payload["title"]),
                description=payload.get("description"),
                category=ObligationCategory(payload.get("category", ObligationCategory.GENERAL.value)),
                importance=Importance(payload.get("importance", Importance.NORMAL.value)),
                estimated_total_effort_minutes=effort,
                estimated_total_effort_low_minutes=15 if provisional else payload.get("estimated_total_effort_low_minutes"),
                estimated_total_effort_high_minutes=60 if provisional else payload.get("estimated_total_effort_high_minutes"),
                remaining_effort_minutes=remaining,
                remaining_effort_low_minutes=15 if provisional else payload.get("remaining_effort_low_minutes"),
                remaining_effort_high_minutes=60 if provisional else payload.get("remaining_effort_high_minutes"),
                effort_estimate_source='SYSTEM_PROVISIONAL' if provisional else None,
                splittable=bool(payload.get("splittable", False)),
                min_chunk_minutes=payload.get("min_chunk_minutes"),
                max_chunk_minutes=payload.get("max_chunk_minutes"),
                actionable_from=_dt(payload.get("actionable_from")),
                target_at=_dt(payload.get("target_at")),
                actual_cutoff=_cutoff(payload.get("actual_cutoff", {"state": "ABSENT"} if provisional else None)),
                actor=ActorCategory.USER_UI,
            )
            ReminderStore(repo).touch(self.account_id, task.obligation.id, self._now())
            return self._task(task)

    def update_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            kwargs: dict[str, Any] = {}
            if "target_at" in payload:
                kwargs["target_at"] = _dt(payload.get("target_at"))
            if "actionable_from" in payload:
                kwargs["actionable_from"] = _dt(payload.get("actionable_from"))
            if "remaining_effort_minutes" in payload:
                kwargs["remaining_effort_minutes"] = int(payload["remaining_effort_minutes"])
            if "estimated_total_effort_minutes" in payload:
                value = payload.get("estimated_total_effort_minutes")
                kwargs["estimated_total_effort_minutes"] = None if value is None else int(value)
            if "splittable" in payload:
                kwargs["splittable"] = bool(payload["splittable"])
            if "min_chunk_minutes" in payload:
                kwargs["min_chunk_minutes"] = payload.get("min_chunk_minutes")
            if "max_chunk_minutes" in payload:
                kwargs["max_chunk_minutes"] = payload.get("max_chunk_minutes")
            if "remaining_effort_low_minutes" in payload:
                kwargs["remaining_effort_low_minutes"] = payload.get("remaining_effort_low_minutes")
            if "remaining_effort_high_minutes" in payload:
                kwargs["remaining_effort_high_minutes"] = payload.get("remaining_effort_high_minutes")
            task = repo.update_task(
                account_id=self.account_id,
                obligation_id=task_id,
                expected_version=int(payload["expected_version"]),
                actor=ActorCategory.USER_UI,
                **kwargs,
            )
            ReminderStore(repo).touch(self.account_id, task_id, self._now())
            return self._task(task)

    def activate_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            task = repo.activate_task(
                account_id=self.account_id,
                obligation_id=task_id,
                expected_version=int(payload["expected_version"]),
                actor=ActorCategory.USER_UI,
            )
            ReminderStore(repo).touch(self.account_id, task_id, self._now())
            return self._task(task)

    def defer_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        until = _dt(payload.get("until"))
        if until is None or until <= self._now():
            raise ValueError("defer until must be a future offset-aware instant")
        with self._repo() as repo:
            task = repo.update_task(
                account_id=self.account_id,
                obligation_id=task_id,
                expected_version=int(payload["expected_version"]),
                actionable_from=until,
                actor=ActorCategory.USER_UI,
            )
            ReminderStore(repo).touch(self.account_id, task_id, self._now(), snooze_until=until)
            return self._task(task)

    def start_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            current = repo.get_task(self.account_id, task_id)
            if current.obligation.version != int(payload["expected_version"]):
                from student_execution_os.domain.errors import VersionConflict
                raise VersionConflict("task version changed")
            result = SyncService(
                repo, account_id=self.account_id, principal_id=self.principal.principal_id, now=self._now()
            ).apply({
                "op_id": str(payload.get("op_id") or f"api-start-{uuid4()}"),
                "type": "task.start", "entity_id": task_id, "payload": {},
            })
            if result["status"] == "CONFLICT":
                raise ValueError(result.get("message") or result.get("code") or "task cannot be started")
            return result["entity"]

    def upload_attachment(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteAttachmentRepository(repo).upload(self.account_id, payload)

    def attachments(self, owner_kind: str, owner_id: str) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return SQLiteAttachmentRepository(repo).list(self.account_id, owner_kind, owner_id)

    def download_attachment(self, attachment_id: str):
        with self._repo() as repo:
            return SQLiteAttachmentRepository(repo).download(self.account_id, attachment_id)

    def unlink_attachment(self, link_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteAttachmentRepository(repo).unlink(
                self.account_id, link_id, int(payload["expected_version"])
            )

    def saved_views(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return SQLiteSavedViewRepository(repo).list(self.account_id)

    def create_saved_view(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteSavedViewRepository(repo).create(self.account_id, payload)

    def update_saved_view(self, view_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteSavedViewRepository(repo).update(self.account_id, view_id, payload)

    def delete_saved_view(self, view_id: str, expected_version: int) -> dict[str, Any]:
        with self._repo() as repo:
            return SQLiteSavedViewRepository(repo).delete(self.account_id, view_id, expected_version)

    def lifecycle(self, obligation_id: str, action: str, expected_version: int) -> dict[str, Any]:
        with self._repo() as repo:
            method = {
                "complete": repo.complete_obligation,
                "cancel": repo.cancel_obligation,
                "reopen": repo.reopen_obligation,
            }.get(action)
            if method is None:
                raise ValueError("unsupported lifecycle action")
            is_task = repo.connection.execute(
                "SELECT 1 FROM tasks t JOIN obligations o ON o.id=t.obligation_id "
                "WHERE o.account_id=? AND t.obligation_id=?",
                (self.account_id, obligation_id),
            ).fetchone() is not None
            # Keep the legacy direct lifecycle API consistent with the offline command
            # boundary: a Task cannot be closed while actual execution keeps running.
            with repo._tx():
                if is_task and action in {"complete", "cancel"}:
                    execution = SQLiteExecutionStore(repo)
                    active = execution.active(self.account_id, self._now())
                    if active is not None and active["task_id"] == obligation_id:
                        if action == "complete":
                            execution.finish(self.account_id, active["id"], self._now(), ActorCategory.USER_UI)
                        else:
                            execution.cancel(self.account_id, active["id"], self._now(), ActorCategory.USER_UI)
                ob = method(
                    account_id=self.account_id,
                    obligation_id=obligation_id,
                    expected_version=expected_version,
                    actor=ActorCategory.USER_UI,
                )
                if is_task:
                    ReminderStore(repo).touch(self.account_id, obligation_id, self._now())
            return {
                "id": ob.id,
                "status": ob.lifecycle_status.value,
                "version": ob.version,
            }
