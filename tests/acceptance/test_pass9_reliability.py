from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from student_execution_os.agent import (
    ActionRequest,
    AgentCommand,
    AuthenticatedPrincipal,
    IntentStrength,
    SQLiteActionGateway,
)
from student_execution_os.connectors import SQLiteConnectorRepository
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import PlanningService, SQLitePlanningStateSource, build_planning_snapshot
from student_execution_os.reconciliation import SQLiteReconciliationRepository
from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.notifications import SQLiteNotificationRepository
from tests.ui_fixture import ACCOUNT, NOW, OTHER_ACCOUNT, seed_ui_database


class Pass9ReliabilityTests(unittest.TestCase):
    def _seed_checkpoint_and_idempotency(self, db: Path) -> tuple[AuthenticatedPrincipal, str]:
        principal = AuthenticatedPrincipal(ACCOUNT, "recovery-user", "recovery-test")
        with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            recon = SQLiteReconciliationRepository(repo)
            connectors = SQLiteConnectorRepository(repo, recon)
            session = connectors.start_session(
                account_id=ACCOUNT,
                connector_id="google-calendar-primary",
                is_full_sync=True,
                session_id="recovery-session",
            )
            connectors.finish_complete(
                account_id=ACCOUNT,
                session_id=session.id,
                checkpoint_after="checkpoint-recovery-1",
                page_count=1,
                record_count=0,
                deletion_count=0,
            )

            gateway = SQLiteActionGateway(repo)
            task = repo.get_task(ACCOUNT, "override-task")
            intent = gateway.mint_intent(
                principal=principal,
                command=AgentCommand.CANCEL_OBLIGATION,
                target_entity_id=task.obligation.id,
                intent_strength=IntentStrength.EXPLICIT_SCOPED,
                expected_version=task.obligation.version,
                intent_id="recovery-intent",
            )
            result = gateway.execute_cancel(
                principal=principal,
                request=ActionRequest(intent.id, "recovery-idempotency-key", task.obligation.version),
            )
            self.assertFalse(result.replayed)
        return principal, "recovery-idempotency-key"

    def test_at62_backup_restore_recovers_provenance_checkpoint_idempotency_and_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "live.sqlite3"
            backup = root / "backup.sqlite3"
            restored = root / "restored.sqlite3"
            seed_ui_database(str(db))
            principal, key = self._seed_checkpoint_and_idempotency(db)

            lifecycle = SQLiteDataLifecycle(db, now=lambda: NOW)
            manifest = lifecycle.create_backup(backup)
            self.assertEqual(manifest.integrity_check, "ok")
            self.assertEqual(manifest.schema_version, 7)
            self.assertTrue(SQLiteDataLifecycle.manifest_path(backup).exists())
            if os.name == "posix":
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                self.assertEqual(SQLiteDataLifecycle.manifest_path(backup).stat().st_mode & 0o777, 0o600)

            restore = SQLiteDataLifecycle.restore_backup(backup, restored)
            self.assertTrue(restore.sha256_verified)
            self.assertEqual(restore.integrity_check, "ok")
            self.assertEqual(restore.foreign_key_violations, 0)
            self.assertEqual(restore.restored_schema_version, 7)
            if os.name == "posix":
                self.assertEqual(restored.stat().st_mode & 0o777, 0o600)

            with SQLiteCanonicalRepository(restored, clock=FrozenClock(NOW)) as repo:
                repo.initialize()
                self.assertEqual(repo.get_task(ACCOUNT, "discrete").obligation.title, "Discrete homework")
                self.assertGreater(
                    repo.connection.execute(
                        "SELECT count(*) FROM observations WHERE account_id=?", (ACCOUNT,)
                    ).fetchone()[0],
                    0,
                )
                recon = SQLiteReconciliationRepository(repo)
                connector = SQLiteConnectorRepository(repo, recon).get_state(ACCOUNT, "google-calendar-primary")
                self.assertEqual(connector.checkpoint, "checkpoint-recovery-1")
                self.assertGreaterEqual(len(SQLiteNotificationRepository(repo).list(ACCOUNT)), 1)

                gateway = SQLiteActionGateway(repo)
                replay = gateway.execute_cancel(
                    principal=principal,
                    request=ActionRequest("recovery-intent", key, 1),
                )
                self.assertTrue(replay.replayed)

                snapshot = build_planning_snapshot(
                    SQLitePlanningStateSource(repo),
                    account_id=ACCOUNT,
                    analysis_horizon_start=NOW,
                    analysis_horizon_end=NOW + timedelta(hours=18),
                    plan_output_horizon_end=NOW + timedelta(hours=18),
                )
                outcome = PlanningService().build(snapshot, now=NOW)
                self.assertEqual(outcome.plan.input_server_revision, repo.get_server_revision(ACCOUNT))

    def test_backup_hash_tamper_is_rejected_before_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "live.sqlite3"
            backup = root / "backup.sqlite3"
            restored = root / "restored.sqlite3"
            seed_ui_database(str(db))
            SQLiteDataLifecycle(db, now=lambda: NOW).create_backup(backup)
            with backup.open("ab") as stream:
                stream.write(b"tamper")
            with self.assertRaisesRegex(Exception, "SHA-256"):
                SQLiteDataLifecycle.restore_backup(backup, restored)

    def test_at69_account_export_is_explicitly_scoped_and_excludes_other_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "live.sqlite3"
            export_path = root / "account-export.json"
            seed_ui_database(str(db))

            export = SQLiteDataLifecycle(db, now=lambda: NOW).write_account_export(ACCOUNT, export_path)
            self.assertEqual(export.account_id, ACCOUNT)
            self.assertTrue(export.contract["includes_provenance_and_workflow"])
            self.assertTrue(export.contract["includes_private_locations"])
            self.assertFalse(export.contract["includes_connector_or_oauth_secrets"])
            self.assertIn("source_records", export.tables)
            self.assertIn("connector_states", export.tables)
            self.assertIn("notifications", export.tables)
            self.assertIn("recurring_templates", export.tables)
            self.assertNotIn("schema_migrations", export.tables)

            raw = export_path.read_text(encoding="utf-8")
            self.assertIn("Discrete homework", raw)
            self.assertNotIn("Other account secret task", raw)
            self.assertNotIn(OTHER_ACCOUNT, raw)
            parsed = json.loads(raw)
            self.assertEqual(parsed["account_id"], ACCOUNT)
            self.assertTrue(all(row.get("account_id", ACCOUNT) == ACCOUNT for rows in parsed["tables"].values() for row in rows))
            if os.name == "posix":
                self.assertEqual(export_path.stat().st_mode & 0o777, 0o600)

    def test_account_export_fails_closed_when_schema_adds_unclassified_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live.sqlite3"
            seed_ui_database(str(db))
            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as repo:
                repo.connection.execute(
                    "CREATE TABLE future_sensitive_state(id TEXT PRIMARY KEY, account_id TEXT NOT NULL, payload TEXT)"
                )
                repo.connection.commit()
            with self.assertRaisesRegex(Exception, "does not classify database tables"):
                SQLiteDataLifecycle(db, now=lambda: NOW).export_account(ACCOUNT)

    def test_database_file_is_restricted_on_posix(self):
        if os.name != "posix":
            self.skipTest("POSIX file mode contract")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "permissions.sqlite3"
            with SQLiteCanonicalRepository(db) as repo:
                repo.initialize()
                repo.create_account("a")
            self.assertEqual(db.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
