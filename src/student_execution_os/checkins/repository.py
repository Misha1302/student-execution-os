"""SQLite owner of check-in templates and occurrences (schema v31, ADR 0034).

Identity: an occurrence is ``(template_id, original_recurrence_id)``; the original
recurrence id is the naive local start the rule produced. Moving one occurrence
records ``moved_to_local`` and never changes that identity.

Outcome vs. attention: the occurrence row owns the outcome. A reminder row (one per
occurrence, id derived from the identity) is only the prompt; this owner arms,
re-times and closes it, but nothing done to the reminder (snooze, «Готово» on the
reminder itself, dismissing an alarm) records an outcome.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso
from student_execution_os.recurrence import (
    RecurrenceRule,
    contains_original,
    iter_original_locals,
    recurrence_id,
    remaining_count,
    resolve_local,
)

from .model import RESOLVED, CheckInKind, CheckInOccurrence, CheckInTemplate, OccurrenceStatus, DELIVERIES

# Occurrences exist from a little in the past (history the worker may have missed
# while it was down is recorded honestly as MISSED) to a short way ahead (prompts,
# Today, phones scheduling alarms offline).
HORIZON_AHEAD = timedelta(hours=48)
HISTORY_BACKFILL = timedelta(days=35)
# A prompt for an occurrence still goes out this late (the worker was down); later
# than that the user is not told about a moment long gone.
PROMPT_GRACE = timedelta(hours=2)

_TEMPLATE_EDITABLE = {"title", "dose_text", "instructions", "target_quantity", "unit", "unit_effort_seconds",
                      "remind", "delivery", "followup_minutes", "window_minutes"}


def _local_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        raise ValidationError("local civil datetime must be naive")
    return value.replace(second=0, microsecond=0).isoformat()


def _local_dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def reminder_id_for(account_id: str, template_id: str, original_recurrence_id: str) -> str:
    digest = hashlib.sha256(f"{account_id}|{template_id}|{original_recurrence_id}".encode()).hexdigest()[:32]
    return f"checkin-{digest}"


def _text(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if len(text) > limit:
        raise ValidationError(f"{field} is longer than {limit} characters")
    return text or None


def _int_or_none(value: Any, field: str, low: int, high: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a whole number")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be a whole number") from exc
    if not low <= number <= high:
        raise ValidationError(f"{field} must be between {low} and {high}")
    return number


@dataclass(frozen=True)
class Transition:
    """Result of a user operation on one occurrence."""
    occurrence: CheckInOccurrence
    status: str  # APPLIED | NOOP | CONFLICT
    code: str | None = None


def clean_template_fields(payload: dict[str, Any], *, kind: CheckInKind, creating: bool) -> dict[str, Any]:
    """Validate the user-editable template fields (shared by create and update)."""
    fields: dict[str, Any] = {}
    if creating or "title" in payload:
        title = _text(payload.get("title"), "title", 300)
        if not title:
            raise ValidationError("title is required")
        fields["title"] = title
    for key, limit in (("dose_text", 200), ("instructions", 1000)):
        if key in payload:
            fields[key] = _text(payload[key], key, limit)
    if "unit" in payload:
        fields["unit"] = _text(payload["unit"], "unit", 40)
    if creating or "target_quantity" in payload:
        fields["target_quantity"] = _int_or_none(payload.get("target_quantity"), "target_quantity", 1, 100_000)
    if "unit_effort_seconds" in payload:
        fields["unit_effort_seconds"] = _int_or_none(payload["unit_effort_seconds"], "unit_effort_seconds", 1, 86_400)
    if "remind" in payload:
        if not isinstance(payload["remind"], bool):
            raise ValidationError("remind must be true or false")
        fields["remind"] = payload["remind"]
    if "delivery" in payload:
        if payload["delivery"] not in DELIVERIES:
            raise ValidationError("delivery must be PUSH, ALARM or PUSH_AND_ALARM")
        fields["delivery"] = payload["delivery"]
    if "followup_minutes" in payload:
        fields["followup_minutes"] = _int_or_none(payload["followup_minutes"], "followup_minutes", 5, 720)
    if "window_minutes" in payload:
        fields["window_minutes"] = _int_or_none(payload["window_minutes"], "window_minutes", 5, 1440)
    if kind is CheckInKind.QUOTA:
        if creating and fields.get("target_quantity") is None:
            raise ValidationError("a quota needs target_quantity")
        if "target_quantity" in fields and fields["target_quantity"] is None:
            raise ValidationError("a quota needs target_quantity")
    else:
        if fields.get("target_quantity") is not None or fields.get("unit") or fields.get("unit_effort_seconds"):
            raise ValidationError("target quantity, unit and effort per unit belong to QUOTA check-ins")
        fields.pop("target_quantity", None)
    if kind is not CheckInKind.MEDICATION and (fields.get("dose_text") or fields.get("instructions")):
        raise ValidationError("dose and instructions belong to MEDICATION check-ins")
    return fields


class SQLiteCheckInRepository:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection
        self.clock = canonical.clock

    # ---- templates --------------------------------------------------------------------

    def _template(self, row) -> CheckInTemplate:
        return CheckInTemplate(
            id=row["id"], account_id=row["account_id"], kind=CheckInKind(row["kind"]), title=row["title"],
            dose_text=row["dose_text"], instructions=row["instructions"], target_quantity=row["target_quantity"],
            unit=row["unit"], unit_effort_seconds=row["unit_effort_seconds"],
            dtstart_local=datetime.fromisoformat(row["dtstart_local"]),
            recurrence_rule=RecurrenceRule.parse(row["recurrence_rule"], allow_weekdays=True),
            timezone_name=row["timezone_name"], remind=bool(row["remind"]), delivery=row["delivery"],
            followup_minutes=row["followup_minutes"], window_minutes=row["window_minutes"], status=row["status"],
            series_end_before_local=_local_dt(row["series_end_before_local"]), version=int(row["version"]),
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    def get_template(self, account_id: str, template_id: str) -> CheckInTemplate:
        row = self.connection.execute(
            "SELECT * FROM checkin_templates WHERE account_id=? AND id=?", (account_id, template_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("check-in not found")
        return self._template(row)

    def owner_of(self, template_id: str) -> str | None:
        row = self.connection.execute("SELECT account_id FROM checkin_templates WHERE id=?", (template_id,)).fetchone()
        return None if row is None else row["account_id"]

    def list_templates(self, account_id: str) -> list[CheckInTemplate]:
        rows = self.connection.execute(
            "SELECT * FROM checkin_templates WHERE account_id=? ORDER BY status,dtstart_local,id", (account_id,)
        ).fetchall()
        return [self._template(row) for row in rows]

    def create_template(self, *, account_id: str, template_id: str, kind: CheckInKind, dtstart_local: datetime,
                        recurrence_rule: str, timezone_name: str, actor: ActorCategory,
                        fields: dict[str, Any]) -> CheckInTemplate:
        self.canonical._require_account(account_id)
        if dtstart_local.tzinfo is not None:
            raise ValidationError("dtstart_local must be local civil time without offset")
        rule = RecurrenceRule.parse(recurrence_rule, allow_weekdays=True)
        resolve_local(dtstart_local, timezone_name)  # validates the zone
        values = {"dose_text": None, "instructions": None, "target_quantity": None, "unit": None,
                  "unit_effort_seconds": None, "remind": True, "delivery": "PUSH", "followup_minutes": None,
                  "window_minutes": None, **fields}
        now = self.clock.now()
        candidate = CheckInTemplate(
            id=template_id, account_id=account_id, kind=kind, title=values["title"], dose_text=values["dose_text"],
            instructions=values["instructions"], target_quantity=values["target_quantity"], unit=values["unit"],
            unit_effort_seconds=values["unit_effort_seconds"], dtstart_local=dtstart_local.replace(second=0, microsecond=0),
            recurrence_rule=rule, timezone_name=timezone_name, remind=bool(values["remind"]), delivery=values["delivery"],
            followup_minutes=values["followup_minutes"], window_minutes=values["window_minutes"], status="ACTIVE",
            series_end_before_local=None, version=1, created_at=now, updated_at=now,
        )
        with self.canonical._tx() as conn:
            self._insert_template(conn, candidate, actor)
            self.canonical._record_change(conn, account_id=account_id, entity_type="CHECKIN_TEMPLATE",
                                          entity_id=template_id, action="CREATE_CHECKIN", actor=actor,
                                          payload={"kind": kind.value})
        return self.get_template(account_id, template_id)

    @staticmethod
    def _insert_template(conn, t: CheckInTemplate, actor: ActorCategory) -> None:
        conn.execute(
            "INSERT INTO checkin_templates(id,account_id,kind,title,dose_text,instructions,target_quantity,unit,"
            "unit_effort_seconds,dtstart_local,recurrence_rule,timezone_name,remind,delivery,followup_minutes,"
            "window_minutes,status,series_end_before_local,actor_category,version,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (t.id, t.account_id, t.kind.value, t.title, t.dose_text, t.instructions, t.target_quantity, t.unit,
             t.unit_effort_seconds, _local_iso(t.dtstart_local), t.recurrence_rule.canonical(), t.timezone_name,
             int(t.remind), t.delivery, t.followup_minutes, t.window_minutes, t.status,
             _local_iso(t.series_end_before_local), actor.value, _iso(t.created_at), _iso(t.updated_at)),
        )

    def update_template(self, account_id: str, template_id: str, payload: dict[str, Any], *,
                        expected_version: int | None, actor: ActorCategory) -> CheckInTemplate:
        current = self.get_template(account_id, template_id)
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict(f"expected check-in version {expected_version}, current {current.version}")
        unknown = set(payload) - _TEMPLATE_EDITABLE
        if unknown:
            raise ValidationError("check-in fields cannot be edited: " + ", ".join(sorted(unknown)))
        fields = clean_template_fields(payload, kind=current.kind, creating=False)
        if not fields:
            return current
        candidate = replace(current, **fields)  # re-validates the invariants
        now = self.clock.now()
        assignments = ",".join(f"{key}=?" for key in fields)
        values = [int(value) if isinstance(value, bool) else value for value in fields.values()]
        with self.canonical._tx() as conn:
            cur = conn.execute(
                f"UPDATE checkin_templates SET {assignments},version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (*values, _iso(now), account_id, template_id, current.version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("check-in changed before commit")
            if "target_quantity" in fields:
                # Today's (and later) open days follow the new target; resolved days keep theirs.
                conn.execute(
                    "UPDATE checkin_occurrences SET target_quantity=?,updated_at=?,version=version+1 "
                    "WHERE account_id=? AND template_id=? AND status='PENDING'",
                    (candidate.target_quantity, _iso(now), account_id, template_id),
                )
            self.canonical._record_change(conn, account_id=account_id, entity_type="CHECKIN_TEMPLATE",
                                          entity_id=template_id, action="UPDATE_CHECKIN", actor=actor,
                                          payload={"fields": sorted(fields)})
            self._refresh_open_prompts(account_id, candidate, now)
        return self.get_template(account_id, template_id)

    def end_template(self, account_id: str, template_id: str, *, expected_version: int | None,
                     actor: ActorCategory) -> tuple[CheckInTemplate, bool]:
        """Stop the series from now on. Recorded history stays; future days disappear."""
        current = self.get_template(account_id, template_id)
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict(f"expected check-in version {expected_version}, current {current.version}")
        if current.status == "ENDED":
            return current, False
        now = self.clock.now()
        local_now = now.astimezone(ZoneInfo(current.timezone_name)).replace(tzinfo=None, second=0, microsecond=0)
        boundary = next(iter_original_locals(current.dtstart_local, current.recurrence_rule,
                                             series_end_before=current.series_end_before_local,
                                             start_local=local_now), None)
        end_before = boundary or local_now
        if current.series_end_before_local is not None and current.series_end_before_local < end_before:
            end_before = current.series_end_before_local
        with self.canonical._tx() as conn:
            self._drop_future(account_id, template_id, end_before, now)
            cur = conn.execute(
                "UPDATE checkin_templates SET status='ENDED',series_end_before_local=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (_local_iso(end_before), _iso(now), account_id, template_id, current.version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("check-in changed before commit")
            self.canonical._record_change(conn, account_id=account_id, entity_type="CHECKIN_TEMPLATE",
                                          entity_id=template_id, action="END_CHECKIN", actor=actor)
        return self.get_template(account_id, template_id), True

    def delete_template(self, account_id: str, template_id: str, *, actor: ActorCategory) -> None:
        """Delete the check-in and its history (account data the user removes)."""
        self.get_template(account_id, template_id)
        now = self.clock.now()
        reminder_ids = [row[0] for row in self.connection.execute(
            "SELECT reminder_id FROM checkin_occurrences WHERE account_id=? AND template_id=? AND reminder_id IS NOT NULL",
            (account_id, template_id)).fetchall()]
        with self.canonical._tx() as conn:
            for reminder_id in reminder_ids:
                self._delete_reminder(account_id, reminder_id, now)
            conn.execute("DELETE FROM checkin_templates WHERE account_id=? AND id=?", (account_id, template_id))
            conn.execute("INSERT OR REPLACE INTO deleted_entities(account_id,entity_kind,entity_id,deleted_at) "
                         "VALUES (?,'CHECKIN',?,?)", (account_id, template_id, _iso(now)))
            self.canonical._record_change(conn, account_id=account_id, entity_type="CHECKIN_TEMPLATE",
                                          entity_id=template_id, action="DELETE_CHECKIN", actor=actor)

    def is_deleted(self, account_id: str, template_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM deleted_entities WHERE account_id=? AND entity_kind='CHECKIN' AND entity_id=?",
            (account_id, template_id)).fetchone() is not None

    def split_this_and_future(self, *, account_id: str, template_id: str, original_recurrence_id: str,
                              successor_id: str, actor: ActorCategory, expected_version: int | None,
                              start_local: datetime | None = None, recurrence_rule: str | None = None,
                              timezone_name: str | None = None, fields: dict[str, Any] | None = None,
                              ) -> tuple[CheckInTemplate, CheckInTemplate]:
        """«Изменить этот и следующие»: the old series ends before the boundary occurrence.

        Days that already have a recorded outcome are history and are never rewritten;
        later open days (including one-day moves/cancels) belong to the new definition.
        """
        current = self.get_template(account_id, template_id)
        if expected_version is not None and current.version != expected_version:
            raise VersionConflict("check-in version changed")
        boundary = datetime.fromisoformat(original_recurrence_id)
        if not contains_original(current.dtstart_local, current.recurrence_rule, boundary,
                                 series_end_before=current.series_end_before_local):
            raise ValidationError("split boundary is not an occurrence of this check-in")
        recorded = self.connection.execute(
            "SELECT 1 FROM checkin_occurrences WHERE account_id=? AND template_id=? AND original_recurrence_id>=? "
            "AND resolved_by='USER' AND status!='CANCELLED' LIMIT 1",
            (account_id, template_id, original_recurrence_id)).fetchone()
        if recorded is not None:
            raise VersionConflict("an occurrence from the boundary on already has a recorded outcome; split after it")
        if recurrence_rule:
            rule = RecurrenceRule.parse(recurrence_rule, allow_weekdays=True)
        else:
            left = remaining_count(current.dtstart_local, current.recurrence_rule, boundary)
            rule = replace(current.recurrence_rule, count=left)
        zone = timezone_name or current.timezone_name
        successor_start = (start_local or boundary).replace(second=0, microsecond=0)
        if successor_start.tzinfo is not None:
            raise ValidationError("start_local must be local civil time")
        resolve_local(successor_start, zone)
        changes = clean_template_fields(fields or {}, kind=current.kind, creating=False)
        now = self.clock.now()
        successor = replace(current, id=successor_id, dtstart_local=successor_start, recurrence_rule=rule,
                            timezone_name=zone, status="ACTIVE", series_end_before_local=None, version=1,
                            created_at=now, updated_at=now, **changes)
        with self.canonical._tx() as conn:
            self._drop_future(account_id, template_id, boundary, now)
            cur = conn.execute(
                "UPDATE checkin_templates SET series_end_before_local=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (_local_iso(boundary), _iso(now), account_id, template_id, current.version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("check-in changed before split")
            self._insert_template(conn, successor, actor)
            self.canonical._record_change(conn, account_id=account_id, entity_type="CHECKIN_TEMPLATE",
                                          entity_id=template_id, action="SPLIT_CHECKIN", actor=actor,
                                          payload={"boundary": original_recurrence_id, "successor_id": successor_id})
        return self.get_template(account_id, template_id), self.get_template(account_id, successor_id)

    def _drop_future(self, account_id: str, template_id: str, boundary: datetime, now: datetime) -> None:
        rows = self.connection.execute(
            "SELECT original_recurrence_id,reminder_id FROM checkin_occurrences WHERE account_id=? AND template_id=? "
            "AND original_recurrence_id>=? AND (resolved_by IS NULL OR resolved_by='POLICY' OR status='CANCELLED')",
            (account_id, template_id, _local_iso(boundary))).fetchall()
        for row in rows:
            if row["reminder_id"]:
                self._delete_reminder(account_id, row["reminder_id"], now)
            self.connection.execute(
                "DELETE FROM checkin_occurrences WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
                (account_id, template_id, row["original_recurrence_id"]))

    # ---- occurrences ------------------------------------------------------------------

    @staticmethod
    def _occurrence(row) -> CheckInOccurrence:
        return CheckInOccurrence(
            account_id=row["account_id"], template_id=row["template_id"],
            original_recurrence_id=row["original_recurrence_id"], moved_to_local=_local_dt(row["moved_to_local"]),
            status=OccurrenceStatus(row["status"]), resolved_by=row["resolved_by"],
            occurred_at=_dt(row["occurred_at"]), acted_at=_dt(row["acted_at"]),
            quantity_done=int(row["quantity_done"]), target_quantity=row["target_quantity"], note=row["note"],
            reminder_id=row["reminder_id"], followups_sent=int(row["followups_sent"]), version=int(row["version"]),
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    def get_occurrence(self, account_id: str, template_id: str, original_recurrence_id: str) -> CheckInOccurrence | None:
        row = self.connection.execute(
            "SELECT * FROM checkin_occurrences WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
            (account_id, template_id, original_recurrence_id)).fetchone()
        return None if row is None else self._occurrence(row)

    def occurrence_for_reminder(self, account_id: str, reminder_id: str) -> CheckInOccurrence | None:
        row = self.connection.execute(
            "SELECT * FROM checkin_occurrences WHERE account_id=? AND reminder_id=?", (account_id, reminder_id)
        ).fetchone()
        return None if row is None else self._occurrence(row)

    def require_occurrence(self, account_id: str, template_id: str, original_recurrence_id: str) -> CheckInOccurrence:
        """The occurrence, materialized on demand when the rule produces it."""
        template = self.get_template(account_id, template_id)
        existing = self.get_occurrence(account_id, template_id, original_recurrence_id)
        if existing is not None:
            return existing
        try:
            original = datetime.fromisoformat(original_recurrence_id)
        except ValueError as exc:
            raise ValidationError("original_recurrence_id must be a local ISO datetime") from exc
        if not contains_original(template.dtstart_local, template.recurrence_rule, original,
                                 series_end_before=template.series_end_before_local):
            raise ValidationError("occurrence identity is not part of the check-in")
        return self._materialize(template, original, self.clock.now())

    def scheduled_at(self, template: CheckInTemplate, occurrence: CheckInOccurrence) -> datetime:
        # UTC: aware datetimes inside a DST fold never compare equal across zones.
        return resolve_local(occurrence.scheduled_local, template.timezone_name).astimezone(timezone.utc)

    def window_end(self, template: CheckInTemplate, occurrence: CheckInOccurrence) -> datetime:
        """When an unanswered occurrence is recorded MISSED (policy, not a medical rule)."""
        scheduled = self.scheduled_at(template, occurrence)
        if template.window_minutes is not None:
            return scheduled + timedelta(minutes=template.window_minutes)
        local = occurrence.scheduled_local
        end = resolve_local(datetime.combine(local.date() + timedelta(days=1), time(0, 0)),
                            template.timezone_name).astimezone(timezone.utc)
        original = datetime.fromisoformat(occurrence.original_recurrence_id)
        following = next(iter_original_locals(template.dtstart_local, template.recurrence_rule,
                                              series_end_before=template.series_end_before_local,
                                              start_local=original + timedelta(minutes=1)), None)
        if following is not None:
            end = min(end, resolve_local(following, template.timezone_name).astimezone(timezone.utc))
        return max(end, scheduled + timedelta(minutes=5))

    def _materialize(self, template: CheckInTemplate, original: datetime, now: datetime) -> CheckInOccurrence:
        rid = recurrence_id(original)
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO checkin_occurrences(account_id,template_id,original_recurrence_id,status,"
                "target_quantity,version,created_at,updated_at) VALUES (?,?,?,'PENDING',?,1,?,?)",
                (template.account_id, template.id, rid, template.target_quantity, _iso(now), _iso(now)),
            )
        occurrence = self.get_occurrence(template.account_id, template.id, rid)
        assert occurrence is not None
        return self._arm_prompt(template, occurrence, now)

    def _arm_prompt(self, template: CheckInTemplate, occurrence: CheckInOccurrence, now: datetime) -> CheckInOccurrence:
        """Create the occurrence's reminder when it should prompt and has none yet."""
        if (not template.remind or occurrence.status is not OccurrenceStatus.PENDING or occurrence.reminder_id
                or template.status != "ACTIVE"):
            return occurrence
        scheduled = self.scheduled_at(template, occurrence)
        if scheduled < now - PROMPT_GRACE or now >= self.window_end(template, occurrence):
            return occurrence  # a moment long gone is not announced
        reminder_id = reminder_id_for(template.account_id, template.id, occurrence.original_recurrence_id)
        if self.connection.execute("SELECT 1 FROM deleted_reminders WHERE account_id=? AND reminder_id=?",
                                   (template.account_id, reminder_id)).fetchone() is not None:
            return occurrence
        with self.canonical._tx() as conn:
            if conn.execute("SELECT 1 FROM reminders WHERE id=?", (reminder_id,)).fetchone() is None:
                conn.execute(
                    "INSERT INTO reminders(id,account_id,title,note,remind_at,delivery,wake_check,raise_volume,"
                    "status,actor_category,created_at,updated_at,version) VALUES (?,?,?,?,?,?,0,0,'SCHEDULED','SYSTEM',?,?,1)",
                    (reminder_id, template.account_id, template.title, template.dose_text, _iso(scheduled),
                     template.delivery, _iso(now), _iso(now)),
                )
            conn.execute(
                "UPDATE checkin_occurrences SET reminder_id=? WHERE account_id=? AND template_id=? "
                "AND original_recurrence_id=?",
                (reminder_id, template.account_id, template.id, occurrence.original_recurrence_id),
            )
            if template.delivery != "PUSH":
                from student_execution_os.reminders.store import ReminderStore
                ReminderStore(self.canonical).signal_alarm_sync(template.account_id, now)
        return replace(occurrence, reminder_id=reminder_id)

    def _refresh_open_prompts(self, account_id: str, template: CheckInTemplate, now: datetime) -> None:
        """Template edits reach the prompts of days that are still open."""
        rows = self.connection.execute(
            "SELECT o.original_recurrence_id,o.reminder_id,r.status FROM checkin_occurrences o "
            "JOIN reminders r ON r.id=o.reminder_id WHERE o.account_id=? AND o.template_id=? AND o.status='PENDING' "
            "AND r.status IN ('SCHEDULED','FIRED')", (account_id, template.id)).fetchall()
        for row in rows:
            if not template.remind:
                self._close_reminder(account_id, row["reminder_id"], now, status="CANCELLED", reason="CHECKIN_EDITED")
                continue
            self.connection.execute(
                "UPDATE reminders SET title=?,note=?,delivery=?,updated_at=?,version=version+1 WHERE account_id=? AND id=?",
                (template.title, template.dose_text, template.delivery, _iso(now), account_id, row["reminder_id"]))
        if rows:
            # Phones re-read their local alarms (delivery may have switched to or from ALARM).
            from student_execution_os.reminders.store import ReminderStore
            ReminderStore(self.canonical).signal_alarm_sync(account_id, now)

    def _close_reminder(self, account_id: str, reminder_id: str | None, now: datetime, *, status: str,
                        reason: str) -> None:
        if not reminder_id:
            return
        self.connection.execute(
            "UPDATE reminder_messages SET delivery_state='CANCELLED',lease_owner=NULL,lease_expires_at=NULL,last_error=? "
            "WHERE account_id=? AND reminder_id=? AND delivery_state IN ('PENDING','LEASED')",
            (reason, account_id, reminder_id))
        cur = self.connection.execute(
            "UPDATE reminders SET status=?,completed_at=CASE WHEN ?='DONE' THEN ? ELSE completed_at END,"
            "updated_at=?,version=version+1 WHERE account_id=? AND id=? AND status IN ('SCHEDULED','FIRED')",
            (status, status, _iso(now), _iso(now), account_id, reminder_id))
        if cur.rowcount:
            delivery = self.connection.execute("SELECT delivery FROM reminders WHERE id=?", (reminder_id,)).fetchone()
            if delivery is not None and delivery["delivery"] != "PUSH":
                from student_execution_os.reminders.store import ReminderStore
                ReminderStore(self.canonical).signal_alarm_sync(account_id, now)

    def _delete_reminder(self, account_id: str, reminder_id: str, now: datetime) -> None:
        self._close_reminder(account_id, reminder_id, now, status="CANCELLED", reason="CHECKIN_REMOVED")
        self.connection.execute("DELETE FROM reminders WHERE account_id=? AND id=?", (account_id, reminder_id))

    # ---- horizon and policy -----------------------------------------------------------

    def ensure_horizon(self, account_id: str, now: datetime) -> dict[str, int]:
        """Materialize occurrences, record MISSED by policy and issue follow-up prompts.

        Idempotent and safe to run from the worker tick, a read, or a command: every
        step is keyed by occurrence identity and current state.
        """
        if now.tzinfo is None:
            raise ValidationError("check-in horizon needs an aware moment")
        stats = {"materialized": 0, "missed": 0, "followups": 0}
        for template in self.list_templates(account_id):
            if template.status != "ACTIVE":
                continue
            zone = ZoneInfo(template.timezone_name)
            start_local = max(template.dtstart_local,
                              (now - HISTORY_BACKFILL).astimezone(zone).replace(tzinfo=None, second=0, microsecond=0))
            existing = {row[0] for row in self.connection.execute(
                "SELECT original_recurrence_id FROM checkin_occurrences WHERE account_id=? AND template_id=? "
                "AND original_recurrence_id>=?", (account_id, template.id, _local_iso(start_local))).fetchall()}
            for original in iter_original_locals(template.dtstart_local, template.recurrence_rule,
                                                 series_end_before=template.series_end_before_local,
                                                 start_local=start_local):
                if resolve_local(original, template.timezone_name) >= now + HORIZON_AHEAD:
                    break
                if recurrence_id(original) in existing:
                    continue
                self._materialize(template, original, now)
                stats["materialized"] += 1
        templates = {t.id: t for t in self.list_templates(account_id)}
        rows = self.connection.execute(
            "SELECT * FROM checkin_occurrences WHERE account_id=? AND status='PENDING' ORDER BY original_recurrence_id",
            (account_id,)).fetchall()
        for row in rows:
            occurrence = self._occurrence(row)
            template = templates[occurrence.template_id]
            if now >= self.window_end(template, occurrence):
                self._resolve(template, occurrence, OccurrenceStatus.MISSED, by="POLICY", now=now, occurred_at=None,
                              actor=ActorCategory.SYSTEM)
                stats["missed"] += 1
                continue
            occurrence = self._arm_prompt(template, occurrence, now)
            if self._follow_up(template, occurrence, now):
                stats["followups"] += 1
        return stats

    def _follow_up(self, template: CheckInTemplate, occurrence: CheckInOccurrence, now: datetime) -> bool:
        """One policy follow-up prompt after an unanswered prompt (a new prompt, not a retry)."""
        if template.followup_minutes is None or occurrence.followups_sent >= 1 or not occurrence.reminder_id:
            return False
        reminder = self.connection.execute(
            "SELECT status,fired_at,remind_at FROM reminders WHERE account_id=? AND id=?",
            (template.account_id, occurrence.reminder_id)).fetchone()
        if reminder is None or reminder["status"] != "FIRED" or not reminder["fired_at"]:
            return False
        if _dt(reminder["fired_at"]) + timedelta(minutes=template.followup_minutes) > now:
            return False
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE reminders SET status='SCHEDULED',remind_at=?,fired_at=NULL,updated_at=?,version=version+1 "
                "WHERE account_id=? AND id=? AND status='FIRED'",
                (_iso(now), _iso(now), template.account_id, occurrence.reminder_id))
            conn.execute(
                "UPDATE checkin_occurrences SET followups_sent=followups_sent+1 WHERE account_id=? AND template_id=? "
                "AND original_recurrence_id=?", (template.account_id, template.id, occurrence.original_recurrence_id))
        return True

    # ---- outcomes ---------------------------------------------------------------------

    def _resolve(self, template: CheckInTemplate, occurrence: CheckInOccurrence, status: OccurrenceStatus, *,
                 by: str, now: datetime, occurred_at: datetime | None, actor: ActorCategory,
                 quantity: int | None = None) -> CheckInOccurrence:
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE checkin_occurrences SET status=?,resolved_by=?,occurred_at=?,acted_at=?,quantity_done=?,"
                "version=version+1,updated_at=? WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
                (status.value, by, _iso(occurred_at), _iso(now),
                 occurrence.quantity_done if quantity is None else quantity, _iso(now),
                 template.account_id, template.id, occurrence.original_recurrence_id))
            self._close_reminder(template.account_id, occurrence.reminder_id, now,
                                 status="DONE" if by == "USER" and status is not OccurrenceStatus.CANCELLED else "CANCELLED",
                                 reason=f"CHECKIN_{status.value}")
            self.canonical._record_change(
                conn, account_id=template.account_id, entity_type="CHECKIN_OCCURRENCE",
                entity_id=f"{template.id}:{occurrence.original_recurrence_id}",
                action=f"CHECKIN_{status.value}", actor=actor, payload={"resolved_by": by})
        return self.get_occurrence(template.account_id, template.id, occurrence.original_recurrence_id)  # type: ignore[return-value]

    def _occurred(self, occurred_at: datetime | None, now: datetime) -> datetime:
        moment = occurred_at or now
        if moment.tzinfo is None:
            raise ValidationError("occurred_at must include a UTC offset")
        if moment > now + timedelta(minutes=5):
            raise ValidationError("occurred_at is in the future")
        return moment

    def mark_done(self, account_id: str, template_id: str, original_recurrence_id: str, *, actor: ActorCategory,
                  occurred_at: datetime | None = None) -> Transition:
        template = self.get_template(account_id, template_id)
        occurrence = self.require_occurrence(account_id, template_id, original_recurrence_id)
        now = self.clock.now()
        if occurrence.status is OccurrenceStatus.DONE:
            return Transition(occurrence, "NOOP", "ALREADY_DONE")
        if occurrence.status in (OccurrenceStatus.SKIPPED, OccurrenceStatus.CANCELLED):
            return Transition(occurrence, "CONFLICT", f"CHECKIN_{occurrence.status.value}")
        # PENDING, or MISSED by policy: the user's later answer is the truth.
        quantity = max(occurrence.quantity_done, occurrence.target_quantity or 0)
        resolved = self._resolve(template, occurrence, OccurrenceStatus.DONE, by="USER", now=now,
                                 occurred_at=self._occurred(occurred_at, now), actor=actor, quantity=quantity)
        return Transition(resolved, "APPLIED")

    def mark_skipped(self, account_id: str, template_id: str, original_recurrence_id: str, *,
                     actor: ActorCategory, note: str | None = None) -> Transition:
        template = self.get_template(account_id, template_id)
        occurrence = self.require_occurrence(account_id, template_id, original_recurrence_id)
        now = self.clock.now()
        if occurrence.status is OccurrenceStatus.SKIPPED:
            return Transition(occurrence, "NOOP", "ALREADY_SKIPPED")
        if occurrence.status in (OccurrenceStatus.DONE, OccurrenceStatus.CANCELLED):
            return Transition(occurrence, "CONFLICT", f"CHECKIN_{occurrence.status.value}")
        resolved = self._resolve(template, occurrence, OccurrenceStatus.SKIPPED, by="USER", now=now,
                                 occurred_at=None, actor=actor)
        if note:
            self._set_note(account_id, template_id, original_recurrence_id, note)
            resolved = self.get_occurrence(account_id, template_id, original_recurrence_id)  # type: ignore[assignment]
        return Transition(resolved, "APPLIED")

    def cancel_occurrence(self, account_id: str, template_id: str, original_recurrence_id: str, *,
                          actor: ActorCategory) -> Transition:
        template = self.get_template(account_id, template_id)
        occurrence = self.require_occurrence(account_id, template_id, original_recurrence_id)
        if occurrence.status is OccurrenceStatus.CANCELLED:
            return Transition(occurrence, "NOOP", "ALREADY_CANCELLED")
        if occurrence.resolved_by == "USER":
            return Transition(occurrence, "CONFLICT", f"CHECKIN_{occurrence.status.value}")
        resolved = self._resolve(template, occurrence, OccurrenceStatus.CANCELLED, by="USER", now=self.clock.now(),
                                 occurred_at=None, actor=actor)
        return Transition(resolved, "APPLIED")

    def reopen_occurrence(self, account_id: str, template_id: str, original_recurrence_id: str, *,
                          actor: ActorCategory) -> Transition:
        """Undo a recorded outcome; the day is open again (the policy may close it later)."""
        template = self.get_template(account_id, template_id)
        occurrence = self.require_occurrence(account_id, template_id, original_recurrence_id)
        if occurrence.status is OccurrenceStatus.PENDING:
            return Transition(occurrence, "NOOP", "ALREADY_OPEN")
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE checkin_occurrences SET status='PENDING',resolved_by=NULL,occurred_at=NULL,acted_at=NULL,"
                "version=version+1,updated_at=? WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
                (_iso(now), account_id, template_id, original_recurrence_id))
            self.canonical._record_change(conn, account_id=account_id, entity_type="CHECKIN_OCCURRENCE",
                                          entity_id=f"{template_id}:{original_recurrence_id}",
                                          action="CHECKIN_REOPEN", actor=actor)
        return Transition(self.get_occurrence(account_id, template_id, original_recurrence_id), "APPLIED")  # type: ignore[arg-type]

    def log_quantity(self, account_id: str, template_id: str, original_recurrence_id: str, *, delta: int,
                     actor: ActorCategory, occurred_at: datetime | None = None) -> Transition:
        """«Решил ещё 5»: a delta, so progress logged on two devices adds up."""
        template = self.get_template(account_id, template_id)
        if template.kind is not CheckInKind.QUOTA:
            raise ValidationError("only a quota check-in counts quantity")
        if isinstance(delta, bool) or not isinstance(delta, int) or not 1 <= delta <= 100_000:
            raise ValidationError("count must be a positive whole number")
        occurrence = self.require_occurrence(account_id, template_id, original_recurrence_id)
        if occurrence.status in (OccurrenceStatus.SKIPPED, OccurrenceStatus.CANCELLED):
            return Transition(occurrence, "CONFLICT", f"CHECKIN_{occurrence.status.value}")
        now = self.clock.now()
        moment = self._occurred(occurred_at, now)
        total = min(1_000_000, occurrence.quantity_done + delta)
        target = occurrence.target_quantity or template.target_quantity or 0
        if total >= target and occurrence.status is not OccurrenceStatus.DONE:
            resolved = self._resolve(template, occurrence, OccurrenceStatus.DONE, by="USER", now=now,
                                     occurred_at=moment, actor=actor, quantity=total)
            return Transition(resolved, "APPLIED")
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE checkin_occurrences SET quantity_done=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
                (total, _iso(now), account_id, template_id, original_recurrence_id))
            self.canonical._record_change(conn, account_id=account_id, entity_type="CHECKIN_OCCURRENCE",
                                          entity_id=f"{template_id}:{original_recurrence_id}",
                                          action="CHECKIN_PROGRESS", actor=actor, payload={"delta": delta})
        return Transition(self.get_occurrence(account_id, template_id, original_recurrence_id), "APPLIED")  # type: ignore[arg-type]

    def move_occurrence(self, account_id: str, template_id: str, original_recurrence_id: str, *,
                        target_local: datetime, actor: ActorCategory) -> Transition:
        """«Перенеси только сегодняшний приём на 22:00»: one day, same identity."""
        template = self.get_template(account_id, template_id)
        occurrence = self.require_occurrence(account_id, template_id, original_recurrence_id)
        if occurrence.status is not OccurrenceStatus.PENDING:
            return Transition(occurrence, "CONFLICT", f"CHECKIN_{occurrence.status.value}")
        if target_local.tzinfo is not None:
            raise ValidationError("target_local must be local civil time")
        target_local = target_local.replace(second=0, microsecond=0)
        scheduled = resolve_local(target_local, template.timezone_name)
        moved = None if recurrence_id(target_local) == original_recurrence_id else target_local
        if moved == occurrence.moved_to_local:
            return Transition(occurrence, "NOOP", "UNCHANGED")
        now = self.clock.now()
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE checkin_occurrences SET moved_to_local=?,followups_sent=0,version=version+1,updated_at=? "
                "WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
                (_local_iso(moved), _iso(now), account_id, template_id, original_recurrence_id))
            if occurrence.reminder_id:
                # A new moment is a new prompt episode; messages about the old one go.
                self.connection.execute(
                    "UPDATE reminder_messages SET delivery_state='CANCELLED',lease_owner=NULL,lease_expires_at=NULL,"
                    "last_error='RESCHEDULED' WHERE account_id=? AND reminder_id=? AND delivery_state='PENDING'",
                    (account_id, occurrence.reminder_id))
                conn.execute(
                    "UPDATE reminders SET remind_at=?,status='SCHEDULED',fired_at=NULL,acknowledged_at=NULL,"
                    "completed_at=NULL,updated_at=?,version=version+1 WHERE account_id=? AND id=?",
                    (_iso(scheduled), _iso(now), account_id, occurrence.reminder_id))
                if template.delivery != "PUSH":
                    from student_execution_os.reminders.store import ReminderStore
                    ReminderStore(self.canonical).signal_alarm_sync(account_id, now)
            self.canonical._record_change(conn, account_id=account_id, entity_type="CHECKIN_OCCURRENCE",
                                          entity_id=f"{template_id}:{original_recurrence_id}",
                                          action="CHECKIN_MOVE", actor=actor,
                                          payload={"moved_to_local": _local_iso(moved)})
        updated = self.get_occurrence(account_id, template_id, original_recurrence_id)
        assert updated is not None
        return Transition(self._arm_prompt(template, updated, now), "APPLIED")

    def _set_note(self, account_id: str, template_id: str, original_recurrence_id: str, note: str) -> None:
        self.connection.execute(
            "UPDATE checkin_occurrences SET note=? WHERE account_id=? AND template_id=? AND original_recurrence_id=?",
            (_text(note, "note", 500), account_id, template_id, original_recurrence_id))

    # ---- reads ------------------------------------------------------------------------

    def occurrences_between(self, account_id: str, start: datetime, end: datetime) -> list[tuple[CheckInTemplate, CheckInOccurrence]]:
        """Occurrences whose effective scheduled instant lies in [start, end)."""
        templates = {t.id: t for t in self.list_templates(account_id)}
        out = []
        # Local ids are bounded loosely (any zone), then filtered on the resolved instant.
        low = (start - timedelta(days=2)).replace(tzinfo=None).isoformat()
        high = (end + timedelta(days=2)).replace(tzinfo=None).isoformat()
        rows = self.connection.execute(
            "SELECT * FROM checkin_occurrences WHERE account_id=? AND ((original_recurrence_id>=? AND original_recurrence_id<?) "
            "OR (moved_to_local>=? AND moved_to_local<?))", (account_id, low, high, low, high)).fetchall()
        for row in rows:
            occurrence = self._occurrence(row)
            template = templates.get(occurrence.template_id)
            if template is None:
                continue
            at = self.scheduled_at(template, occurrence)
            if start <= at < end:
                out.append((template, occurrence))
        out.sort(key=lambda pair: (self.scheduled_at(*pair), pair[0].title, pair[1].original_recurrence_id))
        return out


def template_payload(template: CheckInTemplate) -> dict[str, Any]:
    return {
        "kind": "CHECKIN", "id": template.id, "checkin_kind": template.kind.value, "title": template.title,
        "dose_text": template.dose_text, "instructions": template.instructions,
        "target_quantity": template.target_quantity, "unit": template.unit,
        "unit_effort_seconds": template.unit_effort_seconds,
        "dtstart_local": template.dtstart_local.isoformat(), "recurrence_rule": template.recurrence_rule.canonical(),
        "timezone_name": template.timezone_name, "remind": template.remind, "delivery": template.delivery,
        "followup_minutes": template.followup_minutes, "window_minutes": template.window_minutes,
        "status": template.status,
        "series_end_before_local": None if template.series_end_before_local is None else template.series_end_before_local.isoformat(),
        "version": template.version,
    }


def occurrence_payload(repo: SQLiteCheckInRepository, template: CheckInTemplate,
                       occurrence: CheckInOccurrence) -> dict[str, Any]:
    def at(value: datetime | None) -> str | None:
        return None if value is None else value.astimezone(timezone.utc).isoformat()

    remaining = None
    if template.kind is CheckInKind.QUOTA and occurrence.target_quantity:
        remaining = max(0, occurrence.target_quantity - occurrence.quantity_done)
    return {
        "kind": "CHECKIN_OCCURRENCE", "template_id": template.id,
        "original_recurrence_id": occurrence.original_recurrence_id,
        "identity": [template.id, occurrence.original_recurrence_id],
        "title": template.title, "checkin_kind": template.kind.value, "dose_text": template.dose_text,
        "scheduled_at": at(repo.scheduled_at(template, occurrence)),
        "scheduled_local": occurrence.scheduled_local.isoformat(),
        "moved_to_local": None if occurrence.moved_to_local is None else occurrence.moved_to_local.isoformat(),
        "window_ends_at": at(repo.window_end(template, occurrence)),
        "status": occurrence.status.value, "resolved_by": occurrence.resolved_by,
        "occurred_at": at(occurrence.occurred_at), "acted_at": at(occurrence.acted_at),
        "quantity_done": occurrence.quantity_done, "target_quantity": occurrence.target_quantity,
        "remaining_quantity": remaining, "unit": template.unit,
        # Only a user-given pace turns quantity into time; otherwise time stays unknown.
        "remaining_effort_minutes": (None if remaining is None or template.unit_effort_seconds is None
                                     else -(-remaining * template.unit_effort_seconds // 60)),
        "note": occurrence.note, "reminder_id": occurrence.reminder_id, "version": occurrence.version,
    }
