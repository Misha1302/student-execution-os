"""Real browser -> upload API -> provider -> SourceApplier -> Today."""
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
ACCOUNT = "academic-browser-e2e"
NOW = datetime(2026, 9, 28, 9, 0, tzinfo=ZoneInfo("Europe/Moscow"))
FIXTURE = Path("tests/fixtures/academic_schedule_realistic.ics").resolve()


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class AcademicScheduleBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = str(Path(cls.temp.name) / "academic-browser.sqlite")
        with SQLiteCanonicalRepository(cls.database) as repo:
            repo.initialize()
            repo.create_account(ACCOUNT)
        port = _free_port()
        cls.server = uvicorn.Server(uvicorn.Config(
            create_app(cls.database, account_id=ACCOUNT, principal_id="e2e", now=lambda: NOW),
            host="127.0.0.1", port=port, log_level="warning",
        ))
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
        cls.temp.cleanup()

    @classmethod
    def _count(cls, table: str) -> int:
        with closing(sqlite3.connect(cls.database)) as conn:
            return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    def test_import_repeat_today_and_disconnect_on_phone(self):
        context = self.browser.new_context(
            viewport={"width": 390, "height": 844}, timezone_id="Europe/Moscow", reduced_motion="reduce"
        )
        self.addCleanup(context.close)
        context.add_init_script("try { localStorage.setItem('seos.locale', 'ru') } catch (e) {}")
        page = context.new_page()
        page.clock.set_fixed_time(NOW)
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"{self.origin}/#/settings")
        page.wait_for_selector('#workspace[data-view="settings"][data-view-state="ready"]')
        page.locator("[data-academic-file]").set_input_files(str(FIXTURE))
        page.locator('[data-action="academic-import"]').click()
        page.get_by_text("academic_schedule_realistic.ics", exact=True).wait_for()
        self.assertEqual((self._count("recurring_templates"), self._count("events")), (1, 2))

        # A second download/import is idempotent at stable external identity.
        page.locator("[data-academic-file]").set_input_files(str(FIXTURE))
        page.locator('[data-action="academic-import"]').click()
        page.wait_for_timeout(400)
        self.assertEqual((self._count("recurring_templates"), self._count("events")), (1, 2))

        page.evaluate("location.hash = '#/today'")
        page.wait_for_selector('#workspace[data-view="today"][data-view-state="ready"]')
        self.assertIn("Algorithms and Data Structures", page.locator("#workspace").inner_text())
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 390)

        page.evaluate("location.hash = '#/settings'")
        page.wait_for_selector('#workspace[data-view="settings"][data-view-state="ready"]')
        page.locator('[data-action="academic-disconnect"]').click()
        page.locator("dialog.sheet[open] [data-confirm]").click()
        page.locator('[data-action="academic-disconnect"]').wait_for(state="detached")
        self.assertEqual(self._count("academic_schedule_connections"), 0)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
