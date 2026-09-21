import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository


UTC = timezone.utc
BASE = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


class MigrationV7IntegrationTests(unittest.TestCase):
    def test_v6_database_upgrades_to_v7_without_losing_plan_or_travel_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v6.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            migrations = Path("src/student_execution_os/persistence/migrations")
            for version, name in (
                (1, "001_initial.sql"),
                (2, "002_planning_projection.sql"),
                (3, "003_evidence_reconciliation.sql"),
                (4, "004_connector_sync.sql"),
                (5, "005_llm_action_boundary.sql"),
                (6, "006_travel_planning.sql"),
            ):
                conn.executescript((migrations / name).read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, BASE.isoformat()),
                )
            conn.execute("INSERT INTO accounts(id,server_revision) VALUES ('a',0)")
            conn.execute(
                "INSERT INTO places(id,account_id,alias,display_name,address,latitude,longitude,visibility_policy,version,created_at,updated_at) "
                "VALUES ('home','a','HOME','Home',NULL,NULL,NULL,'PRIVATE_ALIAS',1,?,?)",
                (BASE.isoformat(), BASE.isoformat()),
            )
            conn.execute(
                "INSERT INTO plan_snapshots(id,account_id,plan_revision,input_server_revision,input_hash,horizon_start,horizon_end,feasibility_status,generated_at,explanations_json) "
                "VALUES ('p','a','0:old',0,'hash',?,?, 'FEASIBLE', ?, '[]')",
                (BASE.isoformat(), (BASE.replace(hour=12)).isoformat(), BASE.isoformat()),
            )
            conn.commit()
            conn.close()

            with SQLiteCanonicalRepository(db, clock=FrozenClock(BASE)) as repo:
                repo.initialize()
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
                self.assertEqual(SCHEMA_VERSION, 7)
                self.assertIsNotNone(repo.connection.execute("SELECT id FROM plan_snapshots WHERE id='p'").fetchone())
                self.assertIsNotNone(repo.connection.execute("SELECT id FROM places WHERE id='home'").fetchone())
                tables = {
                    row["name"]
                    for row in repo.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                }
                self.assertTrue({"recurring_templates", "occurrence_overrides", "notifications"}.issubset(tables))


if __name__ == "__main__":
    unittest.main()
