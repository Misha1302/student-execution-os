"""Today's "Soon" window for fixed-time events: a rolling 12 hours, not the local day.

The day list (`events`) answers "what is on today"; `upcoming_events` answers "what
comes next" — so a class at 00:30 is next at 23:00, a running event is still there,
and series occurrences appear moved or not at all exactly as their overrides say.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, ObligationCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.recurrence import OccurrenceOverrideAction, SQLiteRecurrenceRepository
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

ACCOUNT = "today-upcoming"
UTC = timezone.utc


class TodayUpcomingEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "today.sqlite")
        self.now = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(self.now)) as repo:
            repo.initialize()
            repo.create_account(ACCOUNT)
        self.client = TestClient(create_app(self.database, account_id=ACCOUNT, principal_id="u", now=lambda: self.now))

    def tearDown(self):
        self.temp.cleanup()

    def event(self, title, starts_at, ends_at):
        response = self.client.post("/api/v1/events", json={
            "title": title, "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat(),
            "category": "MEETING", "attendance_policy": "REQUIRED", "location_effect": {"kind": "NONE"},
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def today(self):
        response = self.client.get("/api/v1/today")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def upcoming_titles(self):
        return [(e["title"], e["starts_at"]) for e in self.today()["upcoming_events"]]

    def test_event_in_an_hour_and_a_running_event_are_upcoming_finished_and_far_ones_are_not(self):
        self.event("Finished", self.now - timedelta(hours=2), self.now - timedelta(hours=1))
        self.event("Running", self.now - timedelta(minutes=30), self.now + timedelta(minutes=30))
        self.event("In an hour", self.now + timedelta(hours=1), self.now + timedelta(hours=2))
        self.event("Tomorrow evening", self.now + timedelta(hours=30), self.now + timedelta(hours=31))
        self.assertEqual([title for title, _ in self.upcoming_titles()], ["Running", "In an hour"])

    def test_late_evening_sees_the_event_after_midnight_the_day_list_does_not(self):
        self.now = datetime(2026, 9, 28, 22, 0, tzinfo=UTC)
        self.event("Late evening", datetime(2026, 9, 28, 22, 30, tzinfo=UTC), datetime(2026, 9, 28, 23, 0, tzinfo=UTC))
        self.event("Crosses midnight", datetime(2026, 9, 28, 23, 30, tzinfo=UTC), datetime(2026, 9, 29, 0, 30, tzinfo=UTC))
        self.event("After midnight", datetime(2026, 9, 29, 0, 30, tzinfo=UTC), datetime(2026, 9, 29, 1, 30, tzinfo=UTC))
        payload = self.today()
        self.assertEqual([e["title"] for e in payload["upcoming_events"]], ["Late evening", "Crosses midnight", "After midnight"])
        if payload["local_date"] == "2026-09-28":  # the day list is the local day (UTC profile)
            self.assertNotIn("After midnight", [e["title"] for e in payload["events"]])
        # Past midnight the crossing event is running and still there.
        self.now = datetime(2026, 9, 29, 0, 10, tzinfo=UTC)
        self.assertEqual([title for title, _ in self.upcoming_titles()], ["Crosses midnight", "After midnight"])

    def test_series_occurrences_follow_moves_and_cancellations(self):
        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(self.now)) as repo:
            recurrence = SQLiteRecurrenceRepository(repo)
            template = recurrence.create_template(
                account_id=ACCOUNT, title="Seminar", dtstart_local=datetime(2026, 9, 21, 14, 0),
                duration_minutes=90, recurrence_rule="FREQ=DAILY;COUNT=20", timezone_name="UTC",
                category=ObligationCategory.LESSON, actor=ActorCategory.USER_UI)
        titles = self.upcoming_titles()
        self.assertEqual(titles, [("Seminar", "2026-09-28T14:00:00+00:00")])

        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(self.now)) as repo:
            SQLiteRecurrenceRepository(repo).set_override(
                account_id=ACCOUNT, template_id=template.id, original_recurrence_id="2026-09-28T14:00:00",
                action=OccurrenceOverrideAction.MODIFY, replacement_start_local=datetime(2026, 9, 28, 17, 0),
                actor=ActorCategory.USER_UI)
        self.assertEqual(self.upcoming_titles(), [("Seminar", "2026-09-28T17:00:00+00:00")])

        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(self.now)) as repo:
            recurrence = SQLiteRecurrenceRepository(repo)
            current = recurrence.get_override(ACCOUNT, template.id, "2026-09-28T14:00:00")
            recurrence.set_override(
                account_id=ACCOUNT, template_id=template.id, original_recurrence_id="2026-09-28T14:00:00",
                action=OccurrenceOverrideAction.CANCEL, actor=ActorCategory.USER_UI, expected_version=current.version)
        self.assertEqual(self.upcoming_titles(), [])


if __name__ == "__main__":
    unittest.main()
