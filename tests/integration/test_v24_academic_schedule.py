from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from icalendar import Calendar

from student_execution_os.academic import AcademicProviderError, AcademicScheduleService, parse_icalendar
from student_execution_os.academic.service import refresh_due_academic_schedules
from student_execution_os.agent.credentials import CredentialCipher
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SCHEMA_VERSION, SQLiteCanonicalRepository
from student_execution_os.recurrence import OccurrenceOverrideAction, SQLiteRecurrenceRepository
from student_execution_os.reliability import SQLiteDataLifecycle

NOW = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)
ACCOUNT = "academic-student"
FIXTURE = Path("tests/fixtures/academic_schedule_realistic.ics")
ROLLBACK = Path("src/student_execution_os/persistence/rollback/024_academic_schedule_down.sql")
ROLLBACK_V25 = Path("src/student_execution_os/persistence/rollback/025_capability_grants_down.sql")
ROLLBACK_V26 = Path("src/student_execution_os/persistence/rollback/026_oauth_connect_down.sql")


class AcademicScheduleV24Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp.name) / "academic.sqlite")
        with self.repo() as repo:
            repo.initialize()
            repo.create_account(ACCOUNT)

    def tearDown(self):
        self.temp.cleanup()

    def repo(self):
        return SQLiteCanonicalRepository(self.database, clock=FrozenClock(NOW))

    @staticmethod
    def fixture() -> bytes:
        return FIXTURE.read_bytes()

    def test_realistic_ical_normalizes_series_exceptions_and_all_day(self):
        result = parse_icalendar(
            self.fixture(),
            source_system_id="academic-source",
            default_timezone="Europe/Moscow",
        )
        self.assertTrue(result.snapshot.complete)
        self.assertEqual(result.component_count, 5)
        self.assertEqual([series.uid for series in result.snapshot.series], ["course-algorithms-42@example.edu"])
        series = result.snapshot.series[0]
        self.assertEqual(series.recurrence_rule, "FREQ=WEEKLY;COUNT=12")
        self.assertEqual(series.timezone_name, "Europe/Moscow")
        self.assertEqual(series.location_text, "Pokrovsky Boulevard, room R301")
        self.assertEqual(series.teacher, "Dr Ada Example")
        self.assertEqual(series.exdates_local, (datetime(2026, 10, 5, 10, 10),))
        changes = {change.recurrence_local: change for change in series.changes}
        moved = changes[datetime(2026, 10, 12, 10, 10)]
        self.assertEqual((moved.starts_local, moved.duration_minutes, moved.location_text),
                         (datetime(2026, 10, 12, 12, 10), 80, "Pokrovsky Boulevard, room R501"))
        self.assertTrue(changes[datetime(2026, 10, 19, 10, 10)].cancelled)
        events = {event.uid: event for event in result.snapshot.events}
        guest = events["guest-lecture-2026@example.edu"]
        self.assertEqual(int((guest.ends_at - guest.starts_at).total_seconds() // 60), 90)
        self.assertEqual(
            (events["reading-week@example.edu"].starts_at.isoformat(), events["reading-week@example.edu"].ends_at.isoformat()),
            ("2026-11-02T00:00:00+03:00", "2026-11-03T00:00:00+03:00"),
        )

    def test_repeat_reordered_refresh_has_zero_duplicates_and_user_override_survives(self):
        content = self.fixture()
        with self.repo() as repo:
            service = AcademicScheduleService(repo, account_id=ACCOUNT)
            first = service.import_ics(content)
            template_id = repo.connection.execute(
                "SELECT id FROM recurring_templates WHERE account_id=?", (ACCOUNT,)
            ).fetchone()[0]
            SQLiteRecurrenceRepository(repo).set_override(
                account_id=ACCOUNT,
                template_id=template_id,
                original_recurrence_id="2026-10-26T10:10:00",
                action=OccurrenceOverrideAction.MODIFY,
                replacement_start_local=datetime(2026, 10, 26, 17, 0),
                location_text="Personal study room",
                actor=ActorCategory.USER_UI,
            )
            second = service.import_ics(content)
            self.assertEqual(len(first["apply_report"]["created"]), 3)
            self.assertEqual(second["apply_report"]["created"], [])
            self.assertEqual(len(second["apply_report"]["unchanged"]), 3)
            self.assertEqual(repo.connection.execute("SELECT count(*) FROM recurring_templates").fetchone()[0], 1)
            self.assertEqual(repo.connection.execute("SELECT count(*) FROM events").fetchone()[0], 2)
            override = SQLiteRecurrenceRepository(repo).get_override(
                ACCOUNT, template_id, "2026-10-26T10:10:00"
            )
            self.assertEqual((override.layer.value, override.replacement_start_local, override.location_text),
                             ("USER", datetime(2026, 10, 26, 17, 0), "Personal study room"))
            source_overrides = repo.connection.execute(
                "SELECT action,replacement_start_local,location_text FROM occurrence_overrides "
                "WHERE account_id=? AND layer='SOURCE' ORDER BY original_recurrence_id",
                (ACCOUNT,),
            ).fetchall()
            self.assertEqual([tuple(row) for row in source_overrides], [
                ("CANCEL", None, None),
                ("MODIFY", "2026-10-12T12:10:00", "Pokrovsky Boulevard, room R501"),
                ("CANCEL", None, None),
            ])

            changed = content.replace(b"SEQUENCE:4", b"SEQUENCE:40", 1).replace(
                b"Pokrovsky Boulevard\\, room R301", b"Pokrovsky Boulevard\\, room R777", 1
            )
            updated = service.import_ics(changed)
            self.assertEqual(updated["apply_report"]["updated"], ["course-algorithms-42@example.edu"])
            self.assertEqual(SQLiteRecurrenceRepository(repo).get_template(
                ACCOUNT, template_id
            ).location_text, "Pokrovsky Boulevard, room R777")
            # A late older full download is observed as stale and cannot revert the room.
            stale = service.import_ics(content)
            self.assertIn("course-algorithms-42@example.edu", stale["apply_report"]["stale"])
            self.assertEqual(SQLiteRecurrenceRepository(repo).get_template(
                ACCOUNT, template_id
            ).location_text, "Pokrovsky Boulevard, room R777")
            self.assertEqual(repo.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(repo.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_failed_url_refresh_preserves_last_snapshot_and_records_safe_failure(self):
        cipher = CredentialCipher([b"a" * 32])

        def reader(_url: str):
            def fail() -> bytes:
                raise AcademicProviderError("provider unavailable", "PROVIDER_UNAVAILABLE")
            return fail

        with self.repo() as repo:
            service = AcademicScheduleService(repo, account_id=ACCOUNT)
            service.import_ics(self.fixture())
            before = repo.connection.execute("SELECT count(*) FROM external_identities").fetchone()[0]
            url_service = AcademicScheduleService(
                repo,
                account_id=ACCOUNT,
                cipher=cipher,
                reader_factory=reader,
                url_validator=lambda value: value,
            )
            with self.assertRaisesRegex(AcademicProviderError, "provider unavailable"):
                url_service.connect_url(url="https://calendar.example.invalid/private-token.ics")
            after = repo.connection.execute("SELECT count(*) FROM external_identities").fetchone()[0]
            status = url_service.status()
            self.assertEqual(after, before)
            self.assertEqual((status["health"], status["latest_failure_reason"]),
                             ("STALE", "PROVIDER_UNAVAILABLE"))
            serialized = repr(status)
            self.assertNotIn("private-token", serialized)
            stored = repo.connection.execute(
                "SELECT feed_ciphertext,feed_nonce,feed_host FROM academic_schedule_connections"
            ).fetchone()
            self.assertNotIn(b"private-token", bytes(stored[0]))
            self.assertEqual(stored[2], "calendar.example.invalid")

    def test_disconnect_retracts_source_classes_but_not_personal_event_and_removes_secret(self):
        with self.repo() as repo:
            service = AcademicScheduleService(repo, account_id=ACCOUNT)
            service.import_ics(self.fixture())
            personal = repo.create_fixed_event(
                account_id=ACCOUNT,
                obligation_id="personal-event",
                title="Dentist",
                starts_at=datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
                ends_at=datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc),
                actor=ActorCategory.USER_UI,
            )
            status = service.disconnect()
            self.assertFalse(status["connected"])
            self.assertIsNone(repo.connection.execute(
                "SELECT feed_ciphertext FROM academic_schedule_connections WHERE account_id=?", (ACCOUNT,)
            ).fetchone())
            self.assertEqual(repo.get_event(ACCOUNT, personal.obligation.id).obligation.title, "Dentist")
            self.assertEqual(repo.connection.execute(
                "SELECT count(*) FROM external_identities WHERE account_id=? AND external_recurrence_id='' "
                "AND state='ACTIVE'", (ACCOUNT,)
            ).fetchone()[0], 0)

    def test_complete_disappearance_and_reappearance_restore_same_event_identity(self):
        content = self.fixture()
        calendar = Calendar.from_ical(content)
        calendar.subcomponents = [
            component for component in calendar.subcomponents
            if str(component.get("UID") or "") != "guest-lecture-2026@example.edu"
        ]
        without_guest = calendar.to_ical()
        with self.repo() as repo:
            service = AcademicScheduleService(repo, account_id=ACCOUNT)
            service.import_ics(content)
            identity = repo.connection.execute(
                "SELECT local_id FROM external_identities WHERE account_id=? AND external_uid=?",
                (ACCOUNT, "guest-lecture-2026@example.edu"),
            ).fetchone()[0]
            removed = service.import_ics(without_guest)
            self.assertIn("guest-lecture-2026@example.edu", removed["apply_report"]["removed"])
            self.assertEqual(repo.get_event(ACCOUNT, identity).obligation.lifecycle_status.value, "CANCELLED")
            restored = service.import_ics(content)
            self.assertIn("guest-lecture-2026@example.edu", restored["apply_report"]["restored"])
            self.assertEqual(repo.get_event(ACCOUNT, identity).obligation.lifecycle_status.value, "ACTIVE")
            self.assertEqual(repo.connection.execute(
                "SELECT count(*) FROM obligations WHERE account_id=? AND id=?", (ACCOUNT, identity)
            ).fetchone()[0], 1)

    def test_scheduled_refresh_runs_due_urls_and_missing_key_is_visible(self):
        cipher = CredentialCipher([b"b" * 32])
        calls: list[str] = []

        def reader(url: str):
            calls.append(url)
            return self.fixture

        with self.repo() as repo:
            service = AcademicScheduleService(
                repo,
                account_id=ACCOUNT,
                cipher=cipher,
                reader_factory=reader,
                url_validator=lambda value: value,
            )
            service.connect_url(url="https://calendar.example.invalid/private.ics", sync_interval_minutes=60)
        report = refresh_due_academic_schedules(
            self.database,
            cipher=cipher,
            now=NOW.replace(hour=10),
            reader_factory=reader,
        )
        self.assertEqual(report, {"due": 1, "complete": 1, "failed": 0})
        self.assertEqual(len(calls), 2)
        with self.repo() as repo:
            service = AcademicScheduleService(repo, account_id=ACCOUNT, cipher=None)
            with self.assertRaises(AcademicProviderError) as raised:
                service.refresh()
            self.assertEqual(raised.exception.code, "CREDENTIAL_UNREADABLE")
            self.assertEqual(service.status()["health"], "UNAVAILABLE")

    def test_disconnect_during_refresh_wins_and_refresh_cannot_resurrect_classes(self):
        cipher = CredentialCipher([b"e" * 32])
        with self.repo() as repo:
            AcademicScheduleService(
                repo, account_id=ACCOUNT, cipher=cipher,
                reader_factory=lambda _url: self.fixture, url_validator=lambda value: value,
            ).connect_url(url="https://calendar.example.invalid/private.ics")

            def disconnect_mid_fetch(_url: str):
                def read() -> bytes:
                    AcademicScheduleService(repo, account_id=ACCOUNT).disconnect()
                    return self.fixture()
                return read

            service = AcademicScheduleService(
                repo, account_id=ACCOUNT, cipher=cipher,
                reader_factory=disconnect_mid_fetch, url_validator=lambda value: value,
            )
            with self.assertRaises(AcademicProviderError) as raised:
                service.refresh()
            self.assertEqual(raised.exception.code, "CONCURRENT_SYNC_CONFLICT")
            self.assertFalse(service.status()["connected"])
            self.assertEqual(repo.connection.execute(
                "SELECT count(*) FROM academic_schedule_connections").fetchone()[0], 0)
            self.assertEqual(repo.connection.execute(
                "SELECT count(*) FROM connector_sync_sessions WHERE completed_at IS NULL").fetchone()[0], 0)

    def test_scheduled_refresh_isolates_unexpected_account_failure(self):
        cipher = CredentialCipher([b"f" * 32])
        with self.repo() as repo:
            repo.create_account("second-student")
            for account in (ACCOUNT, "second-student"):
                AcademicScheduleService(
                    repo, account_id=account, cipher=cipher,
                    reader_factory=lambda _url: self.fixture, url_validator=lambda value: value,
                ).connect_url(url=f"https://calendar.example.invalid/{account}.ics")

        def reader(url: str):
            if ACCOUNT in url:
                def explode() -> bytes:
                    raise RuntimeError(f"unexpected failure for {url}")
                return explode
            return self.fixture

        report = refresh_due_academic_schedules(
            self.database, cipher=cipher, now=NOW.replace(hour=10), reader_factory=reader,
        )
        self.assertEqual(report, {"due": 2, "complete": 1, "failed": 1})
        with self.repo() as repo:
            status = AcademicScheduleService(repo, account_id=ACCOUNT, cipher=cipher).status()
            self.assertEqual(status["latest_failure_reason"], "PROVIDER_PROTOCOL_ERROR")
            self.assertEqual(repo.connection.execute(
                "SELECT count(*) FROM connector_sync_sessions WHERE completed_at IS NULL").fetchone()[0], 0)

    def test_older_failure_cannot_regress_newer_checkpoint_or_retry_schedule(self):
        cipher = CredentialCipher([b"d" * 32])
        with self.repo() as repo:
            service = AcademicScheduleService(
                repo,
                account_id=ACCOUNT,
                cipher=cipher,
                reader_factory=lambda _url: self.fixture,
                url_validator=lambda value: value,
            )
            service.connect_url(url="https://calendar.example.invalid/private.ics")
            connection_version = int(service._row()["version"])
            older = service.connectors.start_session(
                account_id=ACCOUNT, connector_id=service.connector_id, is_full_sync=True
            )
            newer = service.connectors.start_session(
                account_id=ACCOUNT, connector_id=service.connector_id, is_full_sync=True
            )
            result = parse_icalendar(
                self.fixture(),
                source_system_id=service.source_system_id,
                default_timezone="Europe/Moscow",
            )
            service._apply_result(
                result,
                session_id=newer.id,
                connection_version_before=connection_version,
            )
            after_newer = service.status()
            service._finish_failure(
                older.id, "PROVIDER_UNAVAILABLE", connection_version=connection_version
            )
            final = service.status()
            self.assertEqual(final["health"], "CURRENT")
            self.assertIsNone(final["latest_failure_reason"])
            self.assertEqual(final["next_sync_at"], after_newer["next_sync_at"])
            self.assertEqual(final["last_content_sha256"], after_newer["last_content_sha256"])

    def test_backup_clean_restore_keeps_import_identity_while_export_excludes_feed_secret(self):
        cipher = CredentialCipher([b"c" * 32])
        secret_url = "https://calendar.example.invalid/account-private-token.ics"
        with self.repo() as repo:
            AcademicScheduleService(
                repo,
                account_id=ACCOUNT,
                cipher=cipher,
                reader_factory=lambda _url: self.fixture,
                url_validator=lambda value: value,
            ).connect_url(url=secret_url)
            before = {
                "templates": repo.connection.execute("SELECT count(*) FROM recurring_templates").fetchone()[0],
                "events": repo.connection.execute("SELECT count(*) FROM events").fetchone()[0],
                "identities": repo.connection.execute("SELECT count(*) FROM external_identities").fetchone()[0],
                "overrides": repo.connection.execute("SELECT count(*) FROM occurrence_overrides").fetchone()[0],
            }
        lifecycle = SQLiteDataLifecycle(self.database, now=lambda: NOW)
        exported = lifecycle.export_account(ACCOUNT).to_dict()
        self.assertNotIn("academic_schedule_connections", exported["tables"])
        self.assertNotIn("account-private-token", repr(exported))
        self.assertTrue(exported["tables"]["external_identities"])

        backup = Path(self.temp.name) / "academic-backup.sqlite"
        restored = Path(self.temp.name) / "academic-restored.sqlite"
        manifest = lifecycle.create_backup(backup)
        result = SQLiteDataLifecycle.restore_backup(backup, restored)
        self.assertEqual((manifest.integrity_check, result.integrity_check, result.foreign_key_violations),
                         ("ok", "ok", 0))
        with SQLiteCanonicalRepository(restored, clock=FrozenClock(NOW)) as repo:
            after = {
                "templates": repo.connection.execute("SELECT count(*) FROM recurring_templates").fetchone()[0],
                "events": repo.connection.execute("SELECT count(*) FROM events").fetchone()[0],
                "identities": repo.connection.execute("SELECT count(*) FROM external_identities").fetchone()[0],
                "overrides": repo.connection.execute("SELECT count(*) FROM occurrence_overrides").fetchone()[0],
            }
            self.assertEqual(after, before)
            status = AcademicScheduleService(repo, account_id=ACCOUNT, cipher=cipher).status()
            self.assertTrue(status["connected"])
            self.assertNotIn("account-private-token", repr(status))

    def test_account_deletion_purges_academic_credential_and_preserves_other_account(self):
        cipher = CredentialCipher([b"e" * 32])
        with self.repo() as repo:
            repo.create_account("other-student")
            AcademicScheduleService(
                repo,
                account_id=ACCOUNT,
                cipher=cipher,
                reader_factory=lambda _url: self.fixture,
                url_validator=lambda value: value,
            ).connect_url(url="https://calendar.example.invalid/private.ics")
            revision = int(repo.connection.execute(
                "SELECT server_revision FROM accounts WHERE id=?", (ACCOUNT,)
            ).fetchone()[0])
        result = SQLiteDataLifecycle(self.database, now=lambda: NOW).delete_account(
            ACCOUNT,
            expected_server_revision=revision,
            confirm_account_id=ACCOUNT,
        )
        self.assertEqual(result.deleted_rows["academic_schedule_connections"], 1)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM academic_schedule_connections WHERE account_id=?", (ACCOUNT,)
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM accounts WHERE id='other-student'"
            ).fetchone()[0], 1)

    def test_v23_to_v24_migration_idempotency_and_fail_closed_rollback(self):
        # Build a v23 database by applying all migrations except the newest.
        old = str(Path(self.temp.name) / "v23.sqlite")
        migrations = Path("src/student_execution_os/persistence/migrations")
        conn = sqlite3.connect(old)
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for version in range(1, 24):
            conn.executescript(next(migrations.glob(f"{version:03d}_*.sql")).read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)", (version, NOW.isoformat()))
        conn.execute("INSERT INTO accounts(id,server_revision) VALUES ('upgrade-student',0)")
        conn.commit()
        conn.close()
        with SQLiteCanonicalRepository(old, clock=FrozenClock(NOW)) as repo:
            repo.initialize()
            repo.initialize()
            self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
            self.assertEqual(repo.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        conn = sqlite3.connect(old)
        conn.executescript(ROLLBACK_V26.read_text(encoding="utf-8"))
        conn.executescript(ROLLBACK_V25.read_text(encoding="utf-8"))
        conn.executescript(ROLLBACK.read_text(encoding="utf-8"))
        self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 23)
        conn.close()

        with self.repo() as repo:
            AcademicScheduleService(repo, account_id=ACCOUNT).import_ics(self.fixture())
        conn = sqlite3.connect(self.database)
        conn.executescript(ROLLBACK_V26.read_text(encoding="utf-8"))
        conn.executescript(ROLLBACK_V25.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(sqlite3.IntegrityError, "connections to be removed"):
            conn.executescript(ROLLBACK.read_text(encoding="utf-8"))
        self.assertTrue(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='academic_schedule_connections'"
        ).fetchone())
        conn.close()


if __name__ == "__main__":
    unittest.main()
