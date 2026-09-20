import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository

UTC = timezone.utc
BASE = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


class MigrationV3IntegrationTests(unittest.TestCase):
    def test_v2_database_upgrades_to_v3_without_losing_canonical_or_plan_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v2.sqlite3"
            conn = sqlite3.connect(db)
            conn.execute(
                "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            migrations = Path("src/student_execution_os/persistence/migrations")
            conn.executescript((migrations / "001_initial.sql").read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (1, ?)",
                (BASE.isoformat(),),
            )
            conn.executescript((migrations / "002_planning_projection.sql").read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (2, ?)",
                (BASE.isoformat(),),
            )
            conn.execute("INSERT INTO accounts(id,server_revision) VALUES ('a',1)")
            conn.execute(
                "INSERT INTO obligations(id,account_id,kind,category,title,description,lifecycle_status,importance,created_at,updated_at,completed_at,version) "
                "VALUES ('t','a','TASK','GENERAL','preserved task',NULL,'ACTIVE','NORMAL',?,?,NULL,1)",
                (BASE.isoformat(), BASE.isoformat()),
            )
            conn.execute(
                "INSERT INTO tasks(obligation_id,estimated_total_effort_minutes,remaining_effort_minutes,splittable,min_chunk_minutes,max_chunk_minutes,actionable_from,cutoff_state,actual_cutoff_at,cutoff_boundary,cutoff_precision,target_at) "
                "VALUES ('t',60,60,1,30,60,?,'KNOWN',?,'INCLUSIVE','EXACT_INSTANT',NULL)",
                (BASE.isoformat(), (BASE + timedelta(hours=2)).isoformat()),
            )
            conn.execute(
                "INSERT INTO plan_snapshots(id,account_id,plan_revision,input_server_revision,input_hash,horizon_start,horizon_end,feasibility_status,generated_at,explanations_json) "
                "VALUES ('plan-v2','a','rev-v2',1,'hash-v2',?,?, 'FEASIBLE',?, '[]')",
                (
                    BASE.isoformat(),
                    (BASE + timedelta(hours=2)).isoformat(),
                    BASE.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO current_plans(account_id,plan_id) VALUES ('a','plan-v2')"
            )
            conn.commit()
            conn.close()

            with SQLiteCanonicalRepository(db, clock=FrozenClock(BASE)) as repo:
                repo.initialize()
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
                self.assertGreaterEqual(SCHEMA_VERSION, 3)
                loaded = repo.get_task("a", "t")
                self.assertEqual(loaded.obligation.title, "preserved task")
                self.assertEqual(loaded.remaining_effort_minutes, 60)
                current = repo.connection.execute(
                    "SELECT plan_id FROM current_plans WHERE account_id='a'"
                ).fetchone()
                self.assertEqual(current["plan_id"], "plan-v2")
                plan = repo.connection.execute(
                    "SELECT input_hash FROM plan_snapshots WHERE id='plan-v2'"
                ).fetchone()
                self.assertEqual(plan["input_hash"], "hash-v2")
                tables = {
                    row["name"]
                    for row in repo.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("source_records", tables)
                self.assertIn("effective_fields", tables)


if __name__ == "__main__":
    unittest.main()
