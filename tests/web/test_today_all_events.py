from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

ACCOUNT = "today-events"
NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


class TodayAllEventsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "today.sqlite")
        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account(ACCOUNT)
        self.client = TestClient(create_app(self.database, account_id=ACCOUNT, principal_id="u", now=lambda: NOW))

    def tearDown(self):
        self.temp.cleanup()

    def event(self, title, starts_at, ends_at, category):
        response = self.client.post("/api/v1/events", json={
            "title": title, "starts_at": starts_at, "ends_at": ends_at,
            "category": category, "attendance_policy": "REQUIRED",
            "location_effect": {"kind": "NONE"},
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_today_lists_every_event_intersecting_local_day_and_planner_never_overlaps_them(self):
        events = [
            self.event("Cross midnight", "2026-09-27T23:30:00+00:00", "2026-09-28T00:30:00+00:00", "MEETING"),
            self.event("Morning class", "2026-09-28T08:00:00+00:00", "2026-09-28T09:00:00+00:00", "LESSON"),
            self.event("Current appointment", "2026-09-28T11:30:00+00:00", "2026-09-28T12:30:00+00:00", "PERSONAL_APPOINTMENT"),
            self.event("Evening work", "2026-09-28T16:00:00+00:00", "2026-09-28T17:00:00+00:00", "WORK"),
        ]
        task = self.client.post("/api/v1/tasks", json={
            "title": "Prepare report", "estimated_total_effort_minutes": 90,
            "actual_cutoff": {"state": "KNOWN", "at": "2026-09-28T20:00:00+00:00"},
        })
        self.assertEqual(task.status_code, 201, task.text)

        today = self.client.get("/api/v1/today")
        self.assertEqual(today.status_code, 200, today.text)
        payload = today.json()
        self.assertEqual({e["id"] for e in payload["events"]}, {e["id"] for e in events})

        blocking = [(datetime.fromisoformat(e["starts_at"]), datetime.fromisoformat(e["ends_at"])) for e in events]
        for block in payload["plan"]["blocks"]:
            if block["type"] != "WORK":
                continue
            start, end = datetime.fromisoformat(block["starts_at"]), datetime.fromisoformat(block["ends_at"])
            for event_start, event_end in blocking:
                self.assertFalse(start < event_end and event_start < end, (block, event_start, event_end))


if __name__ == "__main__":
    unittest.main()
