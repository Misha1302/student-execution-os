from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient


ACCOUNT = "daily-account"
NOW = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


class DailyProductApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "daily.sqlite")
        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(NOW)) as repository:
            repository.initialize()
            repository.create_account(ACCOUNT)
        self.client = TestClient(create_app(self.database, account_id=ACCOUNT, principal_id="student", now=lambda: NOW))

    def tearDown(self):
        self.temp.cleanup()

    def test_title_only_capture_refine_activate_and_today_contract(self):
        created = self.client.post("/api/v1/tasks", json={"title": "Unclear essay"})
        self.assertEqual(created.status_code, 201, created.text)
        draft = created.json()
        self.assertEqual(draft["status"], "DRAFT")
        self.assertIsNone(draft["estimated_total_effort_minutes"])
        self.assertFalse(draft["splittable"])
        today = self.client.get("/api/v1/today").json()
        self.assertEqual([item["id"] for item in today["needs_refinement"]], [draft["id"]])
        self.assertNotIn(draft["id"], [item["id"] for item in today["tasks"]])
        refined = self.client.patch(
            f"/api/v1/tasks/{draft['id']}",
            json={"expected_version": draft["version"], "estimated_total_effort_minutes": 45},
        ).json()
        self.assertEqual(refined["status"], "DRAFT")
        self.assertEqual(refined["remaining_effort_minutes"], 45)
        activated = self.client.post(
            f"/api/v1/tasks/{draft['id']}/activate", json={"expected_version": refined["version"]}
        )
        self.assertEqual(activated.status_code, 200, activated.text)
        self.assertEqual(activated.json()["status"], "ACTIVE")

        second = self.client.post("/api/v1/tasks", json={"title": "Still unclear"}).json()
        cancelled = self.client.post(
            f"/api/v1/obligations/{second['id']}/cancel",
            json={"expected_version": second["version"]},
        ).json()
        reopened = self.client.post(
            f"/api/v1/obligations/{second['id']}/reopen",
            json={"expected_version": cancelled["version"]},
        )
        self.assertEqual(reopened.status_code, 200, reopened.text)
        self.assertEqual(reopened.json()["status"], "DRAFT")

        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(NOW)) as repository:
            metric_names = {
                row[0] for row in repository.connection.execute(
                    "SELECT metric_name FROM operational_metrics WHERE account_id=?", (ACCOUNT,)
                ).fetchall()
            }
        self.assertIn("plan_run_latency_ms", metric_names)
        self.assertIn("solver_budget_exhausted_count", metric_names)

    def test_week_month_profile_notification_devices_and_diagnostics(self):
        profile = self.client.get("/api/v1/settings/planning-profile").json()
        self.assertEqual(profile["planning_windows"]["1"], [["08:00", "22:00"]])
        week = self.client.get("/api/v1/outlook?range=week").json()
        month = self.client.get("/api/v1/outlook?range=month").json()
        self.assertEqual(len(week["days"]), 7)
        self.assertEqual(len(month["days"]), 42)
        self.assertEqual(len(month["weeks"]), 6)
        preferences = self.client.get("/api/v1/notification-preferences").json()
        updated = self.client.patch("/api/v1/notification-preferences", json={
            "expected_version": preferences["version"], "timezone": "Europe/Moscow",
            "quiet_hours": {"starts_local": "23:00", "ends_local": "07:00"},
        })
        self.assertEqual(updated.status_code, 200, updated.text)
        device = self.client.post("/api/v1/mobile/devices", json={"token": "fcm-token", "label": "phone"}).json()
        self.assertNotIn("token", device)
        revoked = self.client.post(
            f"/api/v1/mobile/devices/{device['id']}/revoke", json={"expected_version": device["version"]}
        ).json()
        self.assertFalse(revoked["active"])
        diagnostics = self.client.get("/api/v1/settings/diagnostics").json()
        self.assertEqual(diagnostics["external_capabilities"]["fcm"], "UNCONFIGURED")

    def test_assistant_preview_apply_attachment_and_saved_view(self):
        preview = self.client.post("/api/v1/assistant/interpret", json={"text": "task: Read chapter 30 minutes"})
        self.assertEqual(preview.status_code, 200, preview.text)
        batch = preview.json()
        self.assertFalse(batch["mutated_canonical_state"])
        action = batch["actions"][0]
        applied = self.client.post("/api/v1/assistant/apply", json={
            "batch_id": batch["batch_id"], "action_ids": [action["id"]],
            "confirmed_action_ids": [], "idempotency_key": "assistant-create-1",
        })
        self.assertEqual(applied.status_code, 200, applied.text)
        replay = self.client.post("/api/v1/assistant/apply", json={
            "batch_id": batch["batch_id"], "action_ids": [action["id"]],
            "confirmed_action_ids": [], "idempotency_key": "assistant-create-1",
        }).json()
        self.assertTrue(replay["replayed"])
        task_id = applied.json()["results"][0]["entity_id"]

        event_preview = self.client.post("/api/v1/assistant/interpret", json={
            "text": "event: Seminar | 2026-09-24T12:00:00+00:00 | 2026-09-24T13:00:00+00:00"
        }).json()
        event_action = event_preview["actions"][0]
        event_applied = self.client.post("/api/v1/assistant/apply", json={
            "batch_id": event_preview["batch_id"], "action_ids": [event_action["id"]],
            "confirmed_action_ids": [], "idempotency_key": "assistant-event-1",
        })
        self.assertEqual(event_applied.status_code, 200, event_applied.text)
        self.assertEqual(event_applied.json()["results"][0]["status"], "ACTIVE")

        uploaded = self.client.post("/api/v1/attachments", json={
            "owner_kind": "OBLIGATION", "owner_id": task_id, "original_name": "proof.html",
            "mime_type": "text/html", "content_base64": base64.b64encode(b"<script>x</script>").decode(),
        })
        self.assertEqual(uploaded.status_code, 201, uploaded.text)
        item = uploaded.json()
        download = self.client.get(f"/api/v1/attachments/{item['id']}/download")
        self.assertEqual(download.headers["content-type"], "application/octet-stream")
        self.assertTrue(download.headers["content-disposition"].startswith("attachment"))
        self.assertEqual(download.headers["x-content-type-options"], "nosniff")

        definition = {"schema_version": 1, "filters": {"status": ["ACTIVE"]},
                      "sort": [{"field": "deadline", "direction": "asc"}], "grouping": "status"}
        view = self.client.post("/api/v1/tasks/saved-views", json={"name": "My active", "definition": definition})
        self.assertEqual(view.status_code, 201, view.text)
        invalid = self.client.post("/api/v1/tasks/saved-views", json={
            "name": "Bad", "definition": {**definition, "unknown": True},
        })
        self.assertEqual(invalid.status_code, 422)


if __name__ == "__main__":
    unittest.main()
