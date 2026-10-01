"""Recurring work routines and their occurrences."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import Importance, ObligationCategory
from student_execution_os.persistence.sqlite import _iso
from student_execution_os.work_routines import SQLiteWorkRoutineRepository

from ..serialize import routine_occurrence_payload, routine_payload
from ..primitives import APPLIED, NOOP, _ID, Outcome, _minutes, _title, _description

from .base import CommandHandler, Handler


class RoutineCommandHandler(CommandHandler):
    """Recurring work routines and their occurrences."""

    def operations(self) -> dict[str, Handler]:
        return {
            "routine.create": self.routine_create,
            "routine.cancel": self.routine_cancel,
            "routine.split": self.routine_split,
            "routine.occurrence.skip": self.routine_occurrence_skip,
            "routine.occurrence.reopen": self.routine_occurrence_reopen,
            "routine.occurrence.edit": self.routine_occurrence_edit,
        }

    # The JSON views are shared with the work-routines read model (sync/serialize.py).
    _routine_out = staticmethod(routine_payload)
    _routine_occurrence_out = staticmethod(routine_occurrence_payload)

    def routine_create(self, routine_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(routine_id):
            raise ValidationError("routine id must be a client-generated identifier")
        allowed = {
            "title", "description", "category", "importance", "dtstart_local",
            "effort_minutes", "recurrence_rule", "timezone_name", "splittable",
            "min_chunk_minutes", "max_chunk_minutes",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("routine fields are not supported: " + ", ".join(sorted(unknown)))
        store = SQLiteWorkRoutineRepository(self.repo)
        existing = self.repo.connection.execute(
            "SELECT account_id FROM work_routine_templates WHERE id=?", (routine_id,)
        ).fetchone()
        if existing is not None:
            if existing["account_id"] != self.account_id:
                raise ValidationError("routine id is already in use")
            return Outcome(NOOP, self._routine_out(store.get_template(self.account_id, routine_id)), "ALREADY_EXISTS")
        template = store.create_template(
            account_id=self.account_id,
            template_id=routine_id,
            title=_title(payload.get("title")),
            description=_description(payload.get("description")),
            category=ObligationCategory(payload.get("category") or ObligationCategory.GENERAL.value),
            importance=Importance(payload.get("importance") or Importance.NORMAL.value),
            dtstart_local=self._local_instant(payload.get("dtstart_local"), "dtstart_local"),
            effort_minutes=_minutes(payload.get("effort_minutes"), "effort_minutes") or 0,
            recurrence_rule=str(payload.get("recurrence_rule") or ""),
            timezone_name=str(payload.get("timezone_name") or "UTC"),
            splittable=bool(payload.get("splittable", True)),
            min_chunk_minutes=_minutes(payload.get("min_chunk_minutes"), "min_chunk_minutes", allow_none=True),
            max_chunk_minutes=_minutes(payload.get("max_chunk_minutes"), "max_chunk_minutes", allow_none=True),
            actor=self.actor,
        )
        store.ensure_horizon(self.account_id, self.now, self.now + timedelta(days=28))
        return Outcome(APPLIED, self._routine_out(template))

    def routine_cancel(self, routine_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"expected_version"}:
            raise ValidationError("routine.cancel only accepts expected_version")
        store = SQLiteWorkRoutineRepository(self.repo)
        current = store.get_template(self.account_id, routine_id)
        updated = store.cancel_template(
            self.account_id, routine_id,
            int(payload.get("expected_version") or current.version), self.actor,
        )
        return Outcome(APPLIED if updated.version != current.version else NOOP, self._routine_out(updated))

    def routine_split(self, successor_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(successor_id):
            raise ValidationError("successor routine id must be a client-generated identifier")
        allowed = {
            "template_id", "original_recurrence_id", "title", "effort_minutes",
            "target_local", "recurrence_rule", "timezone_name", "expected_version",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("routine split fields are not supported: " + ", ".join(sorted(unknown)))
        template_id = str(payload.get("template_id") or "")
        original = str(payload.get("original_recurrence_id") or "")
        if not template_id or not original:
            raise ValidationError("routine split requires template_id and original_recurrence_id")
        effort = _minutes(payload.get("effort_minutes"), "effort_minutes", allow_none=True)
        target_local = self._local_instant(payload["target_local"], "target_local") if payload.get("target_local") else None
        before, successor = SQLiteWorkRoutineRepository(self.repo).split_this_and_future(
            account_id=self.account_id,
            template_id=template_id,
            original_recurrence_id=original,
            successor_id=successor_id,
            actor=self.actor,
            title=_title(payload["title"]) if "title" in payload else None,
            effort_minutes=effort,
            replacement_start_local=target_local,
            recurrence_rule=str(payload["recurrence_rule"]) if payload.get("recurrence_rule") else None,
            timezone_name=str(payload["timezone_name"]) if payload.get("timezone_name") else None,
            expected_version=(int(payload["expected_version"]) if payload.get("expected_version") is not None else None),
        )
        SQLiteWorkRoutineRepository(self.repo).ensure_horizon(
            self.account_id, self.now, self.now + timedelta(days=28)
        )
        return Outcome(APPLIED, {
            "previous": self._routine_out(before),
            "successor": self._routine_out(successor),
        })

    def _routine_occurrence_identity(self, payload: dict[str, Any]) -> tuple[str, str, str | None]:
        template_id = str(payload.get("template_id") or "")
        original = str(payload.get("original_recurrence_id") or "")
        task_id = str(payload.get("task_id") or "") or None
        if not template_id or not original:
            raise ValidationError("routine occurrence requires template_id and original_recurrence_id")
        return template_id, original, task_id

    @staticmethod
    def _verify_routine_task_ref(item, task_id: str | None) -> None:
        if task_id is not None and task_id != item.task_id:
            raise ValidationError("routine occurrence task_id does not match stable occurrence identity")

    def routine_occurrence_skip(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"template_id", "original_recurrence_id", "task_id"}:
            raise ValidationError("routine occurrence skip fields are not supported")
        template_id, original, task_id = self._routine_occurrence_identity(payload)
        store = SQLiteWorkRoutineRepository(self.repo)
        before = store.get_occurrence(self.account_id, template_id, original)
        item = store.skip_occurrence(self.account_id, template_id, original, self.actor)
        self._verify_routine_task_ref(item, task_id)
        return Outcome(NOOP if before is not None and before.state == "SKIPPED" else APPLIED, self._routine_occurrence_out(item))

    def routine_occurrence_reopen(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"template_id", "original_recurrence_id", "task_id"}:
            raise ValidationError("routine occurrence reopen fields are not supported")
        template_id, original, task_id = self._routine_occurrence_identity(payload)
        store = SQLiteWorkRoutineRepository(self.repo)
        before = store.get_occurrence(self.account_id, template_id, original)
        item = store.reopen_occurrence(self.account_id, template_id, original, self.actor)
        self._verify_routine_task_ref(item, task_id)
        return Outcome(NOOP if before is not None and before.state == "ACTIVE" else APPLIED, self._routine_occurrence_out(item))

    def routine_occurrence_edit(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        allowed = {"template_id", "original_recurrence_id", "task_id", "title", "effort_minutes", "target_local"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("routine occurrence edit fields are not supported: " + ", ".join(sorted(unknown)))
        template_id, original, task_id = self._routine_occurrence_identity(payload)
        effort = _minutes(payload.get("effort_minutes"), "effort_minutes", allow_none=True)
        target = self._local_instant(payload["target_local"], "target_local") if payload.get("target_local") else None
        item = SQLiteWorkRoutineRepository(self.repo).edit_occurrence(
            self.account_id, template_id, original, actor=self.actor,
            title=_title(payload["title"]) if "title" in payload else None,
            effort_minutes=effort, target_local=target,
        )
        self._verify_routine_task_ref(item, task_id)
        return Outcome(APPLIED, self._routine_occurrence_out(item))
