from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient


NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


class WorkRoutinesApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "work-routines.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("a")
        self.cm = TestClient(create_app(self.db, account_id="a", principal_id="u", now=lambda: NOW))
        self.client = self.cm.__enter__()

    def tearDown(self):
        self.cm.__exit__(None, None, None)
        self.tmp.cleanup()

    def sync(self, op_id, kind, entity_id, payload):
        response = self.client.post("/api/v1/sync", json={"operations": [{
            "op_id": op_id, "type": kind, "entity_id": entity_id, "payload": payload,
        }]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["results"][0]

    def create_routine(self):
        result = self.sync("op-routine-create", "routine.create", "routine-study-1", {
            "title": "Algorithms practice",
            "dtstart_local": "2026-10-01T18:00",
            "effort_minutes": 45,
            "recurrence_rule": "FREQ=DAILY;COUNT=3",
            "timezone_name": "UTC",
            "splittable": True,
            "min_chunk_minutes": 15,
            "max_chunk_minutes": 45,
        })
        self.assertEqual(result["status"], "APPLIED", result)

    def test_materializes_stable_occurrences_as_canonical_tasks(self):
        self.create_routine()
        data = self.client.get("/api/v1/work-routines")
        self.assertEqual(data.status_code, 200, data.text)
        routine = data.json()["routines"][0]
        self.assertEqual(routine["id"], "routine-study-1")
        self.assertEqual(len(routine["occurrences"]), 3)

        first = routine["occurrences"][0]
        self.assertEqual(first["identity"], ["routine-study-1", "2026-10-01T18:00:00"])
        self.assertTrue(first["task_id"].startswith("routine-"))
        task = self.client.get(f"/api/v1/tasks/{first['task_id']}")
        self.assertEqual(task.status_code, 200, task.text)
        self.assertEqual(task.json()["title"], "Algorithms practice")
        self.assertEqual(task.json()["target_at"], "2026-10-01T18:00:00+00:00")
        self.assertEqual(task.json()["remaining_effort_minutes"], 45)

        today = self.client.get("/api/v1/plan/agenda?days=3")
        self.assertEqual(today.status_code, 200, today.text)
        ids = {task["id"] for task in today.json()["tasks"]}
        self.assertIn(first["task_id"], ids, "routine occurrence must enter planning through canonical Task")

    def test_skip_reopen_and_edit_preserve_occurrence_identity_and_task(self):
        self.create_routine()
        first = self.client.get("/api/v1/work-routines").json()["routines"][0]["occurrences"][0]
        identity = first["original_recurrence_id"]
        task_id = first["task_id"]

        skipped = self.sync("op-routine-skip", "routine.occurrence.skip", task_id, {
            "template_id": "routine-study-1",
            "original_recurrence_id": identity,
            "task_id": task_id,
        })
        self.assertEqual(skipped["status"], "APPLIED", skipped)
        self.assertEqual(skipped["entity"]["identity"], ["routine-study-1", identity])
        self.assertEqual(self.client.get(f"/api/v1/tasks/{task_id}").json()["status"], "CANCELLED")

        reopened = self.sync("op-routine-reopen", "routine.occurrence.reopen", task_id, {
            "template_id": "routine-study-1",
            "original_recurrence_id": identity,
            "task_id": task_id,
        })
        self.assertEqual(reopened["status"], "APPLIED", reopened)
        self.assertEqual(self.client.get(f"/api/v1/tasks/{task_id}").json()["status"], "ACTIVE")

        edited = self.sync("op-routine-edit", "routine.occurrence.edit", task_id, {
            "template_id": "routine-study-1",
            "original_recurrence_id": identity,
            "task_id": task_id,
            "title": "Hard algorithms practice",
            "effort_minutes": 60,
            "target_local": "2026-10-01T20:00",
        })
        self.assertEqual(edited["status"], "APPLIED", edited)
        self.assertEqual(edited["entity"]["identity"], ["routine-study-1", identity])
        self.assertEqual(edited["entity"]["task_id"], task_id)

        task = self.client.get(f"/api/v1/tasks/{task_id}").json()
        self.assertEqual(task["title"], "Hard algorithms practice")
        self.assertEqual(task["estimated_total_effort_minutes"], 60)
        self.assertEqual(task["remaining_effort_minutes"], 60)
        self.assertEqual(task["target_at"], "2026-10-01T20:00:00+00:00")

        occurrence = self.client.get("/api/v1/work-routines").json()["routines"][0]["occurrences"][0]
        self.assertEqual(occurrence["original_recurrence_id"], identity)
        self.assertEqual(occurrence["task_id"], task_id)
        self.assertEqual(occurrence["override_target_local"], "2026-10-01T20:00:00")

    def test_task_ref_mismatch_is_rejected(self):
        self.create_routine()
        first = self.client.get("/api/v1/work-routines").json()["routines"][0]["occurrences"][0]
        result = self.sync("op-routine-bad-ref", "routine.occurrence.skip", first["task_id"], {
            "template_id": "routine-study-1",
            "original_recurrence_id": first["original_recurrence_id"],
            "task_id": "task-wrong-reference",
        })
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.client.get(f"/api/v1/tasks/{first['task_id']}").json()["status"], "CANCELLED",
                         "server mutation is transactional with sync result and must roll back on ref mismatch")


if __name__ == "__main__":
    unittest.main()
