"""Class series exceptions (schema v23, ADR 0027): user changes through the command
boundary, imported timetables through source apply, and the two never overwrite each other."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SCHEMA_VERSION, SQLiteCanonicalRepository
from student_execution_os.recurrence import OccurrenceOverrideAction, SQLiteRecurrenceRepository
from student_execution_os.recurrence.source import (
    SourceApplier,
    SourceEvent,
    SourceOccurrenceChange,
    SourceSeries,
    SourceSnapshot,
    local_id,
)
from student_execution_os.sync.commands import SyncService
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

UTC = timezone.utc
NOW = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)  # Monday
ACCOUNT = "student"
SOURCE = "timetable-feed"
MIGRATIONS = Path("src/student_execution_os/persistence/migrations")
ROLLBACK = Path("src/student_execution_os/persistence/rollback/023_series_exceptions_down.sql")
ROLLBACK_V24 = Path("src/student_execution_os/persistence/rollback/024_academic_schedule_down.sql")
ROLLBACK_V25 = Path("src/student_execution_os/persistence/rollback/025_capability_grants_down.sql")
ROLLBACK_V26 = Path("src/student_execution_os/persistence/rollback/026_oauth_connect_down.sql")


def seminar(**changes) -> SourceSeries:
    values = dict(uid="UID-SEMINAR", title="Семинар по матанализу", dtstart_local=datetime(2026, 9, 21, 10, 0),
                  duration_minutes=80, recurrence_rule="FREQ=WEEKLY;COUNT=15", timezone_name="UTC",
                  location_text="R205", teacher="Иванова А. А.", sequence=0)
    values.update(changes)
    return SourceSeries(**values)


class SeriesExceptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "v23.sqlite")
        self.now = NOW
        with self.repo() as repo:
            repo.initialize()
            repo.create_account(ACCOUNT)
        self.client = TestClient(create_app(self.db, account_id=ACCOUNT, principal_id="u", now=lambda: self.now))

    def tearDown(self):
        self.tmp.cleanup()

    def repo(self):
        return SQLiteCanonicalRepository(self.db, clock=FrozenClock(self.now))

    def op(self, op_id, op_type, entity_id, payload):
        response = self.client.post("/api/v1/sync", json={"operations": [
            {"op_id": f"r3-{op_id}", "type": op_type, "entity_id": entity_id, "payload": payload}]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["results"][0]

    def occurrences(self, template_id):
        body = self.client.get("/api/v1/calendar").json()
        return {o["original_recurrence_id"]: o for o in body["occurrences"] if o["template_id"] == template_id}

    def create_series(self, series_id="series-algebra", **extra):
        payload = {"title": "Алгебра", "dtstart_local": "2026-09-21T12:00:00", "duration_minutes": 90,
                   "recurrence_rule": "FREQ=WEEKLY;COUNT=10", "timezone_name": "UTC", "location_text": "А-101"}
        payload.update(extra)
        result = self.op(f"op-create-{series_id}", "series.create", series_id, payload)
        self.assertEqual(result["status"], "APPLIED", result)
        return series_id

    def apply(self, *series, events=(), complete=True):
        with self.repo() as repo:
            return SourceApplier(repo, account_id=ACCOUNT).apply(
                SourceSnapshot(source_system_id=SOURCE, series=tuple(series), events=tuple(events), complete=complete))

    # ---- user changes through the command boundary --------------------------------------

    def test_series_create_is_exactly_once(self):
        payload = {"title": "Алгебра", "dtstart_local": "2026-09-21T12:00:00", "duration_minutes": 90,
                   "recurrence_rule": "FREQ=WEEKLY;COUNT=10", "timezone_name": "UTC"}
        first = self.op("op-once", "series.create", "series-once", payload)
        replay = self.op("op-once", "series.create", "series-once", payload)
        self.assertTrue(replay.pop("replayed"))
        self.assertFalse(first.pop("replayed"))
        self.assertEqual(replay, first)
        reused = self.op("op-once", "series.create", "series-once", {**payload, "title": "Другое"})
        self.assertEqual((reused["status"], reused["code"]), ("REJECTED", "OP_ID_REUSED"))
        again = self.op("op-other", "series.create", "series-once", payload)
        self.assertEqual((again["status"], again["code"]), ("NOOP", "ALREADY_EXISTS"))

    def test_move_cancel_restore_and_room_change_of_one_class(self):
        series = self.create_series()
        rid = "2026-09-28T12:00:00"
        moved = self.op("op-move", "series.occurrence.move", series,
                        {"template_id": series, "original_recurrence_id": rid, "starts_local": "2026-09-28T15:00:00"})
        self.assertEqual(moved["status"], "APPLIED", moved)
        occurrence = self.occurrences(series)[rid]
        self.assertEqual((occurrence["starts_at"], occurrence["changed_by"]), ("2026-09-28T15:00:00+00:00", ["USER"]))

        room = self.op("op-room", "series.occurrence.update", series,
                       {"template_id": series, "original_recurrence_id": rid, "location_text": "Б-310", "teacher": "Петров"})
        self.assertEqual(room["status"], "APPLIED", room)
        occurrence = self.occurrences(series)[rid]
        # A room change keeps the move: one USER layer, merged.
        self.assertEqual((occurrence["starts_at"], occurrence["location_text"], occurrence["teacher"]),
                         ("2026-09-28T15:00:00+00:00", "Б-310", "Петров"))
        self.assertEqual(self.occurrences(series)["2026-10-05T12:00:00"]["location_text"], "А-101")

        cancelled = self.op("op-cancel", "series.occurrence.cancel", series, {"template_id": series, "original_recurrence_id": rid})
        self.assertEqual(cancelled["status"], "APPLIED")
        self.assertEqual((self.occurrences(series)[rid]["cancelled"], self.occurrences(series)[rid]["cancelled_by"]), (True, "USER"))
        again = self.op("op-cancel-2", "series.occurrence.cancel", series, {"template_id": series, "original_recurrence_id": rid})
        self.assertEqual((again["status"], again["code"]), ("NOOP", "ALREADY_CANCELLED"))

        restored = self.op("op-restore", "series.occurrence.restore", series, {"template_id": series, "original_recurrence_id": rid})
        self.assertEqual(restored["status"], "APPLIED")
        occurrence = self.occurrences(series)[rid]
        self.assertEqual((occurrence["cancelled"], occurrence["starts_at"], occurrence["location_text"]),
                         (False, "2026-09-28T12:00:00+00:00", "А-101"))

        bad = self.op("op-bad", "series.occurrence.cancel", series, {"template_id": series, "original_recurrence_id": "2026-09-29T12:00:00"})
        self.assertEqual(bad["status"], "REJECTED")  # not an occurrence of this weekly series

    def test_extra_class_holiday_and_split_from_a_date(self):
        series = self.create_series()
        extra = self.op("op-extra", "series.extra.create", "extra-algebra-1",
                        {"template_id": series, "starts_at": "2026-09-30T09:00:00+00:00", "location_text": "Б-101"})
        self.assertEqual(extra["status"], "APPLIED", extra)
        self.now = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
        soon = {e["id"]: e for e in self.client.get("/api/v1/today").json()["upcoming_events"]}
        self.assertEqual((soon["extra-algebra-1"]["title"], soon["extra-algebra-1"]["location_text"]), ("Алгебра", "Б-101"))
        self.assertEqual(soon["extra-algebra-1"]["series"]["template_id"], series)
        self.assertEqual(soon["extra-algebra-1"]["ends_at"], "2026-09-30T10:30:00+00:00")

        holiday = self.op("op-holiday", "series.holiday", "holiday-autumn", {"from_date": "2026-10-05", "to_date": "2026-10-12"})
        self.assertEqual((holiday["status"], holiday["entity"]["cancelled"]), ("APPLIED", 2))
        occ = self.occurrences(series)
        self.assertEqual({rid: occ[rid]["cancel_reason"] for rid in ("2026-10-05T12:00:00", "2026-10-12T12:00:00")},
                         {"2026-10-05T12:00:00": "HOLIDAY", "2026-10-12T12:00:00": "HOLIDAY"})
        self.assertFalse(occ["2026-10-19T12:00:00"]["cancelled"])
        undo = self.op("op-holiday-undo", "series.holiday.restore", "holiday-autumn", {"from_date": "2026-10-05", "to_date": "2026-10-12"})
        self.assertEqual(undo["entity"]["restored"], 2)
        self.assertFalse(self.occurrences(series)["2026-10-05T12:00:00"]["cancelled"])

        split = self.op("op-split", "series.split", "series-algebra-v2", {
            "template_id": series, "original_recurrence_id": "2026-10-19T12:00:00", "starts_local": "2026-10-20T14:00:00",
            "recurrence_rule": "FREQ=WEEKLY;COUNT=5", "location_text": "В-202"})
        self.assertEqual(split["status"], "APPLIED", split)
        old = self.occurrences(series)
        new = self.occurrences("series-algebra-v2")
        self.assertIn("2026-10-12T12:00:00", old)
        self.assertNotIn("2026-10-19T12:00:00", old)
        self.assertEqual(new["2026-10-20T14:00:00"]["location_text"], "В-202")
        with self.repo() as repo:
            full = SQLiteRecurrenceRepository(repo).expand(account_id=ACCOUNT, template_id="series-algebra-v2",
                horizon_start=datetime(2026, 9, 1, tzinfo=UTC), horizon_end=datetime(2027, 1, 1, tzinfo=UTC))
        self.assertEqual(len(full), 5)

    # ---- imported timetable (SOURCE layer) -------------------------------------------------

    def test_initial_import_and_idempotent_resync(self):
        exam = SourceEvent(uid="UID-CONSULT", title="Консультация", starts_at=datetime(2026, 10, 1, 15, 0, tzinfo=UTC),
                           ends_at=datetime(2026, 10, 1, 16, 0, tzinfo=UTC), location_text="Онлайн", series_uid="UID-SEMINAR")
        report = self.apply(seminar(exdates_local=(datetime(2026, 10, 5, 10, 0),)), events=[exam])
        self.assertEqual(sorted(report.created), ["UID-CONSULT", "UID-SEMINAR"])
        template_id = local_id("src-series", SOURCE, "UID-SEMINAR")
        occ = self.occurrences(template_id)
        self.assertEqual((occ["2026-09-28T10:00:00"]["location_text"], occ["2026-09-28T10:00:00"]["teacher"]), ("R205", "Иванова А. А."))
        self.assertEqual((occ["2026-10-05T10:00:00"]["cancelled_by"], occ["2026-10-05T10:00:00"]["cancel_reason"]), ("SOURCE", "SOURCE"))
        with self.repo() as repo:
            template_version = SQLiteRecurrenceRepository(repo).get_template(ACCOUNT, template_id).version
        again = self.apply(seminar(exdates_local=(datetime(2026, 10, 5, 10, 0),)), events=[exam])
        self.assertEqual((again.created, again.updated, sorted(again.unchanged), again.occurrence_changes),
                         ([], [], ["UID-CONSULT", "UID-SEMINAR"], 0))
        with self.repo() as repo:
            self.assertEqual(SQLiteRecurrenceRepository(repo).get_template(ACCOUNT, template_id).version, template_version)
        today = {e["id"]: e for e in self.client.get("/api/v1/events").json()}
        consult = today[local_id("src-event", SOURCE, "UID-CONSULT")]
        self.assertEqual(consult["title"], "Консультация")

    def test_source_move_cancel_restore_location_and_series_update(self):
        self.apply(seminar())
        template_id = local_id("src-series", SOURCE, "UID-SEMINAR")
        moved = SourceOccurrenceChange(recurrence_local=datetime(2026, 10, 12, 10, 0), starts_local=datetime(2026, 10, 13, 16, 0),
                                       location_text="G-100", sequence=1)
        self.apply(seminar(changes=(moved,), exdates_local=(datetime(2026, 10, 19, 10, 0),)))
        occ = self.occurrences(template_id)
        self.assertEqual((occ["2026-10-12T10:00:00"]["starts_at"], occ["2026-10-12T10:00:00"]["location_text"]),
                         ("2026-10-13T16:00:00+00:00", "G-100"))
        self.assertTrue(occ["2026-10-19T10:00:00"]["cancelled"])
        # The source takes both back: restored exactly.
        self.apply(seminar())
        occ = self.occurrences(template_id)
        self.assertEqual((occ["2026-10-12T10:00:00"]["starts_at"], occ["2026-10-12T10:00:00"]["changed_by"]),
                         ("2026-10-12T10:00:00+00:00", []))
        self.assertFalse(occ["2026-10-19T10:00:00"]["cancelled"])
        # Series-wide room change and a new time from the source.
        report = self.apply(seminar(location_text="R310", dtstart_local=datetime(2026, 9, 21, 11, 0), sequence=2))
        self.assertEqual(report.updated, ["UID-SEMINAR"])
        occ = self.occurrences(template_id)
        self.assertIn("2026-10-12T11:00:00", occ)
        self.assertEqual(occ["2026-10-12T11:00:00"]["location_text"], "R310")

    def test_source_wide_time_shift_keeps_user_override_on_same_ordinal_class(self):
        self.apply(seminar())
        template_id = local_id("src-series", SOURCE, "UID-SEMINAR")
        old_rid = "2026-10-12T10:00:00"
        self.op("op-personal-room", "series.occurrence.update", template_id, {
            "template_id": template_id, "original_recurrence_id": old_rid,
            "location_text": "Моя аудитория",
        })
        self.apply(seminar(dtstart_local=datetime(2026, 9, 21, 11, 0), sequence=1))
        occurrences = self.occurrences(template_id)
        new_rid = "2026-10-12T11:00:00"
        self.assertNotIn(old_rid, occurrences)
        self.assertEqual((occurrences[new_rid]["location_text"], occurrences[new_rid]["changed_by"]),
                         ("Моя аудитория", ["USER"]))
        with self.repo() as repo:
            override = SQLiteRecurrenceRepository(repo).get_override(ACCOUNT, template_id, new_rid)
            self.assertIsNotNone(override)

    def test_unchanged_newer_occurrence_version_blocks_late_change(self):
        moved = SourceOccurrenceChange(
            recurrence_local=datetime(2026, 10, 12, 10, 0),
            starts_local=datetime(2026, 10, 13, 16, 0), sequence=3,
        )
        self.apply(seminar(sequence=3, changes=(moved,)))
        self.apply(seminar(sequence=5, changes=(SourceOccurrenceChange(
            recurrence_local=moved.recurrence_local, starts_local=moved.starts_local, sequence=5,
        ),)))
        late = self.apply(seminar(sequence=6, changes=(SourceOccurrenceChange(
            recurrence_local=moved.recurrence_local,
            starts_local=datetime(2026, 10, 14, 18, 0), sequence=4,
        ),)))
        template_id = local_id("src-series", SOURCE, "UID-SEMINAR")
        self.assertEqual(late.stale, ["UID-SEMINAR#2026-10-12T10:00:00"])
        self.assertEqual(self.occurrences(template_id)["2026-10-12T10:00:00"]["starts_at"],
                         "2026-10-13T16:00:00+00:00")

    def test_one_off_source_cancel_restore_and_personal_cancel_layers(self):
        event = SourceEvent(
            uid="UID-EXAM", title="Экзамен",
            starts_at=datetime(2026, 10, 20, 9, 0, tzinfo=UTC),
            ends_at=datetime(2026, 10, 20, 11, 0, tzinfo=UTC), sequence=1,
        )
        event_id = local_id("src-event", SOURCE, event.uid)
        self.apply(events=(event,))
        cancelled = SourceEvent(**{**event.__dict__, "cancelled": True, "sequence": 2})
        report = self.apply(events=(cancelled,))
        self.assertEqual(report.updated, [event.uid])
        with self.repo() as repo:
            self.assertEqual(repo.get_event(ACCOUNT, event_id).obligation.lifecycle_status.value, "CANCELLED")
        blocked = self.op("op-source-reopen", "event.reopen", event_id, {})
        self.assertEqual((blocked["status"], blocked["code"]), ("CONFLICT", "SOURCE_EVENT_CANCELLED"))

        active = SourceEvent(**{**event.__dict__, "sequence": 3})
        restored = self.apply(events=(active,))
        self.assertEqual(restored.restored, [event.uid])
        with self.repo() as repo:
            self.assertEqual(repo.get_event(ACCOUNT, event_id).obligation.lifecycle_status.value, "ACTIVE")

        self.assertEqual(self.op("op-user-cancel", "event.cancel", event_id, {})["status"], "APPLIED")
        # A source cancellation and later restoration must not erase the personal cancel.
        self.apply(events=(SourceEvent(**{**event.__dict__, "cancelled": True, "sequence": 4}),))
        self.apply(events=(SourceEvent(**{**event.__dict__, "sequence": 5}),))
        with self.repo() as repo:
            row = repo.connection.execute(
                "SELECT source_cancelled,user_cancelled FROM external_identities WHERE account_id=? AND local_id=?",
                (ACCOUNT, event_id),
            ).fetchone()
            self.assertEqual(tuple(row), (0, 1))
            self.assertEqual(repo.get_event(ACCOUNT, event_id).obligation.lifecycle_status.value, "CANCELLED")
        self.assertEqual(self.op("op-user-reopen", "event.reopen", event_id, {})["status"], "APPLIED")

        denied = self.op("op-import-delete", "event.delete", event_id, {})
        self.assertEqual((denied["status"], denied["code"]), ("CONFLICT", "IMPORTED_EVENT_SOURCE_OWNED"))

    def test_user_layer_survives_source_updates_and_source_cancel_survives_user_restore(self):
        self.apply(seminar())
        template_id = local_id("src-series", SOURCE, "UID-SEMINAR")
        rid = "2026-10-12T10:00:00"
        self.op("op-note", "series.occurrence.update", template_id,
                {"template_id": template_id, "original_recurrence_id": rid, "location_text": "Моя аудитория"})
        self.apply(seminar(location_text="R999", sequence=1))
        occ = self.occurrences(template_id)
        self.assertEqual((occ[rid]["location_text"], occ["2026-10-19T10:00:00"]["location_text"]), ("Моя аудитория", "R999"))
        self.assertEqual(occ[rid]["changed_by"], ["USER"])

        self.apply(seminar(location_text="R999", sequence=2, exdates_local=(datetime(2026, 10, 12, 10, 0),)))
        occ = self.occurrences(template_id)
        self.assertEqual((occ[rid]["cancelled"], occ[rid]["cancelled_by"]), (True, "SOURCE"))
        self.op("op-restore-src", "series.occurrence.restore", template_id, {"template_id": template_id, "original_recurrence_id": rid})
        self.assertTrue(self.occurrences(template_id)[rid]["cancelled"])  # the timetable still says no class

        split = self.op("op-split-src", "series.split", "series-mine-v2",
                        {"template_id": template_id, "original_recurrence_id": "2026-10-26T10:00:00"})
        self.assertEqual(split["status"], "REJECTED")

    def test_late_update_is_ignored_and_removal_keeps_history(self):
        self.apply(seminar(sequence=3, location_text="New room"))
        template_id = local_id("src-series", SOURCE, "UID-SEMINAR")
        late = self.apply(seminar(sequence=2, location_text="Old room"))
        self.assertEqual(late.stale, ["UID-SEMINAR"])
        self.assertEqual(self.occurrences(template_id)["2026-10-12T10:00:00"]["location_text"], "New room")

        self.now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
        removed = self.apply()
        self.assertEqual(removed.removed, ["UID-SEMINAR"])
        with self.repo() as repo:
            occurrences = SQLiteRecurrenceRepository(repo).expand(
                account_id=ACCOUNT, template_id=template_id,
                horizon_start=datetime(2026, 9, 1, tzinfo=UTC), horizon_end=datetime(2026, 12, 31, tzinfo=UTC))
        self.assertEqual([o.original_recurrence_id for o in occurrences][-1], "2026-10-05T10:00:00")  # past stays
        back = self.apply(seminar(sequence=4, location_text="New room"))
        self.assertEqual(back.restored, ["UID-SEMINAR"])
        self.assertIn("2026-10-12T10:00:00", self.occurrences(template_id))

    def test_partial_snapshot_never_removes(self):
        self.apply(seminar())
        report = self.apply(complete=False)
        self.assertEqual(report.removed, [])


class V23MigrationTests(unittest.TestCase):
    def build(self, path: str, upto: int) -> None:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        for script in sorted(MIGRATIONS.glob("*.sql")):
            version = int(script.name[:3])
            if version > upto:
                continue
            conn.executescript(script.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)", (version, NOW.isoformat()))
        conn.commit()
        conn.close()

    @staticmethod
    def shape(path: str) -> dict[str, list[tuple]]:
        with closing(sqlite3.connect(path)) as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {t: [tuple(c[1:3]) for c in conn.execute(f"PRAGMA table_info({t})")] for t in tables}

    def test_v22_overrides_become_the_user_layer_and_rollback_restores_v22_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "v22.sqlite")
            self.build(db, 22)
            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as repo:
                repo.create_account(ACCOUNT)
                conn = repo.connection
                conn.execute(
                    "INSERT INTO recurring_templates(id,account_id,title,category,importance,dtstart_local,duration_minutes,recurrence_rule,"
                    "timezone_name,attendance_policy,location_effect_kind,resolution_policy,version,created_at,updated_at) VALUES "
                    "('old-series',?,'Физика','LESSON','NORMAL','2026-09-21T09:00:00',90,'FREQ=WEEKLY;COUNT=5','UTC','REQUIRED','NONE',"
                    "'EARLIER_FOLD_SHIFT_FORWARD',1,?,?)", (ACCOUNT, NOW.isoformat(), NOW.isoformat()))
                conn.execute(
                    "INSERT INTO occurrence_overrides(id,account_id,template_id,original_recurrence_id,action,replacement_start_local,"
                    "version,created_at,updated_at) VALUES ('ov-1',?,'old-series','2026-09-28T09:00:00','MODIFY','2026-09-28T11:00:00',1,?,?)",
                    (ACCOUNT, NOW.isoformat(), NOW.isoformat()))
                conn.commit()
            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as repo:
                repo.initialize()
                repo.initialize()  # idempotent
                self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
                store = SQLiteRecurrenceRepository(repo)
                override = store.get_override(ACCOUNT, "old-series", "2026-09-28T09:00:00")
                self.assertEqual((override.layer.value, override.action, override.replacement_start_local),
                                 ("USER", OccurrenceOverrideAction.MODIFY, datetime(2026, 9, 28, 11, 0)))
                self.assertEqual(repo.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(repo.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            conn = sqlite3.connect(db)
            conn.executescript(ROLLBACK_V26.read_text(encoding="utf-8"))
            conn.executescript(ROLLBACK_V25.read_text(encoding="utf-8"))
            conn.executescript(ROLLBACK_V24.read_text(encoding="utf-8"))
            conn.executescript(ROLLBACK.read_text(encoding="utf-8"))
            conn.commit()
            kept = conn.execute("SELECT id, replacement_start_local FROM occurrence_overrides").fetchall()
            conn.close()
            self.assertEqual(kept, [("ov-1", "2026-09-28T11:00:00")])
            reference = str(Path(tmp) / "ref.sqlite")
            self.build(reference, 22)
            self.assertEqual(self.shape(db), self.shape(reference))

    def test_rollback_fails_closed_when_v23_source_state_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "v23-source.sqlite")
            with SQLiteCanonicalRepository(db, clock=FrozenClock(NOW)) as repo:
                repo.initialize()
                repo.create_account(ACCOUNT)
                SourceApplier(repo, account_id=ACCOUNT).apply(
                    SourceSnapshot(source_system_id=SOURCE, series=(seminar(),)))
            conn = sqlite3.connect(db)
            conn.executescript(ROLLBACK_V26.read_text(encoding="utf-8"))
            conn.executescript(ROLLBACK_V25.read_text(encoding="utf-8"))
            conn.executescript(ROLLBACK_V24.read_text(encoding="utf-8"))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "rollback would discard"):
                conn.executescript(ROLLBACK.read_text(encoding="utf-8"))
            self.assertTrue(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_identities'"
            ).fetchone())
            self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 23)
            conn.close()


if __name__ == "__main__":
    unittest.main()
