from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.agent.assistant import SQLiteAssistantService
from student_execution_os.agent.model import AuthenticatedPrincipal
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence import SQLiteCanonicalRepository, extras
from student_execution_os.reminders.standalone import SQLiteReminderRepository
from student_execution_os.sync.commands import Commands


NOW = datetime(2026, 9, 26, 9, 0, tzinfo=timezone.utc)


class Provider:
    name = "fake-llm"
    model = "fake-model"

    def __init__(self, command: str, payload: dict):
        self.command = command
        self.payload = payload

    def interpret(self, _text, _context):
        return {"message": "", "actions": [{
            "command": self.command, "payload": self.payload, "confidence": 0.9,
            "unresolved_fields": [], "expected_version": None, "requires_confirmation": False,
        }]}


class RelatedRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "regressions.sqlite")
        self.repo = SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW))
        self.repo.initialize()
        self.repo.create_account("a")
        self.commands = Commands(self.repo, account_id="a", actor=ActorCategory.USER_UI, now=NOW)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def create_task(self, task_id: str, effort=None):
        payload = {"title": task_id}
        if effort is not None:
            payload["estimated_total_effort_minutes"] = effort
        self.commands.task_create(task_id, payload)
        return self.repo.get_task("a", task_id)

    def test_unknown_effort_lifecycle_reconstructs_after_every_transition(self):
        self.create_task("task-draft-complete")
        self.commands.task_complete("task-draft-complete", {})
        self.assertEqual(self.repo.get_task("a", "task-draft-complete").obligation.lifecycle_status.value, "COMPLETED")
        self.commands.task_reopen("task-draft-complete", {})
        self.assertEqual(self.repo.get_task("a", "task-draft-complete").obligation.lifecycle_status.value, "DRAFT")

        self.create_task("task-draft-cancel")
        self.commands.task_cancel("task-draft-cancel", {})
        self.assertEqual(self.repo.get_task("a", "task-draft-cancel").obligation.lifecycle_status.value, "CANCELLED")
        self.commands.task_reopen("task-draft-cancel", {})
        self.assertEqual(self.repo.get_task("a", "task-draft-cancel").obligation.lifecycle_status.value, "DRAFT")

        self.create_task("task-draft-archive")
        self.commands.task_archive("task-draft-archive", {})
        self.assertEqual(self.repo.get_task("a", "task-draft-archive").obligation.lifecycle_status.value, "ARCHIVED")
        self.commands.task_unarchive("task-draft-archive", {})
        self.assertEqual(self.repo.get_task("a", "task-draft-archive").obligation.lifecycle_status.value, "CANCELLED")
        self.commands.task_archive("task-draft-archive", {})
        self.commands.task_restore("task-draft-archive", {})
        self.assertEqual(self.repo.get_task("a", "task-draft-archive").obligation.lifecycle_status.value, "DRAFT")

        self.create_task("task-known-complete", 60)
        self.commands.task_complete("task-known-complete", {})
        self.assertEqual(self.repo.get_task("a", "task-known-complete").obligation.lifecycle_status.value, "COMPLETED")
        self.commands.task_reopen("task-known-complete", {})
        self.assertEqual(self.repo.get_task("a", "task-known-complete").obligation.lifecycle_status.value, "ACTIVE")

    def test_preexisting_closed_null_effort_rows_are_readable(self):
        for i, status in enumerate(("COMPLETED", "CANCELLED", "ARCHIVED"), 1):
            task_id = f"task-legacy-{i}"
            self.create_task(task_id)
            self.repo.connection.execute("UPDATE obligations SET lifecycle_status=? WHERE account_id='a' AND id=?", (status, task_id))
            self.repo.connection.commit()
            task = self.repo.get_task("a", task_id)
            self.assertEqual(task.obligation.lifecycle_status.value, status)
            self.assertIsNone(task.estimated_total_effort_minutes)

    def assistant(self, provider):
        return SQLiteAssistantService(self.repo, AuthenticatedPrincipal("a", "user", "test"), provider)

    def test_explicit_alarm_floor_survives_ai_omission_conflict_and_preserves_wake(self):
        at = "2026-09-27T08:00:00+00:00"
        cases = [
            ("correct", {"delivery": "ALARM"}),
            ("omitted", {}),
            ("conflicting", {"delivery": "PUSH", "wake_check": False}),
        ]
        for label, extra in cases:
            with self.subTest(label=label):
                service = self.assistant(Provider("CREATE_REMINDER", {"title": "AI title", "remind_at": at, **extra}))
                preview = service.interpret("поставить будильник на 11:00")
                payload = preview["actions"][0]["payload"]
                self.assertEqual(payload["delivery"], "ALARM")
                result = service.apply({"batch_id": preview["batch_id"], "action_ids": [preview["actions"][0]["id"]],
                                        "idempotency_key": f"alarm-{label}"})["results"][0]
                stored = SQLiteReminderRepository(self.repo).get("a", result["entity_id"])
                self.assertEqual((stored["delivery"], stored["title"], stored["remind_at"]),
                                 ("ALARM", "AI title", at))

        service = self.assistant(Provider("CREATE_REMINDER", {"title": "Wake", "remind_at": at}))
        wake = service.interpret("wake me up at 11")
        self.assertTrue(wake["actions"][0]["payload"]["wake_check"])
        self.assertTrue(wake["actions"][0]["payload"]["raise_volume"])

    def test_explicit_combined_delivery_and_manual_edit_authority(self):
        at = "2026-09-27T17:00:00+00:00"
        service = self.assistant(Provider("CREATE_REMINDER", {"title": "Call mum", "remind_at": at, "delivery": "PUSH"}))
        preview = service.interpret("напомни позвонить маме в 20 и поставь будильник")
        action = preview["actions"][0]
        self.assertEqual(action["payload"]["delivery"], "PUSH_AND_ALARM")
        applied = service.apply({"batch_id": preview["batch_id"], "action_ids": [action["id"]],
                                 "edits": {action["id"]: {"delivery": "PUSH", "wake_check": False,
                                                            "raise_volume": False}},
                                 "idempotency_key": "manual-push"})
        stored = SQLiteReminderRepository(self.repo).get("a", applied["results"][0]["entity_id"])
        self.assertEqual(stored["delivery"], "PUSH")

    def test_assistant_event_uses_canonical_command_and_keeps_lead(self):
        service = self.assistant(Provider("CREATE_EVENT", {
            "title": "Созвон", "starts_at": "2026-09-27T15:00:00+00:00",
            "ends_at": "2026-09-27T16:00:00+00:00", "remind_before_minutes": 30,
        }))
        preview = service.interpret("созвон в 18:00, напомни за 30 минут")
        action = preview["actions"][0]
        result = service.apply({"batch_id": preview["batch_id"], "action_ids": [action["id"]],
                                "idempotency_key": "event-lead"})["results"][0]
        self.assertEqual(result["entity"]["remind_before_minutes"], 30)
        self.assertEqual(extras.event_lead(self.repo, "a", result["entity_id"]), 30)

    def test_stale_start_is_semantically_superseded(self):
        self.create_task("task-stale-start", 30)
        self.commands.task_complete("task-stale-start", {})
        result = self.commands.task_start("task-stale-start", {})
        self.assertEqual((result.status, result.code, result.entity["status"]),
                         ("NOOP", "SUPERSEDED", "COMPLETED"))

    def test_acknowledgement_emits_alarm_resync_invalidation(self):
        self.commands.reminder_create("reminder-wake-cross-device", {
            "title": "Wake", "remind_at": "2026-09-27T06:00:00+00:00", "delivery": "ALARM",
            "wake_check": True, "raise_volume": True,
        })
        self.repo.connection.execute("UPDATE reminder_messages SET delivery_state='SENT' WHERE account_id='a' AND stage='ALARM_SYNC'")
        self.repo.connection.commit()
        self.commands.reminder_ack("reminder-wake-cross-device", {"stage": "UP"})
        pending = self.repo.connection.execute(
            "SELECT count(*) FROM reminder_messages WHERE account_id='a' AND stage='ALARM_SYNC' AND delivery_state='PENDING'"
        ).fetchone()[0]
        self.assertEqual(pending, 1)


if __name__ == "__main__":
    unittest.main()
