"""Schema v31: tracked check-ins (incl. medication and quotas) and recurring reminders.

Driven through the real sync command boundary and the reminder engine with frozen
clocks, so replay, conflicts, delivery and policy are exercised as in production.
"""
from __future__ import annotations

import gzip
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.checkins import SQLiteCheckInRepository
from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SCHEMA_VERSION, SQLiteCanonicalRepository
from student_execution_os.reminders.engine import ReminderEngine
from student_execution_os.reminders.push import PushDispatcher, SendResult
from student_execution_os.reminders.standalone import SQLiteReminderRepository
from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.sync.commands import SyncService
from tests.rollback_chain import roll_back_newer_than

MSK = "Europe/Moscow"
# Tuesday 2026-10-06 08:00 in Moscow.
T0 = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)


class Phone:
    name, configured = "fake-fcm", True

    def __init__(self):
        self.sent = []

    def send(self, token, message, *, data_only=False):
        self.sent.append(message)
        return SendResult(True, f"fcm-{len(self.sent)}")

    def verify(self, tokens):  # pragma: no cover - not used here
        return {"ok": True}


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "v31.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            repo.create_account("a")
            repo.create_account("b")
            repo.connection.commit()
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def op(self, op_type, entity_id, payload=None, *, at=T0, account="a", op_id=None):
        self.n += 1
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(at)) as repo:
            repo.initialize()
            result = SyncService(repo, account_id=account, principal_id=f"user-{account}", now=at).apply({
                "op_id": op_id or f"op-{self.n:06d}", "type": op_type, "entity_id": entity_id, "payload": payload or {},
            })
            repo.connection.commit()
            return result

    def sql(self, query, params=()):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def occurrence(self, template_id, rid):
        rows = self.sql("SELECT * FROM checkin_occurrences WHERE template_id=? AND original_recurrence_id=?",
                        (template_id, rid))
        return rows[0] if rows else None

    def create_vitamin(self, **extra):
        payload = {"kind": "MEDICATION", "title": "Витамин D", "dtstart_local": "2026-10-06T09:00",
                   "recurrence_rule": "FREQ=DAILY", "timezone_name": MSK, **extra}
        result = self.op("checkin.create", "checkin-vitamin", payload)
        self.assertEqual(result["status"], "APPLIED", result)
        return result

    def tick(self, at):
        return ReminderEngine(self.db).tick("a", at)


class CheckInOutcomeTests(Harness):
    def test_alarm_feed_carries_the_prompts_occurrence_for_an_answer_on_the_alarm(self):
        self.create_vitamin(delivery="ALARM")
        today = "2026-10-06T09:00:00"
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            alarms = SQLiteReminderRepository(repo).upcoming_alarms("a", T0)
            # The page's reminder list leaves prompts out; the phone's alarm feed has them.
            self.assertEqual(SQLiteReminderRepository(repo).list("a"), [])
        prompt = [x for x in alarms if x["checkin"] and x["checkin"]["original_recurrence_id"] == today]
        self.assertEqual(len(prompt), 1, alarms)
        self.assertEqual(prompt[0]["checkin"], {"template_id": "checkin-vitamin", "original_recurrence_id": today,
                                                "kind": "MEDICATION"})
        # «Принял» from the alarm is the same typed outcome, at the moment of the press.
        pressed = T0 + timedelta(hours=1, minutes=3)
        done = self.op("checkin.occurrence.done", "checkin-vitamin",
                       {"template_id": "checkin-vitamin", "original_recurrence_id": today,
                        "occurred_at": pressed.isoformat()},
                       at=pressed + timedelta(minutes=20), op_id="alarm-reminder-x-1-CHECKIN_DONE")
        self.assertEqual(done["status"], "APPLIED", done)
        self.assertEqual(datetime.fromisoformat(self.occurrence("checkin-vitamin", today)["occurred_at"]), pressed)
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(pressed)) as repo:
            repo.initialize()
            after = [x for x in SQLiteReminderRepository(repo).upcoming_alarms("a", pressed) if x["checkin"]
                     and x["checkin"]["original_recurrence_id"] == today]
        self.assertEqual(after, [], "an answered occurrence no longer rings")

    def test_scenario_daily_medication_prompt_snooze_and_taken(self):
        self.create_vitamin(dose_text="2000 ME")
        today = "2026-10-06T09:00:00"
        occurrence = self.occurrence("checkin-vitamin", today)
        self.assertEqual(occurrence["status"], "PENDING")
        self.assertIsNotNone(occurrence["reminder_id"])
        # The prompt is not a Task and not in the standalone reminder list.
        self.assertEqual(self.sql("SELECT count(*) AS n FROM obligations")[0]["n"], 0)
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            self.assertEqual(SQLiteReminderRepository(repo).list("a"), [])
        nine = T0 + timedelta(hours=1)
        self.assertEqual(len(self.tick(nine).messages), 1)
        message = self.sql("SELECT * FROM reminder_messages WHERE stage='REMINDER'")[0]
        self.assertEqual(message["title"], "💊 Витамин D")
        self.assertIn("2000 ME", message["body"])
        self.assertIn('"CHECKIN_DONE"', message["actions_json"])
        self.assertIn("Принял", message["actions_json"].encode().decode("unicode_escape"))
        # The phone receives the occurrence identity with the prompt.
        phone = Phone()
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(nine)) as repo:
            repo.initialize()
            from student_execution_os.reminders.store import ReminderStore
            ReminderStore(repo).register_device("a", "token-1", "phone", ["reminder-actions-v1"])
            repo.connection.commit()
        PushDispatcher(self.db, phone).run_once(nine)
        self.assertEqual(phone.sent[0]["checkin"], {"template_id": "checkin-vitamin", "original_recurrence_id": today,
                                                    "kind": "MEDICATION"})
        # Snooze: the reminder moves, the outcome stays open.
        snoozed = self.op("reminder.snooze", occurrence["reminder_id"], {"minutes": 15,
                          "reminder_message_id": message["id"]}, at=nine + timedelta(minutes=1))
        self.assertEqual(snoozed["status"], "APPLIED")
        self.assertEqual(self.occurrence("checkin-vitamin", today)["status"], "PENDING")
        self.assertEqual(len(self.tick(nine + timedelta(minutes=16)).messages), 1)
        # «Принял» at 09:04 local, recorded later.
        done = self.op("checkin.occurrence.done", "checkin-vitamin",
                       {"original_recurrence_id": today, "occurred_at": "2026-10-06T06:04:00+00:00"},
                       at=nine + timedelta(minutes=20))
        self.assertEqual((done["status"], done["entity"]["status"]), ("APPLIED", "DONE"))
        self.assertEqual(done["entity"]["occurred_at"], "2026-10-06T06:04:00+00:00")
        self.assertEqual(self.sql("SELECT status FROM reminders WHERE id=?", (occurrence["reminder_id"],))[0]["status"], "DONE")
        # Nothing more is asked about a recorded day.
        self.assertEqual(self.tick(nine + timedelta(hours=2)).messages, [])

    def test_duplicate_done_replay_and_race_with_skip(self):
        self.create_vitamin()
        rid = "2026-10-06T09:00:00"
        first = self.op("checkin.occurrence.done", "checkin-vitamin", {"original_recurrence_id": rid}, op_id="tap-done-1",
                        at=T0 + timedelta(hours=1))
        replay = self.op("checkin.occurrence.done", "checkin-vitamin", {"original_recurrence_id": rid}, op_id="tap-done-1",
                         at=T0 + timedelta(hours=2))
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["entity"], first["entity"])
        second_device = self.op("checkin.occurrence.done", "checkin-vitamin", {"original_recurrence_id": rid},
                                at=T0 + timedelta(hours=1, minutes=1))
        self.assertEqual((second_device["status"], second_device["code"]), ("NOOP", "ALREADY_DONE"))
        race = self.op("checkin.occurrence.skip", "checkin-vitamin", {"original_recurrence_id": rid},
                       at=T0 + timedelta(hours=1, minutes=2))
        self.assertEqual((race["status"], race["code"]), ("CONFLICT", "CHECKIN_DONE"))
        self.assertEqual(self.occurrence("checkin-vitamin", rid)["status"], "DONE")
        # Changing the recorded outcome is an explicit reopen first.
        self.assertEqual(self.op("checkin.occurrence.reopen", "checkin-vitamin", {"original_recurrence_id": rid},
                                 at=T0 + timedelta(hours=1, minutes=3))["status"], "APPLIED")
        skipped = self.op("checkin.occurrence.skip", "checkin-vitamin", {"original_recurrence_id": rid, "note": "забыл"},
                          at=T0 + timedelta(hours=1, minutes=4))
        self.assertEqual(skipped["entity"]["status"], "SKIPPED")
        self.assertEqual(skipped["entity"]["note"], "забыл")

    def test_missed_by_policy_and_late_offline_done_wins(self):
        self.create_vitamin(window_minutes=120)
        rid = "2026-10-06T09:00:00"
        self.tick(T0 + timedelta(hours=1))
        self.tick(T0 + timedelta(hours=2, minutes=59))
        self.assertEqual(self.occurrence("checkin-vitamin", rid)["status"], "PENDING")
        self.tick(T0 + timedelta(hours=3))
        missed = self.occurrence("checkin-vitamin", rid)
        self.assertEqual((missed["status"], missed["resolved_by"]), ("MISSED", "POLICY"))
        reminder = self.sql("SELECT status FROM reminders WHERE id=?", (missed["reminder_id"],))[0]
        self.assertEqual(reminder["status"], "CANCELLED")
        # The phone was offline: «Принял» pressed at 09:10 reaches the server at 15:00.
        late = self.op("checkin.occurrence.done", "checkin-vitamin",
                       {"original_recurrence_id": rid, "occurred_at": "2026-10-06T06:10:00Z"},
                       at=T0 + timedelta(hours=7))
        self.assertEqual((late["status"], late["entity"]["status"], late["entity"]["resolved_by"]),
                         ("APPLIED", "DONE", "USER"))
        self.assertEqual(late["entity"]["occurred_at"], "2026-10-06T06:10:00+00:00")

    def test_default_window_ends_at_next_occurrence_or_end_of_day(self):
        self.op("checkin.create", "checkin-twice", {
            "kind": "MEDICATION", "title": "Сертралин", "dtstart_local": "2026-10-06T09:00",
            "recurrence_rule": "FREQ=DAILY", "timezone_name": MSK})
        self.op("checkin.split", "checkin-evening", {
            "template_id": "checkin-twice", "original_recurrence_id": "2026-10-07T09:00:00",
            "start_local": "2026-10-07T21:00"}, at=T0)
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            store = SQLiteCheckInRepository(repo)
            template = store.get_template("a", "checkin-twice")
            occurrence = store.require_occurrence("a", "checkin-twice", "2026-10-06T09:00:00")
            # Until local midnight (the next occurrence of this series is after the split).
            self.assertEqual(store.window_end(template, occurrence), datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc))

    def test_quota_progress_adds_up_and_partial_day_is_missed_with_quantity(self):
        created = self.op("checkin.create", "checkin-matan", {
            "kind": "QUOTA", "title": "Задачи по матану", "target_quantity": 20, "unit": "задач",
            "dtstart_local": "2026-10-06T10:00", "recurrence_rule": "FREQ=DAILY", "timezone_name": MSK,
            "remind": False})
        self.assertEqual(created["status"], "APPLIED")
        rid = "2026-10-06T10:00:00"
        a = self.op("checkin.occurrence.progress", "checkin-matan", {"original_recurrence_id": rid, "count": 7},
                    at=T0 + timedelta(hours=3))
        b = self.op("checkin.occurrence.progress", "checkin-matan", {"original_recurrence_id": rid, "count": 5},
                    at=T0 + timedelta(hours=3))
        self.assertEqual(a["entity"]["quantity_done"], 7)
        self.assertEqual((b["entity"]["quantity_done"], b["entity"]["remaining_quantity"]), (12, 8))
        # No pace given: the time it needs is unknown, not invented.
        self.assertIsNone(b["entity"]["remaining_effort_minutes"])
        self.tick(datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc))
        day = self.occurrence("checkin-matan", rid)
        self.assertEqual((day["status"], day["quantity_done"], day["target_quantity"]), ("MISSED", 12, 20))
        # Tomorrow with a user-given pace of 3 min per problem; reaching the target closes the day.
        self.op("checkin.update", "checkin-matan", {"unit_effort_seconds": 180})
        tomorrow = "2026-10-07T10:00:00"
        progress = self.op("checkin.occurrence.progress", "checkin-matan", {"original_recurrence_id": tomorrow, "count": 10},
                           at=datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc))
        self.assertEqual(progress["entity"]["remaining_effort_minutes"], 30)
        done = self.op("checkin.occurrence.progress", "checkin-matan", {"original_recurrence_id": tomorrow, "count": 10},
                       at=datetime(2026, 10, 7, 9, 0, tzinfo=timezone.utc))
        self.assertEqual((done["entity"]["status"], done["entity"]["quantity_done"]), ("DONE", 20))
        # A quota is never a Task.
        self.assertEqual(self.sql("SELECT count(*) AS n FROM obligations")[0]["n"], 0)

    def test_quota_rejects_boolean_fields_and_medication_fields_on_routines(self):
        bad = self.op("checkin.create", "checkin-badquota", {
            "kind": "QUOTA", "title": "Страницы", "dtstart_local": "2026-10-06T10:00",
            "recurrence_rule": "FREQ=DAILY", "timezone_name": MSK})
        self.assertEqual((bad["status"], bad["code"]), ("REJECTED", "VALIDATION_ERROR"))
        bad = self.op("checkin.create", "checkin-badroutine", {
            "kind": "ROUTINE", "title": "Мусор", "dose_text": "1 пакет", "dtstart_local": "2026-10-06T22:30",
            "recurrence_rule": "FREQ=DAILY", "timezone_name": MSK})
        self.assertEqual(bad["status"], "REJECTED")


class CheckInRecurrenceTests(Harness):
    def test_weekday_rule_and_move_one_occurrence_keeps_identity(self):
        self.op("checkin.create", "checkin-pass", {
            "kind": "ROUTINE", "title": "Взять пропуск", "dtstart_local": "2026-10-06T08:30",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "timezone_name": MSK})
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            store = SQLiteCheckInRepository(repo)
            # Saturday is not an occurrence; Monday is.
            with self.assertRaisesRegex(Exception, "not part of"):
                store.require_occurrence("a", "checkin-pass", "2026-10-10T08:30:00")
            store.require_occurrence("a", "checkin-pass", "2026-10-12T08:30:00")
            repo.connection.commit()
        moved = self.op("checkin.occurrence.move", "checkin-pass",
                        {"original_recurrence_id": "2026-10-07T08:30:00", "target_local": "2026-10-07T07:45"})
        self.assertEqual(moved["entity"]["original_recurrence_id"], "2026-10-07T08:30:00")
        self.assertEqual(moved["entity"]["scheduled_at"], "2026-10-07T04:45:00+00:00")
        reminder = self.sql("SELECT remind_at FROM reminders WHERE id=?", (moved["entity"]["reminder_id"],))[0]
        self.assertEqual(reminder["remind_at"], "2026-10-07T04:45:00+00:00")

    def test_dst_gap_and_fold_resolve_without_changing_identity(self):
        start = datetime(2026, 3, 27, 12, 0, tzinfo=timezone.utc)
        self.op("checkin.create", "checkin-berlin", {
            "kind": "ROUTINE", "title": "Nachts", "dtstart_local": "2026-03-28T02:30",
            "recurrence_rule": "FREQ=DAILY", "timezone_name": "Europe/Berlin", "remind": False}, at=start)
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(start)) as repo:
            repo.initialize()
            store = SQLiteCheckInRepository(repo)
            template = store.get_template("a", "checkin-berlin")
            gap = store.require_occurrence("a", "checkin-berlin", "2026-03-29T02:30:00")
            self.assertEqual(gap.original_recurrence_id, "2026-03-29T02:30:00")
            # 02:30 does not exist on 29 March: the first valid minute (03:00 CEST = 01:00Z).
            self.assertEqual(store.scheduled_at(template, gap), datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc))
            fold = store.require_occurrence("a", "checkin-berlin", "2026-10-25T02:30:00")
            # 02:30 happens twice on 25 October: the earlier one (CEST, 00:30Z).
            self.assertEqual(store.scheduled_at(template, fold), datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc))
            repo.connection.commit()

    def test_this_and_future_split_keeps_recorded_history(self):
        self.create_vitamin()
        self.op("checkin.occurrence.done", "checkin-vitamin", {"original_recurrence_id": "2026-10-06T09:00:00"},
                at=T0 + timedelta(hours=1))
        refused = self.op("checkin.split", "checkin-vitamin-v2", {
            "template_id": "checkin-vitamin", "original_recurrence_id": "2026-10-06T09:00:00",
            "start_local": "2026-10-06T08:00"}, at=T0 + timedelta(hours=2))
        self.assertEqual(refused["status"], "CONFLICT")
        # Tomorrow on: 08:00 instead of 09:00; tomorrow's untouched 09:00 day disappears.
        self.assertIsNotNone(self.occurrence("checkin-vitamin", "2026-10-07T09:00:00"))
        split = self.op("checkin.split", "checkin-vitamin-v2", {
            "template_id": "checkin-vitamin", "original_recurrence_id": "2026-10-07T09:00:00",
            "start_local": "2026-10-07T08:00", "dose_text": "4000 ME"}, at=T0 + timedelta(hours=2))
        self.assertEqual(split["status"], "APPLIED", split)
        self.assertEqual(split["entity"]["previous"]["series_end_before_local"], "2026-10-07T09:00:00")
        self.assertIsNone(self.occurrence("checkin-vitamin", "2026-10-07T09:00:00"))
        self.assertIsNotNone(self.occurrence("checkin-vitamin-v2", "2026-10-07T08:00:00"))
        self.assertEqual(self.occurrence("checkin-vitamin", "2026-10-06T09:00:00")["status"], "DONE")
        replay = self.op("checkin.split", "checkin-vitamin-v2", {
            "template_id": "checkin-vitamin", "original_recurrence_id": "2026-10-07T09:00:00"}, at=T0 + timedelta(hours=3))
        self.assertEqual(replay["status"], "NOOP")

    def test_end_and_delete_with_late_replay(self):
        self.create_vitamin()
        ended = self.op("checkin.end", "checkin-vitamin", {}, at=T0 + timedelta(hours=2))
        self.assertEqual((ended["status"], ended["entity"]["status"]), ("APPLIED", "ENDED"))
        self.assertIsNone(self.occurrence("checkin-vitamin", "2026-10-07T09:00:00"))
        deleted = self.op("checkin.delete", "checkin-vitamin", {}, at=T0 + timedelta(hours=3))
        self.assertEqual(deleted["status"], "APPLIED")
        self.assertEqual(self.sql("SELECT count(*) AS n FROM checkin_occurrences")[0]["n"], 0)
        late = self.op("checkin.occurrence.done", "checkin-vitamin", {"original_recurrence_id": "2026-10-06T09:00:00"},
                       at=T0 + timedelta(hours=4))
        self.assertEqual((late["status"], late["code"]), ("NOOP", "DELETED"))
        recreate = self.op("checkin.create", "checkin-vitamin", {
            "kind": "ROUTINE", "title": "x", "dtstart_local": "2026-10-08T09:00", "recurrence_rule": "FREQ=DAILY",
            "timezone_name": MSK}, at=T0 + timedelta(hours=4))
        self.assertEqual((recreate["status"], recreate["code"]), ("NOOP", "DELETED"))

    def test_followup_prompt_is_a_new_prompt_issued_once(self):
        self.create_vitamin(followup_minutes=30)
        nine = T0 + timedelta(hours=1)
        self.assertEqual(len(self.tick(nine).messages), 1)
        self.assertEqual(self.tick(nine + timedelta(minutes=29)).messages, [])
        # The tick that re-arms the prompt also sends it: a new prompt, not a retry.
        follow = self.tick(nine + timedelta(minutes=30))
        self.assertEqual(len(follow.messages), 1)
        body = self.sql("SELECT body FROM reminder_messages WHERE id=?", (follow.messages[0],))[0]["body"]
        self.assertIn("Ещё не отмечено", body)
        self.tick(nine + timedelta(hours=1, minutes=10))
        self.assertEqual(self.tick(nine + timedelta(hours=2)).messages, [])
        self.assertEqual(self.occurrence("checkin-vitamin", "2026-10-06T09:00:00")["followups_sent"], 1)

    def test_account_isolation(self):
        self.create_vitamin()
        foreign = self.op("checkin.occurrence.done", "checkin-vitamin",
                          {"original_recurrence_id": "2026-10-06T09:00:00"}, account="b")
        self.assertEqual((foreign["status"], foreign["code"]), ("REJECTED", "NOT_FOUND"))
        stolen_id = self.op("checkin.create", "checkin-vitamin", {
            "kind": "ROUTINE", "title": "x", "dtstart_local": "2026-10-08T09:00", "recurrence_rule": "FREQ=DAILY",
            "timezone_name": MSK}, account="b")
        self.assertEqual(stolen_id["status"], "REJECTED")
        rid = self.occurrence("checkin-vitamin", "2026-10-06T09:00:00")["reminder_id"]
        foreign_snooze = self.op("reminder.snooze", rid, {"minutes": 10}, account="b")
        self.assertEqual(foreign_snooze["status"], "REJECTED")
        self.assertEqual(self.op("reminder_series.split", "rs-b-successor", {
            "series_id": "none", "original_recurrence_id": "2026-10-06T09:00:00"}, account="b")["status"], "REJECTED")

    def test_horizon_is_idempotent_across_restarts(self):
        self.create_vitamin()
        for minutes in (0, 1, 1, 61):
            self.tick(T0 + timedelta(minutes=minutes))
        rows = self.sql("SELECT original_recurrence_id FROM checkin_occurrences ORDER BY 1")
        # 48 h ahead of the last tick (06:01Z on the 8th) reaches the 8th's 09:00 (06:00Z), once each.
        self.assertEqual([row["original_recurrence_id"] for row in rows],
                         ["2026-10-06T09:00:00", "2026-10-07T09:00:00", "2026-10-08T09:00:00"])
        self.assertEqual(len(self.sql("SELECT * FROM reminder_messages WHERE stage='REMINDER'")), 1)


class ReminderSeriesTests(Harness):
    def create_trash(self, **extra):
        result = self.op("reminder_series.create", "series-trash", {
            "title": "Вынести мусор", "dtstart_local": "2026-10-06T22:30", "recurrence_rule": "FREQ=DAILY",
            "timezone_name": MSK, **extra})
        self.assertEqual(result["status"], "APPLIED", result)
        return result

    def test_scenario_daily_trash_is_a_recurring_reminder_not_a_task(self):
        self.create_trash()
        self.assertEqual(self.sql("SELECT count(*) AS n FROM obligations")[0]["n"], 0)
        reminders = self.sql("SELECT r.id,r.remind_at,o.original_recurrence_id FROM reminders r "
                             "JOIN reminder_series_occurrences o ON o.reminder_id=r.id ORDER BY r.remind_at")
        self.assertEqual([row["remind_at"] for row in reminders],
                         ["2026-10-06T19:30:00+00:00", "2026-10-07T19:30:00+00:00"])
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            listed = SQLiteReminderRepository(repo).list("a")
        self.assertEqual(listed[0]["series"], {"series_id": "series-trash",
                                               "original_recurrence_id": "2026-10-06T22:30:00"})
        at = datetime(2026, 10, 6, 19, 30, tzinfo=timezone.utc)
        self.assertEqual(len(self.tick(at).messages), 1)
        # Server restart / second worker: the same occurrence is not announced twice.
        self.assertEqual(self.tick(at).messages, [])
        self.assertEqual(self.tick(at + timedelta(minutes=1)).messages, [])
        self.assertEqual(len(self.sql("SELECT * FROM reminder_messages WHERE stage='REMINDER'")), 1)
        done = self.op("reminder.done", reminders[0]["id"], {}, at=at + timedelta(minutes=5))
        self.assertEqual(done["entity"]["status"], "DONE")
        # Tomorrow is untouched by today's «Готово».
        self.assertEqual(self.sql("SELECT status FROM reminders WHERE id=?", (reminders[1]["id"],))[0]["status"],
                         "SCHEDULED")

    def test_weekdays_skip_future_occurrence_and_series_edit(self):
        self.op("reminder_series.create", "series-pass", {
            "title": "Взять пропуск", "dtstart_local": "2026-10-07T08:00",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "timezone_name": MSK})
        # Next Monday is not materialized yet; skipping it materializes and cancels it.
        skipped = self.op("reminder_series.occurrence.skip", "series-pass", {"original_recurrence_id": "2026-10-12T08:00:00"})
        self.assertEqual((skipped["status"], skipped["entity"]["status"]), ("APPLIED", "CANCELLED"))
        self.assertEqual(skipped["entity"]["series"]["original_recurrence_id"], "2026-10-12T08:00:00")
        bad = self.op("reminder_series.occurrence.skip", "series-pass", {"original_recurrence_id": "2026-10-10T08:00:00"})
        self.assertEqual(bad["status"], "REJECTED")
        moved = self.op("reminder_series.occurrence.move", "series-pass", {
            "original_recurrence_id": "2026-10-08T08:00:00", "remind_at": "2026-10-08T07:30:00+03:00"})
        self.assertEqual(moved["entity"]["remind_at"], "2026-10-08T04:30:00+00:00")
        edited = self.op("reminder_series.update", "series-pass", {"title": "Пропуск и ключи"})
        self.assertEqual(edited["entity"]["title"], "Пропуск и ключи")
        titles = {row["title"] for row in self.sql("SELECT title FROM reminders WHERE status='SCHEDULED'")}
        self.assertIn("Пропуск и ключи", titles)

    def test_this_and_future_split_end_and_delete(self):
        self.create_trash()
        split = self.op("reminder_series.split", "series-trash-21", {
            "series_id": "series-trash", "original_recurrence_id": "2026-10-07T22:30:00", "start_local": "2026-10-07T21:00"})
        self.assertEqual(split["status"], "APPLIED", split)
        times = [row["remind_at"] for row in self.sql("SELECT remind_at FROM reminders ORDER BY remind_at")]
        self.assertEqual(times, ["2026-10-06T19:30:00+00:00", "2026-10-07T18:00:00+00:00"])
        ended = self.op("reminder_series.end", "series-trash-21", {}, at=datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(ended["entity"]["status"], "ENDED")
        deleted = self.op("reminder_series.delete", "series-trash", {})
        self.assertEqual(deleted["status"], "APPLIED")
        late = self.op("reminder_series.update", "series-trash", {"title": "x"})
        self.assertEqual((late["status"], late["code"]), ("NOOP", "DELETED"))

    def test_deleted_occurrence_is_never_rematerialized(self):
        self.create_trash()
        first = self.sql("SELECT reminder_id FROM reminder_series_occurrences ORDER BY original_recurrence_id")[0]["reminder_id"]
        self.op("reminder.delete", first, {})
        self.tick(T0 + timedelta(minutes=5))
        ids = [row["reminder_id"] for row in self.sql("SELECT reminder_id FROM reminder_series_occurrences")]
        self.assertNotIn(first, ids)
        self.assertEqual(self.sql("SELECT count(*) AS n FROM reminders WHERE id=?", (first,))[0]["n"], 0)


class LifecycleAndMigrationTests(Harness):
    def test_export_includes_and_deletion_removes_v31_data(self):
        self.create_vitamin()
        self.op("reminder_series.create", "series-trash", {
            "title": "Вынести мусор", "dtstart_local": "2026-10-06T22:30", "recurrence_rule": "FREQ=DAILY",
            "timezone_name": MSK})
        lifecycle = SQLiteDataLifecycle(self.db)
        export = lifecycle.export_account("a").tables
        self.assertEqual(len(export["checkin_templates"]), 1)
        self.assertEqual(len(export["checkin_occurrences"]), 2)
        self.assertEqual(len(export["reminder_series"]), 1)
        self.assertEqual(len(export["reminder_series_occurrences"]), 2)
        revision = self.sql("SELECT server_revision FROM accounts WHERE id='a'")[0]["server_revision"]
        lifecycle.delete_account("a", expected_server_revision=revision, confirm_account_id="a")
        for table in ("checkin_templates", "checkin_occurrences", "reminder_series", "reminder_series_occurrences",
                      "deleted_entities", "reminders"):
            self.assertEqual(self.sql(f"SELECT count(*) AS n FROM {table} WHERE account_id='a'")[0]["n"], 0, table)

    def test_backup_restore_keeps_check_in_history(self):
        self.create_vitamin()
        self.op("checkin.occurrence.done", "checkin-vitamin", {"original_recurrence_id": "2026-10-06T09:00:00"},
                at=T0 + timedelta(hours=1))
        backup = Path(self.tmp.name) / "backup.sqlite"
        SQLiteDataLifecycle(self.db).create_backup(backup)
        restored = Path(self.tmp.name) / "restored.sqlite"
        SQLiteDataLifecycle.restore_backup(backup, restored)
        with sqlite3.connect(restored) as conn:
            status = conn.execute("SELECT status FROM checkin_occurrences WHERE original_recurrence_id=?",
                                  ("2026-10-06T09:00:00",)).fetchone()[0]
        self.assertEqual(status, "DONE")

    def test_real_v22_fixture_upgrades_to_v31_and_rolls_back(self):
        fixture = Path("tests/fixtures/upgrade/v22_populated_by_r2.sqlite.gz")
        database = Path(self.tmp.name) / "upgrade.sqlite"
        with gzip.open(fixture, "rb") as source, database.open("wb") as target:
            shutil.copyfileobj(source, target)
        with SQLiteCanonicalRepository(database, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            self.assertEqual(repo.schema_version(), SCHEMA_VERSION)
            account = repo.connection.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()[0]
            result = SyncService(repo, account_id=account, principal_id="u", now=T0).apply({
                "op_id": "op-upgrade-1", "type": "checkin.create", "entity_id": "checkin-upgrade",
                "payload": {"kind": "ROUTINE", "title": "Полить цветы", "dtstart_local": "2026-10-06T19:00",
                            "recurrence_rule": "FREQ=DAILY", "timezone_name": MSK}})
            self.assertEqual(result["status"], "APPLIED")
            existing_reminders = repo.connection.execute("SELECT count(*) FROM reminders").fetchone()[0]
            repo.connection.commit()
        with sqlite3.connect(database) as conn:
            roll_back_newer_than(conn, 30)
            self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 30)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='checkin_templates'").fetchone())
            # Prompts already materialized survive as valid v30 one-shot reminders.
            self.assertEqual(conn.execute("SELECT count(*) FROM reminders").fetchone()[0], existing_reminders)
        with SQLiteCanonicalRepository(database, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            self.assertEqual(repo.schema_version(), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
