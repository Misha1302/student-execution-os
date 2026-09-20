import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository


UTC = timezone.utc
BASE = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class MigrationV5IntegrationTests(unittest.TestCase):
    def test_v4_database_upgrades_to_v5_preserving_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v4.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            migrations = Path("src/student_execution_os/persistence/migrations")
            for version, name in (
                (1, "001_initial.sql"),
                (2, "002_planning_projection.sql"),
                (3, "003_evidence_reconciliation.sql"),
                (4, "004_connector_sync.sql"),
            ):
                conn.executescript(
                    (migrations / name).read_text(encoding="utf-8")
                )
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, BASE.isoformat()),
                )
            conn.execute(
                "INSERT INTO accounts(id,server_revision) VALUES ('a',0)"
            )
            conn.execute(
                "INSERT INTO obligations("
                "id,account_id,kind,category,title,description,lifecycle_status,"
                "importance,created_at,updated_at,completed_at,version"
                ") VALUES ('t','a','TASK','GENERAL','Task',NULL,'ACTIVE','NORMAL',?,?,NULL,1)",
                (BASE.isoformat(), BASE.isoformat()),
            )
            conn.execute(
                "INSERT INTO tasks("
                "obligation_id,estimated_total_effort_minutes,remaining_effort_minutes,"
                "splittable,min_chunk_minutes,max_chunk_minutes,actionable_from,"
                "cutoff_state,actual_cutoff_at,cutoff_boundary,cutoff_precision,target_at,"
                "estimated_total_effort_low_minutes,estimated_total_effort_high_minutes,"
                "remaining_effort_low_minutes,remaining_effort_high_minutes"
                ") VALUES ('t',30,30,0,NULL,NULL,NULL,'UNKNOWN',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL)"
            )
            conn.commit()
            conn.close()

            with SQLiteCanonicalRepository(
                db,
                clock=FrozenClock(BASE),
            ) as repo:
                repo.initialize()
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
                self.assertEqual(SCHEMA_VERSION, 5)
                self.assertEqual(repo.get_task("a", "t").obligation.title, "Task")
                tables = {
                    row["name"]
                    for row in repo.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("action_intents", tables)
                self.assertIn("action_idempotency_records", tables)
                self.assertIn("action_intent_history", tables)


if __name__ == "__main__":
    unittest.main()
