from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    EventTimeSemantics,
    Importance,
    ObligationCategory,
)
from student_execution_os.persistence import SQLiteCanonicalRepository


ZONE = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=ZONE)


class ConditionalProvider:
    name = "semantic-fixture"
    model = "fixture-v1"

    def interpret(self, text, context):
        item = next(value for value in context["obligations"] if value["title"] == "Встреча с Ариадной")
        return {"message": "proposal", "actions": [{
            "command": "RESCHEDULE",
            "payload": {
                "obligation_id": item["id"],
                "temporal_transform": {
                    "kind": "SHIFT_WITH_GUARD_AND_FALLBACK",
                    "delta_minutes": 180,
                    "guard": {"not_after_local_time": "22:00"},
                    "fallback": {
                        "relative_day": "NEXT_MORNING",
                        "preferred_local_time": "10:00",
                        "precision": "APPROXIMATE",
                    },
                },
            },
            "confidence": 0.96,
            "unresolved_fields": [],
            "expected_version": item["version"],
            "requires_confirmation": False,
        }]}


class AssistantTemporalControlTest(unittest.TestCase):
    def test_guarded_shift_is_resolved_server_side_and_duration_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "assistant-temporal.sqlite")
            with SQLiteCanonicalRepository(database, clock=FrozenClock(NOW)) as repository:
                repository.initialize()
                repository.create_account("account")
                repository.create_event(
                    account_id="account", obligation_id="ariadne", title="Встреча с Ариадной",
                    description=None, category=ObligationCategory.MEETING, importance=Importance.NORMAL,
                    time_semantics=EventTimeSemantics.FIXED_INTERVAL,
                    starts_at=NOW.replace(hour=20), ends_at=NOW.replace(hour=21),
                    attendance_policy=AttendancePolicy.REQUIRED, actor=ActorCategory.USER_UI,
                )
                service = SQLiteAssistantService(
                    repository, AuthenticatedPrincipal("account", "user", "client"), provider=ConditionalProvider(),
                )
                preview = service.interpret(
                    "Перенеси встречу с Ариадной на три часа позже. "
                    "Но если позже 22:00, то на утро, часов на 10 примерно.",
                    {"timezone": "Europe/Moscow"},
                )
                action = preview["actions"][0]
                self.assertEqual(action["payload"]["when"], "2026-10-02T07:00:00+00:00")
                self.assertEqual(action["resolution"], {
                    "when": "2026-10-02T07:00:00+00:00",
                    "precision": "APPROXIMATE",
                    "reason": "GUARD_TRIGGERED_FALLBACK",
                    "timezone": "Europe/Moscow",
                })
                result = service.apply({
                    "batch_id": preview["batch_id"], "action_ids": [action["id"]],
                    "idempotency_key": "conditional-reschedule",
                })
                self.assertFalse(result["replayed"])
                event = repository.get_event("account", "ariadne")
                self.assertEqual(event.interval.starts_at.astimezone(ZONE).isoformat(), "2026-10-02T10:00:00+03:00")
                self.assertEqual(event.interval.ends_at - event.interval.starts_at, timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
