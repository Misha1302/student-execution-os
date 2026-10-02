from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.persistence.sqlite import SCHEMA_VERSION, SQLiteCanonicalRepository
from tests.rollback_chain import roll_back_newer_than


MIGRATIONS = Path("src/student_execution_os/persistence/migrations")
NOW = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)


class PlanningPreferenceMigrationTest(unittest.TestCase):
    def test_populated_v29_database_upgrades_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "upgrade.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                for version in range(1, 30):
                    migration = next(MIGRATIONS.glob(f"{version:03d}_*.sql"))
                    connection.executescript(migration.read_text(encoding="utf-8"))
                    connection.execute(
                        "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)", (version, NOW.isoformat())
                    )
                connection.execute("INSERT INTO accounts(id) VALUES ('existing-account')")
                connection.execute(
                    "INSERT INTO user_time_constraints(id,account_id,type,starts_at,ends_at,version) "
                    "VALUES ('c-existing','existing-account','UNAVAILABLE',?,?,1)",
                    ("2026-10-03T00:00:00+00:00", "2026-10-03T09:00:00+00:00"),
                )

            with SQLiteCanonicalRepository(database) as repo:
                repo.initialize()
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
                self.assertEqual(SCHEMA_VERSION, 30)
                repo.connection.execute(
                    "INSERT INTO planning_preferences(id,account_id,kind,date_from,minutes,created_at,updated_at) "
                    "VALUES ('p-1','existing-account','WORK_LIMIT','2026-10-03',180,?,?)",
                    (NOW.isoformat(), NOW.isoformat()),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    repo.connection.execute(
                        "INSERT INTO planning_preferences(id,account_id,kind,date_from,created_at,updated_at) "
                        "VALUES ('p-2','existing-account','PLANNER_PROMPT','2026-10-03',?,?)",
                        (NOW.isoformat(), NOW.isoformat()),
                    )
                repo.connection.commit()

            with sqlite3.connect(database) as connection:
                roll_back_newer_than(connection, 29)
                self.assertEqual(connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 29)
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='planning_preferences'").fetchone())
                self.assertEqual(connection.execute(
                    "SELECT reason IS NULL FROM user_time_constraints WHERE id='c-existing'").fetchone()[0], 1)

            with SQLiteCanonicalRepository(database) as repo:  # re-upgrade after rollback is clean
                repo.initialize()
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
