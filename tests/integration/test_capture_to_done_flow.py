"""End-to-end regression tests for the main product flow over the real HTTP API:

text → assistant interpret → preview → apply → stored task → Today → reminder →
Snooze (from a notification) → the reminder comes back later → Start → Done →
completed task opens → Reopen.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import ValidationError
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.reliability.retention import purge_expired
from student_execution_os.reminders import ReminderEngine, ReminderStore
from student_execution_os.reminders.push import PushDispatcher, SendResult, fcm_message
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

# Wednesday 2026-09-23 10:00 in Moscow.
START = datetime(2026, 9, 23, 7, 0, tzinfo=timezone.utc)
PHRASE = "В пятницу к шести сдать лабораторную по физике, займёт часа два, это важно"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class CaptureToDoneFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "flow.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(START)) as repo:
            repo.initialize()
            repo.create_account("a")
        self.clock = Clock(START)
        self.client_cm = TestClient(create_app(self.db, account_id="a", principal_id="user", now=self.clock))
        self.client = self.client_cm.__enter__()
        prefs = self.client.patch("/api/v1/notification-preferences", json={"timezone": "Europe/Moscow"})
        self.assertEqual(prefs.status_code, 200, prefs.text)
        self.pushed: list[tuple[dict, bool]] = []
        pushed = self.pushed

        class Phone:
            name, configured = "fake-fcm", True

            def send(self, token, message, *, data_only=False):
                pushed.append((message, data_only))
                return SendResult(True, f"fcm-{len(pushed)}")

        self.phone = Phone()

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        self.tmp.cleanup()

    # ---- helpers ------------------------------------------------------------------------

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def op(self, op_id, kind, entity, payload=None):
        result = self.ok(self.client.post("/api/v1/sync", json={"operations": [
            {"op_id": op_id, "type": kind, "entity_id": entity, "payload": payload or {}}]}))["results"][0]
        self.assertIn(result["status"], {"APPLIED", "NOOP"}, result)
        return result

    def tick(self, at: datetime) -> list[dict]:
        """One reminder-worker iteration: decide, then deliver to the phone."""
        self.clock.now = at
        ReminderEngine(self.db).tick("a", at)
        PushDispatcher(self.db, self.phone).run_once(at)
        return self.ok(self.client.get("/api/v1/notifications"))

    def capture(self, text: str, **edits) -> dict:
        preview = self.ok(self.client.post("/api/v1/assistant/interpret", json={
            "text": text, "context": {"timezone": "Europe/Moscow", "locale": "ru"}}))
        self.assertFalse(preview["mutated_canonical_state"])
        action = preview["actions"][0]
        body = {"batch_id": preview["batch_id"], "action_ids": [action["id"]], "confirmed_action_ids": [],
                "idempotency_key": f"capture-{preview['batch_id']}"}
        if edits:
            body["edits"] = {action["id"]: edits}
        applied = self.ok(self.client.post("/api/v1/assistant/apply", json=body))
        return {"preview": preview, "action": action, "applied": applied,
                "task": self.ok(self.client.get(f"/api/v1/tasks/{applied['results'][0]['entity_id']}"))}

    # ---- the acceptance scenario --------------------------------------------------------

    def test_spoken_task_is_stored_planned_reminded_snoozed_started_done_and_reopened(self):
        captured = self.capture(PHRASE)
        self.assertEqual(captured["action"]["unresolved_fields"], [])
        task = captured["task"]
        task_id = task["id"]
        # What the preview showed is what was stored (Friday 18:00 Moscow = 15:00 UTC).
        self.assertEqual(task["title"], "Сдать лабораторную по физике")
        self.assertEqual(task["actual_cutoff"]["state"], "KNOWN")
        self.assertEqual(task["actual_cutoff"]["at"], "2026-09-25T15:00:00+00:00")
        self.assertEqual((task["estimated_total_effort_minutes"], task["remaining_effort_minutes"]), (120, 120))
        self.assertEqual((task["importance"], task["category"], task["status"]), ("HIGH", "HOMEWORK", "ACTIVE"))
        self.assertTrue(task["splittable"])

        today = self.ok(self.client.get("/api/v1/today"))
        self.assertIn(task_id, [x["id"] for x in today["tasks"]])
        self.assertEqual(today["plan"]["feasibility_status"], "FEASIBLE")
        self.assertTrue(any(b["obligation_id"] == task_id and b["type"] == "WORK" for b in today["plan"]["blocks"]))

        # Thursday 19:00 Moscow: less than a day left and nothing started → a reminder.
        thursday = datetime(2026, 9, 24, 16, 0, tzinfo=timezone.utc)
        inbox = self.tick(thursday)
        self.assertEqual(len(inbox), 1)
        first = inbox[0]
        self.assertEqual(first["task_ids"], [task_id])
        self.assertTrue({"SNOOZE_60", "START"} <= {a["id"] for a in first["actions"]})

        # Snooze 60 pressed on the notification (the device sends a sync operation).
        snoozed = self.op(f"push-{first['id']}-SNOOZE_60", "reminder.snooze", task_id,
                          {"minutes": 60, "reminder_message_id": first["id"]})
        self.assertEqual(snoozed["entity"]["remind_at"], (thursday + timedelta(minutes=60)).isoformat())
        inbox = self.ok(self.client.get("/api/v1/notifications"))
        self.assertEqual(inbox[0]["acted_action"], "SNOOZE")
        self.assertEqual(len(self.tick(thursday + timedelta(minutes=30))), 1, "nothing new while snoozed")
        inbox = self.tick(thursday + timedelta(minutes=60))
        self.assertEqual(len(inbox), 2, "the reminder comes back when the snooze ends")
        again = inbox[0]
        self.assertEqual(again["created_at"], (thursday + timedelta(minutes=60)).isoformat())
        self.assertEqual(again["task_ids"], [task_id])

        # Start from the notification, then Done.
        started = self.op(f"push-{again['id']}-START", "task.start", task_id, {"reminder_message_id": again["id"]})
        self.assertIsNotNone(started["entity"]["started_at"])
        done = self.op(f"push-{again['id']}-DONE", "task.complete", task_id, {"reminder_message_id": again["id"]})
        self.assertEqual(done["entity"]["status"], "COMPLETED")
        self.assertEqual(len(self.tick(thursday + timedelta(hours=3))), 2, "a completed task is not reminded")

        completed = [x for x in self.ok(self.client.get("/api/v1/tasks")) if x["status"] == "COMPLETED"]
        self.assertEqual([x["id"] for x in completed], [task_id])
        self.assertEqual(self.ok(self.client.get(f"/api/v1/tasks/{task_id}"))["status"], "COMPLETED")
        reopened = self.op("reopen-1", "task.reopen", task_id)
        self.assertEqual(reopened["entity"]["status"], "ACTIVE")
        self.assertGreater(reopened["entity"]["remaining_effort_minutes"], 0)

    def test_every_proposal_field_is_stored_or_rejected(self):
        class Provider:
            name = "fake-llm"

            def __init__(self, payload):
                self.payload = payload

            def interpret(self, _text, _context):
                return {"message": "", "actions": [{"command": "CREATE_TASK", "payload": self.payload, "confidence": 0.9,
                                                    "unresolved_fields": [], "expected_version": None,
                                                    "requires_confirmation": False}]}

        full = {
            "title": "Essay", "description": "Chapter 2", "category": "HOMEWORK", "importance": "CRITICAL",
            "estimated_total_effort_minutes": 150, "remaining_effort_minutes": 150,
            "actual_cutoff": {"state": "KNOWN", "at": "2026-09-26T18:00:00+03:00", "boundary": "INCLUSIVE"},
            "target_at": "2026-09-25T20:00:00+03:00", "actionable_from": "2026-09-24T09:00:00+03:00",
            "remind_at": "2026-09-24T09:00:00+03:00", "splittable": True, "min_chunk_minutes": 30, "max_chunk_minutes": 60,
        }
        principal = AuthenticatedPrincipal("a", "user", "test")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(START)) as repo:
            repo.initialize()
            service = SQLiteAssistantService(repo, principal, Provider(full))
            preview = service.interpret("essay")
            applied = service.apply({"batch_id": preview["batch_id"], "action_ids": [preview["actions"][0]["id"]],
                                     "idempotency_key": "full-1"})
            audit = repo.connection.execute("SELECT actor_category FROM audit_changes WHERE entity_id=?",
                                            (applied["results"][0]["entity_id"],)).fetchone()
            self.assertEqual(audit["actor_category"], "USER_VIA_LLM")
            bad_payloads = [
                {"title": "x", "actual_cutoff": {"state": "KNOWN"}},
                {"title": "x", "actual_cutoff": {"state": "SOON"}},
                {"title": "x", "actual_cutoff": {"state": "KNOWN", "at": "2026-09-26T18:00:00"}},
                {"title": "x", "actual_cutoff": {"state": "ABSENT", "when": "never"}},
                {"title": "x", "target_at": "friday"},
                {"title": "x", "remind_at": "2026-09-01T09:00:00+00:00"},
                {"title": "x", "splittable": "yes"},
                {"title": "x", "min_chunk_minutes": 90, "max_chunk_minutes": 30},
                {"title": "x", "category": "HOBBY"},
            ]
            for payload in bad_payloads:
                with self.subTest(payload=payload), self.assertRaises(ValidationError):
                    SQLiteAssistantService(repo, principal, Provider(payload)).interpret("x")
        stored = self.ok(self.client.get(f"/api/v1/tasks/{applied['results'][0]['entity_id']}"))
        self.assertEqual(stored["description"], "Chapter 2")
        self.assertEqual((stored["category"], stored["importance"]), ("HOMEWORK", "CRITICAL"))
        self.assertEqual(stored["actual_cutoff"]["at"], "2026-09-26T15:00:00+00:00")
        self.assertEqual(stored["target_at"], "2026-09-25T17:00:00+00:00")
        self.assertEqual(stored["actionable_from"], "2026-09-24T06:00:00+00:00")
        self.assertEqual(stored["remind_at"], "2026-09-24T06:00:00+00:00")
        self.assertEqual((stored["estimated_total_effort_minutes"], stored["splittable"],
                          stored["min_chunk_minutes"], stored["max_chunk_minutes"]), (150, True, 30, 60))

    def test_unanswered_questions_block_apply_until_answered_even_with_dont_know(self):
        preview = self.ok(self.client.post("/api/v1/assistant/interpret", json={"text": "купить молоко"}))
        action = preview["actions"][0]
        self.assertEqual(action["payload"]["title"], "Купить молоко")
        self.assertEqual(sorted(action["unresolved_fields"]), ["actual_cutoff", "estimated_total_effort_minutes"])
        base = {"batch_id": preview["batch_id"], "action_ids": [action["id"]], "confirmed_action_ids": []}
        refused = self.client.post("/api/v1/assistant/apply", json={**base, "idempotency_key": "milk-0"})
        self.assertEqual(refused.status_code, 422)
        # "Не знаю" for both questions: an honest draft without a deadline.
        dont_know = self.ok(self.client.post("/api/v1/assistant/apply", json={
            **base, "idempotency_key": "milk-1",
            "edits": {action["id"]: {"estimated_total_effort_minutes": None, "actual_cutoff": {"state": "UNKNOWN"}}}}))
        draft = self.ok(self.client.get(f"/api/v1/tasks/{dont_know['results'][0]['entity_id']}"))
        self.assertEqual((draft["status"], draft["actual_cutoff"]["state"]), ("DRAFT", "UNKNOWN"))
        # Answers from the chips: 30 minutes, no deadline — and a corrected title.
        second = self.ok(self.client.post("/api/v1/assistant/interpret", json={"text": "купить молоко"}))
        answered = self.ok(self.client.post("/api/v1/assistant/apply", json={
            "batch_id": second["batch_id"], "action_ids": [second["actions"][0]["id"]], "idempotency_key": "milk-2",
            "edits": {second["actions"][0]["id"]: {"title": "Купить молоко и хлеб", "estimated_total_effort_minutes": 30,
                                                   "actual_cutoff": {"state": "ABSENT"}}}}))
        task = self.ok(self.client.get(f"/api/v1/tasks/{answered['results'][0]['entity_id']}"))
        self.assertEqual((task["title"], task["status"], task["estimated_total_effort_minutes"], task["actual_cutoff"]["state"]),
                         ("Купить молоко и хлеб", "ACTIVE", 30, "ABSENT"))
        invalid_edit = self.client.post("/api/v1/assistant/apply", json={
            "batch_id": second["batch_id"], "action_ids": [second["actions"][0]["id"]], "idempotency_key": "milk-3",
            "edits": {second["actions"][0]["id"]: {"estimated_total_effort_minutes": -5}}})
        self.assertEqual(invalid_edit.status_code, 422)

    def test_requested_reminder_fires_at_its_time_even_in_quiet_hours_and_uses_native_actions(self):
        self.ok(self.client.post("/api/v1/mobile/devices", json={
            "token": "device-new", "label": "phone", "capabilities": ["reminder-actions-v1", "unknown-cap"]}), 201)
        self.ok(self.client.post("/api/v1/mobile/devices", json={"token": "device-old", "label": "old"}), 201)
        captured = self.capture("Напомни сегодня в 23:15 отправить отчёт, 15 минут", estimated_total_effort_minutes=None)
        task = captured["task"]
        self.assertEqual(task["remind_at"], "2026-09-23T20:15:00+00:00")
        self.assertEqual(task["actual_cutoff"]["state"], "ABSENT")
        self.assertEqual(self.tick(datetime(2026, 9, 23, 20, 0, tzinfo=timezone.utc)), [])
        inbox = self.tick(datetime(2026, 9, 23, 20, 15, tzinfo=timezone.utc))
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["stage"], "REMINDER")
        self.assertEqual(inbox[0]["title"], "Напоминаю: «Отправить отчёт»")
        self.assertEqual([a["id"] for a in inbox[0]["actions"]], ["START", "DONE", "SNOOZE_30"])
        self.assertEqual(inbox[0]["delivery_state"], "SENT", "an explicitly requested reminder is not held by quiet hours")
        self.assertIsNone(self.ok(self.client.get(f"/api/v1/tasks/{task['id']}"))["remind_at"])

        # The new app renders buttons itself (data-only); an old install gets a system notification.
        self.assertEqual(sorted(flag for _, flag in self.pushed), [False, True])
        payload = self.pushed[0][0]
        self.assertEqual(payload["task_title"], "Отправить отчёт")
        self.assertEqual(payload["labels"]["snoozed"], "Напомню в {time}")
        native = fcm_message("tok", payload, data_only=True)["message"]
        self.assertNotIn("notification", native)
        self.assertEqual(native["data"]["render"], "native")
        self.assertEqual(json.loads(native["data"]["actions"])[0]["id"], "START")
        legacy = fcm_message("tok", payload)["message"]
        self.assertEqual(legacy["notification"]["title"], "Напоминаю: «Отправить отчёт»")

        # A prompt the engine decided outside quiet hours still waits for them to end.
        with SQLiteCanonicalRepository(self.db) as repo:
            repo.initialize()
            ReminderStore(repo).add_message("a", stage="START_NOW", task_ids=[task["id"]], dedupe_key="d-quiet",
                                            content={"title": "t", "body": "b", "deep_link": "/today", "actions": []},
                                            now=datetime(2026, 9, 23, 18, 30, tzinfo=timezone.utc))
        stats = PushDispatcher(self.db, self.phone).run_once(datetime(2026, 9, 23, 20, 30, tzinfo=timezone.utc))
        self.assertEqual((stats["sent"], stats["retry"]), (0, 1))

    def test_rescheduling_after_a_fired_reminder_does_not_fire_it_again(self):
        captured = self.capture("Напомни сегодня в 12:00 позвонить в деканат, 15 минут", estimated_total_effort_minutes=15)
        task_id = captured["task"]["id"]
        noon = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(len(self.tick(noon)), 1)
        self.clock.now = noon + timedelta(minutes=5)
        self.op("deadline-1", "task.update", task_id, {"actual_cutoff": {"state": "KNOWN", "at": "2026-09-30T15:00:00+00:00"}})
        self.assertEqual(len(self.tick(noon + timedelta(minutes=20))), 1, "the answered reminder does not come back")

    def test_expired_assistant_input_is_deleted(self):
        self.ok(self.client.post("/api/v1/assistant/interpret", json={"text": "купить молоко, мой email a@b.c"}))
        with SQLiteCanonicalRepository(self.db) as repo:
            repo.initialize()
            stored = repo.connection.execute("SELECT redacted_input FROM assistant_batches").fetchone()[0]
        self.assertNotIn("a@b.c", stored)
        self.assertEqual(purge_expired(self.db, START + timedelta(minutes=10))["assistant_batches"], 0)
        self.assertEqual(purge_expired(self.db, START + timedelta(minutes=31))["assistant_batches"], 1)
        # A later preview also sweeps this account's expired previews on the way in.
        self.ok(self.client.post("/api/v1/assistant/interpret", json={"text": "купить хлеб"}))
        self.clock.now = START + timedelta(hours=1)
        self.ok(self.client.post("/api/v1/assistant/interpret", json={"text": "купить сыр"}))
        with SQLiteCanonicalRepository(self.db) as repo:
            repo.initialize()
            self.assertEqual(repo.connection.execute("SELECT count(*) FROM assistant_batches").fetchone()[0], 1)
        policy = self.ok(self.client.get("/api/v1/account/deletion-policy"))
        self.assertEqual(policy["retention"]["reminder_inbox_days"], 90)


if __name__ == "__main__":
    unittest.main()
