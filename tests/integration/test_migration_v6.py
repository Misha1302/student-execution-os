import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository


UTC = timezone.utc
BASE = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)


class MigrationV6IntegrationTests(unittest.TestCase):
    def test_v5_database_upgrades_to_v6_preserving_plan_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v5.sqlite3"
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
                (5, "005_llm_action_boundary.sql"),
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
                "INSERT INTO plan_snapshots("
                "id,account_id,plan_revision,input_server_revision,input_hash,"
                "horizon_start,horizon_end,feasibility_status,generated_at,explanations_json"
                ") VALUES ('p','a','0:old',0,'oldhash',?,?, 'FEASIBLE', ?, '[]')",
                (
                    BASE.isoformat(),
                    BASE.replace(hour=12).isoformat(),
                    BASE.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO plan_blocks("
                "id,plan_id,block_type,starts_at,ends_at,obligation_id,"
                "source_constraint_ids_json,source_event_id,explanation"
                ") VALUES ('b','p','EVENT_PROJECTION',?,?,?,?,?,?)",
                (
                    BASE.replace(hour=10).isoformat(),
                    BASE.replace(hour=11).isoformat(),
                    None,
                    "[]",
                    "event-old",
                    "OLD_EVENT",
                ),
            )
            conn.commit()
            conn.close()

            with SQLiteCanonicalRepository(
                db,
                clock=FrozenClock(BASE),
            ) as repo:
                repo.initialize()
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
                self.assertGreaterEqual(SCHEMA_VERSION, 6)
                row = repo.connection.execute(
                    "SELECT block_type,source_event_id,travel_estimate_id "
                    "FROM plan_blocks WHERE id='b'"
                ).fetchone()
                self.assertEqual(row["block_type"], "EVENT_PROJECTION")
                self.assertEqual(row["source_event_id"], "event-old")
                self.assertIsNone(row["travel_estimate_id"])
                columns = {
                    item["name"]
                    for item in repo.connection.execute(
                        "PRAGMA table_info(plan_blocks)"
                    ).fetchall()
                }
                self.assertIn("travel_estimate_id", columns)
                tables = {
                    row["name"]
                    for row in repo.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("places", tables)
                self.assertIn("current_location_context", tables)
                self.assertIn("travel_estimates", tables)


if __name__ == "__main__":
    unittest.main()
