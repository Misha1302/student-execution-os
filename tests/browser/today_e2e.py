"""Capture to Today, end to end: Chromium against a real uvicorn + SQLite server.

"Сегодня в 18:00 созвон с Ариадной" typed into + must become an Event in the database
(through the sync queue, like every mutation) and appear in Today's "Soon" at 18:00 —
next to tasks, not in a separate list the user has to scroll to.
"""
from __future__ import annotations

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
ACCOUNT = "today-e2e"
MOSCOW = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 9, 28, 15, 0, tzinfo=MOSCOW)


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class TodayEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "today.sqlite")
        with SQLiteCanonicalRepository(cls.db) as repo:
            repo.initialize()
            repo.create_account(ACCOUNT)
        port = _free_port()
        config = uvicorn.Config(create_app(cls.db, account_id=ACCOUNT, principal_id="e2e", now=lambda: NOW),
                                host="127.0.0.1", port=port, log_level="warning")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        cls.origin = f"http://127.0.0.1:{port}"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, executable_path=CHROMIUM)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.tmp.cleanup()

    def _events(self) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(
                "SELECT o.title, e.starts_at FROM obligations o JOIN events e ON e.obligation_id=o.id "
                "WHERE o.account_id=? AND o.kind='EVENT'", (ACCOUNT,)).fetchall()

    def test_captured_event_reaches_the_database_and_today_soon(self):
        context = self.browser.new_context(viewport={"width": 390, "height": 844}, timezone_id="Europe/Moscow",
                                           reduced_motion="reduce")
        context.add_init_script("try { localStorage.setItem('seos.locale', 'ru') } catch (e) {}")
        self.addCleanup(context.close)
        page = context.new_page()
        page.clock.set_fixed_time(NOW)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{self.origin}/#/today")
        page.wait_for_selector('#workspace[data-view="today"][data-view-state="ready"]')

        page.locator(".fab").click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("#capture-text").fill("Сегодня в 18:00 созвон с Ариадной")
        sheet.locator(".event-card").wait_for()
        self.assertEqual(sheet.locator('[data-chip-group="capture-kind"] .on').get_attribute("data-value"), "EVENT")
        sheet.locator("[data-create]").click()

        for _ in range(100):
            if self._events():
                break
            time.sleep(0.1)
        rows = self._events()
        self.assertEqual(len(rows), 1, rows)
        title, starts_at = rows[0]
        self.assertEqual(title, "Созвон с Ариадной")
        self.assertEqual(datetime.fromisoformat(starts_at).astimezone(MOSCOW).strftime("%H:%M"), "18:00")

        page.evaluate("location.hash = '#/plan'")
        page.wait_for_selector('#workspace[data-view="plan"][data-view-state="ready"]')
        page.evaluate("location.hash = '#/today'")
        row = page.locator('[data-soon] [data-soon-kind="EVENT"]', has_text="Созвон с Ариадной")
        row.wait_for()
        self.assertIn("18:00", row.inner_text())
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
