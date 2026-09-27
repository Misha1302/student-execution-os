from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.execution import SQLiteExecutionStore
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import SQLitePlanStore
from student_execution_os.planning.model import FeasibilityStatus, PlanBlock, PlanBlockType, PlanSnapshot
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient


NOW = datetime(2026, 9, 30, 16, 0, tzinfo=timezone.utc)
DAY_START = datetime(2026, 9, 30, 0, 0, tzinfo=timezone.utc)


class ReflectionApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "reflection.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("a")
            repo.create_task(
                account_id="a", obligation_id="task-reflection-1", title="Architecture review",
                description=None, category=ObligationCategory.WORK, importance=Importance.HIGH,
                estimated_total_effort_minutes=180, remaining_effort_minutes=180,
                splittable=True, min_chunk_minutes=15, max_chunk_minutes=90,
                actionable_from=DAY_START, target_at=None, actual_cutoff=HardCutoff.absent(),
                actor=ActorCategory.USER_UI,
            )
            self._seed_calibration_history(repo)
        self.cm = TestClient(create_app(self.db, account_id="a", principal_id="u", now=lambda: NOW))
        self.client = self.cm.__enter__()

    def tearDown(self):
        self.cm.__exit__(None, None, None)
        self.tmp.cleanup()

    def _seed_calibration_history(self, repo):
        blocks = tuple(
            PlanBlock(
                id=f"reflection-block-{index}",
                type=PlanBlockType.WORK,
                obligation_id="task-reflection-1",
                starts_at=DAY_START + timedelta(hours=9 + index * 2),
                ends_at=DAY_START + timedelta(hours=9 + index * 2, minutes=30),
                explanation="TEST_CALIBRATION",
            )
            for index in range(3)
        )
        plan = PlanSnapshot(
            id="reflection-plan-1",
            account_id="a",
            plan_revision="reflection-test-1",
            input_server_revision=repo.get_server_revision("a"),
            input_hash="f" * 64,
            horizon_start=DAY_START,
            horizon_end=DAY_START + timedelta(days=1),
            feasibility_status=FeasibilityStatus.FEASIBLE,
            generated_at=DAY_START + timedelta(hours=8),
            blocks=blocks,
            explanations=(),
        )
        SQLitePlanStore(repo).save(plan)
        store = SQLiteExecutionStore(repo)
        for index, block in enumerate(blocks):
            start = block.starts_at
            end = start + timedelta(minutes=45)
            with repo._tx():
                store.start(
                    "a", f"reflection-session-{index}", "task-reflection-1", start,
                    ActorCategory.USER_UI, planning_snapshot_id=plan.id,
                    source_plan_block_id=block.id,
                )
                store.finish(
                    "a", f"reflection-session-{index}", end, ActorCategory.USER_UI
                )

    def sync(self, op_id, kind, entity_id, payload):
        response = self.client.post("/api/v1/sync", json={"operations": [{
            "op_id": op_id, "type": kind, "entity_id": entity_id, "payload": payload,
        }]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["results"][0]

    def test_dashboard_derives_plan_actual_and_calibration_without_mutating_task(self):
        response = self.client.get("/api/v1/reflection")
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()

        self.assertEqual(data["day_stats"]["planned_work_minutes"], 90)
        self.assertEqual(data["day_stats"]["actual_work_minutes"], 135)
        self.assertEqual(data["day_stats"]["delta_minutes"], 45)
        self.assertEqual(data["calibration"]["sample_count"], 3)
        self.assertEqual(data["calibration"]["median_actual_to_planned_ratio"], 1.5)
        self.assertEqual(data["calibration"]["suggested_multiplier"], 1.5)
        self.assertEqual(data["calibration"]["bias"], "UNDER_ESTIMATING")
        self.assertFalse(data["calibration"]["automatic_mutation"])

        accepted = self.sync("op-calibration-accept", "calibration.accept", "calibration-hint", {
            "multiplier": 1.5,
            "based_on_samples": 3,
            "expected_version": 0,
        })
        self.assertEqual(accepted["status"], "APPLIED", accepted)
        task = self.client.get("/api/v1/tasks/task-reflection-1").json()
        self.assertEqual(task["estimated_total_effort_minutes"], 180)
        self.assertEqual(task["remaining_effort_minutes"], 180)

        today = self.client.get("/api/v1/today").json()
        self.assertEqual(today["calibration_hint"]["multiplier"], 1.5)
        self.assertEqual(today["calibration_hint"]["based_on_samples"], 3)

    def test_intent_daily_and_weekly_notes_use_versioned_offline_boundary(self):
        dashboard = self.client.get("/api/v1/reflection").json()
        local_date = dashboard["local_date"]
        week = dashboard["week_starts_on"]
        zone = dashboard["timezone_name"]

        intent = self.sync("op-intent-create", "intent.upsert", f"intent-{local_date}", {
            "local_date": local_date,
            "timezone_name": zone,
            "focus_note": "Finish the architecture pass",
            "task_ids": ["task-reflection-1"],
            "expected_version": 0,
        })
        self.assertEqual(intent["status"], "APPLIED", intent)
        self.assertEqual(intent["entity"]["task_ids"], ["task-reflection-1"])

        stale = self.sync("op-intent-stale", "intent.upsert", f"intent-{local_date}", {
            "local_date": local_date,
            "timezone_name": zone,
            "focus_note": "stale",
            "task_ids": [],
            "expected_version": 0,
        })
        self.assertEqual(stale["status"], "CONFLICT", stale)

        daily = self.sync("op-reflection-create", "reflection.upsert", f"reflection-{local_date}", {
            "local_date": local_date,
            "timezone_name": zone,
            "summary": "Worked through the hard part",
            "wins": "Stayed focused",
            "blockers": "Context switching",
            "adjustment": "Protect a longer block tomorrow",
            "expected_version": 0,
        })
        self.assertEqual(daily["status"], "APPLIED", daily)

        weekly = self.sync("op-week-create", "weekly_review.upsert", f"week-{week}", {
            "week_starts_on": week,
            "timezone_name": zone,
            "summary": "Good execution week",
            "wins": "Three calibrated sessions",
            "blockers": None,
            "adjustment": "Use longer initial estimates",
            "expected_version": 0,
        })
        self.assertEqual(weekly["status"], "APPLIED", weekly)

        data = self.client.get("/api/v1/reflection").json()
        self.assertEqual(data["intent"]["focus_note"], "Finish the architecture pass")
        self.assertEqual(data["intent"]["tasks"][0]["title"], "Architecture review")
        self.assertEqual(data["daily_reflection"]["wins"], "Stayed focused")
        self.assertEqual(data["weekly_review"]["summary"], "Good execution week")

        today = self.client.get("/api/v1/today").json()
        self.assertEqual(today["daily_intent"]["task_ids"], ["task-reflection-1"])
        self.assertEqual(today["daily_intent"]["tasks"][0]["title"], "Architecture review")


if __name__ == "__main__":
    unittest.main()
