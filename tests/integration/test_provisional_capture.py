from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.sync.commands import Commands
from student_execution_os.web.queries import UiService


class ProvisionalCaptureTest(unittest.TestCase):
    def test_quick_task_has_a_range_and_can_be_started_and_completed(self):
        now = datetime(2026, 9, 30, 9, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "capture.sqlite")
            with SQLiteCanonicalRepository(database, clock=FrozenClock(now)) as repository:
                repository.initialize()
                repository.create_account("account")
                commands = Commands(repository, account_id="account", actor=ActorCategory.USER_UI, now=now)
                for index, title in enumerate(("Купить молоко", "Позвонить маме", "Ответить преподавателю", "Забрать заказ")):
                    task_id = f"task-quick-{index:04}"
                    outcome = commands.tasks.task_create(task_id, {"title": title, "provisional_effort": True, "actual_cutoff": {"state": "ABSENT"}})
                    self.assertEqual(outcome.entity["status"], "ACTIVE")
                    self.assertEqual(outcome.entity['effort_estimate_source'], 'SYSTEM_PROVISIONAL')
                    self.assertEqual(repository.get_task('account', task_id).effort_estimate_source, 'SYSTEM_PROVISIONAL')
                    self.assertEqual((outcome.entity["estimated_total_effort_low_minutes"], outcome.entity["estimated_total_effort_high_minutes"]), (15, 60))
                    self.assertEqual(commands.tasks.task_create(task_id, {"title": title}).status, "NOOP")
                today = UiService(database, account_id="account", principal_id="user", now=lambda: now).today()
                self.assertEqual(today["needs_refinement"], [])
                self.assertTrue(today["next_actions"])
                commands.tasks.task_update("task-quick-0000", {"estimated_total_effort_minutes": 20})
                updated = repository.get_task("account", "task-quick-0000")
                self.assertEqual(updated.estimated_total_effort_minutes, 20)
                self.assertEqual(updated.effort_estimate_source, 'EXPLICIT')
                self.assertIsNone(updated.estimated_total_effort_low_minutes)
                self.assertIsNone(updated.estimated_total_effort_high_minutes)
                self.assertEqual(commands.tasks.task_start('task-quick-0000', {}).entity['status'], 'ACTIVE')
                self.assertEqual(commands.tasks.task_complete('task-quick-0000', {}).entity['status'], 'COMPLETED')
