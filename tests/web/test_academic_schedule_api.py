from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

NOW = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
ACCOUNT = "schedule-api-student"
OTHER = "schedule-api-other"
FIXTURE = Path("tests/fixtures/academic_schedule_realistic.ics")


class AcademicScheduleApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "web.sqlite")
        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account(ACCOUNT)
            repo.create_account(OTHER)
        self.client = TestClient(create_app(
            self.database, account_id=ACCOUNT, principal_id="student", now=lambda: NOW
        ))
        self.other = TestClient(create_app(
            self.database, account_id=OTHER, principal_id="other", now=lambda: NOW
        ))

    def tearDown(self):
        self.temp.cleanup()

    def test_upload_reaches_today_repeat_is_idempotent_and_disconnect_is_scoped(self):
        initial = self.client.get("/api/v1/settings/academic-schedule")
        self.assertEqual(initial.status_code, 200)
        self.assertFalse(initial.json()["connected"])
        headers = {
            "content-type": "text/calendar",
            "x-calendar-name": "Example University",
            "x-calendar-timezone": "Europe/Moscow",
        }
        imported = self.client.post(
            "/api/v1/settings/academic-schedule/import", content=FIXTURE.read_bytes(), headers=headers
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["apply_report"]["created"], [
            "course-algorithms-42@example.edu",
            "guest-lecture-2026@example.edu",
            "reading-week@example.edu",
        ])
        today = self.client.get("/api/v1/today")
        self.assertEqual(today.status_code, 200, today.text)
        titles = {
            item["title"] for key in ("current_events", "upcoming_events")
            for item in today.json().get(key, [])
        }
        self.assertIn("Algorithms and Data Structures", titles)
        repeat = self.client.post(
            "/api/v1/settings/academic-schedule/import", content=FIXTURE.read_bytes(), headers=headers
        )
        self.assertEqual(repeat.status_code, 200, repeat.text)
        self.assertEqual(repeat.json()["apply_report"]["created"], [])
        self.assertEqual(len(repeat.json()["apply_report"]["unchanged"]), 3)
        self.assertFalse(self.other.get("/api/v1/settings/academic-schedule").json()["connected"])

        disconnected = self.client.delete("/api/v1/settings/academic-schedule")
        self.assertEqual(disconnected.status_code, 200, disconnected.text)
        self.assertFalse(disconnected.json()["connected"])
        self.assertFalse(self.other.get("/api/v1/settings/academic-schedule").json()["connected"])

    def test_malformed_and_oversize_uploads_do_not_replace_last_good_snapshot(self):
        good = self.client.post(
            "/api/v1/settings/academic-schedule/import",
            content=FIXTURE.read_bytes(),
            headers={"content-type": "text/calendar"},
        )
        self.assertEqual(good.status_code, 200, good.text)
        digest = good.json()["last_content_sha256"]
        bad = self.client.post(
            "/api/v1/settings/academic-schedule/import",
            content=b"this is not a calendar",
            headers={"content-type": "text/calendar"},
        )
        self.assertEqual(bad.status_code, 422, bad.text)
        self.assertEqual(bad.json()["error"]["code"], "PARSE_ERROR")
        self.assertEqual(
            self.client.get("/api/v1/settings/academic-schedule").json()["last_content_sha256"], digest
        )

        huge = self.client.post(
            "/api/v1/settings/academic-schedule/import",
            content=b"x" * (5 * 1024 * 1024 + 1),
            headers={"content-type": "text/calendar"},
        )
        self.assertEqual(huge.status_code, 422, huge.text)
        self.assertNotIn("xxxxx", huge.text)

    def test_percent_encoded_cyrillic_calendar_name_and_bad_interval(self):
        from urllib.parse import quote

        imported = self.client.post(
            "/api/v1/settings/academic-schedule/import",
            content=FIXTURE.read_bytes(),
            headers={"content-type": "text/calendar", "x-calendar-name": quote("Расписание ВШЭ.ics")},
        )
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["display_name"], "Расписание ВШЭ.ics")
        bad = self.client.put(
            "/api/v1/settings/academic-schedule",
            json={"url": "https://calendar.example/feed.ics", "sync_interval_minutes": None},
        )
        self.assertEqual(bad.status_code, 422, bad.text)
        self.assertIn("sync_interval_minutes", bad.text)


if __name__ == "__main__":
    unittest.main()
