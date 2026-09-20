import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository


UTC = timezone.utc
BASE = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class MigrationV4IntegrationTests(unittest.TestCase):
    def test_v3_database_upgrades_to_v4_preserving_evidence_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "v3.sqlite3"
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
            ):
                conn.executescript((migrations / name).read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, BASE.isoformat()),
                )
            conn.execute("INSERT INTO accounts(id,server_revision) VALUES ('a',0)")
            conn.execute(
                "INSERT INTO source_systems("
                "id,account_id,kind,policy_context_json,created_at"
                ") VALUES ('s','a','GOOGLE_CALENDAR','{}',?)",
                (BASE.isoformat(),),
            )
            conn.execute(
                "INSERT INTO source_records("
                "id,account_id,source_system_id,external_entity_id,source_revision,"
                "revision_order,observed_at,content_hash,source_uri,raw_payload_ref,metadata_json"
                ") VALUES ('r','a','s','e','rev-1',1,?,NULL,NULL,NULL,'{}')",
                (BASE.isoformat(),),
            )
            conn.commit()
            conn.close()

            with SQLiteCanonicalRepository(db, clock=FrozenClock(BASE)) as repo:
                repo.initialize()
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
                self.assertGreaterEqual(SCHEMA_VERSION, 4)
                source_record = repo.connection.execute(
                    "SELECT external_entity_id FROM source_records WHERE id='r'"
                ).fetchone()
                self.assertEqual(source_record["external_entity_id"], "e")
                tables = {
                    row["name"]
                    for row in repo.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("connector_states", tables)
                self.assertIn("connector_sync_sessions", tables)
                self.assertIn("connector_entities", tables)
                self.assertIn("connector_ingestion_receipts", tables)
                session_columns = {
                    row["name"]
                    for row in repo.connection.execute(
                        "PRAGMA table_info(connector_sync_sessions)"
                    ).fetchall()
                }
                self.assertIn("state_version_before", session_columns)


if __name__ == "__main__":
    unittest.main()