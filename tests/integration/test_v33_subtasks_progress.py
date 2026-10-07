"""Schema v33: checklists inside Tasks, and project progress from real data."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SCHEMA_VERSION, SQLiteCanonicalRepository
from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.sync.commands import SyncService
from student_execution_os.web.queries import UiService
from tests.rollback_chain import roll_back_newer_than

T0 = datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "v33.sqlite")
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
            result = SyncService(repo, account_id=account, principal_id=f"u-{account}", now=at).apply({
                "op_id": op_id or f"op-{self.n:06d}", "type": op_type, "entity_id": entity_id, "payload": payload or {}})
            repo.connection.commit()
            return result

    def ok(self, *args, **kwargs):
        result = self.op(*args, **kwargs)
        self.assertEqual(result["status"], "APPLIED", result)
        return result["entity"]

    def ui(self, at=T0):
        return UiService(self.db, account_id="a", principal_id="u-a", now=lambda: at)

    def task(self, task_id="task-essay-001", effort=120):
        return self.ok("task.create", task_id, {"title": "Курсовая", "estimated_total_effort_minutes": effort,
                                                "actual_cutoff": {"state": "ABSENT"}})


class SubtaskTests(Harness):
    def test_checklist_order_progress_and_no_double_counting(self):
        self.task()
        for index, (sid, title, effort) in enumerate([("sub-intro-0001", "Введение", 30), ("sub-body-00001", "Основная часть", 60),
                                                       ("sub-refs-00001", "Литература", 30)]):
            self.ok("subtask.create", sid, {"task_id": "task-essay-001", "title": title, "effort_minutes": effort})
        # Inserted between the first two from another (offline) device: fractional position.
        self.ok("subtask.create", "sub-plan-00001", {"task_id": "task-essay-001", "title": "План", "position": 1.5})
        detail = self.ui().tasks.task("task-essay-001")
        self.assertEqual([s["title"] for s in detail["subtasks"]], ["Введение", "План", "Основная часть", "Литература"])
        # Not every step has effort: progress by count; the planner still sees only the Task.
        self.ok("subtask.complete", "sub-intro-0001", {"occurred_at": "2026-10-06T05:50:00Z"})
        checklist = self.ui().tasks.task("task-essay-001")["checklist"]
        self.assertEqual((checklist["done"], checklist["total"], checklist["percent"], checklist["basis"]), (1, 4, 25, "COUNT"))
        self.assertEqual(self.ui().tasks.task("task-essay-001")["remaining_effort_minutes"], 120)
        # Every step with effort: progress by effort.
        self.ok("subtask.update", "sub-plan-00001", {"effort_minutes": 10})
        checklist = self.ui().tasks.task("task-essay-001")["checklist"]
        self.assertEqual((checklist["basis"], checklist["percent"], checklist["effort_left_minutes"]), ("EFFORT", 23, 100))
        # Reorder and delete.
        self.ok("subtask.move", "sub-refs-00001", {"position": 0.5})
        self.assertEqual(self.ui().tasks.task("task-essay-001")["subtasks"][0]["title"], "Литература")
        self.ok("subtask.delete", "sub-plan-00001")
        self.assertEqual(len(self.ui().tasks.task("task-essay-001")["subtasks"]), 3)
        # The plan's work is the Task's remaining effort only (checklist effort is not added).
        today = self.ui().planning.today()
        planned = sum(1 for block in today["plan"]["blocks"] if block["type"] == "WORK")
        self.assertGreaterEqual(planned, 1)
        self.assertEqual(sum(t["remaining_effort_minutes"] for t in today["tasks"]), 120)

    def test_offline_replay_duplicates_conflicts_and_deletes(self):
        self.task()
        created = self.op("subtask.create", "sub-step-0001", {"task_id": "task-essay-001", "title": "Шаг"}, op_id="tap-create-1")
        replay = self.op("subtask.create", "sub-step-0001", {"task_id": "task-essay-001", "title": "Шаг"}, op_id="tap-create-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["entity"], created["entity"])
        self.ok("subtask.complete", "sub-step-0001")
        second = self.op("subtask.complete", "sub-step-0001")
        self.assertEqual((second["status"], second["code"]), ("NOOP", "ALREADY_DONE"))
        self.ok("subtask.delete", "sub-step-0001")
        late = self.op("subtask.update", "sub-step-0001", {"title": "Поздно"})
        self.assertEqual((late["status"], late["code"]), ("NOOP", "DELETED"))
        self.assertEqual(self.op("subtask.create", "sub-step-0001", {"task_id": "task-essay-001", "title": "x"})["code"],
                         "DELETED")
        # Deleting the Task removes its checklist.
        self.ok("subtask.create", "sub-other-001", {"task_id": "task-essay-001", "title": "Другой"})
        self.ok("task.delete", "task-essay-001")
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_subtasks").fetchone()[0], 0)

    def test_account_isolation_and_events_have_no_checklists(self):
        self.task()
        self.assertEqual(self.op("subtask.create", "sub-b-000001", {"task_id": "task-essay-001", "title": "x"},
                                 account="b")["code"], "NOT_FOUND")
        self.ok("subtask.create", "sub-a-000001", {"task_id": "task-essay-001", "title": "Мой"})
        self.assertEqual(self.op("subtask.complete", "sub-a-000001", account="b")["code"], "NOT_FOUND")
        self.assertEqual(self.op("subtask.create", "sub-a-000001", {"task_id": "task-essay-001", "title": "x"},
                                 account="b")["status"], "REJECTED")
        self.ok("event.create", "event-lecture-1", {"title": "Лекция", "starts_at": "2026-10-07T07:00:00Z",
                                                    "ends_at": "2026-10-07T08:00:00Z"})
        self.assertEqual(self.op("subtask.create", "sub-event-001", {"task_id": "event-lecture-1", "title": "x"})["code"],
                         "NOT_FOUND")


class ProjectProgressTests(Harness):
    def test_progress_from_tasks_checklists_and_milestones_with_history(self):
        self.ok("project.create", "project-thesis-1", {"title": "Диплом"})
        for task_id, title in (("task-ch1-00001", "Глава 1"), ("task-ch2-00001", "Глава 2")):
            self.ok("project.task.create", "project-thesis-1", {"task_id": task_id, "title": title,
                                                                "actual_cutoff": {"state": "ABSENT"}})
        self.ok("subtask.create", "sub-ch1-a-0001", {"task_id": "task-ch1-00001", "title": "Обзор"})
        self.ok("subtask.create", "sub-ch1-b-0001", {"task_id": "task-ch1-00001", "title": "Выводы"})
        self.ok("subtask.complete", "sub-ch1-a-0001", {"occurred_at": "2026-10-03T10:00:00Z"})
        project = self.ui().projects.project("project-thesis-1")
        # Effort is the canonical basis (provisional 30 min each): a checklist step is a
        # fact about the step, not logged work, so it does not move effort progress...
        self.assertEqual((project["progress"]["basis"], project["progress"]["percent"]), ("EFFORT", 0))
        # ...while the member shows its own checklist share.
        member = next(m for m in project["members"] if m["id"] == "task-ch1-00001")
        self.assertEqual((member["checklist"]["done"], member["checklist"]["total"]), (1, 2))
        self.ok("task.progress", "task-ch1-00001", {"minutes": 15})
        self.assertEqual(self.ui().projects.project("project-thesis-1")["progress"]["percent"], 25)
        self.ok("milestone.create", "milestone-draft-1", {"project_id": "project-thesis-1", "title": "Черновик", "marker_at": "2026-10-05T12:00:00Z"})
        self.ok("milestone.create", "milestone-final-1", {"project_id": "project-thesis-1", "title": "Сдача", "marker_at": "2026-12-01T12:00:00Z"})
        progress = self.ui().projects.project("project-thesis-1")["progress"]
        self.assertEqual((progress["milestones_total"], progress["milestones_done"], progress["milestones_overdue"]),
                         (2, 0, 1))
        self.ok("milestone.complete", "milestone-draft-1")
        project = self.ui().projects.project("project-thesis-1")
        self.assertEqual((project["progress"]["milestones_done"], project["progress"]["milestones_overdue"]), (1, 0))
        self.assertEqual(project["progress"]["next_milestone"]["title"], "Сдача")
        history = project["progress_history"]
        self.assertEqual(history[0]["date"], "2026-10-03")
        self.ok("task.complete", "task-ch2-00001", {"occurred_at": "2026-10-06T05:00:00Z"})
        history = self.ui().projects.project("project-thesis-1")["progress_history"]
        self.assertEqual(history[-1]["date"], "2026-10-06")
        self.assertGreater(history[-1]["percent"], history[0]["percent"])


class QuotaCapacityTests(Harness):
    def test_known_pace_reserves_time_unknown_pace_is_reported_not_invented(self):
        self.ok("checkin.create", "checkin-matan-1", {"kind": "QUOTA", "title": "Задачи по матану", "target_quantity": 20,
                                                      "unit": "задач", "unit_effort_seconds": 180,
                                                      "dtstart_local": "2026-10-06T00:00", "recurrence_rule": "FREQ=DAILY",
                                                      "timezone_name": "Europe/Moscow", "remind": False})
        self.ok("checkin.create", "checkin-anki-01", {"kind": "QUOTA", "title": "Anki", "target_quantity": 100,
                                                      "unit": "карточек", "dtstart_local": "2026-10-06T00:00",
                                                      "recurrence_rule": "FREQ=DAILY", "timezone_name": "Europe/Moscow",
                                                      "remind": False})
        self.ok("reminder.create", "reminder-prefs-01", {"title": "x", "remind_at": "2026-10-07T06:00:00Z"})
        today = self.ui().planning.today()
        capacity = today["day_capacity"]
        self.assertEqual(capacity["quota_known_minutes"], 60)
        self.assertEqual(capacity["quota_unknown_count"], 1)
        self.assertEqual(capacity["safe_reserve_after_quotas_minutes"], max(0, capacity["safe_reserve_minutes"] - 60))
        self.assertEqual({item["title"] for item in today["checkins"]}, {"Задачи по матану", "Anki"})


class MigrationTests(Harness):
    def test_v32_to_v33_and_back(self):
        self.task()
        self.ok("subtask.create", "sub-mig-00001", {"task_id": "task-essay-001", "title": "Шаг"})
        export = SQLiteDataLifecycle(self.db).export_account("a").tables
        self.assertEqual(len(export["task_subtasks"]), 1)
        with sqlite3.connect(self.db) as conn:
            roll_back_newer_than(conn, 32)
            self.assertEqual(conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 32)
            self.assertEqual(conn.execute("SELECT count(*) FROM obligations WHERE id='task-essay-001'").fetchone()[0], 1)
        with SQLiteCanonicalRepository(self.db, clock=FrozenClock(T0)) as repo:
            repo.initialize()
            self.assertEqual(repo.schema_version(), SCHEMA_VERSION)


if __name__ == "__main__":
    unittest.main()
