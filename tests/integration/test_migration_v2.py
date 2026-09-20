import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository

UTC = timezone.utc
BASE = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


class MigrationV2IntegrationTests(unittest.TestCase):
    def test_v1_database_upgrades_to_v2_without_losing_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v1.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            migration = (
                Path("src/student_execution_os/persistence/migrations/001_initial.sql")
                .read_text(encoding="utf-8")
            )
            conn.executescript(migration)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (BASE.isoformat(),),
            )
            conn.execute("INSERT INTO accounts(id,server_revision) VALUES ('a',1)")
            conn.execute(
                "INSERT INTO obligations(id,account_id,kind,category,title,description,lifecycle_status,importance,created_at,updated_at,completed_at,version) "
                "VALUES ('t','a','TASK','GENERAL','old task',NULL,'ACTIVE','NORMAL',?,?,NULL,1)",
                (BASE.isoformat(), BASE.isoformat()),
            )
            conn.execute(
                "INSERT INTO tasks(obligation_id,estimated_total_effort_minutes,remaining_effort_minutes,splittable,min_chunk_minutes,max_chunk_minutes,actionable_from,cutoff_state,actual_cutoff_at,cutoff_boundary,cutoff_precision,target_at) "
                "VALUES ('t',60,60,1,30,60,?,'KNOWN',?,'INCLUSIVE','EXACT_INSTANT',NULL)",
                (BASE.isoformat(), (BASE + timedelta(hours=2)).isoformat()),
            )
            conn.commit()
            conn.close()

            with SQLiteCanonicalRepository(db, clock=FrozenClock(BASE)) as repo:
                repo.initialize()
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
                loaded = repo.get_task("a", "t")
                self.assertEqual(loaded.obligation.title, "old task")
                self.assertEqual(loaded.remaining_effort_minutes, 60)
                self.assertIsNone(loaded.remaining_effort_low_minutes)
                self.assertIsNone(loaded.remaining_effort_high_minutes)


if __name__ == "__main__":
    unittest.main()
