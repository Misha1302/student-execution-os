from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import VersionConflict
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    EventTimeSemantics,
    Importance,
    ObligationCategory,
)
from student_execution_os.persistence import SQLiteCanonicalRepository


NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


class UndoProvider:
    name = "undo-fixture"
    model = "fixture"

    def interpret(self, text, context):
        item = next(value for value in context["obligations"] if value["id"] == "ariadne")
        if text == "reschedule":
            command = "RESCHEDULE"
            payload = {"obligation_id": "ariadne", "when": "2026-10-02T16:00:00+00:00"}
            provenance = {"obligation_id": "MODEL_EXPLICIT", "when": "MODEL_EXPLICIT"}
            expected_version = item["version"]
        elif text == "reminder":
            command = "UPDATE_EVENT"
            payload = {"obligation_id": "ariadne", "remind_before_minutes": 30}
            provenance = {"obligation_id": "MODEL_EXPLICIT", "remind_before_minutes": "MODEL_EXPLICIT"}
            expected_version = item["version"]
        else:
            command = "UNDO_LAST"
            payload = {}
            provenance = {}
            expected_version = None
        return {"message": text, "actions": [{
            "command": command,
            "payload": payload,
            "confidence": 1,
            "unresolved_fields": [],
            "expected_version": expected_version,
            "requires_confirmation": False,
            "field_provenance": provenance,
        }]}


class AssistantUndoTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = SQLiteCanonicalRepository(
            str(Path(self.temporary.name) / "undo.sqlite"), clock=FrozenClock(NOW),
        )
        self.repository.initialize()
        self.repository.create_account("account")
        self.repository.create_event(
            account_id="account", obligation_id="ariadne", title="Встреча с Ариадной",
            description=None, category=ObligationCategory.MEETING, importance=Importance.NORMAL,
            time_semantics=EventTimeSemantics.FIXED_INTERVAL,
            starts_at=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc),
            attendance_policy=AttendancePolicy.REQUIRED, actor=ActorCategory.USER_UI,
        )
        self.service = SQLiteAssistantService(
            self.repository, AuthenticatedPrincipal("account", "user", "client"), provider=UndoProvider(),
        )

    def tearDown(self):
        self.repository.close()
        self.temporary.cleanup()

    def apply(self, text: str, key: str):
        preview = self.service.interpret(text)
        action = preview["actions"][0]
        request = {
            "batch_id": preview["batch_id"],
            "action_ids": [action["id"]],
            "idempotency_key": key,
        }
        return self.service.apply(request), request

    def test_undo_last_reverts_only_latest_reversible_action_and_replays_safely(self):
        self.apply("reschedule", "reschedule")
        self.apply("reminder", "reminder")
        event = self.repository.get_event("account", "ariadne")
        self.assertEqual(event.interval.starts_at, datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(
            self.repository.connection.execute(
                "SELECT lead_minutes FROM event_reminders WHERE account_id='account' AND event_id='ariadne'"
            ).fetchone()[0],
            30,
        )

        undone, request = self.apply("undo", "undo-reminder")
        self.assertEqual(undone["results"][0]["operation"], "event.update")
        self.assertIsNone(
            self.repository.connection.execute(
                "SELECT lead_minutes FROM event_reminders WHERE account_id='account' AND event_id='ariadne'"
            ).fetchone()
        )
        event = self.repository.get_event("account", "ariadne")
        self.assertEqual(event.interval.starts_at, datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc))
        self.assertTrue(self.service.apply(request)["replayed"])

    def test_undo_refuses_to_overwrite_newer_state(self):
        self.apply("reminder", "reminder")
        current = self.repository.get_event("account", "ariadne")
        self.repository.update_fixed_event(
            account_id="account", obligation_id="ariadne", expected_version=current.obligation.version,
            starts_at=current.interval.starts_at, ends_at=current.interval.ends_at,
            attendance_policy=None, actor=ActorCategory.USER_UI, title="Новая ручная правка",
        )
        preview = self.service.interpret("undo")
        with self.assertRaises(VersionConflict):
            self.service.apply({
                "batch_id": preview["batch_id"],
                "action_ids": [preview["actions"][0]["id"]],
                "idempotency_key": "conflicting-undo",
            })
        self.assertEqual(self.repository.get_event("account", "ariadne").obligation.title, "Новая ручная правка")
        self.assertIsNone(
            self.repository.connection.execute(
                "SELECT undone_at FROM assistant_action_history ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )


if __name__ == "__main__":
    unittest.main()
