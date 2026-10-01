from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import (
    ActorCategory,
    AttendancePolicy,
    EventTimeSemantics,
    HardCutoff,
    Importance,
    ObligationCategory,
)
from student_execution_os.persistence import SQLiteCanonicalRepository


NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


class ActionPlanProvider:
    name = "action-plan-fixture"
    model = "fixture"

    def __init__(self, fail_second: bool = False):
        self.fail_second = fail_second

    def interpret(self, text, context):
        event = next(item for item in context["obligations"] if item["id"] == "event")
        second = ({
            "client_ref": "progress",
            "depends_on": ["move"],
            "command": "LOG_PROGRESS",
            "payload": {"obligation_id": "task", "count": 1},
            "confidence": 1,
            "unresolved_fields": [],
            "expected_version": next(item for item in context["obligations"] if item["id"] == "task")["version"],
            "requires_confirmation": False,
        } if self.fail_second else {
            "client_ref": "reminder",
            "depends_on": ["move"],
            "command": "UPDATE_EVENT",
            "payload": {"obligation_id": "event", "remind_before_minutes": 20},
            "confidence": 1,
            "unresolved_fields": [],
            "expected_version": event["version"],
            "requires_confirmation": False,
        })
        return {"message": "plan", "actions": [{
            "client_ref": "move",
            "depends_on": [],
            "command": "RESCHEDULE",
            "payload": {"obligation_id": "event", "when": "2026-10-02T16:00:00+00:00"},
            "confidence": 1,
            "unresolved_fields": [],
            "expected_version": event["version"],
            "requires_confirmation": False,
        }, second]}


class AssistantActionPlanTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = SQLiteCanonicalRepository(
            str(Path(self.temporary.name) / "action-plan.sqlite"), clock=FrozenClock(NOW),
        )
        self.repository.initialize()
        self.repository.create_account("account")
        self.repository.create_event(
            account_id="account", obligation_id="event", title="Встреча",
            description=None, category=ObligationCategory.MEETING, importance=Importance.NORMAL,
            time_semantics=EventTimeSemantics.FIXED_INTERVAL,
            starts_at=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
            ends_at=datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc),
            attendance_policy=AttendancePolicy.REQUIRED, actor=ActorCategory.USER_UI,
        )
        self.repository.create_task(
            account_id="account", obligation_id="task", title="Отчёт",
            category=ObligationCategory.HOMEWORK, importance=Importance.NORMAL,
            estimated_total_effort_minutes=60, remaining_effort_minutes=60,
            splittable=False, min_chunk_minutes=None, max_chunk_minutes=None,
            actionable_from=NOW, target_at=None, actual_cutoff=HardCutoff.absent(),
            actor=ActorCategory.USER_UI,
        )

    def tearDown(self):
        self.repository.close()
        self.temporary.cleanup()

    def test_dependency_closed_plan_uses_declared_order_even_if_selection_is_reversed(self):
        service = SQLiteAssistantService(
            self.repository, AuthenticatedPrincipal("account", "user", "client"),
            provider=ActionPlanProvider(),
        )
        preview = service.interpret("Перенеси встречу и напомни")
        first, second = preview["actions"]
        self.assertEqual(second["depends_on"], [first["id"]])
        with self.assertRaises(ValidationError):
            service.apply({
                "batch_id": preview["batch_id"], "action_ids": [second["id"]],
                "idempotency_key": "missing-dependency",
            })
        result = service.apply({
            "batch_id": preview["batch_id"], "action_ids": [second["id"], first["id"]],
            "idempotency_key": "complete-plan",
        })
        self.assertEqual([item["action_id"] for item in result["results"]], [first["id"], second["id"]])
        self.assertEqual(
            self.repository.get_event("account", "event").interval.starts_at,
            datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc),
        )

    def test_failed_dependent_action_rolls_back_the_whole_plan(self):
        service = SQLiteAssistantService(
            self.repository, AuthenticatedPrincipal("account", "user", "client"),
            provider=ActionPlanProvider(fail_second=True),
        )
        preview = service.interpret("Перенеси встречу и запиши прогресс")
        with self.assertRaises(ValidationError):
            service.apply({
                "batch_id": preview["batch_id"],
                "action_ids": [action["id"] for action in preview["actions"]],
                "idempotency_key": "atomic-plan",
            })
        self.assertEqual(
            self.repository.get_event("account", "event").interval.starts_at,
            datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            self.repository.connection.execute(
                "SELECT count(*) FROM assistant_action_history WHERE account_id='account'"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
