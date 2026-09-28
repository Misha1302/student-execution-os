"""R6: capability grants, the external REST surface and MCP, over the canonical owners.

LOCAL INTEGRATION: the real FastAPI app and SQLite database; nothing is mocked.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.persistence.sqlite import SCHEMA_VERSION
from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig
from tests.asgi_client import TestClient
from tests.rollback_chain import roll_back_newer_than

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
def secret_of(token: str) -> str:
    """The secret part of botay_cap_<32 hex id>_<secret> (the secret itself may contain '_')."""
    return token.removeprefix("Bearer ")[len("botay_cap_") + 33:]


ROLLBACK = Path("src/student_execution_os/persistence/rollback/025_capability_grants_down.sql")


class CapabilityApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "caps.sqlite")
        self.now = NOW
        self.client = TestClient(create_app(self.db, auth=AuthConfig(password_scrypt_n=2**10),
                                            now=lambda: self.now))
        self.alice, self.alice_account = self.register("alice")
        self.bob, self.bob_account = self.register("bob")

    # ---- helpers ------------------------------------------------------------------

    def register(self, login: str) -> tuple[dict[str, str], str]:
        response = self.client.post("/api/v1/auth/register", json={"login": login, "password": "correct horse"})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        return {"Authorization": f"Bearer {body['token']}"}, body["user"]["account_id"]

    def grant(self, scopes: list[str], headers=None, **extra) -> tuple[dict[str, str], dict]:
        response = self.client.post("/api/v1/settings/capabilities", headers=headers or self.alice,
                                    json={"label": "ChatGPT", "scopes": scopes, **extra})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        return {"Authorization": f"Bearer {body['token']}"}, body

    def sync(self, headers, *ops) -> list[dict]:
        response = self.client.post("/api/v1/sync", headers=headers, json={"operations": list(ops)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["results"]

    def ext_ops(self, token, *ops):
        return self.client.post("/api/v1/ext/operations", headers=token, json={"operations": list(ops)})

    def mcp(self, token, method: str, params: dict | None = None, request_id: int | None = 1):
        message = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            message["id"] = request_id
        if params is not None:
            message["params"] = params
        return self.client.post("/mcp", headers=token, json=message)

    def call(self, token, name: str, arguments: dict | None = None) -> dict:
        response = self.mcp(token, "tools/call", {"name": name, "arguments": arguments or {}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def task_titles(self, headers) -> list[str]:
        return sorted(task["title"] for task in self.client.get("/api/v1/tasks", headers=headers).json())

    # ---- grant lifecycle ------------------------------------------------------------

    def test_token_is_shown_once_stored_hashed_and_revocation_is_immediate(self):
        token, created = self.grant(["tasks:read"])
        secret = token["Authorization"].removeprefix("Bearer ")
        self.assertTrue(secret.startswith("botay_cap_"))
        listed = self.client.get("/api/v1/settings/capabilities", headers=self.alice).json()
        self.assertEqual([grant["id"] for grant in listed["grants"]], [created["grant"]["id"]])
        self.assertNotIn(secret, json.dumps(listed))
        self.assertNotIn("token_hash", json.dumps(listed))
        with sqlite3.connect(self.db) as conn:
            dump = "\n".join(conn.iterdump())
        self.assertNotIn(secret, dump)
        self.assertEqual(len(secret_of(secret)), 43)
        self.assertNotIn(secret_of(secret), dump)
        self.assertEqual(self.client.get("/api/v1/ext/tasks", headers=token).status_code, 200)
        revoked = self.client.delete(f"/api/v1/settings/capabilities/{created['grant']['id']}", headers=self.alice)
        self.assertTrue(revoked.json()["revoked"])
        after = self.client.get("/api/v1/ext/tasks", headers=token)
        self.assertEqual((after.status_code, after.json()["error"]["code"]), (401, "INVALID_GRANT"))
        self.assertIn("Bearer", after.headers["www-authenticate"])
        self.assertEqual(self.mcp(token, "ping").status_code, 401)

    def test_grants_are_owned_by_one_account_and_cannot_manage_grants(self):
        token, created = self.grant(["tasks:read", "tasks:write"])
        grant_id = created["grant"]["id"]
        # Bob can neither see nor revoke Alice's grant.
        self.assertEqual(self.client.get("/api/v1/settings/capabilities", headers=self.bob).json()["grants"], [])
        self.assertEqual(self.client.delete(f"/api/v1/settings/capabilities/{grant_id}", headers=self.bob).status_code,
                         404)
        # A grant token is not a session: no management, no regular app API.
        for method, path in (("get", "/api/v1/settings/capabilities"), ("post", "/api/v1/settings/capabilities"),
                             ("get", "/api/v1/tasks"), ("get", "/api/v1/settings/llm"), ("post", "/api/v1/sync")):
            response = getattr(self.client, method)(path, headers=token, **({"json": {}} if method == "post" else {}))
            self.assertEqual(response.status_code, 401, (method, path))
        # A session token is not a grant.
        self.assertEqual(self.client.get("/api/v1/ext/tasks", headers=self.alice).status_code, 401)
        self.assertEqual(self.mcp(self.alice, "ping").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/ext/tasks").status_code, 401)

    def test_scope_validation_and_expiry(self):
        for scopes in ([], ["tasks:admin"], ["destructive"], "tasks:read"):
            response = self.client.post("/api/v1/settings/capabilities", headers=self.alice,
                                        json={"label": "x", "scopes": scopes})
            self.assertEqual(response.status_code, 422, scopes)
        for days in (0, 367, True, "7"):
            response = self.client.post("/api/v1/settings/capabilities", headers=self.alice,
                                        json={"label": "x", "scopes": ["tasks:read"], "expires_in_days": days})
            self.assertEqual(response.status_code, 422, days)
        token, created = self.grant(["tasks:read"], expires_in_days=1)
        self.assertEqual(created["grant"]["expires_at"], "2026-09-29T09:00:00+00:00")
        self.assertEqual(self.client.get("/api/v1/ext/tasks", headers=token).status_code, 200)
        self.now = NOW + timedelta(days=1)
        self.assertEqual(self.client.get("/api/v1/ext/tasks", headers=token).status_code, 401)

    # ---- reads -------------------------------------------------------------------

    def test_allowed_and_denied_reads_and_today_withholds_other_scopes(self):
        self.sync(self.alice, {"op_id": "note-op-0001", "type": "note.create", "entity_id": "note-alice-0001",
                               "payload": {"content": "private diary line"}})
        self.sync(self.alice, {"op_id": "task-op-0001", "type": "task.create", "entity_id": "task-alice-0001",
                               "payload": {"title": "Эссе по истории"}})
        token, _ = self.grant(["today:read", "tasks:read"])
        self.assertEqual([t["title"] for t in self.client.get("/api/v1/ext/tasks", headers=token).json()],
                         ["Эссе по истории"])
        today = self.client.get("/api/v1/ext/today", headers=token).json()
        self.assertEqual((today["inbox_notes"], today["withheld"]), ([], ["inbox_notes"]))
        self.assertNotIn("private diary line", json.dumps(today, ensure_ascii=False))
        for path, scope in (("/api/v1/ext/notes", "notes:read"), ("/api/v1/ext/notes/note-alice-0001", "notes:read"),
                            ("/api/v1/ext/calendar", "calendar:read"), ("/api/v1/ext/events", "calendar:read"),
                            ("/api/v1/ext/reminders", "reminders:read")):
            response = self.client.get(path, headers=token)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "CAPABILITY_DENIED"),
                             path)
            self.assertIn(scope, response.json()["error"]["message"])
        reader, _ = self.grant(["notes:read", "today:read"])
        self.assertIn("private diary line",
                      json.dumps(self.client.get("/api/v1/ext/today", headers=reader).json(), ensure_ascii=False))
        self.assertEqual(self.client.get("/api/v1/ext/notes/note-alice-0001", headers=reader).status_code, 200)

    def test_reads_never_cross_accounts(self):
        self.sync(self.bob, {"op_id": "bob-note-001", "type": "note.create", "entity_id": "note-bob-00001",
                             "payload": {"content": "bob secret"}})
        self.sync(self.bob, {"op_id": "bob-task-001", "type": "task.create", "entity_id": "task-bob-00001",
                             "payload": {"title": "bob task"}})
        token, _ = self.grant(["tasks:read", "notes:read", "today:read", "calendar:read"])
        self.assertEqual(self.client.get("/api/v1/ext/tasks", headers=token).json(), [])
        self.assertEqual(self.client.get("/api/v1/ext/notes", headers=token).json(), [])
        self.assertEqual(self.client.get("/api/v1/ext/notes/note-bob-00001", headers=token).status_code, 404)
        self.assertNotIn("bob", json.dumps(self.client.get("/api/v1/ext/today", headers=token).json()))

    # ---- mutations -----------------------------------------------------------------

    def test_allowed_mutation_is_the_canonical_sync_path_with_provenance(self):
        token, created = self.grant(["tasks:write"])
        op = {"op_id": "agent-op-0001", "type": "task.create", "entity_id": "task-agent-0001",
              "payload": {"title": "Подготовить доклад", "estimated_total_effort_minutes": 90}}
        first = self.ext_ops(token, op).json()["results"][0]
        self.assertEqual((first["status"], first["replayed"]), ("APPLIED", False))
        # The same canonical state the app reads, not a parallel store.
        self.assertEqual(self.task_titles(self.alice), ["Подготовить доклад"])
        replay = self.ext_ops(token, op).json()["results"][0]
        self.assertEqual((replay["status"], replay["replayed"]), ("APPLIED", True))
        # The app's own offline outbox replaying the same op_id also gets the recorded result.
        self.assertTrue(self.sync(self.alice, op)[0]["replayed"])
        reused = self.ext_ops(token, {**op, "payload": {"title": "другое"}}).json()["results"][0]
        self.assertEqual((reused["status"], reused["code"]), ("REJECTED", "OP_ID_REUSED"))
        self.assertEqual(self.task_titles(self.alice), ["Подготовить доклад"])
        with sqlite3.connect(self.db) as conn:
            principal = conn.execute("SELECT principal_id FROM client_operations WHERE op_id='agent-op-0001'").fetchone()
            actor = conn.execute("SELECT actor_category FROM audit_changes WHERE entity_id='task-agent-0001' "
                                 "ORDER BY server_revision LIMIT 1").fetchone()
        self.assertEqual(principal, (f"grant:{created['grant']['id']}",))
        self.assertEqual(actor, ("USER_VIA_LLM",))

    def test_denied_mutations_apply_nothing(self):
        token, _ = self.grant(["tasks:write"])
        mixed = self.ext_ops(token,
                             {"op_id": "mixed-op-0001", "type": "task.create", "entity_id": "task-mixed-0001",
                              "payload": {"title": "should not exist"}},
                             {"op_id": "mixed-op-0002", "type": "note.create", "entity_id": "note-mixed-0001",
                              "payload": {"content": "x"}})
        self.assertEqual((mixed.status_code, mixed.json()["error"]["code"]), (403, "CAPABILITY_DENIED"))
        self.assertEqual(self.task_titles(self.alice), [])
        for op_type in ("project.create", "constraint.create", "series.create", "series.holiday",
                        "execution.start", "calibration.set", "note.transcript.set", "", "task.nuke"):
            response = self.ext_ops(token, {"op_id": "deny-op-0001", "type": op_type, "entity_id": "x" * 10,
                                            "payload": {}})
            self.assertEqual(response.status_code, 403, op_type)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM client_operations WHERE op_id LIKE 'deny%' "
                                          "OR op_id LIKE 'mixed%'").fetchone()[0], 0)

    def test_destructive_operations_need_the_destructive_scope(self):
        self.sync(self.alice, {"op_id": "seed-op-0001", "type": "task.create", "entity_id": "task-doomed-001",
                               "payload": {"title": "doomed"}})
        writer, _ = self.grant(["tasks:write"])
        delete = {"op_id": "del-op-00001", "type": "task.delete", "entity_id": "task-doomed-001", "payload": {}}
        self.assertEqual(self.ext_ops(writer, delete).status_code, 403)
        self.assertEqual(self.task_titles(self.alice), ["doomed"])
        destroyer, _ = self.grant(["tasks:write", "destructive"])
        self.assertEqual(self.ext_ops(destroyer, delete).json()["results"][0]["status"], "APPLIED")
        self.assertEqual(self.task_titles(self.alice), [])

    def test_cross_account_mutation_is_not_found_or_refused(self):
        self.sync(self.bob, {"op_id": "bob-task-001", "type": "task.create", "entity_id": "task-bob-00001",
                             "payload": {"title": "bob task"}})
        token, _ = self.grant(["tasks:write", "destructive"])
        results = self.ext_ops(
            token,
            {"op_id": "cross-op-001", "type": "task.update", "entity_id": "task-bob-00001", "payload": {"title": "pwned"}},
            {"op_id": "cross-op-002", "type": "task.delete", "entity_id": "task-bob-00001", "payload": {}},
            {"op_id": "cross-op-003", "type": "task.create", "entity_id": "task-bob-00001", "payload": {"title": "x"}},
        ).json()["results"]
        self.assertEqual([r["status"] for r in results], ["REJECTED", "REJECTED", "REJECTED"])
        self.assertEqual(self.task_titles(self.bob), ["bob task"])
        # Linking one of Alice's notes to Bob's task cannot reach across accounts either.
        linker, _ = self.grant(["notes:write"])
        created, link = self.ext_ops(
            linker,
            {"op_id": "link-op-0001", "type": "note.create", "entity_id": "note-alice-link",
             "payload": {"content": "mine"}},
            {"op_id": "link-op-0002", "type": "note.link", "entity_id": "note-alice-link",
             "payload": {"target_kind": "TASK", "target_id": "task-bob-00001"}},
        ).json()["results"]
        self.assertEqual((created["status"], link["status"], link["code"]), ("APPLIED", "REJECTED", "NOT_FOUND"))
        self.assertNotIn("bob task", json.dumps(results))

    def test_malformed_operations_and_lifecycle_conflict(self):
        token, _ = self.grant(["tasks:write"])
        for body in ({"operations": []}, {"operations": "task.create"}, {}):
            self.assertEqual(self.client.post("/api/v1/ext/operations", headers=token, json=body).status_code, 422)
        malformed = self.ext_ops(token, {"type": "task.create", "entity_id": "task-no-opid-1",
                                         "payload": {"title": "x"}}).json()["results"][0]
        self.assertEqual((malformed["status"], malformed["code"]), ("REJECTED", "MALFORMED_OPERATION"))
        invalid = self.ext_ops(token, {"op_id": "bad-title-001", "type": "task.create", "entity_id": "task-bad-00001",
                                       "payload": {"title": ""}}).json()["results"][0]
        self.assertEqual(invalid["status"], "REJECTED")
        # The user cancels on their phone; the agent's later "start" is a CONFLICT, not forced.
        self.sync(self.alice, {"op_id": "seed-op-0002", "type": "task.create", "entity_id": "task-conflict-1",
                               "payload": {"title": "контрольная"}},
                  {"op_id": "seed-op-0003", "type": "task.cancel", "entity_id": "task-conflict-1", "payload": {}})
        conflict = self.ext_ops(token, {"op_id": "start-op-001", "type": "task.start",
                                        "entity_id": "task-conflict-1", "payload": {}}).json()["results"][0]
        self.assertEqual((conflict["status"], conflict["code"]), ("CONFLICT", "TASK_CANCELLED"))

    # ---- MCP ---------------------------------------------------------------------

    def test_mcp_handshake_tool_list_and_protocol_errors(self):
        token, _ = self.grant(["tasks:read", "tasks:write"])
        init = self.mcp(token, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                              "clientInfo": {"name": "test", "version": "1"}}).json()["result"]
        self.assertEqual(init["protocolVersion"], "2025-03-26")
        self.assertEqual(init["serverInfo"]["name"], "botay")
        self.assertIn("tasks:write", init["instructions"])
        self.assertEqual(self.mcp(token, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
                         ["protocolVersion"], "2025-06-18")
        notification = self.mcp(token, "notifications/initialized", request_id=None)
        self.assertEqual(notification.status_code, 202)
        names = {tool["name"] for tool in self.mcp(token, "tools/list").json()["result"]["tools"]}
        self.assertEqual(names, {"get_capabilities", "list_tasks", "create_task", "apply_operations"})
        self.assertEqual(self.mcp(token, "resources/list").json()["error"]["code"], -32601)
        self.assertEqual(self.mcp(token, "tools/call", {"name": "drop_database"}).json()["error"]["code"], -32602)
        batch = self.client.post("/mcp", headers=token, json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(batch.status_code, 400)
        self.assertEqual(self.client.post("/mcp", headers=token, json={"id": 1, "method": "ping"}).json()
                         ["error"]["code"], -32600)
        self.assertEqual(self.client.get("/mcp", headers=token).status_code, 405)

    def test_mcp_create_is_exactly_once_and_denials_are_tool_errors(self):
        token, _ = self.grant(["tasks:read", "tasks:write"])
        first = self.call(token, "create_task", {"op_id": "mcp-op-000001", "title": "Сдать лабу",
                                                 "estimated_total_effort_minutes": 60})
        self.assertFalse(first["isError"])
        self.assertEqual(first["structuredContent"]["results"][0]["status"], "APPLIED")
        retry = self.call(token, "create_task", {"op_id": "mcp-op-000001", "title": "Сдать лабу",
                                                 "estimated_total_effort_minutes": 60})
        self.assertTrue(retry["structuredContent"]["results"][0]["replayed"])
        self.assertEqual(self.task_titles(self.alice), ["Сдать лабу"])
        tasks = self.call(token, "list_tasks")["structuredContent"]["items"]
        self.assertEqual([task["title"] for task in tasks], ["Сдать лабу"])
        denied = self.call(token, "create_note", {"op_id": "mcp-op-000002", "content": "x"})
        self.assertTrue(denied["isError"])
        self.assertEqual(denied["structuredContent"]["error"], "CAPABILITY_DENIED")
        self.assertEqual(self.call(token, "get_today")["structuredContent"]["required_scope"], "today:read")
        caps = self.call(token, "get_capabilities")["structuredContent"]
        self.assertIn("task.complete", caps["allowed_operations"])
        self.assertNotIn("task.delete", caps["allowed_operations"])
        bad = self.call(token, "apply_operations", {"operations": "nope"})
        self.assertEqual((bad["isError"], bad["structuredContent"]["error"]), (True, "VALIDATION_ERROR"))

    # ---- lifecycle ---------------------------------------------------------------

    def test_account_deletion_purges_grants_and_export_excludes_them(self):
        token, _ = self.grant(["tasks:read"])
        self.grant(["notes:read"], headers=self.bob)
        from dataclasses import asdict
        from student_execution_os.reliability import SQLiteDataLifecycle
        lifecycle = SQLiteDataLifecycle(self.db, now=lambda: self.now)
        exported = json.dumps(asdict(lifecycle.export_account(self.alice_account)), default=str)
        self.assertNotIn("capability_grants", exported)
        self.assertNotIn(secret_of(token["Authorization"]), exported)
        with sqlite3.connect(self.db) as conn:
            revision = conn.execute("SELECT server_revision FROM accounts WHERE id=?",
                                    (self.alice_account,)).fetchone()[0]
        result = lifecycle.delete_account(self.alice_account, expected_server_revision=revision,
                                          confirm_account_id=self.alice_account)
        self.assertEqual(result.deleted_rows["capability_grants"], 1)
        self.assertEqual(self.client.get("/api/v1/ext/tasks", headers=token).status_code, 401)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT account_id FROM capability_grants").fetchall(),
                             [(self.bob_account,)])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_v24_to_v25_migration_and_fail_closed_rollback(self):
        old = str(Path(self.tmp.name) / "v24.sqlite")
        migrations = Path("src/student_execution_os/persistence/migrations")
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for version in range(1, 25):
            conn.executescript(next(migrations.glob(f"{version:03d}_*.sql")).read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)", (version, NOW.isoformat()))
        conn.execute("INSERT INTO accounts(id,server_revision) VALUES ('upgrade-student',0)")
        conn.commit()
        conn.close()
        from student_execution_os.persistence import SQLiteCanonicalRepository
        with SQLiteCanonicalRepository(old) as repo:
            repo.initialize()
            repo.initialize()
            self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
            self.assertGreaterEqual(SCHEMA_VERSION, 25)
            self.assertEqual(repo.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(repo.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn = sqlite3.connect(old)
        roll_back_newer_than(conn, 25)
        conn.executescript(ROLLBACK.read_text(encoding="utf-8"))
        self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 24)
        conn.close()
        # With a live grant the rollback refuses instead of silently discarding it.
        _token, created = self.grant(["tasks:read"])
        conn = sqlite3.connect(self.db)
        roll_back_newer_than(conn, 25)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "revoked first"):
            conn.executescript(ROLLBACK.read_text(encoding="utf-8"))
        conn.close()
        self.client.delete(f"/api/v1/settings/capabilities/{created['grant']['id']}", headers=self.alice)
        conn = sqlite3.connect(self.db)
        roll_back_newer_than(conn, 25)  # the app re-migrated on its request
        conn.executescript(ROLLBACK.read_text(encoding="utf-8"))
        self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 24)
        conn.close()


if __name__ == "__main__":
    unittest.main()
