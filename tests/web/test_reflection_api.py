from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.execution import SQLiteExecutionStore
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient


NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


class ReflectionApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "reflection.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("a")
        self.current = [NOW]
        self.cm = TestClient(create_app(
            self.db, account_id="a", principal_id="u", now=lambda: self.current[0]
        ))
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

    def create_task(self, task_id, title, category="HOMEWORK", effort=60):
        result = self.sync("op-create-" + task_id, "task.create", task_id, {
            "title": title,
            "category": category,
            "estimated_total_effort_minutes": effort,
            "actual_cutoff": {"state": "ABSENT"},
            "splittable": False,
        })
        self.assertEqual(result["status"], "APPLIED", result)

    def test_daily_intent_is_soft_planner_input_and_visible_on_today(self):
        self.create_task("task-priority-a", "A")
        self.create_task("task-priority-b", "B")

        saved = self.sync("op-intent-set", "intent.set", "2026-10-01", {
            "local_date": "2026-10-01",
            "priority_task_ids": ["task-priority-b", "task-priority-a"],
            "note": "Main focus",
        })
        self.assertEqual(saved["status"], "APPLIED", saved)

        today = self.client.get("/api/v1/today")
        self.assertEqual(today.status_code, 200, today.text)
        data = today.json()
        self.assertEqual(data["daily_intent"]["priority_task_ids"], ["task-priority-b", "task-priority-a"])
        self.assertEqual(data["daily_intent"]["note"], "Main focus")
        self.assertEqual(data["next_actions"][0]["task_id"], "task-priority-b")

        closed = self.sync("op-intent-close", "intent.close", "2026-10-01", {
            "local_date": "2026-10-01",
        })
        self.assertEqual(closed["status"], "APPLIED", closed)
        self.assertIsNotNone(self.client.get("/api/v1/today").json()["daily_intent"]["closed_at"])

    def test_calibration_changes_planning_projection_not_canonical_task(self):
        self.create_task("task-calibrated-1", "Essay", category="HOMEWORK", effort=60)
        applied = self.sync("op-calibration-set", "calibration.set", "calibration-HOMEWORK", {
            "category": "HOMEWORK",
            "safety_multiplier": 1.5,
            "enabled": True,
            "suppress_suggestion": False,
        })
        self.assertEqual(applied["status"], "APPLIED", applied)

        canonical = self.client.get("/api/v1/tasks/task-calibrated-1").json()
        self.assertEqual(canonical["estimated_total_effort_minutes"], 60)
        self.assertEqual(canonical["remaining_effort_minutes"], 60)

        today = self.client.get("/api/v1/today").json()
        self.assertEqual(today["planning_effort_multipliers"], {"HOMEWORK": 1.5})
        planned = sum(
            block["duration_minutes"] for block in today["plan"]["blocks"]
            if block["type"] == "WORK" and block["obligation_id"] == "task-calibrated-1"
        )
        self.assertEqual(planned, 90)
        canonical_after = self.client.get("/api/v1/tasks/task-calibrated-1").json()
        self.assertEqual(canonical_after["remaining_effort_minutes"], 60)

    def test_review_compares_first_plan_with_actual_execution(self):
        self.create_task("task-review-1", "Review task", effort=60)
        baseline = self.client.get("/api/v1/today")
        self.assertEqual(baseline.status_code, 200, baseline.text)

        started = self.sync("op-execution-start-review", "execution.start", "execution-review-1", {
            "task_id": "task-review-1",
            "occurred_at": NOW.isoformat(),
        })
        self.assertEqual(started["status"], "APPLIED", started)
        self.current[0] = NOW + timedelta(minutes=30)
        finished = self.sync("op-execution-finish-review", "execution.finish", "execution-review-1", {
            "task_id": "task-review-1",
            "occurred_at": self.current[0].isoformat(),
            "outcome": "COMPLETE",
        })
        self.assertEqual(finished["status"], "APPLIED", finished)

        review = self.client.get("/api/v1/reflection?days=1")
        self.assertEqual(review.status_code, 200, review.text)
        data = review.json()
        self.assertEqual(data["planned_work_minutes"], 60)
        self.assertEqual(data["actual_work_minutes"], 30)
        self.assertEqual(data["variance_minutes"], -30)
        self.assertEqual(data["completed_tasks"], 1)
        self.assertEqual(data["carry_over_count"], 0)

    def test_calibration_suggestion_never_applies_automatically(self):
        for i in range(5):
            task_id = f"task-history-{i}"
            start = NOW - timedelta(days=i + 1)
            end = start + timedelta(minutes=120)
            with SQLiteCanonicalRepository(self.db, clock=FrozenClock(start)) as repo:
                repo.create_task(
                    account_id="a", obligation_id=task_id, title=f"History {i}",
                    category=ObligationCategory.HOMEWORK, importance=Importance.NORMAL,
                    estimated_total_effort_minutes=60, remaining_effort_minutes=60,
                    splittable=False, actual_cutoff=HardCutoff.absent(),
                    actor=ActorCategory.USER_UI,
                )
                execution = SQLiteExecutionStore(repo)
                execution.start("a", f"execution-history-{i}", task_id, start, ActorCategory.USER_UI)
                execution.finish("a", f"execution-history-{i}", end, ActorCategory.USER_UI)
            with SQLiteCanonicalRepository(self.db, clock=FrozenClock(end)) as repo:
                task = repo.get_task("a", task_id)
                repo.complete_obligation(
                    account_id="a", obligation_id=task_id,
                    expected_version=task.obligation.version, actor=ActorCategory.USER_UI,
                )

        self.create_task("task-future-calibration", "Future homework", category="HOMEWORK", effort=60)
        reflection = self.client.get("/api/v1/reflection?days=7").json()
        row = next(item for item in reflection["calibration"] if item["category"] == "HOMEWORK")
        self.assertEqual(row["sample_size"], 5)
        self.assertEqual(row["suggested_multiplier"], 2.0)

        before = self.client.get("/api/v1/today").json()
        self.assertEqual(before["planning_effort_multipliers"], {})
        planned = sum(
            block["duration_minutes"] for block in before["plan"]["blocks"]
            if block["type"] == "WORK" and block["obligation_id"] == "task-future-calibration"
        )
        self.assertEqual(planned, 60)


if __name__ == "__main__":
    unittest.main()
