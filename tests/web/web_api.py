from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import timedelta
import unittest
from pathlib import Path

from tests.asgi_client import TestClient

from student_execution_os.web.app import create_app
from tests.ui_fixture import ACCOUNT, NOW, seed_ui_database


class WebApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ui.sqlite")
        seed_ui_database(self.db)
        self.client_cm = TestClient(
            create_app(
                self.db,
                account_id=ACCOUNT,
                principal_id="ui-user",
                now=lambda: NOW,
            )
        )
        self.client = self.client_cm.__enter__()

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_health_and_security_headers(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["schema_version"], 11)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_today_exposes_real_feasibility_and_travel_boundary(self):
        body = self.client.get("/api/v1/today").json()
        self.assertEqual(body["plan"]["feasibility_status"], "FEASIBLE")
        self.assertGreaterEqual(len(body["next_actions"]), 1)
        transition = body["travel"]["transitions"][0]
        self.assertEqual(transition["latest_safe_departure"], "2026-09-21T11:05:00+00:00")
        self.assertEqual(transition["safe_duration_minutes"], 45)
        self.assertEqual(transition["arrival_requirement_minutes"], 10)

    def test_plan_distinguishes_canonical_and_derived_blocks(self):
        plan = self.client.get("/api/v1/plan/current").json()
        self.assertTrue(all(e["canonical"] for e in plan["canonical_events"]))
        self.assertTrue(all(b["ownership"] == "DERIVED" for b in plan["blocks"]))
        block_types = {b["type"] for b in plan["blocks"]}
        self.assertTrue({"WORK", "EVENT_PROJECTION", "TRAVEL_TRANSITION", "BUFFER"}.issubset(block_types))
        move = next(e for e in plan["canonical_events"] if e["id"] == "train")
        self.assertEqual(move["location_effect"]["kind"], "MOVE")


    def test_calendar_exposes_canonical_recurrence_rule_and_stable_occurrence_identity(self):
        body = self.client.get("/api/v1/calendar").json()
        template = next(item for item in body["recurring_templates"] if item["id"] == "daily-review")
        self.assertEqual(template["ownership"], "CANONICAL_RULE")
        self.assertEqual(template["recurrence_rule"], "FREQ=DAILY;COUNT=3")
        occurrence = next(item for item in body["occurrences"] if item["template_id"] == "daily-review")
        self.assertEqual(
            occurrence["identity"],
            ["daily-review", occurrence["original_recurrence_id"]],
        )
        self.assertEqual(occurrence["ownership"], "DERIVED_OCCURRENCE")

    def test_recurrence_write_surface_moves_one_occurrence_without_changing_identity(self):
        created = self.client.post(
            "/api/v1/recurrence/templates",
            json={
                "id": "ui-series",
                "title": "UI recurrence",
                "dtstart_local": "2026-09-21T20:00:00",
                "duration_minutes": 30,
                "recurrence_rule": "FREQ=DAILY;COUNT=2",
                "timezone_name": "UTC",
            },
        )
        self.assertEqual(created.status_code, 201)
        original_id = "2026-09-21T20:00:00"
        moved = self.client.post(
            f"/api/v1/recurrence/templates/ui-series/occurrences/{original_id}/override",
            json={
                "action": "MODIFY",
                "replacement_start_local": "2026-09-21T21:00:00",
                "expected_version": 0,
            },
        )
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(moved.json()["original_recurrence_id"], original_id)
        body = self.client.get("/api/v1/calendar").json()
        occurrence = next(
            item for item in body["occurrences"]
            if item["template_id"] == "ui-series" and item["original_recurrence_id"] == original_id
        )
        self.assertEqual(occurrence["identity"], ["ui-series", original_id])
        self.assertEqual(occurrence["starts_at"], "2026-09-21T21:00:00+00:00")

    def test_notification_snooze_is_version_checked_and_does_not_advance_domain_revision(self):
        before_revision = self.client.get("/api/v1/settings/diagnostics").json()["server_revision"]
        item = self.client.get("/api/v1/notifications").json()[0]
        snooze_until = (NOW + timedelta(hours=1)).isoformat()
        response = self.client.post(
            f"/api/v1/notifications/{item['id']}/snooze",
            json={"until": snooze_until, "expected_version": item["version"]},
        )
        self.assertEqual(response.status_code, 200)
        snoozed = response.json()
        self.assertEqual(snoozed["state"], "SNOOZED")
        self.assertEqual(snoozed["snoozed_until"], snooze_until)
        self.assertEqual(snoozed["version"], item["version"] + 1)
        after_revision = self.client.get("/api/v1/settings/diagnostics").json()["server_revision"]
        self.assertEqual(after_revision, before_revision)
        stale = self.client.post(
            f"/api/v1/notifications/{item['id']}/snooze",
            json={"until": (NOW + timedelta(hours=2)).isoformat(), "expected_version": item["version"]},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "VERSION_CONFLICT")

    def test_evidence_surfaces_stale_connector_conflict_and_override(self):
        body = self.client.get("/api/v1/evidence").json()
        google = next(s for s in body["sources"] if s["id"] == "google-calendar")
        self.assertEqual(google["connector"]["health_status"], "STALE")
        open_conflicts = [c for c in body["conflicts"] if c["status"] == "OPEN"]
        self.assertTrue(any(c["entity_ref"] == "conflict-task" for c in open_conflicts))
        effective = {e["entity_ref"]: e for e in body["effective_fields"]}
        self.assertEqual(effective["conflict-task"]["state"], "CONFLICT")
        self.assertEqual(effective["override-task"]["state"], "OVERRIDDEN")
        self.assertEqual(effective["override-task"]["override_id"], "fixture-override")
        self.assertTrue(any(o["id"] == "fixture-override" and o["status"] == "ACTIVE" for o in body["overrides"]))

    def test_task_mutation_rejects_stale_expected_version(self):
        task = next(t for t in self.client.get("/api/v1/tasks").json() if t["id"] == "discrete")
        ok = self.client.patch(
            "/api/v1/tasks/discrete",
            json={"expected_version": task["version"], "remaining_effort_minutes": 60},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["remaining_effort_minutes"], 60)
        stale = self.client.patch(
            "/api/v1/tasks/discrete",
            json={"expected_version": task["version"], "remaining_effort_minutes": 30},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "VERSION_CONFLICT")

    def test_cross_account_isolation_at_application_boundary(self):
        ids = {t["id"] for t in self.client.get("/api/v1/tasks").json()}
        self.assertNotIn("other-secret", ids)
        response = self.client.patch(
            "/api/v1/tasks/other-secret",
            json={"expected_version": 1, "remaining_effort_minutes": 1},
        )
        self.assertEqual(response.status_code, 404)

    def test_private_location_is_redacted_from_default_payload(self):
        response = self.client.get("/api/v1/places")
        body = response.json()
        serialized = json.dumps(body)
        self.assertEqual(body["privacy"]["exact_location_in_default_payload"], False)
        self.assertNotIn("Secret exact address 123", serialized)
        self.assertNotIn("Another secret address", serialized)
        self.assertNotIn("55.75", serialized)
        self.assertNotIn("37.61", serialized)
        self.assertNotIn("address", body["places"][0])
        self.assertNotIn("latitude", body["places"][0])
        self.assertNotIn("longitude", body["places"][0])

    def test_account_export_is_server_scoped_and_explicitly_sensitive(self):
        response = self.client.get("/api/v1/account/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        body = response.json()
        self.assertEqual(body["account_id"], ACCOUNT)
        self.assertTrue(body["contract"]["includes_private_locations"])
        serialized = json.dumps(body)
        self.assertIn("Secret exact address 123", serialized)
        self.assertNotIn("Other account secret task", serialized)
        self.assertNotIn("other-account", serialized)

    def test_account_deletion_is_server_bound_revision_checked_and_retains_only_tombstone(self):
        policy_response = self.client.get("/api/v1/account/deletion-policy")
        self.assertEqual(policy_response.status_code, 200)
        policy = policy_response.json()
        self.assertEqual(policy["account_id"], ACCOUNT)
        self.assertEqual(policy["tombstone_retention_days"], 30)
        self.assertFalse(policy["retained_audit_or_provenance"])
        self.assertEqual(policy["secret_revocation"], "NOT_APPLICABLE_NO_SECRET_STORE")

        wrong = self.client.post(
            "/api/v1/account/delete",
            json={
                "expected_server_revision": policy["server_revision"],
                "confirm_account_id": "other-account",
                "account_id": "other-account",
            },
        )
        self.assertEqual(wrong.status_code, 422)

        response = self.client.post(
            "/api/v1/account/delete",
            json={
                "expected_server_revision": policy["server_revision"],
                "confirm_account_id": ACCOUNT,
                "account_id": "other-account",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["account_id"], ACCOUNT)
        self.assertEqual(body["secret_revocation_status"], "NOT_APPLICABLE_NO_SECRET_STORE")

        connection = sqlite3.connect(self.db)
        try:
            self.assertIsNone(connection.execute("SELECT id FROM accounts WHERE id=?", (ACCOUNT,)).fetchone())
            self.assertIsNotNone(connection.execute("SELECT id FROM accounts WHERE id='other-account'").fetchone())
            tombstone = connection.execute(
                "SELECT account_id,policy_version FROM account_deletion_tombstones WHERE account_id=?",
                (ACCOUNT,),
            ).fetchone()
            self.assertEqual(tombstone, (ACCOUNT, "account-deletion-v1"))
        finally:
            connection.close()

    def test_agent_destructive_action_requires_preview_confirmation(self):
        task = next(t for t in self.client.get("/api/v1/tasks").json() if t["id"] == "discrete")
        preview = self.client.post(
            "/api/v1/agent/cancel/preview",
            json={"obligation_id": "discrete", "expected_version": task["version"]},
        )
        self.assertEqual(preview.status_code, 200)
        proposal = preview.json()
        self.assertTrue(proposal["requires_confirmation"])
        self.assertEqual(proposal["target_entity_id"], "discrete")
        self.assertEqual(proposal["scope"], "one obligation")
        execute = self.client.post(
            "/api/v1/agent/cancel/confirm-execute",
            json={"intent_id": proposal["intent_id"], "idempotency_key": "ui-test-cancel"},
        )
        self.assertEqual(execute.status_code, 200)
        refreshed = next(t for t in self.client.get("/api/v1/tasks").json() if t["id"] == "discrete")
        self.assertEqual(refreshed["status"], "CANCELLED")

    def test_spa_is_served_without_inline_script(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn('src="/assets/app.js"', response.text)
        self.assertNotIn("<script>", response.text)


if __name__ == "__main__":
    unittest.main()
