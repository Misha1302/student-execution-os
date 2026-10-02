"""Daily intent, reflection calibration and canonical time constraints."""
from __future__ import annotations

from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import UserTimeConstraintType
from student_execution_os.persistence.sqlite import _iso
from student_execution_os.reflection import SQLiteReflectionStore

from ..primitives import APPLIED, NOOP, _ID, Outcome, parse_instant, _description

from .base import CommandHandler, Handler


class PlanningCommandHandler(CommandHandler):
    """Daily intent, reflection calibration and canonical time constraints."""

    def operations(self) -> dict[str, Handler]:
        return {
            "constraint.create": self.constraint_create,
            "constraint.update": self.constraint_update,
            "constraint.delete": self.constraint_delete,
            "preference.create": self.preference_create,
            "preference.delete": self.preference_delete,
            "intent.set": self.intent_set,
            "intent.close": self.intent_close,
            "calibration.set": self.calibration_set,
        }

    @staticmethod
    def _constraint_out(constraint) -> dict[str, Any]:
        return {
            "id": constraint.id,
            "type": constraint.type.value,
            "starts_at": _iso(constraint.interval.starts_at),
            "ends_at": _iso(constraint.interval.ends_at),
            "obligation_id": constraint.obligation_id,
            "reason": constraint.reason,
            "version": constraint.version,
            "ownership": "CANONICAL",
        }

    def intent_set(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        allowed = {"local_date", "priority_task_ids", "note", "expected_version"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("daily intent fields are not supported: " + ", ".join(sorted(unknown)))
        local_date = str(payload.get("local_date") or "")
        raw = payload.get("priority_task_ids") or []
        if not isinstance(raw, list):
            raise ValidationError("priority_task_ids must be a list")
        item = SQLiteReflectionStore(self.repo).set_intent(
            self.account_id, local_date, [str(value) for value in raw],
            _description(payload.get("note")), self.actor,
            expected_version=(int(payload["expected_version"]) if payload.get("expected_version") is not None else None),
        )
        return Outcome(APPLIED, item)

    def intent_close(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        if set(payload) - {"local_date", "expected_version"}:
            raise ValidationError("intent.close only accepts local_date and expected_version")
        item = SQLiteReflectionStore(self.repo).close_intent(
            self.account_id, str(payload.get("local_date") or ""), self.actor,
            expected_version=(int(payload["expected_version"]) if payload.get("expected_version") is not None else None),
        )
        return Outcome(APPLIED, item)

    def calibration_set(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        allowed = {"category", "safety_multiplier", "enabled", "suppress_suggestion", "expected_version"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("calibration fields are not supported: " + ", ".join(sorted(unknown)))
        item = SQLiteReflectionStore(self.repo).set_calibration(
            self.account_id,
            str(payload.get("category") or ""),
            float(payload.get("safety_multiplier") or 1.0),
            bool(payload.get("enabled", True)),
            bool(payload.get("suppress_suggestion", False)),
            self.actor,
            expected_version=(int(payload["expected_version"]) if payload.get("expected_version") is not None else None),
        )
        return Outcome(APPLIED, item)

    def constraint_create(self, constraint_id: str, payload: dict[str, Any]) -> Outcome:
        if not _ID.match(constraint_id):
            raise ValidationError("constraint id must be a client-generated identifier (8-128 safe characters)")
        allowed = {"type", "starts_at", "ends_at", "task_id", "reason"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("constraint fields are not supported: " + ", ".join(sorted(unknown)))
        starts_at = parse_instant(payload.get("starts_at"), "starts_at")
        ends_at = parse_instant(payload.get("ends_at"), "ends_at")
        if starts_at is None or ends_at is None:
            raise ValidationError("constraint needs starts_at and ends_at")
        type_ = UserTimeConstraintType(str(payload.get("type") or ""))
        task_id = str(payload.get("task_id") or "") or None
        if type_ is UserTimeConstraintType.PINNED_WORK and not task_id:
            raise ValidationError("PINNED_WORK requires task_id")
        existing = self.repo.connection.execute(
            "SELECT account_id FROM user_time_constraints WHERE id=?", (constraint_id,)
        ).fetchone()
        if existing is not None:
            if existing["account_id"] != self.account_id:
                raise ValidationError("constraint id is already in use")
            return Outcome(NOOP, self._constraint_out(self.repo.get_time_constraint(self.account_id, constraint_id)), "ALREADY_EXISTS")
        constraint = self.repo.create_time_constraint(
            account_id=self.account_id,
            type=type_,
            starts_at=starts_at,
            ends_at=ends_at,
            obligation_id=task_id,
            reason=_description(payload.get("reason")),
            constraint_id=constraint_id,
            actor=self.actor,
        )
        return Outcome(APPLIED, self._constraint_out(constraint))

    def constraint_update(self, constraint_id: str, payload: dict[str, Any]) -> Outcome:
        allowed = {"starts_at", "ends_at", "reason", "expected_version"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValidationError("constraint update fields are not supported: " + ", ".join(sorted(unknown)))
        current = self.repo.get_time_constraint(self.account_id, constraint_id)
        expected = int(payload.get("expected_version") or current.version)
        starts_at = parse_instant(payload.get("starts_at"), "starts_at") or current.interval.starts_at
        ends_at = parse_instant(payload.get("ends_at"), "ends_at") or current.interval.ends_at
        if "starts_at" in payload and "ends_at" not in payload:
            ends_at = starts_at + (current.interval.ends_at - current.interval.starts_at)
        reason = payload["reason"] if "reason" in payload else current.reason
        updated = self.repo.update_time_constraint(
            account_id=self.account_id,
            constraint_id=constraint_id,
            expected_version=expected,
            starts_at=starts_at,
            ends_at=ends_at,
            reason=reason,
            actor=self.actor,
        )
        return Outcome(APPLIED, self._constraint_out(updated))

    def constraint_delete(self, constraint_id: str, payload: dict[str, Any]) -> Outcome:
        unknown = set(payload) - {"expected_version"}
        if unknown:
            raise ValidationError("constraint.delete only accepts expected_version")
        current = self.repo.get_time_constraint(self.account_id, constraint_id)
        expected = int(payload.get("expected_version") or current.version)
        self.repo.delete_time_constraint(
            account_id=self.account_id,
            constraint_id=constraint_id,
            expected_version=expected,
            actor=self.actor,
        )
        return Outcome(APPLIED, {"kind": "USER_TIME_CONSTRAINT", "id": constraint_id, "deleted": True})

    def _local_today(self):
        from zoneinfo import ZoneInfo
        from student_execution_os.planning.outlook import SQLitePlanningProfileRepository
        zone = ZoneInfo(SQLitePlanningProfileRepository(self.repo).get(self.account_id).timezone_name)
        return self.now.astimezone(zone).date()

    def preference_create(self, preference_id: str, payload: dict[str, Any]) -> Outcome:
        from student_execution_os.planning.preference_store import SQLitePlanningPreferenceRepository
        from student_execution_os.planning.preferences import preference_from_payload
        if not _ID.match(preference_id):
            raise ValidationError("preference id must be a client-generated identifier (8-128 safe characters)")
        store = SQLitePlanningPreferenceRepository(self.repo)
        owner = store.owner_of(preference_id)
        if owner is not None:
            if owner != self.account_id:
                raise ValidationError("preference id is already in use")
            return Outcome(NOOP, store.get(self.account_id, preference_id).payload(), "ALREADY_EXISTS")
        preference = preference_from_payload(preference_id, self.account_id, payload)
        created = store.create(preference, today=self._local_today(), actor=self.actor)
        return Outcome(APPLIED, created.payload())

    def preference_delete(self, preference_id: str, payload: dict[str, Any]) -> Outcome:
        from student_execution_os.planning.preference_store import SQLitePlanningPreferenceRepository
        if set(payload) - {"expected_version"}:
            raise ValidationError("preference.delete only accepts expected_version")
        store = SQLitePlanningPreferenceRepository(self.repo)
        current = store.get(self.account_id, preference_id)
        expected = int(payload.get("expected_version") or current.version)
        store.delete(self.account_id, preference_id, expected_version=expected, actor=self.actor)
        return Outcome(APPLIED, {"kind": "PLANNING_PREFERENCE", "id": preference_id, "deleted": True})
