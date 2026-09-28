"""Real browsers + real server: a starosta and a student share a class schedule."""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from playwright.sync_api import sync_playwright

from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig

CHROMIUM = os.environ.get("CHROMIUM_PATH") or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None)
NOW = datetime(2026, 9, 21, 9, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GroupsBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = str(Path(cls.temp.name) / "groups.sqlite")
        port = _free_port()
        cls.server = uvicorn.Server(uvicorn.Config(
            create_app(cls.database, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: NOW),
            host="127.0.0.1", port=port, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        cls.origin = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=cls.origin, trust_env=False) as http:
            for login in ("starosta", "student"):
                http.post("/api/v1/auth/register", json={"login": login, "password": "correct horse"})
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, executable_path=CHROMIUM)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.temp.cleanup()

    def phone(self, login):
        context = self.browser.new_context(viewport={"width": 390, "height": 844}, timezone_id="Europe/Moscow",
                                           reduced_motion="reduce")
        self.addCleanup(context.close)
        context.add_init_script("try { localStorage.setItem('seos.locale', 'ru') } catch (e) {}")
        page = context.new_page()
        page.clock.set_fixed_time(NOW)
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(self.origin)
        self.ready(page, "welcome")
        page.locator('[data-chip-group="auth-mode"] [data-value="login"]').click()
        page.fill("input[name=login]", login)
        page.fill("input[name=password]", "correct horse")
        page.locator("button[type=submit]").click()
        self.ready(page, "today")
        return page, errors

    @staticmethod
    def ready(page, view):
        page.wait_for_selector(f'#workspace[data-view="{view}"][data-view-state="ready"]')

    def test_starosta_publishes_student_joins_suggests_and_sees_approved_exam(self):
        staff, staff_errors = self.phone("starosta")
        staff.goto(f"{self.origin}/#/more")
        self.ready(staff, "more")
        staff.locator('#workspace [data-nav="groups"]').click()
        self.ready(staff, "groups")
        staff.locator('[data-action="group-create"]').click()
        staff.locator("dialog.sheet[open] [data-name]").fill("БИ-24-1")
        staff.locator("dialog.sheet[open] [data-save]").click()
        self.ready(staff, "group")
        staff.locator('[data-action="group-item-add"]').click()
        sheet = staff.locator("dialog.sheet[open]")
        sheet.locator('[data-g="title"]').fill("Матанализ")
        sheet.locator('[data-g="start"]').fill("2026-09-22T10:00")
        sheet.locator('[data-g="location"]').fill("R205")
        sheet.locator("[data-save]").click()
        staff.locator("[data-group-schedule]", has_text="Матанализ").wait_for()
        staff.locator('[data-action="group-invite"]').click()
        code = staff.locator("dialog.sheet[open] [data-invite-code]").input_value()
        self.assertTrue(code.startswith("BOTAY-"))
        staff.keyboard.press("Escape")

        student, student_errors = self.phone("student")
        student.goto(f"{self.origin}/#/groups")
        self.ready(student, "groups")
        student.locator('[data-action="group-join"]').click()
        student.locator("dialog.sheet[open] [data-code]").fill(code)
        student.locator("dialog.sheet[open] [data-save]").click()
        self.ready(student, "group")
        # A member reads the schedule but cannot publish or invite.
        self.assertEqual(student.locator('[data-action="group-item-add"], [data-action="group-invite"]').count(), 0)
        self.assertIn("Участник", student.locator("[data-group]").inner_text())
        student.goto(f"{self.origin}/#/calendar")
        self.ready(student, "calendar")
        self.assertIn("Матанализ", student.locator("#workspace").inner_text())

        # The student suggests an exam; nothing is published until the starosta approves.
        student.goto(f"{self.origin}/#/groups")
        self.ready(student, "groups")
        student.locator('[data-action="group-open"]').first.click()
        self.ready(student, "group")
        student.locator('[data-group-schedule] .button.wide').click()
        sheet = student.locator("dialog.sheet[open]")
        sheet.locator('[data-chip-group="group-kind"] [data-value="EVENT"]').click()
        sheet.locator('[data-g="title"]').fill("Контрольная по матанализу")
        sheet.locator('[data-g="start"]').fill("2026-10-06T14:00")
        sheet.locator('[data-g="note"]').fill("Препод объявил на паре")
        sheet.locator("[data-save]").click()
        student.locator("[data-group-proposals]", has_text="На рассмотрении").wait_for()

        staff.reload()
        self.ready(staff, "group")
        staff.locator("[data-group-proposals]", has_text="Контрольная по матанализу").wait_for()
        staff.locator('[data-action="group-approve"]').click()
        staff.locator("[data-group-schedule]", has_text="Контрольная по матанализу").wait_for()

        student.goto(f"{self.origin}/#/calendar")
        self.ready(student, "calendar")
        student.goto(f"{self.origin}/#/tasks")
        self.ready(student, "tasks")
        with httpx.Client(base_url=self.origin, trust_env=False) as http:
            token = student.evaluate("localStorage.getItem('seos.token')")
            events = http.get("/api/v1/events", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertIn("Контрольная по матанализу", [event["title"] for event in events])
        for page in (staff, student):
            self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.assertEqual(staff_errors + student_errors, [])


if __name__ == "__main__":
    unittest.main()
