from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import AuthorizationDenied
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    EventTimeSemantics,
    Importance,
    ObligationCategory,
)
from student_execution_os.persistence import SQLiteCanonicalRepository


NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


class SessionProvider:
    name = "session-fixture"
    model = "fixture"

    def interpret(self, text, context):
        item = next(value for value in context["obligations"] if value["id"] == "ariadne")
        if "первый" in text:
            return {"message": "first", "actions": [{
                "command": "UPDATE_EVENT",
                "payload": {"obligation_id": item["id"], "title": "Созвон с Ариадной"},
                "confidence": 0.9,
                "unresolved_fields": [],
                "expected_version": item["version"],
                "requires_confirmation": False,
                "field_provenance": {"obligation_id": "MODEL_EXPLICIT", "title": "MODEL_INFERRED"},
            }]}
        previous = context["assistant_session"]["previous_actions"][0]
        if "название" in text:
            return {"message": "latest correction", "actions": [{
                "command": "UPDATE_EVENT",
                "payload": {
                    "obligation_id": item["id"],
                    "title": "Финальный созвон с Ариадной",
                    "remind_before_minutes": previous["payload"]["remind_before_minutes"],
                },
                "confidence": 0.95,
                "unresolved_fields": [],
                "expected_version": item["version"],
                "requires_confirmation": False,
                "field_provenance": {
                    "obligation_id": "MODEL_EXPLICIT",
                    "title": "MODEL_EXPLICIT",
                    "remind_before_minutes": "MODEL_INFERRED",
                },
            }]}
        self.follow_up_context = context
        return {"message": "follow-up", "actions": [{
            "command": "UPDATE_EVENT",
            "payload": {
                "obligation_id": item["id"],
                "title": "Wrong inferred title",
                "remind_before_minutes": 30,
            },
            "confidence": 0.94,
            "unresolved_fields": [],
            "expected_version": item["version"],
            "requires_confirmation": False,
            "field_provenance": {
                "obligation_id": "MODEL_EXPLICIT",
                "title": "MODEL_INFERRED",
                "remind_before_minutes": "MODEL_EXPLICIT",
            },
        }]}


class AssistantSessionTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = SQLiteCanonicalRepository(
            str(Path(self.temporary.name) / "session.sqlite"), clock=FrozenClock(NOW),
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

    def tearDown(self):
        self.repository.close()
        self.temporary.cleanup()

    def test_voice_follow_up_preserves_unrelated_user_edit_and_latest_explicit_correction_wins(self):
        provider = SessionProvider()
        principal = AuthenticatedPrincipal("account", "user", "client")
        service = SQLiteAssistantService(self.repository, principal, provider=provider)
        first = service.interpret("первый ход")
        first_action = first["actions"][0]
        manual_title = "Созвон с Ариадной (важно)"
        second = service.interpret(
            "И напомни за полчаса",
            {
                "source": "VOICE",
                "previous_batch_id": first["batch_id"],
                "previous_edits": {first_action["id"]: {"title": manual_title}},
            },
        )
        action = second["actions"][0]
        self.assertEqual(action["payload"]["title"], manual_title)
        self.assertEqual(action["payload"]["remind_before_minutes"], 30)
        self.assertEqual(action["provenance"]["fields"]["title"], "USER_EDIT")
        self.assertEqual(action["provenance"]["input"], "voice-transcript")
        self.assertNotIn("redacted_input", provider.follow_up_context["assistant_session"])

        third = service.interpret(
            "Нет, название — Финальный созвон с Ариадной",
            {"previous_batch_id": second["batch_id"]},
        )
        self.assertEqual(third["actions"][0]["payload"]["title"], "Финальный созвон с Ариадной")
        self.assertEqual(third["actions"][0]["provenance"]["fields"]["title"], "MODEL_EXPLICIT")

        result = service.apply({
            "batch_id": second["batch_id"],
            "action_ids": [action["id"]],
            "idempotency_key": "voice-follow-up",
        })
        self.assertFalse(result["replayed"])
        event = self.repository.get_event("account", "ariadne")
        self.assertEqual(event.obligation.title, manual_title)
        reminder = self.repository.connection.execute(
            "SELECT lead_minutes FROM event_reminders WHERE account_id='account' AND event_id='ariadne'"
        ).fetchone()
        self.assertEqual(reminder[0], 30)

    def test_previous_batch_is_principal_scoped(self):
        provider = SessionProvider()
        first = SQLiteAssistantService(
            self.repository, AuthenticatedPrincipal("account", "user-a", "client"), provider=provider,
        ).interpret("первый ход")
        with self.assertRaises(AuthorizationDenied):
            SQLiteAssistantService(
                self.repository, AuthenticatedPrincipal("account", "user-b", "client"), provider=provider,
            ).interpret("И напомни", {"previous_batch_id": first["batch_id"]})


if __name__ == "__main__":
    unittest.main()
