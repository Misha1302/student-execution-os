"""Schema v15 behaviour over the real HTTP API: task lifecycle (won't do / archive /
delete with tombstones), fixed-time events with reminders, counted progress, sleep
hours in the plan, optional events and the 7-day agenda."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence import SCHEMA_VERSION, SQLiteCanonicalRepository
from student_execution_os.reminders import ReminderEngine
from student_execution_os.reminders.push import PushDispatcher, SendResult
from student_execution_os.web.app import create_app
from tests.asgi_client import TestClient

MOSCOW = ZoneInfo("Europe/Moscow")
# Wednesday 2026-09-23 12:00 in Moscow.
START = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class V15Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "v15.sqlite")
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(START)) as repo:
            repo.initialize()
            repo.create_account("a")
            repo.create_account("b")
        self.clock = Clock(START)
        self.client_cm = TestClient(create_app(self.db, account_id="a", principal_id="user", now=self.clock))
        self.client = self.client_cm.__enter__()
        self.ok(self.client.patch("/api/v1/notification-preferences", json={"timezone": "Europe/Moscow", "locale": "ru"}))
        profile = self.ok(self.client.get("/api/v1/settings/planning-profile"))
        self.ok(self.client.patch("/api/v1/settings/planning-profile",
                                  json={"expected_version": profile["version"], "timezone": "Europe/Moscow"}))
        self.pushed: list[dict] = []
        pushed = self.pushed

        class Phone:
            name, configured = "fake-fcm", True

            def send(self, token, message, *, data_only=False):
                pushed.append(message)
                return SendResult(True, f"fcm-{len(pushed)}")

        self.phone = Phone()

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        self.tmp.cleanup()

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def sync(self, *ops):
        body = {"operations": [{"op_id": f"op-{op_id}-v15", "type": kind, "entity_id": entity, "payload": payload or {}}
                               for op_id, kind, entity, payload in ops]}
        results = self.ok(self.client.post("/api/v1/sync", json=body))["results"]
        for result in results:
            result["op_id"] = result["op_id"].removeprefix("op-").removesuffix("-v15")
        return results

    def op(self, op_id, kind, entity, payload=None):
        result = self.sync((op_id, kind, entity, payload))[0]
        self.assertIn(result["status"], {"APPLIED", "NOOP"}, result)
        return result

    def local(self, value: str) -> str:
        return datetime.fromisoformat(value).astimezone(MOSCOW).strftime("%Y-%m-%d %H:%M")

    def at_moscow(self, day: int, hour: int, minute: int = 0) -> str:
        return datetime(2026, 9, day, hour, minute, tzinfo=MOSCOW).astimezone(timezone.utc).isoformat()

    def create_task(self, task_id, title="Задача", minutes=60, **extra):
        payload = {"title": title, "estimated_total_effort_minutes": minutes, "actual_cutoff": {"state": "ABSENT"}, **extra}
        return self.op(f"create-{task_id}", "task.create", task_id, payload)["entity"]

    # ---- lifecycle --------------------------------------------------------------------

    def test_wont_do_archive_restore_and_delete_with_tombstone(self):
        self.create_task("task-life-1")
        cancelled = self.op("c1", "task.cancel", "task-life-1")["entity"]
        self.assertEqual(cancelled["status"], "CANCELLED")
        archived = self.op("a1", "task.archive", "task-life-1")["entity"]
        self.assertEqual(archived["status"], "ARCHIVED")
        restored = self.op("u1", "task.unarchive", "task-life-1")["entity"]
        self.assertEqual(restored["status"], "CANCELLED")
        reopened = self.op("r1", "task.reopen", "task-life-1")["entity"]
        self.assertEqual(reopened["status"], "ACTIVE")
        # Archiving an open task first takes it out of the plan.
        self.assertEqual(self.op("a2", "task.archive", "task-life-1")["entity"]["status"], "ARCHIVED")
        today = self.ok(self.client.get("/api/v1/today"))
        self.assertNotIn("task-life-1", [t["id"] for t in today["tasks"]])

        deleted = self.op("d1", "task.delete", "task-life-1")
        self.assertEqual(deleted["status"], "APPLIED")
        self.assertNotIn("task-life-1", [t["id"] for t in self.ok(self.client.get("/api/v1/tasks"))])
        # Late replays from an offline device are harmless: no error, no resurrection.
        late = self.sync(("late-complete", "task.complete", "task-life-1", {}),
                         ("late-create", "task.create", "task-life-1", {"title": "Задача", "actual_cutoff": {"state": "ABSENT"}}))
        self.assertEqual([(r["status"], r.get("code")) for r in late], [("NOOP", "DELETED"), ("NOOP", "DELETED")])
        self.assertNotIn("task-life-1", [t["id"] for t in self.ok(self.client.get("/api/v1/tasks"))])
        # The same op_id replayed returns its first answer.
        self.assertEqual(self.sync(("d1", "task.delete", "task-life-1", {}))[0]["status"], "APPLIED")
        # Another account cannot delete (or even see) it.
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deleted_obligations WHERE account_id='a'").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM deleted_obligations WHERE account_id='b'").fetchone()[0], 0)

    def test_archive_is_only_for_finished_tasks_in_the_domain(self):
        self.create_task("task-life-2")
        self.op("done", "task.complete", "task-life-2")
        archived = self.op("arch", "task.archive", "task-life-2")["entity"]
        self.assertEqual((archived["status"], archived["completed_at"] is not None), ("ARCHIVED", True))
        self.assertEqual(self.op("unarch", "task.unarchive", "task-life-2")["entity"]["status"], "COMPLETED")

    # ---- fixed-time events ---------------------------------------------------------------

    def test_fixed_event_from_speech_is_21_to_22_with_no_deadline(self):
        preview = self.ok(self.client.post("/api/v1/assistant/interpret", json={
            "text": "Сегодня с 21 до 22 провести занятие по программированию",
            "context": {"timezone": "Europe/Moscow", "locale": "ru"}}))
        action = preview["actions"][0]
        self.assertEqual(action["command"], "CREATE_EVENT")
        payload = action["payload"]
        self.assertEqual(payload["title"], "Провести занятие по программированию")
        self.assertEqual((self.local(payload["starts_at"]), self.local(payload["ends_at"])),
                         ("2026-09-23 21:00", "2026-09-23 22:00"))
        self.assertNotIn("actual_cutoff", payload)
        # The device creates it through the offline queue with a reminder.
        created = self.op("ev-create", "event.create", "event-lesson", {**payload, "remind_before_minutes": 15})["entity"]
        self.assertEqual((created["duration_minutes"], created["remind_before_minutes"]), (60, 15))
        self.assertEqual(self.local(created["remind_at"]), "2026-09-23 20:45")
        # It is an event, not a task: no deadline anywhere, and the plan keeps the hour free.
        self.assertNotIn("event-lesson", [t["id"] for t in self.ok(self.client.get("/api/v1/tasks"))])
        today = self.ok(self.client.get("/api/v1/today"))
        self.assertIn("event-lesson", [e["id"] for e in today["plan"]["canonical_events"]])

    def test_event_reminder_is_sent_before_start_and_follows_a_move(self):
        self.op("ev", "event.create", "event-call", {
            "title": "Созвон с командой", "starts_at": self.at_moscow(23, 18), "ends_at": self.at_moscow(23, 19),
            "remind_before_minutes": 30})
        # Moved to 19:00 (duration kept): the reminder moves to 18:30.
        moved = self.op("ev-move", "event.update", "event-call", {"starts_at": self.at_moscow(23, 19)})["entity"]
        self.assertEqual((self.local(moved["ends_at"]), self.local(moved["remind_at"])), ("2026-09-23 20:00", "2026-09-23 18:30"))
        for minute in (datetime.fromisoformat(self.at_moscow(23, 18, 0)), datetime.fromisoformat(self.at_moscow(23, 18, 30))):
            self.clock.now = minute
            ReminderEngine(self.db).tick("a", minute)
        inbox = self.ok(self.client.get("/api/v1/notifications"))
        event_messages = [n for n in inbox if "event-call" in (n.get("task_ids") or []) or "Созвон" in n["title"]]
        self.assertEqual(len(event_messages), 1, inbox)
        self.assertIn("Созвон с командой", event_messages[0]["title"])
        self.assertIn("19:00", event_messages[0]["body"])
        # Cancelling the event stops its reminder; deleting removes it for good.
        self.op("ev-cancel", "event.cancel", "event-call")
        self.op("ev-delete", "event.delete", "event-call")
        self.assertNotIn("event-call", [e["id"] for e in self.ok(self.client.get("/api/v1/events"))])
        self.assertEqual(self.sync(("ev-late", "event.update", "event-call", {"title": "x"}))[0]["code"], "DELETED")

    def test_event_validation(self):
        bad = self.sync(("bad-ev", "event.create", "event-bad", {
            "title": "x", "starts_at": self.at_moscow(23, 20), "ends_at": self.at_moscow(23, 19)}))[0]
        self.assertEqual(bad["status"], "REJECTED")
        bad_lead = self.sync(("bad-lead", "event.create", "event-bad2", {
            "title": "x", "starts_at": self.at_moscow(23, 19), "ends_at": self.at_moscow(23, 20), "remind_before_minutes": 5000}))[0]
        self.assertEqual(bad_lead["status"], "REJECTED")

    # ---- counted progress ------------------------------------------------------------

    def test_counted_progress_lowers_remaining_time_proportionally(self):
        task = self.create_task("task-count", "Решить задачи", 100, count_total=10, count_unit="задач")
        self.assertEqual(task["count_progress"], {"total": 10, "done": 0, "unit": "задач"})
        after = self.op("p1", "task.progress", "task-count", {"count": 3})["entity"]
        self.assertEqual((after["count_progress"]["done"], after["remaining_effort_minutes"]), (3, 70))
        both = self.op("p2", "task.progress", "task-count", {"count": 2, "minutes": 10})["entity"]
        self.assertEqual(both["count_progress"]["done"], 5)
        listed = next(t for t in self.ok(self.client.get("/api/v1/tasks")) if t["id"] == "task-count")
        self.assertEqual(listed["count_progress"]["done"], 5)

    # ---- sleep hours, optional events, agenda --------------------------------------------

    def test_no_work_is_planned_during_sleep_hours(self):
        profile = self.ok(self.client.get("/api/v1/settings/planning-profile"))
        windows = {str(d): [["08:00", "22:00"]] for d in range(1, 8)}
        self.ok(self.client.patch("/api/v1/settings/planning-profile",
                                  json={"expected_version": profile["version"], "planning_windows": windows}))
        self.clock.now = datetime.fromisoformat(self.at_moscow(23, 21, 30))
        self.create_task("task-night", "Эссе", 180, actual_cutoff={"state": "KNOWN", "at": self.at_moscow(25, 18)})
        agenda = self.ok(self.client.get("/api/v1/plan/agenda?days=3"))
        self.assertEqual(agenda["plan"]["feasibility_status"], "FEASIBLE")
        work = [b for b in agenda["plan"]["blocks"] if b["type"] == "WORK"]
        self.assertTrue(work)
        for block in work:
            start = datetime.fromisoformat(block["starts_at"]).astimezone(MOSCOW)
            end = datetime.fromisoformat(block["ends_at"]).astimezone(MOSCOW)
            self.assertGreaterEqual(start.hour, 8, block)
            self.assertLessEqual((end.hour, end.minute), (22, 0), block)
        self.assertTrue(agenda["plan"]["off_hours"])
        self.assertFalse(any(c.get("id", "").startswith("off-hours:") for c in agenda["plan"]["constraints"]))

    def test_a_preferred_event_does_not_make_the_plan_unknown(self):
        self.create_task("task-opt", "Конспект", 60, actual_cutoff={"state": "KNOWN", "at": self.at_moscow(24, 18)})
        self.op("pref", "event.create", "event-pref", {
            "title": "Лекция по желанию", "starts_at": self.at_moscow(23, 15), "ends_at": self.at_moscow(23, 16, 30),
            "attendance_policy": "PREFERRED"})
        today = self.ok(self.client.get("/api/v1/today"))
        self.assertEqual(today["plan"]["feasibility_status"], "FEASIBLE", today["plan"]["explanations"])
        self.assertTrue(today["next_actions"])
        event = next(e for e in today["plan"]["canonical_events"] if e["id"] == "event-pref")
        self.assertEqual(event["attendance_policy"], "PREFERRED")  # shown as the user set it

    def test_agenda_covers_seven_local_days(self):
        agenda = self.ok(self.client.get("/api/v1/plan/agenda"))
        self.assertEqual((agenda["days"], agenda["timezone"]), (7, "Europe/Moscow"))
        end = datetime.fromisoformat(agenda["plan"]["horizon_end"])
        self.assertGreaterEqual(end.astimezone(MOSCOW).date(), datetime(2026, 9, 30).date())

    def test_schema_is_v15(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 15)
        with closing(sqlite3.connect(self.db)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"deleted_obligations", "event_reminders", "task_progress_counts"} <= tables)


if __name__ == "__main__":
    unittest.main()
