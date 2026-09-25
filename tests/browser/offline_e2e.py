"""Offline-first end to end: Chromium against a real server and database.

The page's static files are served from disk (as the Android app bundles them), and
every /api/ call goes to a real uvicorn + SQLite server — unless the test switches
the "network" off, in which case API calls fail exactly like a phone without signal.
Nothing about the API is mocked: what reaches the database is what the queue sent.
"""
from __future__ import annotations

import json
import mimetypes
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from playwright.sync_api import sync_playwright

from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app

CHROMIUM = os.environ.get("CHROMIUM_PATH") or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None)
STATIC = Path("src/student_execution_os/web/static")
ACCOUNT = "offline-e2e"
MOSCOW = ZoneInfo("Europe/Moscow")
PENDING_JS = ("Object.entries(localStorage).filter(([k]) => k.startsWith('seos.ops.'))"
              ".flatMap(([, v]) => JSON.parse(v)).filter((x) => x.state === 'PENDING').length")


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class OfflineEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "e2e.sqlite")
        with SQLiteCanonicalRepository(cls.db) as repo:
            repo.initialize()
            repo.create_account(ACCOUNT)
        cls.port = _free_port()
        config = uvicorn.Config(create_app(cls.db, account_id=ACCOUNT, principal_id="e2e"),
                                host="127.0.0.1", port=cls.port, log_level="warning")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, executable_path=CHROMIUM)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.online = True
        self.sync_requests: list[dict] = []
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844}, timezone_id="Europe/Moscow",
                                                reduced_motion="reduce")
        self.context.add_init_script("try { localStorage.setItem('seos.locale', 'ru') } catch (e) {}")
        self.context.route(f"{self.origin}/**", self._route)
        self.errors: list[str] = []

    def tearDown(self) -> None:
        self.context.close()

    # ---- harness --------------------------------------------------------------------

    def _route(self, route):
        request = route.request
        path = request.url.removeprefix(self.origin).split("#")[0] or "/"
        if path.startswith("/api/"):
            if not self.online:
                route.abort("internetdisconnected")
                return
            if path == "/api/v1/sync":
                self.sync_requests.append(json.loads(request.post_data or "{}"))
            route.continue_()
            return
        # The app shell comes from the device, not the network.
        if path.startswith("/assets/"):
            file = STATIC / path.removeprefix("/assets/")
            ctype = "text/javascript" if file.suffix == ".js" else mimetypes.guess_type(file.name)[0] or "text/plain"
            route.fulfill(status=200, content_type=ctype, body=file.read_text())
            return
        route.fulfill(status=200, content_type="text/html", body=(STATIC / "index.html").read_text())

    def _page(self, hash_: str = ""):
        page = self.context.new_page()
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(f"{self.origin}/{hash_}")
        return page

    @staticmethod
    def _ready(page, view: str) -> None:
        page.wait_for_selector(f'#workspace[data-view="{view}"][data-view-state="ready"]')

    def _go(self, page, view: str) -> None:
        page.evaluate(f"location.hash = '#/{view}'")
        self._ready(page, view.split("/", 1)[0].split("?", 1)[0])

    def _pending(self, page) -> int:
        return page.evaluate(PENDING_JS)

    def _wait_synced(self, page) -> None:
        for _ in range(100):
            if self._pending(page) == 0:
                return
            page.wait_for_timeout(100)
        self.fail("queued changes were not sent")

    def _wait_cached(self, page) -> None:
        """While online the app caches every main screen in the background."""
        wanted = ["/api/v1/today", "/api/v1/tasks", "/api/v1/events", "/api/v1/plan/agenda?days=7"]
        for _ in range(100):
            keys = page.evaluate("Object.keys(localStorage)")
            if all(f"seos.cache.{path}" in keys for path in wanted):
                return
            page.wait_for_timeout(100)
        self.fail("read models were not cached")

    def _capture(self, page, text: str) -> None:
        page.locator(".fab").click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("#capture-text").fill(text)
        sheet.locator(".capture-card").wait_for()
        sheet.locator("[data-create]").click()
        page.locator("dialog.sheet[open]").wait_for(state="detached")

    def _db(self, sql: str, *args):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, args).fetchall()

    def _task_row(self, title_prefix: str):
        rows = self._db("SELECT * FROM obligations WHERE account_id=? AND title LIKE ?", ACCOUNT, f"{title_prefix}%")
        return rows

    # ---- scenarios ------------------------------------------------------------------

    def test_offline_day_create_edit_start_snooze_done_restart_reconnect(self):
        page = self._page()
        self._ready(page, "today")
        self._wait_cached(page)

        # No network from here on.
        self.online = False
        self._capture(page, "Прочитать главу 5 по истории, займёт 45 минут")
        # The task is on screen at once — no spinner, no waiting for a server.
        self._go(page, "tasks")
        card = page.locator(".task-card", has_text="Прочитать главу 5 по истории")
        card.wait_for()
        self.assertIn("ждёт отправки", card.inner_text())
        self.assertTrue(page.locator("#offline-chip").is_visible())

        # Edit (title), start, «не сейчас» (put off an hour) and done — all offline.
        card.click()
        self._ready(page, "task")
        page.locator('[data-action="detail-edit"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator('[data-f="title"]').fill("Прочитать главу 5 и 6 по истории")
        sheet.locator("[data-save]").click()
        page.locator(".detail-title", has_text="главу 5 и 6").wait_for()
        page.locator('[data-action="detail-start"]').click()
        page.locator(".detail-head", has_text="В работе").wait_for()
        page.locator('[data-action="detail-reschedule"]').click()
        page.locator('dialog.sheet[open] [data-later="h1"]').click()
        page.locator(".kv-list", has_text="Не раньше").wait_for()
        page.locator('[data-action="detail-lifecycle"][data-op="complete"]').click()
        page.locator(".detail-head", has_text="Выполнено").wait_for()
        pending_before_restart = self._pending(page)
        self.assertEqual(pending_before_restart, 5)  # create, update, start, defer, complete

        # A fixed-time event, still offline.
        self._capture(page, "Сегодня с 21 до 22 провести занятие по программированию")
        self.assertEqual(self._pending(page), 6)

        # Restart the app without network: everything is still there.
        page.close()
        page = self._page("#/tasks")
        self._ready(page, "tasks")
        page.locator('[data-chip-group="task-filter"] [data-value="done"]').click()
        page.locator(".task-card", has_text="главу 5 и 6").wait_for()
        self.assertEqual(self._pending(page), 6)
        self.assertEqual(self.sync_requests, [])

        # Reconnect: every change reaches the server once, in order.
        self.online = True
        page.evaluate("window.dispatchEvent(new Event('online'))")
        self._wait_synced(page)
        sent = [op for request in self.sync_requests for op in request["operations"]]
        self.assertEqual([op["type"] for op in sent],
                         ["task.create", "task.update", "task.start", "task.defer", "task.complete", "event.create"])
        self.assertEqual(len({op["op_id"] for op in sent}), len(sent))
        applied = self._db("SELECT op_id, status FROM client_operations WHERE account_id=?", ACCOUNT)
        self.assertEqual(sorted(r["op_id"] for r in applied), sorted(op["op_id"] for op in sent))
        self.assertTrue(all(r["status"] == "APPLIED" for r in applied))
        tasks = self._task_row("Прочитать главу")
        self.assertEqual(len(tasks), 1)
        self.assertEqual((tasks[0]["title"], tasks[0]["lifecycle_status"]), ("Прочитать главу 5 и 6 по истории", "COMPLETED"))
        events = self._db("SELECT o.title, e.starts_at, e.ends_at FROM obligations o JOIN events e "
                          "ON e.obligation_id=o.id WHERE o.account_id=?", ACCOUNT)
        self.assertEqual(len(events), 1)
        start = datetime.fromisoformat(events[0]["starts_at"]).astimezone(MOSCOW)
        end = datetime.fromisoformat(events[0]["ends_at"]).astimezone(MOSCOW)
        self.assertEqual((events[0]["title"], start.strftime("%H:%M"), end.strftime("%H:%M")),
                         ("Провести занятие по программированию", "21:00", "22:00"))
        self.assertEqual(start.date(), datetime.now(MOSCOW).date())
        # No deadline was invented for the event.
        self.assertEqual(self._db("SELECT COUNT(*) AS n FROM tasks t JOIN obligations o ON o.id=t.obligation_id "
                                  "WHERE o.title LIKE 'Провести занятие%'")[0]["n"], 0)

        # Replaying the whole queue again (e.g. a lost response) changes nothing.
        before = self._db("SELECT id, version FROM obligations WHERE account_id=? ORDER BY id", ACCOUNT)
        page.evaluate("""(async () => {
          const ops = Object.entries(localStorage).filter(([k]) => k.startsWith('seos.ops.')).flatMap(([, v]) => JSON.parse(v)).map((x) => x.operation);
          await fetch('/api/v1/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ operations: ops }) });
        })()""")
        page.wait_for_timeout(300)
        self.assertEqual(self._db("SELECT id, version FROM obligations WHERE account_id=? ORDER BY id", ACCOUNT), before)
        self.assertEqual(self.errors, [])
        page.close()

    def test_repeated_taps_on_a_bad_network_send_one_change(self):
        page = self._page()
        self._ready(page, "today")
        self._capture(page, "Сдать справку в деканат, займёт 15 минут, без дедлайна")
        self._wait_synced(page)
        page.wait_for_timeout(700)
        self._wait_cached(page)
        task_id = self._task_row("Сдать справку")[0]["id"]
        # Flaky network: requests fail; the user taps "Выполнено" again and again.
        self.online = False
        self._go(page, f"task/{task_id}")
        for _ in range(4):
            page.evaluate("""(id) => import('/assets/js/sync.js').then((m) => m.queueOperation('task.complete', id, {}))""", task_id)
        self.assertEqual(self._pending(page), 1)
        self.online = True
        page.evaluate("window.dispatchEvent(new Event('online'))")
        self._wait_synced(page)
        completes = [op for r in self.sync_requests for op in r["operations"] if op["type"] == "task.complete"]
        self.assertEqual(len(completes), 1)
        self.assertEqual(self._task_row("Сдать справку")[0]["lifecycle_status"], "COMPLETED")
        # Reopen from the list, then the flow ends where it started.
        self._go(page, f"task/{task_id}")
        page.locator('[data-action="detail-lifecycle"][data-op="reopen"]').click()
        self._wait_synced(page)
        self.assertEqual(self._task_row("Сдать справку")[0]["lifecycle_status"], "ACTIVE")
        self.assertEqual(self.errors, [])
        page.close()


if __name__ == "__main__":
    unittest.main()
