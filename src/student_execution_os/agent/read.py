from __future__ import annotations

import json
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from student_execution_os.domain.errors import ValidationError
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso


class AssistantReadKind(StrEnum):
    AGENDA_WINDOW = "AGENDA_WINDOW"
    FREE_TIME = "FREE_TIME"
    ITEM_LOOKUP = "ITEM_LOOKUP"
    DUE_BEFORE = "DUE_BEFORE"
    URGENT_TASKS = "URGENT_TASKS"
    WHAT_NOW = "WHAT_NOW"
    PLAN_EXPLANATION = "PLAN_EXPLANATION"


_FIELDS = {
    AssistantReadKind.AGENDA_WINDOW: {"kind", "starts_at", "ends_at"},
    AssistantReadKind.FREE_TIME: {"kind", "starts_at", "ends_at"},
    AssistantReadKind.ITEM_LOOKUP: {"kind", "obligation_id", "reminder_id"},
    AssistantReadKind.DUE_BEFORE: {"kind", "before"},
    AssistantReadKind.URGENT_TASKS: {"kind"},
    AssistantReadKind.WHAT_NOW: {"kind"},
    AssistantReadKind.PLAN_EXPLANATION: {"kind", "obligation_id"},
}


def _instant(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"assistant read query {field} must be an ISO-8601 instant")
    try:
        parsed = _dt(value)
    except ValueError:
        raise ValidationError(f"assistant read query {field} must be an ISO-8601 instant") from None
    if parsed is None or parsed.utcoffset() is None:
        raise ValidationError(f"assistant read query {field} must include an offset")
    return parsed


def validate_read_query(raw: object, canonical: SQLiteCanonicalRepository, account_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValidationError("assistant read_query must be an object")
    try:
        kind = AssistantReadKind(raw.get("kind"))
    except ValueError:
        raise ValidationError("assistant read_query has an invalid kind") from None
    if set(raw) - _FIELDS[kind]:
        raise ValidationError("assistant read_query has unsupported fields")
    query = dict(raw)
    if kind in {AssistantReadKind.AGENDA_WINDOW, AssistantReadKind.FREE_TIME}:
        starts = _instant(query.get("starts_at"), "starts_at")
        ends = _instant(query.get("ends_at"), "ends_at")
        if ends <= starts or ends - starts > timedelta(days=14):
            raise ValidationError("assistant read window must be positive and at most 14 days")
        query.update(starts_at=_iso(starts), ends_at=_iso(ends))
    elif kind is AssistantReadKind.DUE_BEFORE:
        query["before"] = _iso(_instant(query.get("before"), "before"))
    elif kind in {AssistantReadKind.ITEM_LOOKUP, AssistantReadKind.PLAN_EXPLANATION}:
        obligation_id = query.get("obligation_id")
        reminder_id = query.get("reminder_id")
        if kind is AssistantReadKind.PLAN_EXPLANATION and reminder_id is not None:
            raise ValidationError("plan explanation requires an obligation")
        if bool(obligation_id) == bool(reminder_id):
            raise ValidationError("assistant read target must name exactly one item")
        if obligation_id:
            exists = canonical.connection.execute(
                "SELECT 1 FROM obligations WHERE account_id=? AND id=?", (account_id, str(obligation_id)),
            ).fetchone()
        else:
            exists = canonical.connection.execute(
                "SELECT 1 FROM reminders WHERE account_id=? AND id=?", (account_id, str(reminder_id)),
            ).fetchone()
        if exists is None:
            raise ValidationError("assistant read query references an unknown item")
    return query


class SQLiteAssistantReadService:
    def __init__(self, canonical: SQLiteCanonicalRepository, account_id: str) -> None:
        self.canonical = canonical
        self.account_id = account_id

    def execute(self, raw: object) -> dict[str, Any]:
        query = validate_read_query(raw, self.canonical, self.account_id)
        kind = AssistantReadKind(query["kind"])
        if kind is AssistantReadKind.AGENDA_WINDOW:
            facts = self._agenda(query["starts_at"], query["ends_at"])
        elif kind is AssistantReadKind.FREE_TIME:
            facts = self._free_time(query["starts_at"], query["ends_at"])
        elif kind is AssistantReadKind.ITEM_LOOKUP:
            facts = self._item(query)
        elif kind is AssistantReadKind.DUE_BEFORE:
            facts = self._due_before(query["before"])
        elif kind is AssistantReadKind.URGENT_TASKS:
            facts = self._urgent()
        elif kind is AssistantReadKind.WHAT_NOW:
            facts = self._what_now()
        else:
            facts = self._plan_explanation(str(query["obligation_id"]))
        return {"query": query, "facts": facts, "mutated_canonical_state": False}

    def _agenda(self, starts_at: str, ends_at: str) -> list[dict[str, Any]]:
        events = self.canonical.connection.execute(
            "SELECT o.id,o.title,o.kind,e.starts_at,e.ends_at FROM obligations o "
            "JOIN events e ON e.obligation_id=o.id WHERE o.account_id=? "
            "AND o.lifecycle_status='ACTIVE' AND e.starts_at<? AND e.ends_at>? ORDER BY e.starts_at,o.id",
            (self.account_id, ends_at, starts_at),
        ).fetchall()
        tasks = self.canonical.connection.execute(
            "SELECT o.id,o.title,o.kind,t.actual_cutoff_at AS at FROM obligations o "
            "JOIN tasks t ON t.obligation_id=o.id WHERE o.account_id=? "
            "AND o.lifecycle_status IN ('ACTIVE','DRAFT') AND t.cutoff_state='KNOWN' "
            "AND t.actual_cutoff_at>=? AND t.actual_cutoff_at<? ORDER BY t.actual_cutoff_at,o.id",
            (self.account_id, starts_at, ends_at),
        ).fetchall()
        reminders = self.canonical.connection.execute(
            "SELECT id,title,'REMINDER' AS kind,remind_at AS at FROM reminders WHERE account_id=? "
            "AND status IN ('SCHEDULED','FIRED') AND remind_at>=? AND remind_at<? ORDER BY remind_at,id",
            (self.account_id, starts_at, ends_at),
        ).fetchall()
        values = [dict(row) for row in events] + [dict(row) for row in tasks] + [dict(row) for row in reminders]
        return sorted(values, key=lambda value: (value.get("starts_at") or value.get("at") or "", value["id"]))

    def _free_time(self, starts_at: str, ends_at: str) -> dict[str, Any]:
        start, end = _dt(starts_at), _dt(ends_at)
        rows = self.canonical.connection.execute(
            "SELECT e.starts_at,e.ends_at FROM obligations o JOIN events e ON e.obligation_id=o.id "
            "WHERE o.account_id=? AND o.lifecycle_status='ACTIVE' AND e.starts_at<? AND e.ends_at>? "
            "ORDER BY e.starts_at,e.ends_at",
            (self.account_id, ends_at, starts_at),
        ).fetchall()
        intervals = sorted((max(start, _dt(row[0])), min(end, _dt(row[1]))) for row in rows)
        busy = timedelta()
        cursor = start
        for interval_start, interval_end in intervals:
            if interval_end <= cursor:
                continue
            if interval_start > cursor:
                cursor = interval_start
            busy += interval_end - cursor
            cursor = interval_end
        window_minutes = int((end - start).total_seconds() // 60)
        return {
            "window_minutes": window_minutes,
            "calendar_busy_minutes": int(busy.total_seconds() // 60),
            "calendar_free_minutes": window_minutes - int(busy.total_seconds() // 60),
            "scope": "fixed_events_only",
        }

    def _item(self, query: dict[str, Any]) -> dict[str, Any]:
        if query.get("reminder_id"):
            row = self.canonical.connection.execute(
                "SELECT id,title,'REMINDER' AS kind,remind_at,status,version FROM reminders "
                "WHERE account_id=? AND id=?", (self.account_id, str(query["reminder_id"])),
            ).fetchone()
        else:
            row = self.canonical.connection.execute(
                "SELECT o.id,o.title,o.kind,o.lifecycle_status AS status,o.version,e.starts_at,e.ends_at,"
                "t.actual_cutoff_at,t.actionable_from,t.target_at FROM obligations o "
                "LEFT JOIN events e ON e.obligation_id=o.id LEFT JOIN tasks t ON t.obligation_id=o.id "
                "WHERE o.account_id=? AND o.id=?", (self.account_id, str(query["obligation_id"])),
            ).fetchone()
        return dict(row)

    def _due_before(self, before: str) -> list[dict[str, Any]]:
        rows = self.canonical.connection.execute(
            "SELECT o.id,o.title,o.importance,t.actual_cutoff_at FROM obligations o "
            "JOIN tasks t ON t.obligation_id=o.id WHERE o.account_id=? "
            "AND o.lifecycle_status IN ('ACTIVE','DRAFT') AND t.cutoff_state='KNOWN' "
            "AND t.actual_cutoff_at<=? ORDER BY t.actual_cutoff_at,o.id LIMIT 100",
            (self.account_id, before),
        ).fetchall()
        return [dict(row) for row in rows]

    def _urgent(self) -> list[dict[str, Any]]:
        threshold = _iso(self.canonical.clock.now() + timedelta(days=3))
        rows = self.canonical.connection.execute(
            "SELECT o.id,o.title,o.importance,t.actual_cutoff_at FROM obligations o "
            "JOIN tasks t ON t.obligation_id=o.id WHERE o.account_id=? "
            "AND o.lifecycle_status IN ('ACTIVE','DRAFT') AND (o.importance IN ('HIGH','CRITICAL') "
            "OR (t.cutoff_state='KNOWN' AND t.actual_cutoff_at<=?)) "
            "ORDER BY CASE o.importance WHEN 'CRITICAL' THEN 0 WHEN 'HIGH' THEN 1 ELSE 2 END,"
            "t.actual_cutoff_at,o.id LIMIT 100",
            (self.account_id, threshold),
        ).fetchall()
        return [dict(row) for row in rows]

    def _what_now(self) -> dict[str, Any] | None:
        now = _iso(self.canonical.clock.now())
        row = self.canonical.connection.execute(
            "SELECT b.block_type,b.starts_at,b.ends_at,b.obligation_id,o.title,b.explanation "
            "FROM current_plans c JOIN plan_blocks b ON b.plan_id=c.plan_id "
            "LEFT JOIN obligations o ON o.account_id=c.account_id AND o.id=b.obligation_id "
            "WHERE c.account_id=? AND b.ends_at>? ORDER BY CASE WHEN b.starts_at<=? THEN 0 ELSE 1 END,b.starts_at LIMIT 1",
            (self.account_id, now, now),
        ).fetchone()
        return None if row is None else dict(row)

    def _plan_explanation(self, obligation_id: str) -> dict[str, Any]:
        row = self.canonical.connection.execute(
            "SELECT b.block_type,b.starts_at,b.ends_at,b.source_constraint_ids_json,b.explanation,"
            "p.feasibility_status,p.input_server_revision FROM current_plans c "
            "JOIN plan_snapshots p ON p.id=c.plan_id JOIN plan_blocks b ON b.plan_id=p.id "
            "WHERE c.account_id=? AND b.obligation_id=? ORDER BY b.starts_at LIMIT 1",
            (self.account_id, obligation_id),
        ).fetchone()
        if row is None:
            return {"scheduled": False, "obligation_id": obligation_id}
        value = dict(row)
        value["source_constraint_ids"] = json.loads(value.pop("source_constraint_ids_json"))
        value.update(scheduled=True, obligation_id=obligation_id)
        return value
