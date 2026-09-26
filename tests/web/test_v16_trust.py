"""Schema v16 trust fixes over the real HTTP API: the AI smoke test and the visible
AI/local engine, notification health with a test notification, the one-step
archive restore, and the v15 → v16 migration."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from student_execution_os.agent.credentials import TEST_LIMITER, CredentialCipher
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.reminders.push import PushDispatcher, SendResult
from student_execution_os.web.app import create_app
from student_execution_os.web.queries import TEST_NOTIFICATION_LIMITER
from tests.asgi_client import TestClient

NOW = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)
MASTER = CredentialCipher.generate_key()
MIGRATIONS = Path(__file__).resolve().parents[2] / "src/student_execution_os/persistence/migrations"


def llm_reply(content: str, status: int = 200):
    r = Mock()
    r.status_code = status
    r.json.return_value = {"choices": [{"message": {"content": content}}]}
    return r


def proposal(**action):
    base = {"command": "CREATE_TASK", "payload": {"title": "Эссе"}, "confidence": 0.9,
            "unresolved_fields": [], "expected_version": None, "requires_confirmation": False}
    base.update(action)
    return json.dumps({"message": "ok", "actions": [base]})


class Phone:
    name, configured = "fake-fcm", True

    def __init__(self):
        self.sent = []

    def send(self, token, message, *, data_only=False):
        self.sent.append(message)
        return SendResult(True, f"fcm-{len(self.sent)}")


class TrustTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "v16.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("a")
        self.env = patch.dict(os.environ, {"SEOS_CREDENTIAL_KEY": MASTER})
        self.env.start()
        TEST_LIMITER._hits.clear()
        TEST_NOTIFICATION_LIMITER._hits.clear()
        self.cm = TestClient(create_app(self.db, account_id="a", principal_id="user", now=lambda: NOW))
        self.client = self.cm.__enter__()

    def tearDown(self):
        self.cm.__exit__(None, None, None)
        self.env.stop()
        self.tmp.cleanup()

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def sync(self, op_id, kind, entity, payload=None):
        body = {"operations": [{"op_id": f"op-{op_id}-v16", "type": kind, "entity_id": entity, "payload": payload or {}}]}
        return self.ok(self.client.post("/api/v1/sync", json=body))["results"][0]

    # ---- AI -----------------------------------------------------------------------

    def test_connection_test_needs_the_json_contract_not_just_http_200(self):
        self.ok(self.client.put("/api/v1/settings/llm", json={"provider": "openai", "model": "m", "api_key": "sk-test-0123456789"}))
        with patch("student_execution_os.agent.providers.httpx.post", return_value=llm_reply("Hello! How can I help?")):
            tested = self.ok(self.client.post("/api/v1/settings/llm/test"))
        self.assertFalse(tested["ok"])
        self.assertEqual(tested["status"], "UNSUPPORTED_FORMAT")
        self.assertEqual(tested["reason"], "FORMAT")
        self.assertEqual(tested["checked"], ["key", "endpoint", "model"])
        self.assertEqual(tested["settings"]["credential"]["status"], "UNSUPPORTED_FORMAT")
        with patch("student_execution_os.agent.providers.httpx.post", return_value=llm_reply(proposal())):
            tested = self.ok(self.client.post("/api/v1/settings/llm/test"))
        self.assertTrue(tested["ok"])
        self.assertEqual(tested["checked"], ["key", "endpoint", "model", "format"])
        self.assertIsNotNone(tested["latency_ms"])

    def test_capture_says_which_engine_read_the_text(self):
        preview = self.ok(self.client.post("/api/v1/assistant/interpret", json={"text": "купить молоко"}))
        self.assertEqual((preview["engine"], preview["fallback"], preview["credential_source"]), ("LOCAL", False, "NONE"))
        self.ok(self.client.put("/api/v1/settings/llm", json={"provider": "openai", "model": "gpt-x", "api_key": "sk-test-0123456789"}))
        with patch("student_execution_os.agent.providers.httpx.post", return_value=llm_reply(proposal())):
            preview = self.ok(self.client.post("/api/v1/assistant/interpret", json={"text": "эссе"}))
        self.assertEqual((preview["engine"], preview["model"], preview["fallback"]), ("AI", "gpt-x", False))
        # A well-formed but invalid proposal never reaches the preview, and the switch
        # to the local parser is reported rather than silent.
        bad = proposal(command="COMPLETE_OBLIGATION", payload={"obligation_id": "task-invented"}, expected_version=1)
        with patch("student_execution_os.agent.providers.httpx.post", return_value=llm_reply(bad)):
            preview = self.ok(self.client.post("/api/v1/assistant/interpret", json={"text": "купить молоко"}))
        self.assertEqual((preview["engine"], preview["fallback_reason"]), ("LOCAL", "INVALID_PROPOSAL"))
        self.assertEqual(preview["actions"][0]["payload"]["title"].lower(), "купить молоко")
        with patch("student_execution_os.agent.providers.httpx.post", return_value=llm_reply("not json")):
            preview = self.ok(self.client.post("/api/v1/assistant/interpret", json={"text": "купить молоко"}))
        self.assertEqual((preview["engine"], preview["fallback_reason"]), ("LOCAL", "FORMAT"))

    # ---- notification health --------------------------------------------------------

    def beat(self, push_configured=True):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT OR REPLACE INTO worker_heartbeats(name,beat_at,detail_json) VALUES ('reminder-worker',?,?)",
                         (datetime.now(timezone.utc).isoformat(), json.dumps({"push_configured": push_configured})))
            conn.commit()

    def test_health_never_promises_what_cannot_arrive(self):
        health = self.ok(self.client.get("/api/v1/notifications/health"))
        self.assertEqual(health["reach"], "NONE")
        self.assertIn("WORKER_NEVER_SEEN", health["problems"])
        self.beat(push_configured=False)
        health = self.ok(self.client.get("/api/v1/notifications/health"))
        self.assertEqual(health["reach"], "IN_APP")
        self.assertIn("PUSH_UNCONFIGURED", health["problems"])
        self.beat()
        device = self.ok(self.client.post("/api/v1/mobile/devices", json={
            "token": "tok-1", "capabilities": ["reminder-actions-v1", "wake-alarm-v1"],
            "status": {"notifications": False, "exact_alarms": False}}), 201)
        health = self.ok(self.client.get("/api/v1/notifications/health"))
        self.assertEqual((health["reach"], health["alarm"]), ("IN_APP", "INEXACT"))
        self.assertIn("DEVICE_NOTIFICATIONS_OFF", health["problems"])
        self.ok(self.client.post(f"/api/v1/mobile/devices/{device['id']}/status",
                                 json={"status": {"notifications": True, "exact_alarms": True, "full_screen": True}}))
        health = self.ok(self.client.get("/api/v1/notifications/health"))
        self.assertEqual((health["reach"], health["alarm"], health["problems"]), ("PUSH", "OK", []))
        bad = self.client.post(f"/api/v1/mobile/devices/{device['id']}/status", json={"status": {"notifications": "yes"}})
        self.assertEqual(bad.status_code, 422)

    def test_test_notification_is_delivered_but_is_not_a_reminder(self):
        self.ok(self.client.post("/api/v1/mobile/devices", json={"token": "tok-1", "capabilities": ["reminder-actions-v1"]}), 201)
        sent = self.ok(self.client.post("/api/v1/notifications/test"), 201)
        self.assertEqual(sent["delivery_state"], "PENDING")
        phone = Phone()
        PushDispatcher(self.db, phone).run_once(NOW, "w")
        self.assertEqual(self.ok(self.client.get(f"/api/v1/notifications/{sent['id']}/delivery"))["delivery_state"], "SENT")
        self.assertEqual(len(phone.sent), 1)
        self.assertEqual(self.ok(self.client.get("/api/v1/notifications")), [])  # not in the inbox
        for _ in range(2):
            self.client.post("/api/v1/notifications/test")
        self.assertEqual(self.client.post("/api/v1/notifications/test").status_code, 429)

    # ---- archive lifecycle ---------------------------------------------------------

    def test_archive_is_one_place_and_restore_leaves_it(self):
        def create(tid):
            self.sync(f"c-{tid}", "task.create", tid, {"title": tid, "estimated_total_effort_minutes": 30})
        for tid in ("task-cancelled", "task-done-archived", "task-open-archived"):
            create(tid)
        self.sync("x1", "task.cancel", "task-cancelled")
        self.sync("x2", "task.complete", "task-done-archived")
        self.sync("x3", "task.archive", "task-done-archived")
        self.sync("x4", "task.archive", "task-open-archived")
        restored = self.sync("r1", "task.restore", "task-cancelled")
        self.assertEqual((restored["status"], restored["entity"]["status"]), ("APPLIED", "ACTIVE"))
        restored = self.sync("r2", "task.restore", "task-done-archived")
        self.assertEqual(restored["entity"]["status"], "COMPLETED")
        restored = self.sync("r3", "task.restore", "task-open-archived")
        self.assertEqual(restored["entity"]["status"], "ACTIVE")
        again = self.sync("r4", "task.restore", "task-open-archived")
        self.assertEqual((again["status"], again["code"]), ("NOOP", "ALREADY_OPEN"))
        self.assertEqual(self.sync("r5", "task.restore", "task-done-archived")["code"], "NOT_ARCHIVED")


    def test_connector_sync_now_reports_a_clear_state_and_is_rate_limited(self):
        from student_execution_os.connectors.repository import SQLiteConnectorRepository
        from student_execution_os.reconciliation import SQLiteReconciliationRepository
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            recon = SQLiteReconciliationRepository(repo)
            from student_execution_os.domain.model import ActorCategory
            recon.create_source_system(account_id="a", source_system_id="google-calendar", kind="CALENDAR",
                                       actor=ActorCategory.SYSTEM, policy_context={"provider": "google_calendar"})
            SQLiteConnectorRepository(repo, recon).register(account_id="a", connector_id="gcal-primary",
                                                            source_system_id="google-calendar", provider="GOOGLE_CALENDAR",
                                                            scope="calendar:x", connector_version="1")
        listed = self.ok(self.client.get("/api/v1/connectors"))
        self.assertEqual((listed[0]["id"], listed[0]["can_sync"], listed[0]["last_successful_sync_at"]), ("gcal-primary", True, None))
        result = self.ok(self.client.post("/api/v1/connectors/gcal-primary/sync"))
        self.assertFalse(result["ok"])
        self.assertEqual((result["connector"]["error"], result["connector"]["health"]), ("AUTH_REQUIRED", "UNAVAILABLE"))
        self.assertEqual(self.client.post("/api/v1/connectors/gcal-primary/sync").status_code, 429)
        self.assertEqual(self.client.post("/api/v1/connectors/unknown/sync").status_code, 404)

    def test_commitments_endpoint_lists_tasks_events_and_reminders_together(self):
        self.sync("t1", "task.create", "task-essay-0001", {"title": "Эссе", "estimated_total_effort_minutes": 30})
        self.sync("e1", "event.create", "event-call-0001", {"title": "Созвон", "starts_at": "2026-09-26T15:00:00+00:00",
                                                            "ends_at": "2026-09-26T16:00:00+00:00"})
        self.sync("r1", "reminder.create", "reminder-bread-1", {"title": "Купить хлеб", "remind_at": "2026-09-26T12:00:00+00:00"})
        items = self.ok(self.client.get("/api/v1/commitments?place=open"))["items"]
        self.assertEqual([(i["kind"], i["id"]) for i in items],
                         [("REMINDER", "reminder-bread-1"), ("EVENT", "event-call-0001"), ("TASK", "task-essay-0001")])
        found = self.ok(self.client.get("/api/v1/commitments?q=хлеб"))["items"]
        self.assertEqual([i["id"] for i in found], ["reminder-bread-1"])


class V16MigrationTest(unittest.TestCase):
    def test_v15_database_keeps_ai_keys_and_gains_reminders(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "old.sqlite")
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
                for version, path in enumerate(sorted(MIGRATIONS.glob("0*.sql"))[:15], start=1):
                    conn.executescript(path.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, NOW.isoformat()))
                conn.execute("INSERT INTO accounts(id) VALUES ('a')")
                conn.execute("INSERT INTO llm_credentials(account_id,provider,model,key_ciphertext,key_nonce,key_id,key_hint,"
                             "status,created_at,updated_at) VALUES ('a','openai','m',x'00',x'00','k','sk-••••abcd','OK',?,?)",
                             (NOW.isoformat(), NOW.isoformat()))
                conn.commit()
            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as repo:
                repo.initialize()
                self.assertEqual(repo.schema_version(), 16)
                row = repo.connection.execute("SELECT model,status,key_hint FROM llm_credentials").fetchone()
                self.assertEqual(tuple(row), ("m", "OK", "sk-••••abcd"))
                repo.connection.execute("UPDATE llm_credentials SET status='QUOTA_EXCEEDED'")
                columns = {r[1] for r in repo.connection.execute("PRAGMA table_info(reminder_messages)")}
                self.assertTrue({"reminder_id", "delivery"} <= columns)
                self.assertEqual(repo.connection.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
