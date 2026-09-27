from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient


NOW = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)


class PlanControlApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "plan-control.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("a")
            repo.create_task(
                account_id="a", obligation_id="task-plan-control", title="Architecture review",
                description=None, category=ObligationCategory.WORK, importance=Importance.HIGH,
                estimated_total_effort_minutes=60, remaining_effort_minutes=60,
                splittable=False, min_chunk_minutes=30, max_chunk_minutes=90,
                actionable_from=NOW, target_at=None,
                actual_cutoff=HardCutoff.known(NOW + timedelta(hours=8)),
                actor=ActorCategory.USER_UI,
            )
        self.cm = TestClient(create_app(self.db, account_id="a", principal_id="u", now=lambda: NOW))
        self.client = self.cm.__enter__()

    def tearDown(self):
        self.cm.__exit__(None, None, None)
        self.tmp.cleanup()

    def sync(self, op):
        response = self.client.post("/api/v1/sync", json={"operations": [op]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["results"][0]

    def test_preview_rolls_back_then_pin_is_canonical_planner_input(self):
        op = {
            "op_id": "unused-preview-op",
            "type": "constraint.create",
            "entity_id": "constraint-preview-1",
            "payload": {
                "type": "PINNED_WORK",
                "task_id": "task-plan-control",
                "starts_at": (NOW + timedelta(hours=2)).isoformat(),
                "ends_at": (NOW + timedelta(hours=3)).isoformat(),
                "reason": "USER_PINNED_PLAN_WORK",
            },
        }
        preview = self.client.post("/api/v1/plan/control/preview", json={"operation": {
            "type": op["type"], "entity_id": op["entity_id"], "payload": op["payload"],
        }})
        self.assertEqual(preview.status_code, 200, preview.text)
        data = preview.json()
        self.assertEqual(data["constraint"]["type"], "PINNED_WORK")
        self.assertEqual(data["after"]["feasibility_status"], "FEASIBLE")
        self.assertFalse(any(x["id"] == "constraint-preview-1" for x in self.client.get("/api/v1/plan/constraints").json()),
                         "preview must not persist canonical state")

        applied = self.sync(op)
        self.assertEqual(applied["status"], "APPLIED", applied)
        constraints = self.client.get("/api/v1/plan/constraints").json()
        self.assertEqual([x["id"] for x in constraints], ["constraint-preview-1"])

        agenda = self.client.get("/api/v1/plan/agenda?days=2").json()["plan"]
        work = next(x for x in agenda["blocks"] if x["type"] == "WORK" and x["obligation_id"] == "task-plan-control")
        self.assertEqual(work["starts_at"], (NOW + timedelta(hours=2)).isoformat())
        self.assertEqual(work["ends_at"], (NOW + timedelta(hours=3)).isoformat())
        self.assertEqual(work["source_constraint_ids"], ["constraint-preview-1"])

    def test_constraint_update_and_delete_are_versioned(self):
        created = self.sync({
            "op_id": "op-constraint-create",
            "type": "constraint.create",
            "entity_id": "constraint-avoid-1",
            "payload": {
                "type": "UNAVAILABLE",
                "starts_at": (NOW + timedelta(hours=4)).isoformat(),
                "ends_at": (NOW + timedelta(hours=5)).isoformat(),
                "reason": "USER_AVOIDED_PLAN_TIME",
            },
        })
        self.assertEqual(created["entity"]["version"], 1)

        moved = self.sync({
            "op_id": "op-constraint-update",
            "type": "constraint.update",
            "entity_id": "constraint-avoid-1",
            "payload": {
                "expected_version": 1,
                "starts_at": (NOW + timedelta(hours=5)).isoformat(),
                "ends_at": (NOW + timedelta(hours=6)).isoformat(),
            },
        })
        self.assertEqual(moved["status"], "APPLIED", moved)
        self.assertEqual(moved["entity"]["version"], 2)

        stale = self.sync({
            "op_id": "op-constraint-stale-delete",
            "type": "constraint.delete",
            "entity_id": "constraint-avoid-1",
            "payload": {"expected_version": 1},
        })
        self.assertEqual(stale["status"], "CONFLICT")

        deleted = self.sync({
            "op_id": "op-constraint-delete",
            "type": "constraint.delete",
            "entity_id": "constraint-avoid-1",
            "payload": {"expected_version": 2},
        })
        self.assertEqual(deleted["status"], "APPLIED", deleted)
        self.assertEqual(self.client.get("/api/v1/plan/constraints").json(), [])


if __name__ == "__main__":
    unittest.main()
