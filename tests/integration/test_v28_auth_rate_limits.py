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


class AuthRateLimitMigrationTest(unittest.TestCase):
    def test_fresh_database_contains_auth_limit_and_action_history_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "fresh.sqlite"
            with SQLiteCanonicalRepository(database) as repo:
                repo.initialize()
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
                columns = {
                    row[1] for row in repo.connection.execute("PRAGMA table_info(auth_rate_limits)")
                }
                self.assertEqual(
                    columns,
                    {"scope", "key_hash", "window_started_at", "last_attempt_at", "attempt_count"},
                )
                self.assertEqual(
                    repo.connection.execute(
                        "SELECT count(*) FROM pragma_table_info('assistant_action_history')"
                    ).fetchone()[0],
                    12,
                )

    def test_retention_drops_finished_abuse_windows_even_without_new_attempts(self):
        from datetime import timedelta

        from student_execution_os.reliability.retention import purge_expired

        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "retention.sqlite")
            with SQLiteCanonicalRepository(database) as repo:
                repo.initialize()
                for key, started in (("old", NOW - timedelta(hours=2)), ("live", NOW - timedelta(minutes=5))):
                    repo.connection.execute(
                        "INSERT INTO auth_rate_limits VALUES ('IP', ?, ?, ?, 3)",
                        (key.ljust(64, "0"), started.isoformat(), started.isoformat()),
                    )
                repo.connection.commit()
            self.assertEqual(purge_expired(database, NOW)["auth_rate_limits"], 1)
            with SQLiteCanonicalRepository(database) as repo:
                left = [row[0][:4] for row in repo.connection.execute("SELECT key_hash FROM auth_rate_limits")]
            self.assertEqual(left, ["live"])

    def test_populated_v27_database_upgrades_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "upgrade.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                for version in range(1, 28):
                    migration = next(MIGRATIONS.glob(f"{version:03d}_*.sql"))
                    connection.executescript(migration.read_text(encoding="utf-8"))
                    connection.execute(
                        "INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                        (version, NOW.isoformat()),
                    )
                connection.execute("INSERT INTO accounts(id) VALUES ('existing-account')")
                connection.execute(
                    "INSERT INTO auth_users(id,account_id,login,password_hash,created_at) "
                    "VALUES ('existing-user','existing-account','existing','hash',?)",
                    (NOW.isoformat(),),
                )

            with SQLiteCanonicalRepository(database) as repo:
                repo.initialize()
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
                self.assertEqual(
                    repo.connection.execute(
                        "SELECT login FROM auth_users WHERE id='existing-user'"
                    ).fetchone()[0],
                    "existing",
                )
                repo.connection.execute(
                    "INSERT INTO auth_rate_limits VALUES ('LOGIN', ?, ?, ?, 1)",
                    ("a" * 64, NOW.isoformat(), NOW.isoformat()),
                )
                repo.connection.commit()

            with sqlite3.connect(database) as connection:
                roll_back_newer_than(connection, 27)
                self.assertEqual(
                    connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0],
                    27,
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_rate_limits'"
                    ).fetchone()
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT login FROM auth_users WHERE id='existing-user'"
                    ).fetchone()[0],
                    "existing",
                )


if __name__ == "__main__":
    unittest.main()
