"""Upgrade a populated schema-v21 database to botay schema v22 and document rollback shape."""
from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from datetime import timedelta

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import (
    ActorCategory, EventTimeSemantics, HardCutoff, Importance, ObligationCategory,
)
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient
from tests.rollback_chain import roll_back_newer_than

NOW = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
MIGRATIONS = Path(__file__).resolve().parents[2] / "src/student_execution_os/persistence/migrations"


def build_v21(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for script in sorted(MIGRATIONS.glob("*.sql")):
        version = int(script.name[:3])
        if version > 21:
            continue
        conn.executescript(script.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, NOW.isoformat()))
    conn.commit()
    conn.close()


class V22BotayMigrationTests(unittest.TestCase):
    def test_populated_v21_upgrades_to_v22_without_losing_existing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "prod-v21.sqlite")
            build_v21(db)
            repo = SQLiteCanonicalRepository(db, clock=FrozenClock(NOW))
            try:
                repo.create_account("existing")
                self.assertEqual(repo.schema_version(), 21)
            finally:
                repo.close()

            client = TestClient(create_app(db, account_id="existing", principal_id="u", now=lambda: NOW))
            health = client.get("/api/v1/health").json()
            self.assertGreaterEqual(SCHEMA_VERSION, 22)
            self.assertEqual(health["schema_version"], SCHEMA_VERSION)
            created = client.post("/api/v1/sync", json={"operations": [{
                "op_id": "op-note-after-upgrade", "type": "note.create", "entity_id": "note-upgraded",
                "payload": {"content": "survived upgrade", "source_kind": "CAPTURE"},
            }]})
            self.assertEqual(created.status_code, 200, created.text)
            self.assertEqual(created.json()["results"][0]["status"], "APPLIED")
            self.assertEqual(client.get("/api/v1/notes").json()[0]["content"], "survived upgrade")
            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as check:
                check.initialize()
                self.assertEqual(
                    [r[0] for r in check.connection.execute("SELECT version FROM schema_migrations")],
                    list(range(1, SCHEMA_VERSION + 1)),
                )
                self.assertEqual(check.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_populated_v21_keeps_tasks_events_and_notes_can_link_to_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "populated-v21.sqlite")
            build_v21(db)
            # Populate while still at v21 (no initialize(): nothing may upgrade yet).
            repo = SQLiteCanonicalRepository(db, clock=FrozenClock(NOW))
            try:
                repo.create_account("acct-v21")
                repo.create_task(account_id="acct-v21", obligation_id="task-v21", title="Лабораторная",
                                 category=ObligationCategory.HOMEWORK, importance=Importance.HIGH,
                                 estimated_total_effort_minutes=95, remaining_effort_minutes=95, splittable=True,
                                 actual_cutoff=HardCutoff.absent(), actor=ActorCategory.USER_UI)
                repo.create_event(account_id="acct-v21", obligation_id="event-v21", title="Семинар",
                                  time_semantics=EventTimeSemantics.FIXED_INTERVAL, actor=ActorCategory.USER_UI,
                                  starts_at=NOW + timedelta(hours=2), ends_at=NOW + timedelta(hours=3))
                self.assertEqual(repo.schema_version(), 21)
                before = {
                    table: repo.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("obligations", "tasks", "events")
                }
            finally:
                repo.close()

            client = TestClient(create_app(db, account_id="acct-v21", principal_id="u", now=lambda: NOW))
            self.assertEqual(client.get("/api/v1/health").json()["schema_version"], SCHEMA_VERSION)
            task = client.get("/api/v1/tasks/task-v21").json()
            self.assertEqual((task["title"], task["version"]), ("Лабораторная", 1))
            today = client.get("/api/v1/today").json()
            self.assertIn("event-v21", [event["id"] for event in today["events"]])

            ops = [
                {"op_id": "op-v22-note-1", "type": "note.create", "entity_id": "note-v22-1",
                 "payload": {"content": "Идея после апгрейда", "source_kind": "CAPTURE"}},
                {"op_id": "op-v22-link-1", "type": "note.link", "entity_id": "note-v22-1",
                 "payload": {"target_kind": "TASK", "target_id": "task-v21"}},
            ]
            results = client.post("/api/v1/sync", json={"operations": ops}).json()["results"]
            self.assertEqual([r["status"] for r in results], ["APPLIED", "APPLIED"])
            self.assertEqual(results[1]["entity"]["links"][0]["target_id"], "task-v21")
            # The same op_id with a different payload is a conflict, never a second Note.
            reused = client.post("/api/v1/sync", json={"operations": [{
                **ops[0], "payload": {"content": "другое", "source_kind": "CAPTURE"},
            }]}).json()["results"][0]
            self.assertEqual((reused["status"], reused["code"]), ("REJECTED", "OP_ID_REUSED"))
            self.assertEqual(len(client.get("/api/v1/notes").json()), 1)

            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as check:
                check.initialize()
                check.initialize()  # migrations are idempotent
                after = {
                    table: check.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for table in ("obligations", "tasks", "events")
                }
                self.assertEqual(after, {k: before[k] for k in after})
                self.assertEqual(
                    [r[0] for r in check.connection.execute("SELECT version FROM schema_migrations")],
                    list(range(1, SCHEMA_VERSION + 1)),
                )
                self.assertEqual(check.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_documented_rollback_returns_to_v21_table_shape_when_v22_has_no_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "rollback.sqlite")
            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as repo:
                repo.initialize()
                repo.create_account("a")

            conn = sqlite3.connect(db)
            counts = sum(
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("notes", "note_audio", "note_links", "deleted_notes", "beta_feedback")
            )
            self.assertEqual(counts, 0)
            # Later releases roll back first (ADR 0027), then v22.
            roll_back_newer_than(conn, 22)
            conn.executescript(
                "DROP TABLE beta_feedback; DROP TABLE deleted_notes; DROP TABLE note_links; "
                "DROP TABLE note_audio; DROP TABLE notes; DELETE FROM schema_migrations WHERE version=22;"
            )
            conn.commit()
            conn.close()

            reference = str(Path(tmp) / "v21.sqlite")
            build_v21(reference)

            def tables(path):
                with closing(sqlite3.connect(path)) as connection:
                    return sorted(
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    )

            self.assertEqual(tables(db), tables(reference))


if __name__ == "__main__":
    unittest.main()
