from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import VersionConflict
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from student_execution_os.work_routines import SQLiteWorkRoutineRepository
from tests.asgi_client import TestClient


NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


class WorkRoutinesApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "work-routines.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("a")
        self.now = NOW
        self.cm = TestClient(create_app(self.db, account_id="a", principal_id="u", now=lambda: self.now))
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

    def test_split_this_and_future_replaces_untouched_materialized_future(self):
        self.create_routine()
        before = self.client.get("/api/v1/work-routines").json()["routines"][0]
        occurrences = before["occurrences"]
        boundary = occurrences[1]
        old_future_ids = {
            item["task_id"] for item in occurrences
            if item["original_recurrence_id"] >= boundary["original_recurrence_id"]
        }

        split = self.sync("op-routine-split", "routine.split", "routine-study-successor", {
            "template_id": before["id"],
            "original_recurrence_id": boundary["original_recurrence_id"],
            "expected_version": before["version"],
            "title": "Algorithms deep",
            "effort_minutes": 60,
            "target_local": "2026-10-02T19:30",
            "timezone_name": "UTC",
        })
        self.assertEqual(split["status"], "APPLIED", split)

        routines = self.client.get("/api/v1/work-routines").json()["routines"]
        predecessor = next(x for x in routines if x["id"] == "routine-study-1")
        successor = next(x for x in routines if x["id"] == "routine-study-successor")
        self.assertEqual(predecessor["series_end_before_local"], boundary["original_recurrence_id"])
        self.assertEqual(successor["dtstart_local"], "2026-10-02T19:30:00")
        self.assertEqual(successor["effort_minutes"], 60)
        # COUNT=3 split at the second occurrence leaves exactly two successor occurrences.
        self.assertIn("COUNT=2", successor["recurrence_rule"])
        tasks = {x["id"] for x in self.client.get("/api/v1/tasks").json()}
        self.assertFalse(old_future_ids & tasks, "untouched old future materializations must be removed")
        self.assertEqual(len(successor["occurrences"]), 2)

    def test_split_refuses_to_rewrite_started_future_history(self):
        self.create_routine()
        routine = self.client.get("/api/v1/work-routines").json()["routines"][0]
        boundary = routine["occurrences"][1]
        started = self.sync("op-routine-start", "execution.start", "execution-routine-start", {
            "task_id": boundary["task_id"], "occurred_at": NOW.isoformat(),
        })
        self.assertEqual(started["status"], "APPLIED")

        result = self.sync("op-routine-split-conflict", "routine.split", "routine-conflict-successor", {
            "template_id": routine["id"],
            "original_recurrence_id": boundary["original_recurrence_id"],
            "expected_version": routine["version"],
            "title": "Changed",
            "effort_minutes": 50,
        })
        self.assertEqual(result["status"], "CONFLICT")
        current = next(x for x in self.client.get("/api/v1/work-routines").json()["routines"] if x["id"] == routine["id"])
        self.assertIsNone(current["series_end_before_local"])

    def routine(self, routine_id="routine-study-1"):
        return next(x for x in self.client.get("/api/v1/work-routines").json()["routines"] if x["id"] == routine_id)

    def task_ids(self):
        return {x["id"] for x in self.client.get("/api/v1/tasks").json()}

    def test_cancel_stops_series_removes_untouched_future_and_keeps_history(self):
        # Daily routine with five occurrences: 10-01 .. 10-05 at 18:00 UTC.
        self.sync("op-routine-create-5", "routine.create", "routine-five", {
            "title": "Reading", "dtstart_local": "2026-10-01T18:00", "effort_minutes": 30,
            "recurrence_rule": "FREQ=DAILY;COUNT=5", "timezone_name": "UTC",
        })
        occ = {o["original_recurrence_id"]: o for o in self.routine("routine-five")["occurrences"]}
        self.assertEqual(len(occ), 5)
        first, second, third, fourth, fifth = (occ[k] for k in sorted(occ))
        # History: the first occurrence is completed, the fourth (still in the future)
        # was already started early. The second becomes past-due and untouched.
        self.now = datetime(2026, 10, 1, 19, 0, tzinfo=timezone.utc)
        self.assertEqual(self.sync("op-done-1", "task.complete", first["task_id"], {
            "occurred_at": "2026-10-01T18:30:00+00:00",
        })["status"], "APPLIED")
        self.assertEqual(self.sync("op-start-4", "execution.start", "exec-routine-4", {
            "task_id": fourth["task_id"], "occurred_at": self.now.isoformat(),
        })["status"], "APPLIED")
        # Stop the routine on 10-02 at 19:00: the second occurrence (10-02 18:00) is past.
        self.now = datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc)
        version = self.routine("routine-five")["version"]
        result = self.sync("op-routine-cancel", "routine.cancel", "routine-five", {"expected_version": version})
        self.assertEqual(result["status"], "APPLIED", result)
        self.assertEqual(result["entity"]["status"], "CANCELLED")
        self.assertEqual(result["entity"]["version"], version + 1)

        tasks = self.task_ids()
        self.assertIn(first["task_id"], tasks, "completed history is preserved")
        self.assertIn(second["task_id"], tasks, "past-due occurrence stays an ordinary Task")
        self.assertIn(fourth["task_id"], tasks, "a started future occurrence keeps its execution history")
        self.assertNotIn(third["task_id"], tasks, "untouched future materialization is removed")
        self.assertNotIn(fifth["task_id"], tasks, "untouched future materialization is removed")
        remaining = {o["original_recurrence_id"] for o in self.routine("routine-five")["occurrences"]}
        self.assertEqual(remaining, {first["original_recurrence_id"], second["original_recurrence_id"],
                                     fourth["original_recurrence_id"]})

        # A stopped series no longer materializes, even far into the horizon.
        self.now = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
        agenda = self.client.get("/api/v1/plan/agenda?days=7")
        self.assertEqual(agenda.status_code, 200, agenda.text)
        self.assertEqual(self.task_ids(), tasks)

        # Skipping a removed occurrence of a stopped routine must not resurrect it.
        resurrect = self.sync("op-skip-removed", "routine.occurrence.skip", third["task_id"], {
            "template_id": "routine-five",
            "original_recurrence_id": third["original_recurrence_id"],
            "task_id": third["task_id"],
        })
        self.assertEqual((resurrect["status"], resurrect.get("code")), ("NOOP", "DELETED"), resurrect)
        self.assertNotIn(third["task_id"], self.task_ids())

        # An occurrence without a materialized row (removed by the stop) cannot be materialized for a stopped series.
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(self.now)) as repo:
            store = SQLiteWorkRoutineRepository(repo)
            with self.assertRaises(VersionConflict):
                store.skip_occurrence("a", "routine-five", fifth["original_recurrence_id"], ActorCategory.USER_UI)
            self.assertIsNone(store.get_occurrence("a", "routine-five", fifth["original_recurrence_id"]))

    def test_cancel_is_idempotent_and_replay_safe(self):
        self.create_routine()
        version = self.routine()["version"]
        first = self.sync("op-cancel-a", "routine.cancel", "routine-study-1", {"expected_version": version})
        self.assertEqual(first["status"], "APPLIED", first)
        # The same operation replayed after a lost ack returns the recorded result.
        replay = self.sync("op-cancel-a", "routine.cancel", "routine-study-1", {"expected_version": version})
        self.assertEqual(replay["status"], "APPLIED", replay)
        self.assertEqual(replay["entity"]["version"], version + 1)
        # A second, independent stop (double tap / second device with the stale version)
        # reaches the same terminal state and changes nothing.
        again = self.sync("op-cancel-b", "routine.cancel", "routine-study-1", {"expected_version": version})
        self.assertEqual(again["status"], "NOOP", again)
        self.assertEqual(self.routine()["version"], version + 1)
        self.assertEqual(self.routine()["status"], "CANCELLED")

    def test_cancel_with_stale_version_conflicts_without_mutation(self):
        self.create_routine()
        routine = self.routine()
        occurrences = routine["occurrences"]
        split = self.sync("op-split-first", "routine.split", "routine-study-next", {
            "template_id": routine["id"],
            "original_recurrence_id": occurrences[2]["original_recurrence_id"],
            "expected_version": routine["version"],
        })
        self.assertEqual(split["status"], "APPLIED", split)
        before_tasks = self.task_ids()
        result = self.sync("op-cancel-stale", "routine.cancel", "routine-study-1",
                           {"expected_version": routine["version"]})
        self.assertEqual(result["status"], "CONFLICT", result)
        self.assertEqual(self.routine()["status"], "ACTIVE")
        self.assertEqual(self.task_ids(), before_tasks)

    def test_task_ref_mismatch_is_rejected(self):
        self.create_routine()
        first = self.client.get("/api/v1/work-routines").json()["routines"][0]["occurrences"][0]
        result = self.sync("op-routine-bad-ref", "routine.occurrence.skip", first["task_id"], {
            "template_id": "routine-study-1",
            "original_recurrence_id": first["original_recurrence_id"],
            "task_id": "task-wrong-reference",
        })
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.client.get(f"/api/v1/tasks/{first['task_id']}").json()["status"], "ACTIVE",
                         "server mutation is transactional with sync result and must roll back on ref mismatch")


if __name__ == "__main__":
    unittest.main()
