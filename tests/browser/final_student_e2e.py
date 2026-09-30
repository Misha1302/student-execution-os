"""Final production-like student journey on one real server and one real browser.

register → import the university calendar → classes appear in Today → capture a task, a
note and an explicit reminder → change one class personally → the university feed
changes → the personal change survives → offline edits → reconnect (exactly once) →
a study group shares a class → an MCP client reads and writes with a scoped grant and is
refused outside it → the assistant answers (local parser, labelled as such) → the
reminder is delivered by the real engine → backup → clean restore serves the same state.

Automated end to end; the Android part is covered by the CI device job (R10).
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import uvicorn
from playwright.sync_api import expect, sync_playwright

from student_execution_os.reliability import SQLiteDataLifecycle
from student_execution_os.reminders import ReminderEngine
from student_execution_os.reminders.push import PushDispatcher, SendResult
from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig

CHROMIUM = os.environ.get("CHROMIUM_PATH") or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None)
FIXTURE = Path("tests/fixtures/academic_schedule_realistic.ics").resolve()
NOW = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)  # Monday 09:00 Moscow


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Phone:
    name, configured = "fake-fcm", True

    def __init__(self):
        self.sent = []

    def send(self, token, message, *, data_only=False):
        self.sent.append(dict(message))
        return SendResult(True, f"fcm-{len(self.sent)}")


class FinalStudentJourneyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = str(Path(cls.temp.name) / "final.sqlite")
        cls.clock = {"now": NOW}
        port = _free_port()
        cls.server = uvicorn.Server(uvicorn.Config(
            create_app(cls.database, auth=AuthConfig(password_scrypt_n=2**10), now=lambda: cls.clock["now"]),
            host="127.0.0.1", port=port, log_level="warning"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        for _ in range(100):
            if cls.server.started:
                break
            time.sleep(0.05)
        cls.origin = f"http://127.0.0.1:{port}"
        cls.http = httpx.Client(base_url=cls.origin, trust_env=False, timeout=15)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, executable_path=CHROMIUM)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()
        cls.http.close()
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.temp.cleanup()

    def rows(self, sql, *args):
        with closing(sqlite3.connect(self.database)) as conn:
            return conn.execute(sql, args).fetchall()

    @staticmethod
    def ready(page, view):
        page.wait_for_selector(f'#workspace[data-view="{view}"][data-view-state="ready"]')

    def capture(self, page, text, kind=None):
        page.locator(".fab").click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("#capture-text").fill(text)
        if kind:
            sheet.locator('[data-kind-switch] summary').click()
            sheet.locator(f'[data-chip-group="capture-kind"] [data-value="{kind}"]').click()
        sheet.locator("[data-create]").click()
        page.locator("dialog.sheet[open]").wait_for(state="detached")

    def pending(self, page):
        return page.evaluate("Object.entries(localStorage).filter(([k]) => k.startsWith('seos.ops.'))"
                             ".flatMap(([, v]) => JSON.parse(v)).filter((x) => x.state === 'PENDING').length")

    def wait_synced(self, page):
        for _ in range(150):
            if self.pending(page) == 0:
                return
            page.wait_for_timeout(100)
        queued = page.evaluate("Object.entries(localStorage).filter(([k]) => k.startsWith('seos.ops.'))"
                               ".flatMap(([, v]) => JSON.parse(v))")
        self.fail(f"queued changes were not sent: {json.dumps(queued, ensure_ascii=False)[:1500]}")

    def test_student_journey(self):
        context = self.browser.new_context(viewport={"width": 390, "height": 844}, timezone_id="Europe/Moscow",
                                           reduced_motion="reduce")
        self.addCleanup(context.close)
        context.add_init_script("try { localStorage.setItem('seos.locale', 'ru') } catch (e) {}")
        page = context.new_page()
        page.clock.set_fixed_time(NOW)
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        # 1. Register in the app.
        page.goto(self.origin)
        self.ready(page, "welcome")
        page.fill("input[name=login]", "student-final")
        page.fill("input[name=password]", "correct horse battery")
        page.fill("input[name=password2]", "correct horse battery")
        page.locator("button[type=submit]").click()
        self.ready(page, "today")
        # A first sign-up opens Capture right away ("start with the first task"); later.
        page.locator("dialog.sheet[open]").wait_for()
        page.keyboard.press("Escape")
        page.locator("dialog.sheet[open]").wait_for(state="detached")
        token = page.evaluate("localStorage.getItem('seos.token')")
        me = {"Authorization": f"Bearer {token}"}

        # 2. Import the university calendar; classes appear in Today.
        page.goto(f"{self.origin}/#/settings")
        self.ready(page, "settings")
        page.locator("[data-academic-file]").set_input_files(str(FIXTURE))
        page.locator('[data-action="academic-import"]').click()
        page.get_by_text("academic_schedule_realistic.ics", exact=True).wait_for()
        page.goto(f"{self.origin}/#/today")
        self.ready(page, "today")
        self.assertIn("Algorithms and Data Structures", page.locator("#workspace").inner_text())

        # 3. Capture a task, a note and an explicit reminder.
        self.capture(page, "Сдать лабораторную по физике до пятницы, займёт 2 часа")
        self.capture(page, "Идея для курсовой: расписание как граф", kind="NOTE")
        self.capture(page, "Завтра в 10:00 взять зачётку", kind="REMINDER")
        self.wait_synced(page)
        tasks = self.http.get("/api/v1/tasks", headers=me).json()
        self.assertTrue(any("лабораторную" in t["title"].lower() for t in tasks), tasks)
        self.assertTrue(any("курсовой" in n.get("content", "") for n in self.http.get("/api/v1/notes", headers=me).json()))
        reminders = self.http.get("/api/v1/reminders", headers=me).json()
        self.assertEqual(len(reminders), 1, reminders)

        # 4. Personal change of one imported class, then the university feed changes.
        occurrences = [o for o in self.http.get("/api/v1/calendar", headers=me).json()["occurrences"]
                       if o["template_id"].startswith("src-series-") and not o["cancelled"]]
        target = occurrences[1]
        change = self.http.post("/api/v1/sync", headers=me, json={"operations": [{
            "op_id": "final-personal-1", "type": "series.occurrence.update", "entity_id": target["template_id"],
            "payload": {"template_id": target["template_id"], "original_recurrence_id": target["original_recurrence_id"],
                        "note": "сесть в первый ряд"}}]}).json()["results"][0]
        self.assertEqual(change["status"], "APPLIED")
        changed_feed = FIXTURE.read_text(encoding="utf-8").replace("SEQUENCE:0", "SEQUENCE:9")
        refreshed = self.http.post("/api/v1/settings/academic-schedule/import", headers={**me, "X-Calendar-Name": "HSE"},
                                   content=changed_feed.encode())
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        after = {(o["template_id"], o["original_recurrence_id"]): o
                 for o in self.http.get("/api/v1/calendar", headers=me).json()["occurrences"]}
        self.assertEqual(after[(target["template_id"], target["original_recurrence_id"])]["note"], "сесть в первый ряд")
        self.assertEqual(len(self.rows("SELECT id FROM recurring_templates")), 1)  # no duplicate classes

        # 5. Offline edits, then reconnect: applied exactly once.
        context.set_offline(True)
        self.capture(page, "Купить тетради, займёт 20 минут")
        self.assertGreater(self.pending(page), 0)
        context.set_offline(False)
        page.evaluate("window.dispatchEvent(new Event('online'))")
        self.wait_synced(page)
        self.assertEqual(len(self.rows("SELECT id FROM obligations WHERE title LIKE 'Купить тетради%'")), 1)

        # 6. A study group shares a class with a classmate.
        classmate = self.http.post("/api/v1/auth/register", json={"login": "classmate", "password": "correct horse"}).json()
        mate = {"Authorization": f"Bearer {classmate['token']}"}
        self.http.post("/api/v1/groups", headers=me, json={"id": "final-group-01", "name": "БИ-24"})
        code = self.http.post("/api/v1/groups/final-group-01/invitations", headers=me, json={}).json()["code"]
        self.http.post("/api/v1/groups/join", headers=mate, json={"code": code})
        published = self.http.put("/api/v1/groups/final-group-01/schedule/final-g-exam1", headers=me, json={
            "kind": "EVENT", "expected_revision": 0, "item": {
                "title": "Контрольная", "starts_at": "2026-10-05T11:00:00+03:00", "ends_at": "2026-10-05T12:30:00+03:00"}})
        self.assertEqual(published.status_code, 200, published.text)
        mate_view = json.dumps(self.http.get("/api/v1/events", headers=mate).json(), ensure_ascii=False)
        self.assertIn("Контрольная", mate_view)
        self.assertNotIn("лабораторную", mate_view.lower())

        # 7. An MCP client with a scoped grant reads, writes, and is refused outside its scope.
        grant = self.http.post("/api/v1/settings/capabilities", headers=me,
                               json={"label": "Codex", "scopes": ["tasks:read", "tasks:write"]}).json()
        mcp = {"Authorization": f"Bearer {grant['token']}"}
        listed = self.http.post("/mcp", headers=mcp, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                            "params": {"name": "list_tasks"}}).json()
        self.assertFalse(listed["result"]["isError"])
        created = self.http.post("/mcp", headers=mcp, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                                             "params": {"name": "create_task", "arguments": {
                                                                 "op_id": "final-mcp-0001", "title": "Задача от агента"}}}).json()
        self.assertEqual(created["result"]["structuredContent"]["results"][0]["status"], "APPLIED")
        denied = self.http.post("/mcp", headers=mcp, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                            "params": {"name": "create_note", "arguments": {
                                                                "op_id": "final-mcp-0002", "content": "x"}}}).json()
        self.assertTrue(denied["result"]["isError"])
        page.goto(f"{self.origin}/#/tasks")
        self.ready(page, "tasks")
        page.locator('#refresh-button').click()
        expect(page.locator("#workspace")).to_contain_text("Задача от агента")

        # 8. The assistant answers; without a configured model it is the local parser, labelled.
        answer = self.http.post("/api/v1/assistant/interpret", headers=me, json={"text": "подготовить доклад к среде"}).json()
        self.assertEqual((answer["engine"], answer["credential_source"]), ("LOCAL", "NONE"))

        # 9. The explicit reminder is delivered by the real engine at its moment.
        self.http.post("/api/v1/mobile/devices", headers=me, json={"token": "phone", "capabilities": ["reminder-actions-v1"]})
        phone = Phone()
        account = self.rows("SELECT account_id FROM auth_users WHERE login='student-final'")[0][0]
        remind_at = datetime.fromisoformat(reminders[0]["remind_at"])
        for at in (remind_at - timedelta(minutes=5), remind_at, remind_at + timedelta(minutes=5)):
            ReminderEngine(self.database).tick(account, at)
            PushDispatcher(self.database, phone).run_once(at, "final")
        self.assertEqual(sum("зачётку" in json.dumps(m, ensure_ascii=False) for m in phone.sent), 1)

        # 10. Backup → clean restore → a fresh server serves the same state.
        backup = Path(self.temp.name) / "backup" / "botay.sqlite"
        SQLiteDataLifecycle(self.database).create_backup(backup)
        restored = Path(self.temp.name) / "restored" / "botay.sqlite"
        result = SQLiteDataLifecycle.restore_backup(backup, restored)
        self.assertEqual((result.integrity_check, result.foreign_key_violations), ("ok", 0))
        port = _free_port()
        second = uvicorn.Server(uvicorn.Config(create_app(str(restored), auth=AuthConfig(password_scrypt_n=2**10),
                                                          now=lambda: self.clock["now"]),
                                               host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=second.run, daemon=True)
        thread.start()
        for _ in range(100):
            if second.started:
                break
            time.sleep(0.05)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as fresh:
                for path in ("/api/v1/tasks", "/api/v1/notes", "/api/v1/reminders", "/api/v1/groups"):
                    self.assertEqual(fresh.get(path, headers=me).json(), self.http.get(path, headers=me).json(), path)
        finally:
            second.should_exit = True
            thread.join(timeout=10)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
