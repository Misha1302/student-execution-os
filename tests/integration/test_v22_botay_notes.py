"""Upgrade a populated schema-v21 database to botay schema v22 and document rollback shape."""
from __future__ import annotations

import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

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
            self.assertEqual((SCHEMA_VERSION, health["schema_version"]), (22, 22))
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
                    list(range(1, 23)),
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
