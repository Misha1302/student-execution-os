"""Upgrading a populated schema-v13 database (the previous production release) to v14."""
from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory, HardCutoff, Importance, ObligationCategory
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository
from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

NOW = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
MIGRATIONS = Path(__file__).resolve().parents[2] / "src/student_execution_os/persistence/migrations"


def build_v13(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for script in sorted(MIGRATIONS.glob("*.sql")):
        version = int(script.name[:3])
        if version > 13:
            continue
        conn.executescript(script.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, NOW.isoformat()))
    conn.commit()
    conn.close()


class V14MigrationTest(unittest.TestCase):
    def test_populated_v13_database_upgrades_and_keeps_working(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "prod-v13.sqlite")
            build_v13(db)
            # Populate while still at v13 (no initialize(): nothing may upgrade yet).
            repo = SQLiteCanonicalRepository(db, clock=FrozenClock(NOW))
            try:
                repo.create_account("acct-old")
                repo.create_task(account_id="acct-old", obligation_id="task-old", title="Old essay",
                                 category=ObligationCategory.GENERAL, importance=Importance.NORMAL,
                                 estimated_total_effort_minutes=60, remaining_effort_minutes=60, splittable=False,
                                 actual_cutoff=HardCutoff.absent(), actor=ActorCategory.USER_UI)
                self.assertEqual(repo.schema_version(), 13)
            finally:
                repo.close()
            client = TestClient(create_app(db, account_id="acct-old", principal_id="u", now=lambda: NOW))
            health = client.get("/api/v1/health").json()
            self.assertEqual(health["schema_version"], SCHEMA_VERSION)
            self.assertGreaterEqual(SCHEMA_VERSION, 14)
            task = client.get("/api/v1/tasks/task-old").json()
            self.assertEqual((task["title"], task["version"]), ("Old essay", 1))
            done = client.post("/api/v1/sync", json={"operations": [{
                "op_id": "op-complete-old", "type": "task.complete", "entity_id": "task-old", "payload": {}}]}).json()
            self.assertEqual(done["results"][0]["status"], "APPLIED", done)
            llm = client.get("/api/v1/settings/llm").json()
            self.assertEqual(llm["source"], "NONE")
            export = SQLiteDataLifecycle(db, now=lambda: NOW).export_account("acct-old")
            self.assertIn("llm_entitlements", export.tables)
            self.assertNotIn("llm_credentials", export.tables)
            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as repo:
                repo.initialize()  # re-entrant
                self.assertEqual([r[0] for r in repo.connection.execute("SELECT version FROM schema_migrations")],
                                 list(range(1, SCHEMA_VERSION + 1)))
                self.assertEqual(repo.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_documented_rollback_leaves_a_v13_shaped_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "rollback.sqlite")
            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as repo:
                repo.initialize()
                repo.create_account("a")
            conn = sqlite3.connect(db)
            # Newer releases are rolled back first (see their ADRs), then v14.
            conn.executescript("DROP TABLE reminders; DROP TABLE deleted_reminders; "
                               "DELETE FROM schema_migrations WHERE version=16;")
            conn.executescript("DROP TABLE deleted_obligations; DROP TABLE event_reminders; "
                               "DROP TABLE task_progress_counts; DELETE FROM schema_migrations WHERE version=15;")
            conn.executescript("DROP TABLE llm_credentials; DROP TABLE llm_entitlements; "
                               "DELETE FROM schema_migrations WHERE version=14;")
            conn.commit()
            reference = str(Path(tmp) / "v13.sqlite")
            build_v13(reference)
            conn.close()

            def tables(path):
                with closing(sqlite3.connect(path)) as c:
                    return sorted(r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'"))

            self.assertEqual(tables(db), tables(reference))


if __name__ == "__main__":
    unittest.main()
