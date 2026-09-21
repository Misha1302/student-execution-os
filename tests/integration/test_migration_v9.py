import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository


UTC = timezone.utc
BASE = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


class MigrationV9IntegrationTests(unittest.TestCase):
    def test_v8_database_upgrades_to_v9_with_delivery_outbox_and_preserves_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v8.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            migrations = Path("src/student_execution_os/persistence/migrations")
            names = (
                "001_initial.sql",
                "002_planning_projection.sql",
                "003_evidence_reconciliation.sql",
                "004_connector_sync.sql",
                "005_llm_action_boundary.sql",
                "006_travel_planning.sql",
                "007_recurrence_notifications.sql",
                "008_account_deletion.sql",
            )
            for version, name in enumerate(names, start=1):
                conn.executescript((migrations / name).read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, BASE.isoformat()),
                )
            conn.execute("INSERT INTO accounts(id,server_revision) VALUES ('a',0)")
            conn.execute(
                "INSERT INTO notifications(id,account_id,suppression_key,kind,entity_ref,domain_revision,"
                "plan_id,plan_revision,scheduled_for,state,initial_notification_id,group_key,cooldown_until,"
                "snoozed_until,delivered_at,attempt_count,version,last_error,created_at,updated_at) "
                "VALUES ('n','a','k','DEADLINE_WARNING',NULL,0,NULL,NULL,?,'PENDING',NULL,NULL,NULL,NULL,NULL,0,1,NULL,?,?)",
                (BASE.isoformat(), BASE.isoformat(), BASE.isoformat()),
            )
            conn.commit()
            conn.close()

            with SQLiteCanonicalRepository(db, clock=FrozenClock(BASE)) as repo:
                repo.initialize()
                self.assertEqual(SCHEMA_VERSION, 9)
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
                self.assertIsNotNone(repo.connection.execute("SELECT id FROM notifications WHERE id='n'").fetchone())
                table = repo.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='notification_delivery_outbox'"
                ).fetchone()
                self.assertIsNotNone(table)


if __name__ == "__main__":
    unittest.main()
