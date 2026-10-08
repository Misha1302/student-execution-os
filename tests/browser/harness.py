"""Shared harness for end-to-end browser tests against a real uvicorn + SQLite server.

The static shell is served from disk (as the Android app bundles it) and every /api/
call goes to the real server unless the test switches the "network" off. Waiting must
go through Playwright (``page.wait_for_timeout``): routed requests are continued by
this thread, so a plain sleep would stall them.
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
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app

CHROMIUM = os.environ.get("CHROMIUM_PATH") or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None)
STATIC = Path("src/student_execution_os/web/static")
ACCOUNT = "e2e-account"
MOSCOW = ZoneInfo("Europe/Moscow")
# Tuesday 08:00 in Moscow: today's 09:00 dose is still ahead.
NOW = datetime(2026, 10, 6, 8, 0, tzinfo=MOSCOW)
PENDING_JS = ("Object.entries(localStorage).filter(([k]) => k.startsWith('seos.ops.'))"
              ".flatMap(([, v]) => JSON.parse(v)).filter((x) => x.state === 'PENDING').length")


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RealServerTestCase(unittest.TestCase):
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
        # Every API request with whether it was let through: evidence when an
        # "offline" assertion fails (which request reached the server, and when).
        self.api_log: list[tuple[float, str, str, bool]] = []

    def tearDown(self) -> None:
        self.context.close()

    def _route(self, route):
        path = route.request.url.removeprefix(self.origin).split("#")[0] or "/"
        if path.startswith("/api/"):
            self.api_log.append((time.monotonic(), route.request.method, path, self.online))
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

    # A frozen page clock makes "acked after this response was fetched" undecidable
    # (equal timestamps); tests that replay offline queues across restarts let the page
    # clock run, starting at the scenario's moment.
    freeze_page_clock = True

    def _page(self, hash_: str = "#/today"):
        page = self.context.new_page()
        if self.freeze_page_clock:
            page.clock.set_fixed_time(self.clock["now"])
        else:
            page.clock.install(time=self.clock["now"])
        page.on("pageerror", lambda e: self.errors.append(str(e)))
        page.goto(f"{self.origin}/{hash_}")
        return page

    def _ready(self, page, view: str) -> None:
        try:
            page.wait_for_selector(f'#workspace[data-view="{view}"][data-view-state="ready"]')
        except PlaywrightTimeoutError:
            # What the screen was doing instead: its state, its text, page errors and the
            # last API requests (method, path, let through, seconds before now).
            state = page.evaluate("""() => { const w = document.querySelector('#workspace');
                return w ? { view: w.dataset.view, state: w.dataset.viewState, text: w.innerText.slice(0, 300) } : null; }""")
            now = time.monotonic()
            recent = [(method, path, online, round(now - at, 1)) for at, method, path, online in self.api_log[-12:]]
            self.fail(f"#{view} never became ready: {state}; page errors {self.errors}; last API requests {recent}")

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
