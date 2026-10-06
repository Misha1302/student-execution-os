"""Recurring reminders: «каждый день в 22:30 напоминай вынести мусор» (schema v31).

A reminder series is attention only. It is neither a Task (nothing to plan) nor a
check-in (nobody tracks whether it happened): each occurrence materializes one
ordinary standalone reminder, so push, Android alarms, snooze and «Готово» are the
same as for a one-shot reminder.

Identity: ``(series_id, original_recurrence_id)``; the reminder id is derived from
it. Editing, snoozing or cancelling that one reminder changes only that day. A
deleted occurrence stays deleted (``deleted_reminders``) and is never
materialized again.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
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

from .standalone import DELIVERIES, has_alarm

HORIZON_AHEAD = timedelta(hours=48)
# Occurrences whose moment passed while nothing materialized them (worker down)
# still go out this late; older ones are not announced at all.
LOOKBACK = timedelta(hours=2)
_EDITABLE = {"title", "note", "delivery"}


@dataclass(frozen=True)
class ReminderSeries:
    id: str
    account_id: str
    title: str
    note: str | None
    dtstart_local: datetime
    recurrence_rule: RecurrenceRule
    timezone_name: str
    delivery: str
    status: str
    series_end_before_local: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id or not self.account_id or not self.title.strip():
            raise ValidationError("reminder series identity and title are required")
        if self.dtstart_local.tzinfo is not None:
            raise ValidationError("reminder series DTSTART must be local civil time")
        if self.delivery not in DELIVERIES:
            raise ValidationError("delivery must be PUSH, ALARM or PUSH_AND_ALARM")
        if self.status not in {"ACTIVE", "ENDED"}:
            raise ValidationError("invalid reminder series status")


def series_reminder_id(account_id: str, series_id: str, original_recurrence_id: str) -> str:
    digest = hashlib.sha256(f"{account_id}|{series_id}|{original_recurrence_id}".encode()).hexdigest()[:32]
    return f"rseries-{digest}"


def series_payload(series: ReminderSeries) -> dict[str, Any]:
    return {
        "kind": "REMINDER_SERIES", "id": series.id, "title": series.title, "note": series.note,
        "dtstart_local": series.dtstart_local.isoformat(), "recurrence_rule": series.recurrence_rule.canonical(),
        "timezone_name": series.timezone_name, "delivery": series.delivery, "status": series.status,
        "series_end_before_local": None if series.series_end_before_local is None else series.series_end_before_local.isoformat(),
        "version": series.version,
    }


def _local(value: datetime | None) -> str | None:
    return None if value is None else value.replace(second=0, microsecond=0).isoformat()


class SQLiteReminderSeriesRepository:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection
        self.clock = canonical.clock

    def _series(self, row) -> ReminderSeries:
        return ReminderSeries(
            id=row["id"], account_id=row["account_id"], title=row["title"], note=row["note"],
            dtstart_local=datetime.fromisoformat(row["dtstart_local"]),
            recurrence_rule=RecurrenceRule.parse(row["recurrence_rule"], allow_weekdays=True),
            timezone_name=row["timezone_name"], delivery=row["delivery"], status=row["status"],
            series_end_before_local=None if row["series_end_before_local"] is None else datetime.fromisoformat(row["series_end_before_local"]),
            version=int(row["version"]), created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    def get(self, account_id: str, series_id: str) -> ReminderSeries:
        row = self.connection.execute("SELECT * FROM reminder_series WHERE account_id=? AND id=?",
                                      (account_id, series_id)).fetchone()
        if row is None:
            raise EntityNotFound("reminder series not found")
        return self._series(row)

    def owner_of(self, series_id: str) -> str | None:
        row = self.connection.execute("SELECT account_id FROM reminder_series WHERE id=?", (series_id,)).fetchone()
        return None if row is None else row["account_id"]

    def is_deleted(self, account_id: str, series_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM deleted_entities WHERE account_id=? AND entity_kind='REMINDER_SERIES' AND entity_id=?",
            (account_id, series_id)).fetchone() is not None

    def list(self, account_id: str) -> list[ReminderSeries]:
        rows = self.connection.execute("SELECT * FROM reminder_series WHERE account_id=? ORDER BY status,dtstart_local,id",
                                       (account_id,)).fetchall()
        return [self._series(row) for row in rows]

    def create(self, *, account_id: str, series_id: str, title: str, note: str | None, dtstart_local: datetime,
               recurrence_rule: str, timezone_name: str, delivery: str, actor: ActorCategory) -> ReminderSeries:
        self.canonical._require_account(account_id)
        rule = RecurrenceRule.parse(recurrence_rule, allow_weekdays=True)
        if dtstart_local.tzinfo is not None:
            raise ValidationError("dtstart_local must be local civil time without offset")
        resolve_local(dtstart_local, timezone_name)
        now = self.clock.now()
        series = ReminderSeries(id=series_id, account_id=account_id, title=title, note=note,
                                dtstart_local=dtstart_local.replace(second=0, microsecond=0), recurrence_rule=rule,
                                timezone_name=timezone_name, delivery=delivery, status="ACTIVE",
                                series_end_before_local=None, version=1, created_at=now, updated_at=now)
        with self.canonical._tx() as conn:
            self._insert(conn, series, actor)
            self.canonical._record_change(conn, account_id=account_id, entity_type="REMINDER_SERIES",
                                          entity_id=series_id, action="CREATE_REMINDER_SERIES", actor=actor)
        return self.get(account_id, series_id)

    @staticmethod
    def _insert(conn, s: ReminderSeries, actor: ActorCategory) -> None:
        conn.execute(
            "INSERT INTO reminder_series(id,account_id,title,note,dtstart_local,recurrence_rule,timezone_name,delivery,"
            "status,series_end_before_local,actor_category,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (s.id, s.account_id, s.title, s.note, _local(s.dtstart_local), s.recurrence_rule.canonical(), s.timezone_name,
             s.delivery, s.status, _local(s.series_end_before_local), actor.value, _iso(s.created_at), _iso(s.updated_at)),
        )

    def update(self, account_id: str, series_id: str, fields: dict[str, Any], *, expected_version: int | None,
               actor: ActorCategory) -> ReminderSeries:
        """Title, note, delivery of the whole series. Days the user changed keep their change."""
        current = self.get(account_id, series_id)
        if expected_version is not None and expected_version != current.version:
            raise VersionConflict(f"expected series version {expected_version}, current {current.version}")
        unknown = set(fields) - _EDITABLE
        if unknown:
            raise ValidationError("series fields cannot be edited: " + ", ".join(sorted(unknown)))
        if not fields:
            return current
        candidate = replace(current, **fields)
        now = self.clock.now()
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "UPDATE reminder_series SET title=?,note=?,delivery=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (candidate.title, candidate.note, candidate.delivery, _iso(now), account_id, series_id, current.version))
            if cur.rowcount != 1:
                raise VersionConflict("reminder series changed before commit")
            conn.execute(
                "UPDATE reminders SET title=?,note=?,delivery=?,wake_check=CASE WHEN ? THEN wake_check ELSE 0 END,"
                "raise_volume=CASE WHEN ? THEN raise_volume ELSE 0 END,updated_at=? "
                "WHERE account_id=? AND status='SCHEDULED' AND version=1 AND id IN "
                "(SELECT reminder_id FROM reminder_series_occurrences WHERE account_id=? AND series_id=?)",
                (candidate.title, candidate.note, candidate.delivery, int(has_alarm(candidate.delivery)),
                 int(has_alarm(candidate.delivery)), _iso(now), account_id, account_id, series_id))
            self.canonical._record_change(conn, account_id=account_id, entity_type="REMINDER_SERIES",
                                          entity_id=series_id, action="UPDATE_REMINDER_SERIES", actor=actor,
                                          payload={"fields": sorted(fields)})
            if has_alarm(candidate.delivery) or has_alarm(current.delivery):
                from .store import ReminderStore
                ReminderStore(self.canonical).signal_alarm_sync(account_id, now)
        return self.get(account_id, series_id)

    def _drop_unfired_from(self, account_id: str, series_id: str, boundary_local: datetime, now: datetime) -> bool:
        """Remove materialized occurrences from the boundary on that never went out."""
        rows = self.connection.execute(
            "SELECT o.reminder_id,r.delivery FROM reminder_series_occurrences o JOIN reminders r ON r.id=o.reminder_id "
            "WHERE o.account_id=? AND o.series_id=? AND o.original_recurrence_id>=? AND r.status IN ('SCHEDULED','CANCELLED') "
            "AND r.fired_at IS NULL", (account_id, series_id, _local(boundary_local))).fetchall()
        alarm = False
        for row in rows:
            alarm = alarm or has_alarm(row["delivery"])
            self.connection.execute(
                "UPDATE reminder_messages SET delivery_state='CANCELLED',lease_owner=NULL,lease_expires_at=NULL,"
                "last_error='SERIES_CHANGED' WHERE account_id=? AND reminder_id=? AND delivery_state IN ('PENDING','LEASED')",
                (account_id, row["reminder_id"]))
            self.connection.execute("DELETE FROM reminders WHERE account_id=? AND id=?", (account_id, row["reminder_id"]))
            # A device that still has this occurrence cached replays into a NOOP, not an error.
            self.connection.execute("INSERT OR REPLACE INTO deleted_reminders(account_id,reminder_id,deleted_at) "
                                    "VALUES (?,?,?)", (account_id, row["reminder_id"], _iso(now)))
        if alarm:
            from .store import ReminderStore
            ReminderStore(self.canonical).signal_alarm_sync(account_id, now)
        return bool(rows)

    def _next_original(self, series: ReminderSeries, now: datetime) -> datetime | None:
        local_now = now.astimezone(ZoneInfo(series.timezone_name)).replace(tzinfo=None, second=0, microsecond=0)
        return next(iter_original_locals(series.dtstart_local, series.recurrence_rule,
                                         series_end_before=series.series_end_before_local,
                                         start_local=local_now), None)

    def end(self, account_id: str, series_id: str, *, expected_version: int | None,
            actor: ActorCategory) -> tuple[ReminderSeries, bool]:
        current = self.get(account_id, series_id)
        if expected_version is not None and expected_version != current.version:
            raise VersionConflict(f"expected series version {expected_version}, current {current.version}")
        if current.status == "ENDED":
            return current, False
        now = self.clock.now()
        boundary = self._next_original(current, now) or now.astimezone(ZoneInfo(current.timezone_name)).replace(tzinfo=None)
        with self.canonical._tx() as conn:
            self._drop_unfired_from(account_id, series_id, boundary, now)
            cur = conn.execute(
                "UPDATE reminder_series SET status='ENDED',series_end_before_local=coalesce(series_end_before_local,?),"
                "version=version+1,updated_at=? WHERE account_id=? AND id=? AND version=?",
                (_local(boundary), _iso(now), account_id, series_id, current.version))
            if cur.rowcount != 1:
                raise VersionConflict("reminder series changed before commit")
            self.canonical._record_change(conn, account_id=account_id, entity_type="REMINDER_SERIES",
                                          entity_id=series_id, action="END_REMINDER_SERIES", actor=actor)
        return self.get(account_id, series_id), True

    def delete(self, account_id: str, series_id: str, *, actor: ActorCategory) -> None:
        self.get(account_id, series_id)
        now = self.clock.now()
        with self.canonical._tx() as conn:
            self._drop_unfired_from(account_id, series_id, datetime.min, now)
            # A prompt that already went out and is still open is closed with the series.
            conn.execute(
                "UPDATE reminders SET status='CANCELLED',updated_at=?,version=version+1 WHERE account_id=? "
                "AND status='FIRED' AND id IN (SELECT reminder_id FROM reminder_series_occurrences "
                "WHERE account_id=? AND series_id=?)", (_iso(now), account_id, account_id, series_id))
            conn.execute("DELETE FROM reminder_series WHERE account_id=? AND id=?", (account_id, series_id))
            conn.execute("INSERT OR REPLACE INTO deleted_entities(account_id,entity_kind,entity_id,deleted_at) "
                         "VALUES (?,'REMINDER_SERIES',?,?)", (account_id, series_id, _iso(now)))
            self.canonical._record_change(conn, account_id=account_id, entity_type="REMINDER_SERIES",
                                          entity_id=series_id, action="DELETE_REMINDER_SERIES", actor=actor)

    def split(self, *, account_id: str, series_id: str, original_recurrence_id: str, successor_id: str,
              actor: ActorCategory, expected_version: int | None, start_local: datetime | None = None,
              recurrence_rule: str | None = None, timezone_name: str | None = None,
              fields: dict[str, Any] | None = None) -> tuple[ReminderSeries, ReminderSeries]:
        """«Этот и следующие»: one-day changes from the boundary on are replaced, as in a calendar."""
        current = self.get(account_id, series_id)
        if expected_version is not None and expected_version != current.version:
            raise VersionConflict("reminder series version changed")
        boundary = datetime.fromisoformat(original_recurrence_id)
        if not contains_original(current.dtstart_local, current.recurrence_rule, boundary,
                                 series_end_before=current.series_end_before_local):
            raise ValidationError("split boundary is not an occurrence of this series")
        if recurrence_rule:
            rule = RecurrenceRule.parse(recurrence_rule, allow_weekdays=True)
        else:
            rule = replace(current.recurrence_rule, count=remaining_count(current.dtstart_local, current.recurrence_rule, boundary))
        zone = timezone_name or current.timezone_name
        start = (start_local or boundary).replace(second=0, microsecond=0)
        if start.tzinfo is not None:
            raise ValidationError("start_local must be local civil time")
        resolve_local(start, zone)
        changes = fields or {}
        if set(changes) - _EDITABLE:
            raise ValidationError("series fields cannot be edited: " + ", ".join(sorted(set(changes) - _EDITABLE)))
        now = self.clock.now()
        successor = replace(current, id=successor_id, dtstart_local=start, recurrence_rule=rule, timezone_name=zone,
                            status="ACTIVE", series_end_before_local=None, version=1, created_at=now, updated_at=now,
                            **changes)
        with self.canonical._tx() as conn:
            self._drop_unfired_from(account_id, series_id, boundary, now)
            cur = conn.execute(
                "UPDATE reminder_series SET series_end_before_local=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND id=? AND version=?",
                (_local(boundary), _iso(now), account_id, series_id, current.version))
            if cur.rowcount != 1:
                raise VersionConflict("reminder series changed before split")
            self._insert(conn, successor, actor)
            self.canonical._record_change(conn, account_id=account_id, entity_type="REMINDER_SERIES",
                                          entity_id=series_id, action="SPLIT_REMINDER_SERIES", actor=actor,
                                          payload={"boundary": original_recurrence_id, "successor_id": successor_id})
        return self.get(account_id, series_id), self.get(account_id, successor_id)

    # ---- occurrences --------------------------------------------------------------------

    def occurrence_reminder(self, account_id: str, series_id: str, original_recurrence_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT reminder_id FROM reminder_series_occurrences WHERE account_id=? AND series_id=? "
            "AND original_recurrence_id=?", (account_id, series_id, original_recurrence_id)).fetchone()
        return None if row is None else row["reminder_id"]

    def require_occurrence(self, account_id: str, series_id: str, original_recurrence_id: str) -> str:
        """The reminder id of one occurrence, materialized on demand (e.g. next week's)."""
        series = self.get(account_id, series_id)
        existing = self.occurrence_reminder(account_id, series_id, original_recurrence_id)
        if existing:
            return existing
        try:
            original = datetime.fromisoformat(original_recurrence_id)
        except ValueError as exc:
            raise ValidationError("original_recurrence_id must be a local ISO datetime") from exc
        if not contains_original(series.dtstart_local, series.recurrence_rule, original,
                                 series_end_before=series.series_end_before_local):
            raise ValidationError("occurrence identity is not part of the series")
        reminder_id = self._materialize(series, original, self.clock.now())
        if reminder_id is None:
            raise EntityNotFound("this occurrence was deleted")
        return reminder_id

    def _materialize(self, series: ReminderSeries, original: datetime, now: datetime) -> str | None:
        rid = recurrence_id(original)
        reminder_id = series_reminder_id(series.account_id, series.id, rid)
        if self.connection.execute("SELECT 1 FROM deleted_reminders WHERE account_id=? AND reminder_id=?",
                                   (series.account_id, reminder_id)).fetchone() is not None:
            return None
        with self.canonical._tx() as conn:
            if conn.execute("SELECT 1 FROM reminders WHERE id=?", (reminder_id,)).fetchone() is None:
                conn.execute(
                    "INSERT INTO reminders(id,account_id,title,note,remind_at,delivery,wake_check,raise_volume,status,"
                    "actor_category,created_at,updated_at,version) VALUES (?,?,?,?,?,?,0,0,'SCHEDULED','SYSTEM',?,?,1)",
                    (reminder_id, series.account_id, series.title, series.note,
                     _iso(resolve_local(original, series.timezone_name)), series.delivery, _iso(now), _iso(now)))
            conn.execute(
                "INSERT OR IGNORE INTO reminder_series_occurrences(account_id,series_id,original_recurrence_id,"
                "reminder_id,created_at) VALUES (?,?,?,?,?)", (series.account_id, series.id, rid, reminder_id, _iso(now)))
        return reminder_id

    def ensure_horizon(self, account_id: str, now: datetime) -> int:
        created = 0
        alarm = False
        for series in self.list(account_id):
            if series.status != "ACTIVE":
                continue
            zone = ZoneInfo(series.timezone_name)
            start = max(series.dtstart_local,
                        (now - LOOKBACK).astimezone(zone).replace(tzinfo=None, second=0, microsecond=0))
            existing = {row[0] for row in self.connection.execute(
                "SELECT original_recurrence_id FROM reminder_series_occurrences WHERE account_id=? AND series_id=? "
                "AND original_recurrence_id>=?", (account_id, series.id, _local(start))).fetchall()}
            for original in iter_original_locals(series.dtstart_local, series.recurrence_rule,
                                                 series_end_before=series.series_end_before_local, start_local=start):
                if resolve_local(original, series.timezone_name) >= now + HORIZON_AHEAD:
                    break
                if recurrence_id(original) in existing:
                    continue
                if self._materialize(series, original, now):
                    created += 1
                    alarm = alarm or has_alarm(series.delivery)
        if alarm:
            from .store import ReminderStore
            ReminderStore(self.canonical).signal_alarm_sync(account_id, now)
        return created

    def links(self, account_id: str) -> dict[str, tuple[str, str]]:
        """reminder id -> (series id, original recurrence id), for read models."""
        rows = self.connection.execute(
            "SELECT reminder_id,series_id,original_recurrence_id FROM reminder_series_occurrences WHERE account_id=?",
            (account_id,)).fetchall()
        return {row["reminder_id"]: (row["series_id"], row["original_recurrence_id"]) for row in rows}
