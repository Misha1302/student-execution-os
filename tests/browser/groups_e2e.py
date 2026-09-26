"""Collaborative groups end to end: two people, two browsers, one real server.

The starosta creates a group, invites with a join code and publishes a control work;
the student joins, sees it in «Дела», changes it *for themselves* (the action sheet
keeps «Для меня» and «Для группы» apart) and creates a private preparation task.
Nothing is mocked: uvicorn + SQLite in session mode, Chromium in a phone viewport.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig

CHROMIUM = os.environ.get("CHROMIUM_PATH") or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None)
MOSCOW = ZoneInfo("Europe/Moscow")
SHOTS = os.environ.get("SEOS_SCREENSHOTS")


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GroupsEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / "groups-e2e.sqlite")
        with SQLiteCanonicalRepository(cls.db) as repo:
            repo.initialize()
        cls.port = _free_port()
        app = create_app(cls.db, auth=AuthConfig(password_scrypt_n=2**10))
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=cls.port, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.sessions = {}
        for login in ("starosta", "student"):
            response = httpx.post(f"{cls.origin}/api/v1/auth/register", json={"login": login, "password": "correct horse"})
            response.raise_for_status()
            cls.sessions[login] = response.json()
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, executable_path=CHROMIUM)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.tmp.cleanup()

    def page_for(self, login: str):
        session = self.sessions[login]
        context = self.browser.new_context(viewport={"width": 390, "height": 844}, timezone_id="Europe/Moscow",
                                           reduced_motion="reduce")
        context.add_init_script(
            "try { localStorage.setItem('seos.locale', 'ru');"
            f"localStorage.setItem('seos.token', {json.dumps(session['token'])});"
            f"localStorage.setItem('seos.user', {json.dumps(json.dumps(session['user']))}); }} catch (e) {{}}")
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        self.addCleanup(context.close)
        self.addCleanup(lambda: self.assertEqual(errors, [], f"JavaScript errors for {login}"))
        return page

    def shot(self, page, name: str) -> None:
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=True)

    def sheet(self, page):
        return page.locator("dialog[open]").last

    def actions_of(self, page, row):
        """Right click until the action sheet opens: the previous action may still be finishing
        (quick actions are single-flight per item, a click during that is dropped)."""
        for _ in range(10):
            row.click(button="right")
            try:
                page.locator("dialog[open]").first.wait_for(timeout=500)
                return self.sheet(page)
            except Exception:
                continue
        self.fail("the action sheet did not open")

    def test_starosta_publishes_student_sees_and_decides_for_themselves(self):
        starosta = self.page_for("starosta")
        starosta.goto(f"{self.origin}/#/groups")
        starosta.get_by_role("button", name="Создать группу").first.click()
        self.sheet(starosta).locator('[data-g="name"]').fill("ПИ-261")
        self.sheet(starosta).get_by_role("button", name="Создать группу").click()
        expect(starosta.locator("#page-title")).to_have_text("Группа")
        starosta.get_by_role("button", name="Пригласить").click()
        self.sheet(starosta).get_by_role("button", name="Новый код").click()
        code = self.sheet(starosta).locator(".invite-value").input_value()
        self.assertRegex(code, r"^[A-Z0-9]+-[A-Z0-9]{4}-[A-Z0-9]{4}$")
        self.sheet(starosta).get_by_role("button", name="Закрыть").last.click()

        student = self.page_for("student")
        student.goto(f"{self.origin}/#/groups")
        student.get_by_role("button", name="Вступить").first.click()
        self.sheet(student).locator('[data-g="invite"]').fill(code.lower())
        self.sheet(student).get_by_role("button", name="Вступить").click()
        subscribe = self.sheet(student)
        expect(subscribe.locator("h2")).to_have_text("Подключить из группы")
        expect(subscribe.get_by_text("Контрольные и квизы")).to_be_visible()
        subscribe.get_by_role("button", name="Сохранить").click()

        # The starosta publishes a control work: REQUIRED + CRITICAL by default.
        starosta.reload()
        starosta.get_by_role("button", name="Событие").click()
        form = self.sheet(starosta)
        expect(form.get_by_text("Это изменение увидят все участники группы «ПИ-261».")).to_be_visible()
        form.locator('[data-g="title"]').fill("Контрольная работа №2")
        start = (datetime.now(MOSCOW) + timedelta(days=3)).replace(hour=12, minute=10, second=0, microsecond=0)
        form.locator('[data-g="start"]').fill(start.strftime("%Y-%m-%dT%H:%M"))
        form.locator('[data-g="end"]').fill((start + timedelta(minutes=80)).strftime("%Y-%m-%dT%H:%M"))
        form.locator('[data-g="location"]').fill("R201")
        form.get_by_role("button", name="Опубликовать для группы").click()
        expect(starosta.locator("dialog[open]")).to_have_count(0)
        self.shot(starosta, "starosta-group")

        # The student finds it in «Дела» next to their own things.
        student.goto(f"{self.origin}/#/tasks")
        student.reload()  # the user pulls to refresh
        row = student.locator('[data-kind="SHARED_EVENT"]').first
        expect(row).to_contain_text("Контрольная работа №2")
        expect(row).to_contain_text("Критическое")
        expect(row).to_contain_text("ПИ-261")
        self.shot(student, "student-agenda")
        actions = self.actions_of(student, row)
        expect(actions.locator(".menu-section")).to_have_text(["Для меня"])  # a member has no «Для группы» actions
        actions.get_by_role("button", name="Изменить личную критичность").click()
        self.sheet(student).get_by_role("button", name="Важное").click()
        expect(row).to_contain_text("Важное · для меня")  # shown at once, before the server answers

        # A private preparation task, planned like any other task.
        self.actions_of(student, row).get_by_role("button", name="Создать подготовку").click()
        self.sheet(student).get_by_role("button", name="Создать").click()
        expect(student.locator('[data-kind="TASK"]').first).to_contain_text("Подготовиться: Контрольная работа №2")
        student.wait_for_function("() => !Object.entries(localStorage).filter(([k]) => k.startsWith('seos.ops.'))"
                                  ".flatMap(([, v]) => JSON.parse(v)).some((x) => x.state === 'PENDING')", timeout=15000)

        # The starosta's sheet separates the two kinds of change, and sees nothing private.
        starosta.goto(f"{self.origin}/#/tasks")
        starosta.reload()
        own = starosta.locator('[data-kind="SHARED_EVENT"]').first
        expect(own).to_contain_text("Критическое")  # the student's override is theirs only
        self.actions_of(starosta, own)
        expect(self.sheet(starosta).locator(".menu-section")).to_have_text(["Для меня", "Для группы — увидят все участники"])
        expect(self.sheet(starosta).get_by_role("button", name="Отменить для группы")).to_be_visible()
        self.shot(starosta, "starosta-actions")
        self.assertNotIn("Подготовиться", starosta.content())
