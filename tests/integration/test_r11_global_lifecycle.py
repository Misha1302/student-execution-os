"""R11: global data lifecycle over a database populated with every feature.

LOCAL INTEGRATION: real app (session mode), real SQLite, real SourceApplier/providers;
no mocks except the absence of network. Everything is created through the same APIs the
clients use, then:

    populated DB -> backup -> clean target -> restore -> integrity/FK -> semantic compare

plus account export (intended data in, secrets out) and account deletion (right account
purged, others untouched, integrity kept), and an upgrade from a v22 database that was
populated by the real R2-era code.
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from student_execution_os.agent.credentials import CredentialCipher
from student_execution_os.persistence.sqlite import SCHEMA_VERSION
from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig
from tests.asgi_client import TestClient
from tests.rollback_chain import roll_back_newer_than

NOW = datetime(2026, 9, 21, 7, 0, tzinfo=timezone.utc)
FIXTURE_ICS = Path("tests/fixtures/academic_schedule_realistic.ics")
V22_FIXTURE = Path("tests/fixtures/upgrade/v22_populated_by_r2.sqlite.gz")
MASTER = CredentialCipher.generate_key()
FEED_KEY = CredentialCipher.generate_key()
BYOK = "sk-lifecycle-secret-0123456789abcdef"

# Every read a client makes; compared before backup and after a clean restore.
READS = ("/api/v1/tasks", "/api/v1/events", "/api/v1/notes", "/api/v1/reminders", "/api/v1/projects",
         "/api/v1/calendar", "/api/v1/settings/academic-schedule", "/api/v1/settings/capabilities",
         "/api/v1/settings/llm", "/api/v1/groups", "/api/v1/execution/active", "/api/v1/notification-preferences")


class GlobalLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = patch.dict(os.environ, {"SEOS_CREDENTIAL_KEY": MASTER, "SEOS_ACADEMIC_FEED_KEY": FEED_KEY})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.db = str(self.tmp / "live.sqlite")
        self.client = self.app(self.db)
        self.anna = self.register("anna")
        self.boris = self.register("boris")
        self.populate()

    def app(self, database):
        return TestClient(create_app(database, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))

    def register(self, login):
        body = self.client.post("/api/v1/auth/register", json={"login": login, "password": "correct horse"}).json()
        return {"headers": {"Authorization": f"Bearer {body['token']}"}, "account": body["user"]["account_id"]}

    def call(self, method, path, who, body=None, status=(200, 201), client=None, **kwargs):
        response = getattr(client or self.client, method)(path, headers=who["headers"],
                                                          **({"json": body} if body is not None else {}), **kwargs)
        self.assertIn(response.status_code, status, (method, path, response.text))
        return response.json()

    def sync(self, who, *ops):
        results = self.call("post", "/api/v1/sync", who, {"operations": list(ops)})["results"]
        for result in results:
            self.assertIn(result["status"], ("APPLIED", "NOOP"), result)
        return results

    def populate(self):
        a = self.anna
        self.sync(a,
            {"op_id": "life-task-001", "type": "task.create", "entity_id": "life-task-essay",
             "payload": {"title": "Эссе", "estimated_total_effort_minutes": 120, "importance": "HIGH",
                         "actual_cutoff": {"state": "KNOWN", "at": "2026-09-30T20:59:00+00:00"}}},
            {"op_id": "life-task-002", "type": "task.progress", "entity_id": "life-task-essay", "payload": {"minutes": 30}},
            {"op_id": "life-exec-001", "type": "execution.start", "entity_id": "life-execution-1",
             "payload": {"task_id": "life-task-essay", "occurred_at": NOW.isoformat()}},
            {"op_id": "life-event-01", "type": "event.create", "entity_id": "life-event-meet",
             "payload": {"title": "Научрук", "starts_at": "2026-09-23T12:00:00+00:00",
                         "ends_at": "2026-09-23T13:00:00+00:00", "remind_before_minutes": 30}},
            {"op_id": "life-note-001", "type": "note.create", "entity_id": "life-note-ideas",
             "payload": {"content": "идеи для курсовой"}},
            {"op_id": "life-rem-0001", "type": "reminder.create", "entity_id": "life-reminder-1",
             "payload": {"title": "Тетради", "remind_at": "2026-09-22T15:00:00+00:00"}},
            {"op_id": "life-proj-001", "type": "project.create", "entity_id": "life-project-1",
             "payload": {"title": "Курсовая"}},
            {"op_id": "life-rout-001", "type": "routine.create", "entity_id": "life-routine-1",
             "payload": {"title": "Английский", "dtstart_local": "2026-09-22T18:00", "effort_minutes": 30,
                         "recurrence_rule": "FREQ=DAILY;COUNT=5", "timezone_name": "Europe/Moscow"}},
            {"op_id": "life-seri-001", "type": "series.create", "entity_id": "life-series-pe",
             "payload": {"title": "Физкультура", "dtstart_local": "2026-09-22T08:00:00", "duration_minutes": 90,
                         "recurrence_rule": "FREQ=WEEKLY;COUNT=10", "timezone_name": "Europe/Moscow"}},
            {"op_id": "life-seri-002", "type": "series.occurrence.move", "entity_id": "life-series-pe",
             "payload": {"template_id": "life-series-pe", "original_recurrence_id": "2026-09-29T08:00:00",
                         "starts_local": "2026-09-29T10:00:00"}},
        )
        # Imported academic schedule (SOURCE) with a personal USER change on one class.
        imported_response = self.client.post("/api/v1/settings/academic-schedule/import",
                                             content=FIXTURE_ICS.read_bytes(),
                                             headers={**a["headers"], "X-Calendar-Name": "HSE"})
        self.assertEqual(imported_response.status_code, 200, imported_response.text)
        imported = [o for o in self.call("get", "/api/v1/calendar", a)["occurrences"]
                    if o["template_id"].startswith("src-series-") and not o["cancelled"]]
        self.assertTrue(imported)
        target = imported[0]
        self.sync(a, {"op_id": "life-src-user1", "type": "series.occurrence.update", "entity_id": target["template_id"],
                      "payload": {"template_id": target["template_id"],
                                  "original_recurrence_id": target["original_recurrence_id"], "note": "сесть ближе"}})
        # Group with a shared class, Boris as a member.
        self.call("post", "/api/v1/groups", a, {"id": "life-group-01", "name": "БИ-24", "timezone_name": "Europe/Moscow"})
        code = self.call("post", "/api/v1/groups/life-group-01/invitations", a, {})["code"]
        self.call("post", "/api/v1/groups/join", self.boris, {"code": code})
        self.call("put", "/api/v1/groups/life-group-01/schedule/life-g-series", a, {
            "kind": "SERIES", "expected_revision": 0, "item": {
                "title": "Матанализ", "dtstart_local": "2026-09-22T12:00:00", "duration_minutes": 90,
                "recurrence_rule": "FREQ=WEEKLY;COUNT=10", "timezone_name": "Europe/Moscow"}})
        # External capability grant, OAuth client, BYOK credential.
        self.grant = self.call("post", "/api/v1/settings/capabilities", a, {"label": "Codex", "scopes": ["tasks:read"]})
        self.call("put", "/api/v1/settings/llm", a, {"provider": "openai", "model": "gpt-5-mini", "api_key": BYOK})
        self.sync(self.boris, {"op_id": "life-task-b01", "type": "task.create", "entity_id": "life-task-boris",
                               "payload": {"title": "Лабораторная"}})

    def snapshot(self, client, who):
        state = {}
        for path in READS:
            body = client.get(path, headers=who["headers"]).json()
            state[path] = body
        return json.loads(json.dumps(state, sort_keys=True, default=str))

    def test_backup_clean_restore_is_semantically_identical_and_isolated(self):
        before = {name: self.snapshot(self.client, who) for name, who in (("anna", self.anna), ("boris", self.boris))}
        lifecycle = SQLiteDataLifecycle(self.db, now=lambda: NOW)
        backup = self.tmp / "backups" / "botay.sqlite"
        manifest = lifecycle.create_backup(backup)
        self.assertEqual((manifest.integrity_check, manifest.schema_version, manifest.account_count),
                         ("ok", SCHEMA_VERSION, 2))
        restored = self.tmp / "clean" / "restored.sqlite"
        result = SQLiteDataLifecycle.restore_backup(backup, restored)
        self.assertEqual((result.integrity_check, result.foreign_key_violations, result.sha256_verified),
                         ("ok", 0, True))
        client = self.app(str(restored))  # a clean server on the restored file
        after = {name: self.snapshot(client, who) for name, who in (("anna", self.anna), ("boris", self.boris))}
        self.assertEqual(after, before)
        # Credentials still work where they should: session, grant, encrypted BYOK and feed key.
        token = {"Authorization": f"Bearer {self.grant['token']}"}
        self.assertEqual([t["id"] for t in client.get("/api/v1/ext/tasks", headers=token).json()],
                         [t["id"] for t in before["anna"]["/api/v1/tasks"]])
        self.assertEqual(after["anna"]["/api/v1/settings/llm"]["source"], "USER_BYOK")
        # Boris never sees Anna's private data, before or after.
        boris_text = json.dumps(after["boris"], ensure_ascii=False)
        for private in ("Эссе", "идеи для курсовой", "Тетради", "сесть ближе", "Научрук"):
            self.assertNotIn(private, boris_text)
        self.assertIn("Матанализ", boris_text)
        with sqlite3.connect(restored) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_export_contains_the_users_data_and_no_secrets(self):
        exported = SQLiteDataLifecycle(self.db, now=lambda: NOW).export_account(self.anna["account"])
        text = json.dumps(asdict(exported), ensure_ascii=False, default=str)
        for mine in ("Эссе", "идеи для курсовой", "Тетради", "Курсовая", "Английский", "Физкультура", "сесть ближе"):
            self.assertIn(mine, text)
        self.assertNotIn("Лабораторная", text)  # Boris's data
        secret_part = self.grant["token"][len("botay_cap_") + 33:]
        for secret in (BYOK, secret_part, "correct horse", MASTER, FEED_KEY):
            self.assertNotIn(secret, text)
        for table in ("llm_credentials", "auth_sessions", "auth_users", "capability_grants",
                      "academic_schedule_connections", "oauth_authorizations"):
            self.assertNotIn(table, exported.tables)

    def test_deleting_one_account_purges_it_and_keeps_everyone_else_intact(self):
        boris_before = self.snapshot(self.client, self.boris)
        with sqlite3.connect(self.db) as conn:
            revision = conn.execute("SELECT server_revision FROM accounts WHERE id=?",
                                    (self.anna["account"],)).fetchone()[0]
        SQLiteDataLifecycle(self.db, now=lambda: NOW).delete_account(
            self.anna["account"], expected_server_revision=revision, confirm_account_id=self.anna["account"])
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if "account_id" in columns:
                    left = conn.execute(f"SELECT count(*) FROM {table} WHERE account_id=?",
                                        (self.anna["account"],)).fetchone()[0]
                    if table == "account_deletion_tombstones":
                        # Deliberately retained: id + deletion metadata only, blocks id reuse
                        # until purge_after; no user content.
                        self.assertEqual(left, 1)
                        self.assertEqual(columns, {"account_id", "deletion_id", "deleted_at", "purge_after",
                                                   "policy_version", "retained_reason"})
                    else:
                        self.assertEqual(left, 0, table)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.client.get("/api/v1/tasks", headers=self.anna["headers"]).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/ext/tasks",
                                         headers={"Authorization": f"Bearer {self.grant['token']}"}).status_code, 401)
        boris_after = self.snapshot(self.client, self.boris)
        # The group outlives its owner and passes to Boris; everything else of his is unchanged.
        self.assertEqual(boris_after["/api/v1/groups"][0]["role"], "OWNER")
        for path in READS:
            if path != "/api/v1/groups":
                self.assertEqual(boris_after[path], boris_before[path], path)


class UpgradeFromRealOldDataTest(unittest.TestCase):
    def test_database_written_by_r2_code_upgrades_reads_and_rolls_back_losslessly(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        db = tmp / "v22.sqlite"
        db.write_bytes(gzip.decompress(V22_FIXTURE.read_bytes()))
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 22)
            original = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                        for t in ("accounts", "obligations", "tasks", "events", "notes", "reminders", "projects",
                                  "client_operations", "auth_users")}
        client = TestClient(create_app(str(db), auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW))
        login = client.post("/api/v1/auth/login", json={"login": "legacy-anna", "password": "correct horse"})
        self.assertEqual(login.status_code, 200, login.text)
        headers = {"Authorization": f"Bearer {login.json()['token']}"}
        tasks = {t["id"]: t for t in client.get("/api/v1/tasks", headers=headers).json()}
        self.assertEqual(tasks["v22-task-essay"]["title"], "Эссе по истории")
        self.assertEqual(tasks["v22-task-done"]["status"], "COMPLETED")
        self.assertIn("Встреча с научруком", [e["title"] for e in client.get("/api/v1/events", headers=headers).json()])
        self.assertIn("Идеи для курсовой", json.dumps(client.get("/api/v1/notes", headers=headers).json(),
                                                      ensure_ascii=False))
        self.assertIn("Купить тетради", [r["title"] for r in client.get("/api/v1/reminders", headers=headers).json()])
        # An old op replayed by a phone that was offline during the upgrade stays exactly-once.
        replay = client.post("/api/v1/sync", headers=headers, json={"operations": [{
            "op_id": "v22-task-0002", "type": "task.progress", "entity_id": "v22-task-essay", "payload": {"minutes": 30}}]})
        self.assertTrue(replay.json()["results"][0]["replayed"])
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            upgraded = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in original}
        self.assertEqual({k: v for k, v in upgraded.items() if k not in ("client_operations",)},
                         {k: v for k, v in original.items() if k not in ("client_operations",)})
        # The documented rollback back to v22 is lossless for this data.
        with sqlite3.connect(db) as conn:
            roll_back_newer_than(conn, 22)
            self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 22)
            rolled = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in original}
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual({k: v for k, v in rolled.items() if k != "client_operations"},
                         {k: v for k, v in original.items() if k != "client_operations"})


if __name__ == "__main__":
    unittest.main()
