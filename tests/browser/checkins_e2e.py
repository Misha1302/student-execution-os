"""Check-ins and recurring reminders, end to end: Chromium against a real server and DB.

Scenario 1/2: «Каждый день в 9 утра напоминай принять витамин D» typed into + becomes a
MEDICATION check-in (not a Task); today's 09:00 occurrence appears on Today; «Принял»
records the outcome with the moment it was pressed — offline included, replayed once.
Scenario 3: «Каждый вечер в 22:30 напоминай вынести мусор» is a recurring reminder.
Scenario 7: «Решать по 20 задач матана каждый день» counts quantity.
The static shell is served from disk; the network can be switched off like a phone's.
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
ACCOUNT = "checkins-e2e"
MOSCOW = ZoneInfo("Europe/Moscow")
# Tuesday 08:00 in Moscow: today's 09:00 dose is still ahead.
NOW = datetime(2026, 10, 6, 8, 0, tzinfo=MOSCOW)
PENDING_JS = ("Object.entries(localStorage).filter(([k]) => k.startsWith('seos.ops.'))"
              ".flatMap(([, v]) => JSON.parse(v)).filter((x) => x.state === 'PENDING').length")


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CheckInsEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "checkins.sqlite")
        with SQLiteCanonicalRepository(cls.db) as repo:
            repo.initialize()
            repo.create_account(ACCOUNT)
        cls.clock = {"now": NOW}
        port = _free_port()
        config = uvicorn.Config(create_app(cls.db, account_id=ACCOUNT, principal_id="e2e", now=lambda: cls.clock["now"]),
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

    def setUp(self) -> None:
        self.online = True
        self.clock["now"] = NOW
        self.context = self.browser.new_context(viewport={"width": 360, "height": 780}, timezone_id="Europe/Moscow",
                                                reduced_motion="reduce")
        self.context.add_init_script("try { localStorage.setItem('seos.locale', 'ru') } catch (e) {}")
        self.context.route(f"{self.origin}/**", self._route)
        self.errors: list[str] = []

    def tearDown(self) -> None:
        self.context.close()

    def _route(self, route):
        path = route.request.url.removeprefix(self.origin).split("#")[0] or "/"
        if path.startswith("/api/"):
            if not self.online:
                route.abort("internetdisconnected")
                return
            route.continue_()
            return
        if path.startswith("/assets/"):
            file = STATIC / path.removeprefix("/assets/")
            ctype = "text/javascript" if file.suffix == ".js" else mimetypes.guess_type(file.name)[0] or "text/plain"
            route.fulfill(status=200, content_type=ctype, body=file.read_text())
            return
        route.fulfill(status=200, content_type="text/html", body=(STATIC / "index.html").read_text())

    def _page(self, hash_: str = "#/today"):
        page = self.context.new_page()
        page.clock.set_fixed_time(self.clock["now"])
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(f"{self.origin}/{hash_}")
        return page

    @staticmethod
    def _ready(page, view: str) -> None:
        page.wait_for_selector(f'#workspace[data-view="{view}"][data-view-state="ready"]')

    def _go(self, page, view: str) -> None:
        page.evaluate(f"location.hash = '#/{view}'")
        self._ready(page, view.split("/", 1)[0])

    def _db(self, sql: str, *args):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, args).fetchall()

    def _wait_db(self, page, sql: str, *args, want=lambda rows: bool(rows)):
        # Poll through Playwright: a plain sleep would stall the routed API requests.
        for _ in range(100):
            rows = self._db(sql, *args)
            if want(rows):
                return rows
            page.wait_for_timeout(100)
        self.fail(f"database never satisfied: {sql}")

    def _say(self, page, text: str) -> None:
        page.locator(".fab").click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("#capture-text").fill(text)
        sheet.locator(".command-card").wait_for()
        return sheet

    def _no_overflow(self, page) -> None:
        width = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        self.assertLessEqual(width, 0, "the page scrolls sideways on a small phone")

    def test_1_medication_scenario_taken_with_timestamp_and_history(self):
        page = self._page()
        self._ready(page, "today")
        sheet = self._say(page, "Каждый день в 9 утра напоминай принять витамин D")
        card = sheet.locator(".command-card")
        self.assertIn("Витамин D", card.inner_text())
        self.assertIn("Принял", card.inner_text())
        card.locator("[data-run]").click()
        page.locator("dialog.sheet[open]").wait_for(state="detached")
        templates = self._wait_db(page, "SELECT * FROM checkin_templates WHERE account_id=?", ACCOUNT)
        self.assertEqual((templates[0]["kind"], templates[0]["title"]), ("MEDICATION", "Витамин D"))
        self.assertEqual(self._db("SELECT count(*) AS n FROM obligations")[0]["n"], 0, "a check-in is not a Task")
        # 09:04: the user opens the app; Today shows the 09:00 dose with «Принял».
        page.close()
        self.clock["now"] = NOW.replace(hour=9, minute=4)
        page = self._page()
        self._ready(page, "today")
        row = page.locator("[data-checkins] .checkin-row", has_text="Витамин D")
        row.wait_for()
        self.assertIn("09:00", row.inner_text())
        self._no_overflow(page)
        row.locator('[data-action="checkin-done"]').click()
        done = self._wait_db(page, "SELECT * FROM checkin_occurrences WHERE template_id=? AND status='DONE'", templates[0]["id"])
        self.assertEqual(datetime.fromisoformat(done[0]["occurred_at"]).astimezone(MOSCOW).strftime("%H:%M"), "09:04")
        # History shows the fact, in words.
        self._go(page, f"checkin/{templates[0]['id']}")
        history = page.locator(".history-day").first
        history.wait_for()
        self.assertIn("Принял в 09:04", history.inner_text())
        self.assertIn("1 / 1", history.inner_text())
        self._no_overflow(page)
        self.assertEqual(self.errors, [])

    def test_2_offline_taken_survives_restart_and_replays_once(self):
        page = self._page("#/checkins")
        self._ready(page, "checkins")
        page.locator('[data-action="checkin-new"]').first.click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator('[data-chip-group="checkin-kind"] [data-value="MEDICATION"]').click()
        sheet.locator("[data-checkin-title]").fill("Сертралин")
        sheet.locator("[data-checkin-dose]").fill("50 мг")
        sheet.locator("[data-checkin-time]").fill("21:00")
        sheet.locator("[data-checkin-create]").click()
        templates = self._wait_db(page, "SELECT * FROM checkin_templates WHERE title='Сертралин'")
        self.assertEqual(templates[0]["dose_text"], "50 мг")
        page.evaluate("location.reload()")
        self._ready(page, "checkins")
        row = page.locator(".checkin-row", has_text="Сертралин")
        row.wait_for()
        # 21:02, signal lost: «Принял», then the app is closed and opened again offline.
        page.close()
        self.clock["now"] = NOW.replace(hour=21, minute=2)
        page = self._page("#/checkins")
        self._ready(page, "checkins")
        row = page.locator(".checkin-row", has_text="Сертралин")
        row.wait_for()
        self.online = False
        row.locator('[data-action="checkin-done"]').click()
        page.locator(".checkin-row", has_text="Сертралин").filter(has_text="Принял в 21:02").wait_for()
        page.close()
        page = self._page("#/checkins")
        self._ready(page, "checkins")
        self.assertIn("Принял", page.locator(".checkin-row", has_text="Сертралин").first.inner_text())
        self.assertEqual(self._db("SELECT count(*) AS n FROM checkin_occurrences WHERE template_id=? AND status='DONE'",
                                  templates[0]["id"])[0]["n"], 0)
        # Back online: delivered once, with the moment the button was pressed.
        self.online = True
        page.evaluate("window.dispatchEvent(new Event('online'))")
        done = self._wait_db(page, "SELECT * FROM checkin_occurrences WHERE template_id=? AND status='DONE'", templates[0]["id"])
        self.assertEqual(datetime.fromisoformat(done[0]["occurred_at"]).astimezone(MOSCOW).strftime("%H:%M"), "21:02")
        for _ in range(50):
            if page.evaluate(PENDING_JS) == 0:
                break
            page.wait_for_timeout(100)
        self.assertEqual(page.evaluate(PENDING_JS), 0)
        ops = self._db("SELECT count(*) AS n FROM client_operations WHERE op_type='checkin.occurrence.done' "
                       "AND result_json LIKE ?", f"%{templates[0]['id']}%")
        self.assertEqual(ops[0]["n"], 1)
        self.assertEqual([e for e in self.errors if "internetdisconnected" not in e and "Failed to fetch" not in e], [])

    def test_3_recurring_reminder_and_quota_are_not_tasks(self):
        page = self._page()
        self._ready(page, "today")
        sheet = self._say(page, "Каждый вечер в 22:30 напоминай вынести мусор")
        self.assertIn("Вынести мусор", sheet.locator(".command-card").inner_text())
        sheet.locator("[data-run]").click()
        page.locator("dialog.sheet[open]").wait_for(state="detached")
        series = self._wait_db(page, "SELECT * FROM reminder_series WHERE title='Вынести мусор'")
        self.assertEqual(series[0]["recurrence_rule"], "FREQ=DAILY")
        self._wait_db(page, "SELECT * FROM reminder_series_occurrences WHERE series_id=?", series[0]["id"])
        sheet = self._say(page, "Решать по 20 задач матана каждый день")
        sheet.locator("[data-run]").click()
        page.locator("dialog.sheet[open]").wait_for(state="detached")
        quota = self._wait_db(page, "SELECT * FROM checkin_templates WHERE kind='QUOTA'")
        self.assertEqual((quota[0]["target_quantity"], quota[0]["unit"]), (20, "задач"))
        self.assertEqual(self._db("SELECT count(*) AS n FROM obligations")[0]["n"], 0)
        # Quantity, not a checkbox: +5 from the check-ins screen.
        self._go(page, "checkins")
        row = page.locator(".checkin-row", has_text="Решать 20 задач матана")
        row.wait_for()
        self.assertIn("0 из 20 задач", row.inner_text())
        row.locator('[data-action="checkin-progress"]').click()
        dialog = page.locator("dialog.sheet[open]")
        dialog.locator('[data-chip-group="quota-count"] [data-value="5"]').click()
        dialog.locator("[data-save]").click()
        self._wait_db(page, "SELECT * FROM checkin_occurrences WHERE template_id=? AND quantity_done=5", quota[0]["id"])
        self.assertIn("5 из 20 задач", page.locator(".checkin-row", has_text="Решать 20 задач матана").inner_text())
        self.assertIn("Вынести мусор", page.locator("body").inner_text())
        self._no_overflow(page)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
