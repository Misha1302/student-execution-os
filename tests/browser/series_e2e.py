"""Class series in the calendar, end to end: Chromium against a real server and database.

Every change goes through the sync queue as a series.* operation — the test reads the
database to prove it, and the calendar to prove the user sees it.
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
ACCOUNT = "series-e2e"
NOW = datetime(2026, 9, 28, 9, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SeriesEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "series.sqlite")
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

    def _rows(self, sql: str) -> list[tuple]:
        with closing(sqlite3.connect(self.db)) as conn:
            return conn.execute(sql).fetchall()

    def _wait(self, sql: str, expected) -> list[tuple]:
        for _ in range(100):
            rows = self._rows(sql)
            if rows == expected:
                return rows
            time.sleep(0.1)
        return self._rows(sql)

    def _calendar(self, page):
        page.evaluate("location.hash = '#/today'")
        page.wait_for_selector('#workspace[data-view="today"][data-view-state="ready"]')
        page.evaluate("location.hash = '#/calendar'")
        page.wait_for_selector('#workspace[data-view="calendar"][data-view-state="ready"]')

    def test_late_autofocus_never_steals_a_field_the_person_already_chose(self):
        # CI once saved "МатанализR205": the sheet's delayed title autofocus fired after the
        # room field had been focused, so the rest of the typing went into the title.
        context = self.browser.new_context(viewport={"width": 390, "height": 844}, timezone_id="Europe/Moscow")
        self.addCleanup(context.close)
        page = context.new_page()
        page.goto(f"{self.origin}/#/calendar")
        page.wait_for_selector('#workspace[data-view="calendar"][data-view-state="ready"]')
        page.locator('[data-action="cal-new-recurring"]').click()
        sheet = page.locator("dialog.sheet[open]")
        room = sheet.locator('[data-f="location"]')
        room.focus()  # before the 80 ms autofocus
        page.keyboard.type("R2")
        page.wait_for_timeout(400)  # the autofocus timer has certainly fired
        page.keyboard.type("05")
        self.assertEqual(room.input_value(), "R205")
        self.assertEqual(sheet.locator('[data-f="title"]').input_value(), "")
        page.keyboard.press("Escape")
        page.wait_for_selector("dialog.sheet[open]", state="detached")
        # With nothing chosen yet, the title is still focused for the person.
        page.locator('[data-action="cal-new-recurring"]').click()
        page.wait_for_timeout(400)
        self.assertTrue(page.locator('dialog.sheet[open] [data-f="title"]').evaluate("el => el === document.activeElement"))

    def test_series_room_cancel_restore_and_day_off_go_through_sync(self):
        context = self.browser.new_context(viewport={"width": 390, "height": 844}, timezone_id="Europe/Moscow",
                                           reduced_motion="reduce")
        context.add_init_script("try { localStorage.setItem('seos.locale', 'ru') } catch (e) {}")
        self.addCleanup(context.close)
        page = context.new_page()
        page.clock.set_fixed_time(NOW)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{self.origin}/#/calendar")
        page.wait_for_selector('#workspace[data-view="calendar"][data-view-state="ready"]')

        page.locator('[data-action="cal-new-recurring"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator('[data-f="title"]').fill("Матанализ")
        sheet.locator('[data-f="start"]').fill("2026-09-28T10:00")
        sheet.locator('[data-f="location"]').fill("R205")
        sheet.locator('[data-f="teacher"]').fill("Иванова")
        sheet.locator("[data-save]").click()
        rows = self._wait("SELECT title, location_text, teacher, timezone_name FROM recurring_templates",
                          [("Матанализ", "R205", "Иванова", "Europe/Moscow")])
        self.assertEqual(rows, [("Матанализ", "R205", "Иванова", "Europe/Moscow")])

        self._calendar(page)
        first = page.locator('[data-action="cal-item"]', has_text="Матанализ").first
        self.assertIn("R205", first.inner_text())
        first.click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("[data-occ-open-edit]").click()
        sheet.locator("[data-occ-location]").fill("G-100")
        sheet.locator("[data-occ-save]").click()
        self.assertEqual(self._wait("SELECT layer, action, location_text FROM occurrence_overrides",
                                    [("USER", "MODIFY", "G-100")]), [("USER", "MODIFY", "G-100")])

        self._calendar(page)
        first = page.locator('[data-action="cal-item"]', has_text="Матанализ").first
        self.assertIn("G-100", first.inner_text())
        first.click()
        page.locator("dialog.sheet[open] [data-occ-cancel]").click()
        self.assertEqual(self._wait("SELECT action FROM occurrence_overrides", [("CANCEL",)]), [("CANCEL",)])

        self._calendar(page)
        first = page.locator('[data-action="cal-item"].cancelled', has_text="Матанализ").first
        first.click()
        page.locator("dialog.sheet[open] [data-occ-restore]").click()
        self.assertEqual(self._wait("SELECT count(*) FROM occurrence_overrides", [(0,)]), [(0,)])

        self._calendar(page)
        page.locator('[data-action="cal-holiday"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("[data-h-from]").fill("2026-10-05")
        sheet.locator("[data-h-to]").fill("2026-10-05")
        sheet.locator("[data-h-save]").click()
        self.assertEqual(self._wait("SELECT original_recurrence_id, reason FROM occurrence_overrides",
                                    [("2026-10-05T10:00:00", "HOLIDAY")]), [("2026-10-05T10:00:00", "HOLIDAY")])
        types = [r[0] for r in self._rows("SELECT op_type FROM client_operations ORDER BY rowid")]
        self.assertEqual(types, ["series.create", "series.occurrence.update", "series.occurrence.cancel",
                                 "series.occurrence.restore", "series.holiday"])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
