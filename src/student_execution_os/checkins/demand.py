"""Open daily quotas as planning demand (ADR 0034/0036).

The occurrence owns quantity and outcome; this only turns what is still open into
time at the user's own pace, for the planner to reserve. No pace, no demand.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from student_execution_os.planning.model import QuotaDemand

from .model import CheckInKind, OccurrenceStatus
from .repository import SQLiteCheckInRepository


def quota_demands(store: SQLiteCheckInRepository, account_id: str, start: datetime, end: datetime,
                  now: datetime) -> list[QuotaDemand]:
    """Open quota occurrences whose window overlaps [start, end) and whose pace is known."""
    out: list[QuotaDemand] = []
    # A quota scheduled this morning is still open this afternoon: look back a day.
    for template, occurrence in store.occurrences_between(account_id, start - timedelta(days=1), end):
        if template.kind is not CheckInKind.QUOTA or occurrence.status is not OccurrenceStatus.PENDING:
            continue
        if template.unit_effort_seconds is None or not occurrence.target_quantity:
            continue
        remaining = max(0, occurrence.target_quantity - occurrence.quantity_done)
        if remaining <= 0:
            continue
        due = store.window_end(template, occurrence)
        if due <= max(start, now):
            continue
        out.append(QuotaDemand(
            template_id=template.id,
            original_recurrence_id=occurrence.original_recurrence_id,
            title=template.title,
            remaining_quantity=remaining,
            unit=template.unit,
            remaining_minutes=-(-remaining * template.unit_effort_seconds // 60),
            available_from=max(store.scheduled_at(template, occurrence), now),
            due_by=due,
        ))
    return out
