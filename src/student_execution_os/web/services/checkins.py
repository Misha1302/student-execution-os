"""Check-ins (today, history) and reminder series."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from student_execution_os.checkins import (
    CheckInKind,
    OccurrenceStatus,
    SQLiteCheckInRepository,
    occurrence_payload,
    template_payload,
)
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.reminders.series import SQLiteReminderSeriesRepository, series_payload

from .common import _jsonify

from .base import ApplicationService

HISTORY_DAYS = 30


def day_checkins(repo: SQLiteCanonicalRepository, account_id: str, day_start: datetime, day_end: datetime,
                 now: datetime) -> dict[str, Any]:
    """Today's check-in occurrences plus what the open quotas still ask for.

    Quantity stays the truth; time is reported only where the user gave a pace, and
    the rest is counted as unknown instead of being guessed.
    """
    store = SQLiteCheckInRepository(repo)
    store.ensure_horizon(account_id, now)
    items = [occurrence_payload(store, template, occurrence)
             for template, occurrence in store.occurrences_between(account_id, day_start, day_end)]
    open_quotas = [item for item in items if item["checkin_kind"] == CheckInKind.QUOTA.value
                   and item["status"] == OccurrenceStatus.PENDING.value]
    known = [item["remaining_effort_minutes"] for item in open_quotas if item["remaining_effort_minutes"] is not None]
    return {
        "items": items,
        "quota_demand": {
            "known_minutes": sum(known),
            "unknown_effort_count": sum(1 for item in open_quotas if item["remaining_effort_minutes"] is None
                                        and item["remaining_quantity"]),
        },
    }


def _history(store: SQLiteCheckInRepository, account_id: str, template, now: datetime, days: int) -> list[dict[str, Any]]:
    zone = ZoneInfo(template.timezone_name)
    today = now.astimezone(zone).date()
    start = datetime.combine(today - timedelta(days=days - 1), datetime.min.time(), zone)
    end = datetime.combine(today + timedelta(days=1), datetime.min.time(), zone)
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for t, occurrence in store.occurrences_between(account_id, start, end):
        if t.id != template.id:
            continue
        local_day = store.scheduled_at(t, occurrence).astimezone(zone).date().isoformat()
        grouped.setdefault(local_day, []).append(occurrence_payload(store, t, occurrence))
    out = []
    for local_day in sorted(grouped, reverse=True):
        items = grouped[local_day]
        counted = [item for item in items if item["status"] != OccurrenceStatus.CANCELLED.value]
        out.append({
            "local_date": local_day,
            "done": sum(1 for item in counted if item["status"] == OccurrenceStatus.DONE.value),
            "total": len(counted),
            "open": sum(1 for item in counted if item["status"] == OccurrenceStatus.PENDING.value),
            "occurrences": items,
        })
    return out


class CheckInQueries(ApplicationService):
    """Check-ins (today, history) and reminder series."""

    def _day_bounds(self, zone_name: str) -> tuple[datetime, datetime]:
        zone = ZoneInfo(zone_name)
        today = self._now().astimezone(zone).date()
        start = datetime.combine(today, datetime.min.time(), zone)
        return start, start + timedelta(days=1)

    def checkins(self) -> dict[str, Any]:
        with self._repo() as repo:
            store = SQLiteCheckInRepository(repo)
            now = self._now()
            store.ensure_horizon(self.account_id, now)
            templates = []
            for template in store.list_templates(self.account_id):
                start, end = self._day_bounds(template.timezone_name)
                today = [occurrence_payload(store, t, o)
                         for t, o in store.occurrences_between(self.account_id, start, end) if t.id == template.id]
                upcoming = [occurrence_payload(store, t, o)
                            for t, o in store.occurrences_between(self.account_id, end, now + timedelta(hours=48))
                            if t.id == template.id][:3]
                history = _history(store, self.account_id, template, now, 7)
                templates.append({**template_payload(template), "today": today, "upcoming": upcoming,
                                  "recent_days": [{key: day[key] for key in ("local_date", "done", "total", "open")}
                                                  for day in history]})
            series_store = SQLiteReminderSeriesRepository(repo)
            series_store.ensure_horizon(self.account_id, now)
            series = []
            for item in series_store.list(self.account_id):
                next_row = repo.connection.execute(
                    "SELECT r.id,r.remind_at,r.status FROM reminder_series_occurrences o JOIN reminders r ON r.id=o.reminder_id "
                    "WHERE o.account_id=? AND o.series_id=? AND r.status IN ('SCHEDULED','FIRED') ORDER BY r.remind_at LIMIT 1",
                    (self.account_id, item.id)).fetchone()
                series.append({**series_payload(item), "next": None if next_row is None else dict(next_row)})
            return {"now": _jsonify(now), "checkins": templates, "reminder_series": series}

    def checkin(self, template_id: str) -> dict[str, Any]:
        with self._repo() as repo:
            store = SQLiteCheckInRepository(repo)
            now = self._now()
            store.ensure_horizon(self.account_id, now)
            template = store.get_template(self.account_id, template_id)
            history = _history(store, self.account_id, template, now, HISTORY_DAYS)
            # The detail shows a bounded window, and says so: it is not the full history.
            return {"now": _jsonify(now), **template_payload(template), "history": history, "history_days": HISTORY_DAYS}

    def reminder_series(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            store = SQLiteReminderSeriesRepository(repo)
            store.ensure_horizon(self.account_id, self._now())
            return [series_payload(item) for item in store.list(self.account_id)]

