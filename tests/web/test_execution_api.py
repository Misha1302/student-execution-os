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


NOW = datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)


class ExecutionApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "api.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            for account in ("a", "b"):
                repo.create_account(account)
            repo.create_task(
                account_id="a", obligation_id="task-api-execution", title="Compiler report",
                description=None, category=ObligationCategory.WORK, importance=Importance.HIGH,
                estimated_total_effort_minutes=90, remaining_effort_minutes=90,
                splittable=True, min_chunk_minutes=15, max_chunk_minutes=60,
                actionable_from=None, target_at=None, actual_cutoff=HardCutoff.absent(),
                actor=ActorCategory.USER_UI,
            )
        self.clock = [NOW]
        self.cm = TestClient(create_app(self.db, account_id="a", principal_id="user-a", now=lambda: self.clock[0]))
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

    def test_execution_read_api_and_account_isolation(self):
        started = self.sync(
            "op-api-start", "execution.start", "execution-api-1",
            {"task_id": "task-api-execution", "occurred_at": NOW.isoformat()},
        )
        self.assertEqual(started["status"], "APPLIED")

        active = self.client.get("/api/v1/execution/active")
        self.assertEqual(active.status_code, 200)
        self.assertEqual(active.json()["session"]["id"], "execution-api-1")
        self.assertEqual(active.json()["session"]["task_title"], "Compiler report")

        self.clock[0] = NOW + timedelta(minutes=20)
        paused = self.sync(
            "op-api-pause", "execution.pause", "execution-api-1",
            {"occurred_at": self.clock[0].isoformat()},
        )
        self.assertEqual(paused["entity"]["actual_work_seconds"], 20 * 60)

        detail = self.client.get("/api/v1/execution/sessions/execution-api-1")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual((detail.json()["state"], detail.json()["actual_work_seconds"]), ("PAUSED", 20 * 60))

        history = self.client.get("/api/v1/execution/sessions?task_id=task-api-execution&days=7")
        self.assertEqual(history.status_code, 200)
        self.assertEqual([x["id"] for x in history.json()["sessions"]], ["execution-api-1"])

        with TestClient(create_app(self.db, account_id="b", principal_id="user-b", now=lambda: self.clock[0])) as other:
            self.assertEqual(other.get("/api/v1/execution/active").json()["session"], None)
            self.assertEqual(other.get("/api/v1/execution/sessions/execution-api-1").status_code, 404)

    def test_export_contains_execution_history_but_not_other_account(self):
        self.sync(
            "op-export-start", "execution.start", "execution-export-1",
            {"task_id": "task-api-execution", "occurred_at": NOW.isoformat()},
        )
        self.clock[0] = NOW + timedelta(minutes=15)
        self.sync(
            "op-export-finish", "execution.finish", "execution-export-1",
            {"task_id": "task-api-execution", "outcome": "KEEP_REMAINING",
             "occurred_at": self.clock[0].isoformat()},
        )
        exported = self.client.get("/api/v1/account/export")
        self.assertEqual(exported.status_code, 200, exported.text)
        tables = exported.json()["tables"]
        self.assertEqual([x["id"] for x in tables["execution_sessions"]], ["execution-export-1"])
        self.assertEqual(len(tables["execution_segments"]), 1)
        self.assertTrue(all(x["account_id"] == "a" for x in tables["execution_sessions"]))
        self.assertTrue(all(x["account_id"] == "a" for x in tables["execution_segments"]))


if __name__ == "__main__":
    unittest.main()
