from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from student_execution_os.agent import AuthenticatedPrincipal, SQLiteAssistantService
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import AuthorizationDenied, ValidationError
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository
from student_execution_os.sync.commands import SyncService
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)


class V12ExecutionLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "fresh.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize(); repo.create_account("a")
        self.client_cm = TestClient(create_app(self.db, account_id="a", principal_id="user", now=lambda: NOW))
        self.client = self.client_cm.__enter__()

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        self.tmp.cleanup()

    def op(self, op_id, kind, entity, payload=None):
        response = self.client.post("/api/v1/sync", json={"operations":[{
            "op_id": op_id, "type": kind, "entity_id": entity, "payload": payload or {},
        }]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["results"][0]

    def test_fresh_db_and_full_task_lifecycle_including_completed_open_and_reopen(self):
        self.assertEqual(self.client.get("/api/v1/health").json()["schema_version"], SCHEMA_VERSION)
        created = self.op("create-0001", "task.create", "task-0001", {
            "title":"Execution test", "estimated_total_effort_minutes":60,
            "actual_cutoff":{"state":"UNKNOWN"}, "splittable":False,
        })
        self.assertEqual(created["status"], "APPLIED")
        started = self.op("start-0001", "task.start", "task-0001")
        self.assertEqual(started["entity"]["started_at"], NOW.isoformat())
        progress = self.op("progress-0001", "task.progress", "task-0001", {"minutes":15})
        self.assertEqual(progress["entity"]["remaining_effort_minutes"], 45)
        completed = self.op("complete-0001", "task.complete", "task-0001")
        self.assertEqual(completed["entity"]["status"], "COMPLETED")
        loaded = next(x for x in self.client.get("/api/v1/tasks").json() if x["id"] == "task-0001")
        self.assertEqual(loaded["status"], "COMPLETED")
        reopened = self.op("reopen-0001", "task.reopen", "task-0001", {"remaining_effort_minutes":30})
        self.assertEqual(reopened["entity"]["status"], "ACTIVE")
        self.assertEqual(reopened["entity"]["remaining_effort_minutes"], 30)

    def test_operation_is_exactly_once_and_reuse_with_different_payload_is_rejected(self):
        operation = {"op_id":"create-0002", "type":"task.create", "entity_id":"task-0002",
                     "payload":{"title":"Once", "estimated_total_effort_minutes":30,
                                "actual_cutoff":{"state":"UNKNOWN"}, "splittable":False}}
        first = self.client.post("/api/v1/sync", json={"operations":[operation]}).json()["results"][0]
        replay = self.client.post("/api/v1/sync", json={"operations":[operation]}).json()["results"][0]
        self.assertFalse(first["replayed"]); self.assertTrue(replay["replayed"])
        changed = {**operation, "payload":{**operation["payload"], "title":"Different"}}
        rejected = self.client.post("/api/v1/sync", json={"operations":[changed]}).json()["results"][0]
        self.assertEqual(rejected["code"], "OP_ID_REUSED")
        with SQLiteCanonicalRepository(self.db) as repo:
            repo.initialize()
            self.assertEqual(repo.connection.execute("SELECT count(*) FROM obligations WHERE id='task-0002'").fetchone()[0], 1)

    def test_lifecycle_conflict_is_visible_and_does_not_overwrite_newer_state(self):
        self.op("create-0003", "task.create", "task-0003", {"title":"Conflict", "estimated_total_effort_minutes":30,
                "actual_cutoff":{"state":"UNKNOWN"}, "splittable":False})
        self.op("cancel-0003", "task.cancel", "task-0003")
        result = self.op("complete-0003", "task.complete", "task-0003")
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(result["entity"]["status"], "CANCELLED")

    def test_field_edits_preserve_newer_other_fields_and_progress_deltas_add(self):
        self.op("create-0004", "task.create", "task-0004", {"title":"Original", "estimated_total_effort_minutes":60,
                "actual_cutoff":{"state":"UNKNOWN"}, "splittable":False})
        newer_target = "2026-09-26T12:00:00+00:00"
        self.op("device-b-0001", "task.update", "task-0004", {"target_at":newer_target})
        edited = self.op("device-a-0001", "task.update", "task-0004", {"title":"Offline title"})
        self.assertEqual(edited["entity"]["target_at"], newer_target)
        self.op("progress-a-01", "task.progress", "task-0004", {"minutes":15})
        second = self.op("progress-b-01", "task.progress", "task-0004", {"minutes":10})
        self.assertEqual(second["entity"]["remaining_effort_minutes"], 35)

    def test_invalid_assistant_provider_action_is_rejected_before_persistence(self):
        class BadProvider:
            name = "bad"
            def interpret(self, _text, _context):
                return {"message":"oops", "actions":[{"command":"DROP_DATABASE", "payload":{}, "confidence":1,
                    "unresolved_fields":[], "expected_version":None, "requires_confirmation":False}]}
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            service = SQLiteAssistantService(repo, AuthenticatedPrincipal("a", "user", "test"), BadProvider())
            with self.assertRaises(ValidationError): service.interpret("do it")
            self.assertEqual(repo.connection.execute("SELECT count(*) FROM assistant_batches").fetchone()[0], 0)

    def test_provider_cannot_disable_confirmation_for_destructive_action(self):
        self.op("create-0005", "task.create", "task-0005", {"title":"Protected", "estimated_total_effort_minutes":30,
                "actual_cutoff":{"state":"UNKNOWN"}, "splittable":False})
        class UnsafeProvider:
            name = "unsafe"
            def interpret(self, _text, _context):
                return {"message":"done", "actions":[{"command":"CANCEL_OBLIGATION",
                    "payload":{"obligation_id":"task-0005"}, "confidence":1,
                    "unresolved_fields":[], "expected_version":1, "requires_confirmation":False}]}
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            service = SQLiteAssistantService(repo, AuthenticatedPrincipal("a", "user", "test"), UnsafeProvider())
            preview = service.interpret("cancel it")
            action = preview["actions"][0]
            self.assertTrue(action["requires_confirmation"])
            with self.assertRaises(AuthorizationDenied):
                service.apply({"batch_id":preview["batch_id"], "action_ids":[action["id"]],
                               "confirmed_action_ids":[], "idempotency_key":"unsafe-apply-1"})
            self.assertEqual(repo.get_obligation("a", "task-0005").lifecycle_status.value, "ACTIVE")


if __name__ == "__main__": unittest.main()
