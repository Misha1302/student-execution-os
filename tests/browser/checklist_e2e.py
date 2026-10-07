"""Task checklists end to end: Chromium against a real server and database.

Steps are added and checked off on the task screen, offline included (the check
survives an app restart and is sent once on reconnect); the plan keeps using only the
task's own remaining effort.
"""
from __future__ import annotations

import json
import unittest

from tests.browser.harness import ACCOUNT, PENDING_JS, RealServerTestCase

TASK_ID = "task-checklist-e2e-1"


class ChecklistEndToEndTest(RealServerTestCase):
    freeze_page_clock = False

    def test_steps_online_offline_and_reconnect(self):
        page = self._page()
        self._ready(page, "today")
        page.evaluate("""(id) => fetch('/api/v1/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ operations: [{ op_id: 'op-e2e-task-1', type: 'task.create', entity_id: id,
            payload: { title: 'Курсовая по истории', estimated_total_effort_minutes: 180, actual_cutoff: { state: 'ABSENT' } } }] }) })""",
                      TASK_ID)
        self._wait_db(page, "SELECT * FROM obligations WHERE id=?", TASK_ID)
        self._go(page, f"task/{TASK_ID}")
        card = page.locator("[data-checklist]")
        card.wait_for()
        for title in ("Найти источники", "Написать введение"):
            card.locator("[data-subtask-title]").fill(title)
            card.locator("[data-subtask-form] button[type=submit]").click()
            page.locator(".subtask-row", has_text=title).wait_for()
        self._wait_db(page, "SELECT * FROM task_subtasks WHERE task_id=?", TASK_ID, want=lambda rows: len(rows) == 2)
        page.locator(".subtask-row", has_text="Найти источники").locator("input[type=checkbox]").check()
        self._wait_db(page, "SELECT * FROM task_subtasks WHERE title='Найти источники' AND done_at IS NOT NULL")
        self.assertIn("1 из 2", card.inner_text())
        self._no_overflow(page)
        # Offline: tick the second step, close and reopen the app, still ticked; reconnect sends it once.
        self.online = False
        page.locator(".subtask-row", has_text="Написать введение").locator("input[type=checkbox]").check()
        page.locator(".subtask-row.done", has_text="Написать введение").wait_for()
        page.close()
        page = self._page(f"#/task/{TASK_ID}")
        self._ready(page, "task")
        self.assertIn("2 из 2", page.locator("[data-checklist]").inner_text())
        self.online = True
        page.evaluate("window.dispatchEvent(new Event('online'))")
        self._wait_db(page, "SELECT * FROM task_subtasks WHERE title='Написать введение' AND done_at IS NOT NULL")
        for _ in range(50):
            if page.evaluate(PENDING_JS) == 0:
                break
            page.wait_for_timeout(100)
        self.assertEqual(page.evaluate(PENDING_JS), 0)
        # The plan still uses only the task's remaining effort.
        remaining = json.loads(page.evaluate("fetch('/api/v1/tasks').then((r) => r.text())"))
        task = next(t for t in remaining if t["id"] == TASK_ID)
        self.assertEqual((task["remaining_effort_minutes"], task["checklist"]["done"]), (180, 2))
        self.assertEqual([e for e in self.errors if "internetdisconnected" not in e and "Failed to fetch" not in e], [])


if __name__ == "__main__":
    unittest.main()
