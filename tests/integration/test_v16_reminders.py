"""Schema v16 reminders over the real HTTP API with a frozen clock: standalone
reminders and wake alarms, the CRITICAL escalation ladder, and text commands on
existing items (preview → confirmation → the same sync command handlers)."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.reminders import ReminderEngine
from student_execution_os.reminders.push import PushDispatcher, SendResult
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

# Wednesday 2026-09-23 12:00 in Moscow.
START = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Phone:
    name, configured = "fake-fcm", True

    def __init__(self):
        self.sent: list[tuple[dict, bool]] = []

    def send(self, token, message, *, data_only=False):
        self.sent.append((dict(message, _token=token), data_only))
        return SendResult(True, f"fcm-{len(self.sent)}")


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "rem.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(START)) as repo:
            repo.initialize()
            repo.create_account("a")
        self.clock = Clock(START)
        self.cm = TestClient(create_app(self.db, account_id="a", principal_id="user", now=self.clock))
        self.client = self.cm.__enter__()
        self.ok(self.client.patch("/api/v1/notification-preferences", json={"timezone": "Europe/Moscow", "locale": "ru"}))
        self.phone = Phone()
        self.n = 0

    def tearDown(self):
        self.cm.__exit__(None, None, None)
        self.tmp.cleanup()

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def sync(self, kind, entity, payload=None):
        self.n += 1
        body = {"operations": [{"op_id": f"op-{self.n:04d}-rem", "type": kind, "entity_id": entity, "payload": payload or {}}]}
        return self.ok(self.client.post("/api/v1/sync", json=body))["results"][0]

    def tick(self, at):
        self.clock.now = at
        ReminderEngine(self.db).tick("a", at)
        PushDispatcher(self.db, self.phone).run_once(at, "w")

    def device(self, token, caps):
        self.ok(self.client.post("/api/v1/mobile/devices", json={"token": token, "capabilities": caps}), 201)

    # ---- standalone reminders ---------------------------------------------------------

    def test_reminder_fires_once_at_its_moment_even_in_quiet_hours_and_snooze_brings_it_back(self):
        self.device("phone", ["reminder-actions-v1"])
        at = "2026-09-23T20:30:00+00:00"  # 23:30 in Moscow: inside the default quiet hours
        created = self.sync("reminder.create", "reminder-bread-1", {"title": "Купить хлеб", "remind_at": at})
        self.assertEqual((created["status"], created["entity"]["status"], created["entity"]["delivery"]), ("APPLIED", "SCHEDULED", "PUSH"))
        self.tick(datetime(2026, 9, 23, 20, 29, tzinfo=timezone.utc))
        self.assertEqual(self.phone.sent, [])
        self.tick(datetime(2026, 9, 23, 20, 30, tzinfo=timezone.utc))
        self.assertEqual(len(self.phone.sent), 1)
        payload, data_only = self.phone.sent[0]
        self.assertTrue(data_only)
        self.assertEqual((payload["title"], payload["reminder_id"], payload["task_ids"]), ("Купить хлеб", "reminder-bread-1", []))
        self.assertEqual([a["id"] for a in payload["actions"]], ["DONE", "SNOOZE_10", "SNOOZE_60"])
        self.assertNotIn("alarm", payload)
        self.tick(datetime(2026, 9, 23, 20, 45, tzinfo=timezone.utc))
        self.assertEqual(len(self.phone.sent), 1, "fires once")
        listed = self.ok(self.client.get("/api/v1/reminders"))
        self.assertEqual(listed[0]["status"], "FIRED")
        # «Через 10 мин» on the notification: a new moment, it fires again then.
        message_id = self.ok(self.client.get("/api/v1/notifications"))[0]["id"]
        snoozed = self.sync("reminder.snooze", "reminder-bread-1", {"until": "2026-09-23T20:55:00+00:00",
                                                                     "reminder_message_id": message_id})
        self.assertEqual(snoozed["entity"]["status"], "SCHEDULED")
        self.assertEqual(self.ok(self.client.get("/api/v1/notifications"))[0]["acted_action"], "SNOOZE")
        self.tick(datetime(2026, 9, 23, 20, 55, tzinfo=timezone.utc))
        self.assertEqual(len(self.phone.sent), 2)
        done = self.sync("reminder.done", "reminder-bread-1")
        self.assertEqual(done["entity"]["status"], "DONE")
        self.assertEqual(self.sync("reminder.done", "reminder-bread-1")["code"], "ALREADY_DONE")

    def test_rescheduling_or_cancelling_before_delivery_drops_the_stale_message(self):
        self.device("phone", ["reminder-actions-v1"])
        self.sync("reminder.create", "reminder-late-01", {"title": "Позвонить", "remind_at": "2026-09-23T10:00:00+00:00"})
        self.clock.now = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
        ReminderEngine(self.db).tick("a", self.clock.now)  # decided, not yet delivered
        self.sync("reminder.cancel", "reminder-late-01")
        PushDispatcher(self.db, self.phone).run_once(self.clock.now, "w")
        self.assertEqual(self.phone.sent, [])
        self.assertEqual(self.sync("reminder.update", "reminder-late-01", {"title": "x"})["status"], "CONFLICT")
        reopened = self.sync("reminder.reopen", "reminder-late-01")
        self.assertEqual(reopened["entity"]["status"], "SCHEDULED")
        self.assertEqual(reopened["entity"]["remind_at"], "2026-09-23T10:10:00+00:00")

    def test_wake_alarm_rings_on_alarm_phones_and_arrives_as_a_notification_elsewhere(self):
        self.device("alarm-phone", ["reminder-actions-v1", "wake-alarm-v1"])
        self.device("old-phone", ["reminder-actions-v1"])
        created = self.sync("reminder.create", "reminder-wake-01", {
            "title": "Подъём", "remind_at": "2026-09-24T04:00:00+00:00", "delivery": "ALARM",
            "wake_check": True, "raise_volume": True})
        self.assertTrue(created["entity"]["wake_check"])
        # Every change of an alarm reminder asks the alarm phones (only) to reschedule.
        PushDispatcher(self.db, self.phone).run_once(self.clock.now, "w")
        signals = [(p["_token"], p["type"]) for p, _ in self.phone.sent]
        self.assertEqual(signals, [("alarm-phone", "alarm-sync")])
        alarms = self.ok(self.client.get("/api/v1/reminders/alarms"))["alarms"]
        self.assertEqual([a["id"] for a in alarms], ["reminder-wake-01"])
        self.assertEqual(self.ok(self.client.get("/api/v1/notifications")), [], "signals are not in the inbox")
        self.phone.sent.clear()
        self.tick(datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc))
        by_token = {p["_token"]: p for p, _ in self.phone.sent}
        self.assertEqual(by_token["alarm-phone"]["alarm"]["id"], "reminder-wake-01")
        self.assertTrue(by_token["alarm-phone"]["alarm"]["raise_volume"])
        self.assertEqual(by_token["alarm-phone"]["delivery"], "ALARM")
        self.assertNotIn("alarm", by_token["old-phone"])
        self.assertEqual(by_token["old-phone"]["delivery"], "PUSH")
        # «Я встал» keeps it open until the awake check is answered.
        up = self.sync("reminder.ack", "reminder-wake-01", {"stage": "UP"})
        self.assertEqual((up["entity"]["status"], bool(up["entity"]["acknowledged_at"])), ("FIRED", True))
        awake = self.sync("reminder.ack", "reminder-wake-01", {"stage": "AWAKE"})
        self.assertEqual(awake["entity"]["status"], "DONE")
        self.assertIsNotNone(awake["entity"]["awake_confirmed_at"])

    def test_validation_and_offline_replays(self):
        bad = self.sync("reminder.create", "reminder-bad-001", {"title": "x", "remind_at": "2026-09-24T04:00:00+00:00",
                                                                "delivery": "PUSH", "wake_check": True})
        self.assertEqual(bad["status"], "REJECTED")
        self.assertEqual(self.sync("reminder.create", "reminder-bad-002", {"title": "x"})["status"], "REJECTED")
        first = self.sync("reminder.create", "reminder-ok-0001", {"title": "x", "remind_at": "2026-09-24T04:00:00+00:00"})
        again = self.sync("reminder.create", "reminder-ok-0001", {"title": "x", "remind_at": "2026-09-24T04:00:00+00:00"})
        self.assertEqual((first["status"], again["code"]), ("APPLIED", "ALREADY_EXISTS"))
        self.assertEqual(self.sync("reminder.delete", "reminder-ok-0001")["status"], "APPLIED")
        late = self.sync("reminder.snooze", "reminder-ok-0001", {"minutes": 10})
        self.assertEqual((late["status"], late["code"]), ("NOOP", "DELETED"))
        recreated = self.sync("reminder.create", "reminder-ok-0001", {"title": "x", "remind_at": "2026-09-24T04:00:00+00:00"})
        self.assertEqual(recreated["code"], "DELETED", "a deleted reminder cannot come back from a late replay")
        export = self.ok(self.client.get("/api/v1/account/export"))
        self.assertIn("reminders", json.dumps(export))

    # ---- CRITICAL escalation --------------------------------------------------------

    def critical(self, cutoff, **extra):
        return self.sync("task.create", "task-critical-01", {
            "title": "Заявление в деканат", "importance": "CRITICAL", "estimated_total_effort_minutes": 30,
            "actual_cutoff": {"state": "KNOWN", "at": cutoff.isoformat()}, **extra})

    def stages(self):
        return [(m["stage"], m["created_at"]) for m in reversed(self.ok(self.client.get("/api/v1/notifications")))]

    def test_critical_ladder_fires_each_rung_once_and_only_the_latest_crossed(self):
        self.device("phone", ["reminder-actions-v1"])
        prefs = self.ok(self.client.get("/api/v1/notification-preferences"))
        self.ok(self.client.patch("/api/v1/notification-preferences", json={
            "expected_version": prefs["version"], "quiet_hours": {"starts_local": "03:00", "ends_local": "03:01"}}))
        due = START + timedelta(hours=60)
        self.critical(due)
        rung = {}
        # Walk the clock in 5-minute steps up to the deadline.
        at = START + timedelta(minutes=15)
        while at < due:
            before = len(self.stages())
            self.tick(at)
            for stage, _ in self.stages()[before:]:
                rung.setdefault(stage, []).append(round((due - at).total_seconds() / 3600, 2))
            at += timedelta(minutes=5)
        escalations = self.ok(self.client.get("/api/v1/notifications"))
        ladder = [m for m in escalations if m["stage"] == "ESCALATION"]
        self.assertEqual(len(ladder), 7, rung)
        self.assertEqual(rung["ESCALATION"], [48.0, 24.0, 12.0, 6.0, 3.0, 1.0, 0.25], rung)
        self.assertNotIn("DEADLINE_24H", rung)
        self.assertNotIn("DEADLINE_2H", rung)
        self.assertIn("До срока «Заявление в деканат»", ladder[0]["title"])

    def test_snooze_and_reschedule_recompute_the_ladder(self):
        due = START + timedelta(hours=10)
        self.critical(due)
        self.tick(START + timedelta(minutes=15))  # 9h45m left: only the 12h rung, once
        ladder = [m for m in self.ok(self.client.get("/api/v1/notifications")) if m["stage"] == "ESCALATION"]
        self.assertEqual(len(ladder), 1)
        # Snoozed over the 6h and 3h rungs: when it ends only the latest crossed one is sent.
        self.sync("reminder.snooze", "task-critical-01", {"until": (due - timedelta(hours=2)).isoformat()})
        for minutes in range(20, 8 * 60 + 1, 20):
            self.tick(START + timedelta(minutes=minutes))
        ladder = [m for m in self.ok(self.client.get("/api/v1/notifications")) if m["stage"] == "ESCALATION"]
        self.assertEqual(len(ladder), 2, [m["title"] for m in ladder])
        # A new deadline is a new ladder.
        self.sync("task.update", "task-critical-01", {"actual_cutoff": {"state": "KNOWN", "at": (due + timedelta(days=3)).isoformat()}})
        self.tick(START + timedelta(hours=8, minutes=40))
        ladder = [m for m in self.ok(self.client.get("/api/v1/notifications")) if m["stage"] == "ESCALATION"]
        self.assertEqual(len(ladder), 2, "70 hours left: nothing crossed yet")
        self.sync("task.complete", "task-critical-01")
        for hours in (40, 60, 72):
            self.tick(START + timedelta(hours=hours))
        ladder = [m for m in self.ok(self.client.get("/api/v1/notifications")) if m["stage"] == "ESCALATION"]
        self.assertEqual(len(ladder), 2, "a finished task gets nothing more")

    def test_normal_tasks_keep_the_generic_deadline_prompts(self):
        self.sync("task.create", "task-normal-001", {"title": "Обычная", "estimated_total_effort_minutes": 30,
                                                      "actual_cutoff": {"state": "KNOWN", "at": (START + timedelta(hours=20)).isoformat()}})
        self.tick(START + timedelta(minutes=15))
        stages = {m["stage"] for m in self.ok(self.client.get("/api/v1/notifications"))}
        self.assertNotIn("ESCALATION", stages)
        self.assertIn("DEADLINE_24H", stages)

    # ---- text commands -------------------------------------------------------------

    def interpret(self, text):
        return self.ok(self.client.post("/api/v1/assistant/interpret", json={"text": text, "context": {"timezone": "Europe/Moscow"}}))

    def apply(self, preview, *, confirm=True, edits=None):
        action = preview["actions"][0]
        body = {"batch_id": preview["batch_id"], "action_ids": [action["id"]],
                "confirmed_action_ids": [action["id"]] if confirm else [], "idempotency_key": f"k-{preview['batch_id']}"}
        if edits:
            body["edits"] = {action["id"]: edits}
        return self.client.post("/api/v1/assistant/apply", json=body)

    def test_text_commands_preview_confirm_and_run_as_sync_operations(self):
        self.sync("task.create", "task-essay-0001", {"title": "Эссе по английскому", "estimated_total_effort_minutes": 90,
                                                      "actual_cutoff": {"state": "KNOWN", "at": "2026-09-25T15:00:00+00:00"}})
        self.sync("event.create", "event-call-0001", {"title": "Созвон с научруком", "starts_at": "2026-09-23T15:00:00+00:00",
                                                       "ends_at": "2026-09-23T16:00:00+00:00", "remind_before_minutes": 15})
        preview = self.interpret("напомни купить хлеб завтра в 18")
        self.assertEqual(preview["actions"][0]["command"], "CREATE_REMINDER")
        result = self.ok(self.apply(preview, confirm=False))["results"][0]
        self.assertEqual((result["operation"], result["entity"]["remind_at"]), ("reminder.create", "2026-09-24T15:00:00+00:00"))

        preview = self.interpret("перенеси эссе на понедельник")
        action = preview["actions"][0]
        self.assertEqual((action["command"], action["payload"]["obligation_id"], action["requires_confirmation"]),
                         ("RESCHEDULE", "task-essay-0001", False))
        moved = self.ok(self.apply(preview, confirm=False))["results"][0]
        self.assertEqual(moved["entity"]["actual_cutoff"]["at"], "2026-09-28T15:00:00+00:00", "same time of day, new day")

        preview = self.interpret("перенеси созвон на 19:00")
        moved = self.ok(self.apply(preview, confirm=False))["results"][0]
        self.assertEqual((moved["entity"]["starts_at"], moved["entity"]["ends_at"]),
                         ("2026-09-23T16:00:00+00:00", "2026-09-23T17:00:00+00:00"))

        preview = self.interpret("поработал над эссе 30 минут")
        progressed = self.ok(self.apply(preview, confirm=False))["results"][0]
        self.assertEqual(progressed["entity"]["remaining_effort_minutes"], 60)

        preview = self.interpret("напомни про эссе через час")
        snoozed = self.ok(self.apply(preview, confirm=False))["results"][0]
        self.assertEqual(snoozed["entity"]["remind_at"], "2026-09-23T10:00:00+00:00")

        # Closing something needs the user's explicit confirmation.
        preview = self.interpret("готово эссе")
        self.assertTrue(preview["actions"][0]["requires_confirmation"])
        self.assertEqual(self.apply(preview, confirm=False).status_code, 403)
        self.assertEqual(self.ok(self.apply(preview))["results"][0]["entity"]["status"], "COMPLETED")
        preview = self.interpret("эссе в архив")
        self.assertEqual(self.ok(self.apply(preview))["results"][0]["entity"]["status"], "ARCHIVED")

    def test_an_unclear_target_is_picked_by_the_user(self):
        for tid, title in (("task-hist-00001", "Эссе по истории"), ("task-law-000001", "Эссе по праву")):
            self.sync("task.create", tid, {"title": title, "estimated_total_effort_minutes": 30})
        preview = self.interpret("готово эссе")
        action = preview["actions"][0]
        self.assertIn("obligation_id", action["unresolved_fields"])
        self.assertEqual(self.apply(preview).status_code, 422)
        chosen = self.ok(self.apply(preview, edits={"obligation_id": "task-law-000001", "expected_version": 1}))
        self.assertEqual(chosen["results"][0]["entity_id"], "task-law-000001")
        self.assertEqual(self.ok(self.client.get("/api/v1/tasks/task-hist-00001"))["status"], "ACTIVE")


if __name__ == "__main__":
    unittest.main()
