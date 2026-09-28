"""R9: explicit reminders follow source-driven changes; DST-safe quiet hours.

LOCAL INTEGRATION with a frozen clock: real HTTP API (bound mode), the real reminder
engine and push dispatcher (fake FCM), and SourceApplier/GroupService as the source.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.reconciliation import SQLiteReconciliationRepository
from student_execution_os.recurrence.source import SourceApplier, SourceEvent, SourceSnapshot
from student_execution_os.reminders import ReminderEngine
from student_execution_os.reminders.policy import ReminderPrefs
from student_execution_os.reminders.push import PushDispatcher, SendResult
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

START = datetime(2026, 9, 23, 6, 0, tzinfo=timezone.utc)  # Wednesday 09:00 Moscow
EXAM_START = datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc)  # Thursday 14:00 Moscow
SOURCE = "src-university"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Phone:
    name, configured = "fake-fcm", True

    def __init__(self):
        self.sent: list[tuple[datetime, dict]] = []
        self.at: datetime | None = None

    def send(self, token, message, *, data_only=False):
        self.sent.append((self.at, dict(message)))
        return SendResult(True, f"fcm-{len(self.sent)}")


class EventReminderReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / "r9.sqlite")
        with self.repo(START) as repo:
            repo.create_account("a")
            SQLiteReconciliationRepository(repo).create_source_system(
                account_id="a", kind="ACADEMIC_ICAL", actor=ActorCategory.CONNECTOR_INGESTION,
                source_system_id=SOURCE, policy_context={})
        self.clock = Clock(START)
        self.cm = TestClient(create_app(self.db, account_id="a", principal_id="user", now=self.clock))
        self.client = self.cm.__enter__()
        self.addCleanup(self.cm.__exit__, None, None, None)
        self.client.patch("/api/v1/notification-preferences", json={
            "timezone": "Europe/Moscow", "locale": "ru"})
        prefs = self.client.get("/api/v1/notification-preferences").json()
        self.client.patch("/api/v1/notification-preferences", json={
            "expected_version": prefs["version"], "quiet_hours": {"starts_local": "03:00", "ends_local": "03:01"}})
        self.client.post("/api/v1/mobile/devices", json={"token": "phone", "capabilities": ["reminder-actions-v1"]})
        self.phone = Phone()
        self.n = 0

    def repo(self, at):
        repo = SQLiteCanonicalRepository(self.db, clock=FrozenClock(at))
        repo.initialize()
        return repo

    def source(self, at, *, starts=EXAM_START, sequence=1, cancelled=False, present=True):
        events = (SourceEvent(uid="exam-1", title="Экзамен по алгебре", starts_at=starts,
                              ends_at=starts + timedelta(minutes=90), sequence=sequence, cancelled=cancelled),)
        with self.repo(at) as repo:
            SourceApplier(repo, account_id="a").apply(
                SourceSnapshot(SOURCE, events=events if present else (), complete=True))
            return repo.connection.execute(
                "SELECT local_id FROM external_identities WHERE external_uid='exam-1'").fetchone()[0]

    def set_lead(self, event_id, minutes):
        self.n += 1
        body = {"operations": [{"op_id": f"lead-op-{self.n:04d}", "type": "event.update", "entity_id": event_id,
                                "payload": {"remind_before_minutes": minutes}}]}
        result = self.client.post("/api/v1/sync", json=body).json()["results"][0]
        self.assertEqual(result["status"], "APPLIED", result)

    def tick(self, at, *, deliver=True):
        self.clock.now = at
        ReminderEngine(self.db).tick("a", at)
        if deliver:
            self.phone.at = at
            PushDispatcher(self.db, self.phone).run_once(at, "w")

    def walk(self, start, end, step=timedelta(minutes=5)):
        at = start
        while at <= end:
            self.tick(at)
            at += step

    def exam_pushes(self):
        return [at for at, message in self.phone.sent if "Экзамен по алгебре" in str(message)]

    def remind_at(self, event_id):
        with sqlite3.connect(self.db) as conn:
            row = conn.execute("SELECT remind_at FROM reminder_states WHERE task_id=?", (event_id,)).fetchone()
        return row and row[0]

    def test_source_move_retimes_the_users_reminder_and_nothing_fires_at_the_old_time(self):
        exam = self.source(START)
        self.set_lead(exam, 60)
        self.assertEqual(self.remind_at(exam), "2026-09-24T10:00:00+00:00")
        moved = EXAM_START + timedelta(hours=2)  # the university moves the exam to 16:00 Moscow
        self.source(START + timedelta(minutes=1), starts=moved, sequence=2)
        self.assertEqual(self.remind_at(exam), "2026-09-24T12:00:00+00:00")
        self.walk(datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc), datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(self.exam_pushes(), [datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)])

    def test_a_message_queued_for_the_old_time_is_withdrawn_and_the_new_one_sent(self):
        exam = self.source(START)
        self.set_lead(exam, 60)
        old = datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)
        self.tick(old, deliver=False)  # decided and queued, not yet delivered (e.g. phone offline)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM reminder_messages WHERE delivery_state='PENDING'")
                             .fetchone()[0], 1)
        self.source(old + timedelta(minutes=1), starts=EXAM_START + timedelta(hours=2), sequence=2)
        self.walk(old + timedelta(minutes=2), datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc))
        self.assertEqual(self.exam_pushes(), [datetime(2026, 9, 24, 12, 2, tzinfo=timezone.utc)])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT last_error FROM reminder_messages WHERE delivery_state='CANCELLED'")
                             .fetchall(), [("SOURCE_CHANGED",)])

    def test_after_delivery_a_later_move_produces_one_new_correct_reminder(self):
        exam = self.source(START)
        self.set_lead(exam, 60)
        self.walk(datetime(2026, 9, 24, 9, 50, tzinfo=timezone.utc), datetime(2026, 9, 24, 10, 20, tzinfo=timezone.utc))
        self.assertEqual(len(self.exam_pushes()), 1)
        self.source(datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc), starts=EXAM_START + timedelta(hours=5),
                    sequence=2)
        self.walk(datetime(2026, 9, 24, 10, 35, tzinfo=timezone.utc), datetime(2026, 9, 24, 16, 30, tzinfo=timezone.utc))
        self.assertEqual(self.exam_pushes(), [datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
                                              datetime(2026, 9, 24, 15, 0, tzinfo=timezone.utc)])

    def test_source_cancel_suppresses_restore_and_disconnect_reconnect_bring_it_back(self):
        exam = self.source(START)
        self.set_lead(exam, 30)
        self.source(START + timedelta(minutes=1), sequence=2, cancelled=True)
        self.assertIsNone(self.remind_at(exam))
        self.source(START + timedelta(minutes=2), sequence=3)  # the cancellation is withdrawn
        self.assertEqual(self.remind_at(exam), "2026-09-24T10:30:00+00:00")
        self.source(START + timedelta(minutes=3), present=False)  # feed disconnected
        self.assertIsNone(self.remind_at(exam))
        self.source(START + timedelta(minutes=4), sequence=4)  # reconnected: same identity, same lead
        self.assertEqual(self.remind_at(exam), "2026-09-24T10:30:00+00:00")
        self.walk(datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc), datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc))
        self.assertEqual(self.exam_pushes(), [datetime(2026, 9, 24, 10, 30, tzinfo=timezone.utc)])

    def test_unchanged_refresh_and_restarts_never_duplicate_or_erase(self):
        exam = self.source(START)
        self.set_lead(exam, 60)
        standalone = self.client.post("/api/v1/sync", json={"operations": [{
            "op_id": "own-rem-0001", "type": "reminder.create", "entity_id": "own-reminder-01",
            "payload": {"title": "Взять калькулятор", "remind_at": "2026-09-24T09:00:00+00:00"}}]}).json()
        self.assertEqual(standalone["results"][0]["status"], "APPLIED")
        for minute in range(3):  # scheduled refreshes with identical content
            self.source(START + timedelta(minutes=minute + 1))
        self.assertEqual(self.remind_at(exam), "2026-09-24T10:00:00+00:00")
        at = datetime(2026, 9, 24, 8, 55, tzinfo=timezone.utc)
        while at <= datetime(2026, 9, 24, 11, 0, tzinfo=timezone.utc):
            self.tick(at)
            self.tick(at)  # a worker restart re-running the same tick
            at += timedelta(minutes=5)
        self.assertEqual(self.exam_pushes(), [datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc)])
        own = [at for at, message in self.phone.sent if "Взять калькулятор" in str(message)]
        self.assertEqual(own, [datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)])

    def test_the_users_own_event_edit_still_retimes_as_before(self):
        self.n += 1
        body = {"operations": [{"op_id": "own-event-01", "type": "event.create", "entity_id": "own-event-0001",
                                "payload": {"title": "Встреча с научруком", "starts_at": "2026-09-24T12:00:00+00:00",
                                            "ends_at": "2026-09-24T13:00:00+00:00", "remind_before_minutes": 15}}]}
        self.assertEqual(self.client.post("/api/v1/sync", json=body).json()["results"][0]["status"], "APPLIED")
        self.assertEqual(self.remind_at("own-event-0001"), "2026-09-24T11:45:00+00:00")
        move = {"operations": [{"op_id": "own-event-02", "type": "event.update", "entity_id": "own-event-0001",
                                "payload": {"starts_at": "2026-09-24T14:00:00+00:00"}}]}
        self.client.post("/api/v1/sync", json=move)
        self.assertEqual(self.remind_at("own-event-0001"), "2026-09-24T13:45:00+00:00")


class GroupExamReminderTest(unittest.TestCase):
    def test_starosta_moving_an_exam_retimes_each_members_own_reminder_only(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = str(Path(tmp.name) / "g.sqlite")
        from student_execution_os.groups import GroupService
        from student_execution_os.sync.commands import SyncService
        with SQLiteCanonicalRepository(db, clock=FrozenClock(START)) as repo:
            repo.initialize()
            for account in ("owner", "bob", "carol"):
                repo.create_account(account)
            owner = GroupService(repo, account_id="owner")
            owner.create({"id": "group-r9-test", "name": "БИ", "timezone_name": "Europe/Moscow"})
            code = owner.invite("group-r9-test", {})["code"]
            for member in ("bob", "carol"):
                GroupService(repo, account_id=member).join({"code": code})
            exam = {"title": "Контрольная", "starts_at": EXAM_START.isoformat(),
                    "ends_at": (EXAM_START + timedelta(minutes=90)).isoformat(), "category": "EXAM"}
            owner.publish("group-r9-test", "event-exam-r9x", {"kind": "EVENT", "item": exam, "expected_revision": 0})

            def local(account):
                return repo.connection.execute(
                    "SELECT local_id FROM external_identities WHERE account_id=? AND external_uid='event-exam-r9x'",
                    (account,)).fetchone()[0]

            SyncService(repo, account_id="bob", principal_id="bob").apply({
                "op_id": "bob-lead-0001", "type": "event.update", "entity_id": local("bob"),
                "payload": {"remind_before_minutes": 120}})
            owner.publish("group-r9-test", "event-exam-r9x", {
                "kind": "EVENT", "expected_revision": 1,
                "item": {**exam, "starts_at": (EXAM_START + timedelta(days=1)).isoformat(),
                         "ends_at": (EXAM_START + timedelta(days=1, minutes=90)).isoformat()}})
            states = {account: repo.connection.execute(
                "SELECT remind_at FROM reminder_states WHERE account_id=? AND task_id=?",
                (account, local(account))).fetchone() for account in ("bob", "carol")}
        self.assertEqual(states["bob"][0], "2026-09-25T09:00:00+00:00")  # his 2h lead, new day
        self.assertIsNone(states["carol"])  # Carol never asked for a reminder: none is invented


class QuietHoursDstTest(unittest.TestCase):
    def test_quiet_hours_end_is_local_wall_clock_across_both_dst_switches(self):
        prefs = ReminderPrefs(timezone_name="Europe/Berlin", quiet_starts_local="23:00", quiet_ends_local="07:00")
        utc = timezone.utc
        # Autumn: 2026-10-25 03:00 CEST -> 02:00 CET. 07:00 CET is 06:00 UTC.
        self.assertEqual(prefs.quiet_until(datetime(2026, 10, 24, 22, 30, tzinfo=utc)),
                         datetime(2026, 10, 25, 6, 0, tzinfo=utc))
        self.assertEqual(prefs.quiet_until(datetime(2026, 10, 25, 5, 30, tzinfo=utc)),
                         datetime(2026, 10, 25, 6, 0, tzinfo=utc))
        self.assertIsNone(prefs.quiet_until(datetime(2026, 10, 25, 6, 0, tzinfo=utc)))
        # Spring: 2026-03-29 02:00 CET -> 03:00 CEST. 07:00 CEST is 05:00 UTC.
        self.assertEqual(prefs.quiet_until(datetime(2026, 3, 29, 0, 30, tzinfo=utc)),
                         datetime(2026, 3, 29, 5, 0, tzinfo=utc))
        # A quiet period ending inside the skipped hour ends at the first valid minute.
        gap = ReminderPrefs(timezone_name="Europe/Berlin", quiet_starts_local="23:00", quiet_ends_local="02:30")
        self.assertEqual(gap.quiet_until(datetime(2026, 3, 29, 0, 30, tzinfo=utc)),
                         datetime(2026, 3, 29, 1, 0, tzinfo=utc))


if __name__ == "__main__":
    unittest.main()
