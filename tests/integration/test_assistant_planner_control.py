from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import SQLitePlanningStateSource


NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


class PlannerControlProvider:
    name = "planner-control-fixture"
    model = "fixture"

    def interpret(self, text, context):
        return {"message": "protected morning", "actions": [{
            "command": "CREATE_TIME_CONSTRAINT",
            "payload": {
                "type": "UNAVAILABLE",
                "starts_at": "2026-10-03T00:00:00+00:00",
                "ends_at": "2026-10-03T09:00:00+00:00",
                "reason": "USER_REQUESTED_NO_WORK_BEFORE_NOON",
            },
            "confidence": 0.97,
            "unresolved_fields": [],
            "expected_version": None,
            "requires_confirmation": False,
            "field_provenance": {
                "type": "MODEL_EXPLICIT",
                "starts_at": "MODEL_EXPLICIT",
                "ends_at": "MODEL_EXPLICIT",
                "reason": "MODEL_INFERRED",
            },
        }]}


class AssistantPlannerControlTest(unittest.TestCase):
    def test_natural_language_control_creates_canonical_constraint_not_plan_block(self):
        with tempfile.TemporaryDirectory() as directory:
            with SQLiteCanonicalRepository(
                str(Path(directory) / "planner-control.sqlite"), clock=FrozenClock(NOW),
            ) as repository:
                repository.initialize()
                repository.create_account("account")
                service = SQLiteAssistantService(
                    repository, AuthenticatedPrincipal("account", "user", "client"),
                    provider=PlannerControlProvider(),
                )
                preview = service.interpret("Завтра ничего до 12", {"timezone": "Europe/Moscow"})
                action = preview["actions"][0]
                self.assertEqual(action["command"], "CREATE_TIME_CONSTRAINT")
                self.assertEqual(
                    repository.connection.execute("SELECT count(*) FROM user_time_constraints").fetchone()[0],
                    0,
                )
                result = service.apply({
                    "batch_id": preview["batch_id"],
                    "action_ids": [action["id"]],
                    "idempotency_key": "planner-control",
                })
                self.assertFalse(result["replayed"])
                constraints = SQLitePlanningStateSource(repository).list_time_constraints("account")
                self.assertEqual(len(constraints), 1)
                self.assertEqual(constraints[0].type.value, "UNAVAILABLE")
                self.assertEqual(constraints[0].reason, "USER_REQUESTED_NO_WORK_BEFORE_NOON")
                self.assertEqual(repository.connection.execute("SELECT count(*) FROM plan_blocks").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
