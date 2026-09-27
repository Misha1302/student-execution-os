from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient


NOW = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


class ProjectsApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "projects.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.create_account("a")
            repo.create_account("b")
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

    def test_project_checklist_milestones_and_derived_progress(self):
        created = self.sync("op-project-create", "project.create", "project-product-1", {
            "title": "Compiler release", "description": "Ship the release",
        })
        self.assertEqual(created["status"], "APPLIED")

        task = self.sync("op-project-task", "project.task.create", "project-product-1", {
            "task_id": "task-project-check-1",
            "title": "Write release notes",
            "estimated_total_effort_minutes": 60,
            "actual_cutoff": {"state": "ABSENT"},
            "splittable": True,
            "min_chunk_minutes": 15,
            "max_chunk_minutes": 60,
        })
        self.assertEqual(task["status"], "APPLIED", task)

        milestone = self.sync("op-project-milestone", "milestone.create", "milestone-project-1", {
            "project_id": "project-product-1",
            "title": "Release candidate",
            "marker_at": (NOW + timedelta(days=2)).isoformat(),
            "role": "INTERMEDIATE",
        })
        self.assertEqual(milestone["status"], "APPLIED", milestone)

        project = self.client.get("/api/v1/projects/project-product-1")
        self.assertEqual(project.status_code, 200, project.text)
        payload = project.json()
        self.assertEqual(payload["progress"]["percent"], 0)
        self.assertEqual(payload["progress"]["tasks_total"], 1)
        self.assertEqual(payload["members"][0]["id"], "task-project-check-1")
        self.assertEqual(payload["milestones"][0]["id"], "milestone-project-1")

        done = self.sync("op-project-task-done", "task.complete", "task-project-check-1", {
            "occurred_at": NOW.isoformat(),
        })
        self.assertEqual(done["status"], "APPLIED")
        payload = self.client.get("/api/v1/projects/project-product-1").json()
        self.assertEqual(payload["progress"]["percent"], 100)
        self.assertEqual(payload["progress"]["tasks_completed"], 1)
        self.assertEqual(payload["status"], "ACTIVE", "child completion must not silently close Project")

        closed = self.sync("op-project-complete", "project.complete", "project-product-1", {
            "expected_version": payload["version"],
        })
        self.assertEqual(closed["entity"]["status"], "COMPLETED")

    def test_project_data_is_account_scoped(self):
        self.sync("op-project-private", "project.create", "project-private-1", {"title": "Private project"})
        with TestClient(create_app(self.db, account_id="b", principal_id="other", now=lambda: NOW)) as other:
            self.assertEqual(other.get("/api/v1/projects").json(), [])
            self.assertEqual(other.get("/api/v1/projects/project-private-1").status_code, 404)


if __name__ == "__main__":
    unittest.main()
