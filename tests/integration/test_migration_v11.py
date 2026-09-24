from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SCHEMA_VERSION, SQLiteCanonicalRepository


NOW = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


class MigrationV11IntegrationTests(unittest.TestCase):
    def test_v10_upgrades_to_v11_preserving_task_and_allowing_draft_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "v10.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)")
            migrations = sorted(Path("src/student_execution_os/persistence/migrations").glob("0*.sql"))[:10]
            self.assertEqual(len(migrations), 10)
            for version, path in enumerate(migrations, start=1):
                connection.executescript(path.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO schema_migrations VALUES (?,?)", (version, NOW.isoformat()))
            connection.execute("INSERT INTO accounts(id) VALUES ('a')")
            connection.execute(
                "INSERT INTO obligations VALUES ('t','a','TASK','GENERAL','Legacy',NULL,'ACTIVE','NORMAL',?,?,NULL,1)",
                (NOW.isoformat(), NOW.isoformat()),
            )
            connection.execute(
                "INSERT INTO tasks(obligation_id,estimated_total_effort_minutes,remaining_effort_minutes,splittable,cutoff_state) "
                "VALUES ('t',30,30,1,'UNKNOWN')"
            )
            connection.commit()
            connection.close()

            with SQLiteCanonicalRepository(database, clock=FrozenClock(NOW)) as repository:
                repository.initialize()
                self.assertEqual(repository.schema_version(), SCHEMA_VERSION)
                self.assertEqual(repository.get_task("a", "t").remaining_effort_minutes, 30)
                columns = {row[1]: row[3] for row in repository.connection.execute("PRAGMA table_info(tasks)")}
                self.assertEqual(columns["estimated_total_effort_minutes"], 0)
                self.assertFalse(repository.connection.execute("PRAGMA foreign_key_check").fetchall())


if __name__ == "__main__":
    unittest.main()
