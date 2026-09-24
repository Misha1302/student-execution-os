import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository
from student_execution_os.reliability import SQLiteDataLifecycle


BASE = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


class MigrationV10IntegrationTests(unittest.TestCase):
    def test_v9_database_upgrades_to_v10_with_auth_tables_and_preserves_accounts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v9.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            migrations = sorted(Path("src/student_execution_os/persistence/migrations").glob("00*.sql"))
            self.assertEqual(len(migrations), 9)
            for version, path in enumerate(migrations, start=1):
                conn.executescript(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (version, BASE.isoformat()))
            conn.execute("INSERT INTO accounts(id,server_revision) VALUES ('legacy',3)")
            conn.commit()
            conn.close()

            with SQLiteCanonicalRepository(db, clock=FrozenClock(BASE)) as repo:
                repo.initialize()
                self.assertEqual(SCHEMA_VERSION, 10)
                self.assertEqual(repo.schema_version(), 10)
                self.assertEqual(repo.get_server_revision("legacy"), 3)
                tables = {
                    r[0] for r in repo.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'auth_%'"
                    )
                }
                self.assertEqual(tables, {"auth_users", "auth_sessions"})

            # Lifecycle contract classifies the new tables (fails closed otherwise).
            export = SQLiteDataLifecycle(db, now=lambda: BASE).export_account("legacy")
            self.assertNotIn("auth_users", export.tables)


if __name__ == "__main__":
    unittest.main()
