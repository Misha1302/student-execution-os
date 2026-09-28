"""Phone-width layout: no screen may scroll sideways, and every action stays on screen.

Chromium against a real uvicorn + SQLite server seeded with the UI fixture. The
check is the page itself — document scrollWidth equals clientWidth — not a hidden
overflow: a global `overflow-x: hidden` would pass the first assertion but hide
buttons, which is why each action's bounding rect is checked against the viewport.
"""
from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

from student_execution_os.web.app import create_app
from tests.ui_fixture import ACCOUNT, seed_ui_database

CHROMIUM = os.environ.get("CHROMIUM_PATH") or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None)
WIDTHS = (320, 360, 375, 390, 412)
LOCALES = ("ru", "en")
TASKS = ("discrete", "conflict-task", "override-task")
ROUTES = ("today", "plan", "tasks", "calendar", "notes", "more", "settings", "reminders", "routines",
          "projects", "reflection", "evidence", "notifications", "places")

# A button inside a deliberate horizontal scroller (day strip, tab chips) is reachable by
# swiping; one clipped by overflow: hidden is not, so only auto/scroll ancestors excuse it.
MEASURE_JS = """(selector) => {
  const d = document.documentElement;
  const inScroller = (el) => {
    for (let a = el.parentElement; a && a !== d; a = a.parentElement) {
      if (['auto', 'scroll'].includes(getComputedStyle(a).overflowX)) return true;
    }
    return false;
  };
  const offscreen = [...document.querySelectorAll(selector)].filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && (r.left < -0.5 || r.right > d.clientWidth + 0.5) && !inScroller(el);
  }).map((el) => el.textContent.trim());
  return {scroll: d.scrollWidth, client: d.clientWidth, offscreen,
          count: document.querySelectorAll(selector).length};
}"""


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ResponsiveLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        db = str(Path(cls.tmp.name) / "responsive.sqlite")
        seed_ui_database(db)
        port = _free_port()
        config = uvicorn.Config(create_app(db, account_id=ACCOUNT, principal_id="responsive"),
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

    def _page(self, width: int, locale: str):
        context = self.browser.new_context(viewport={"width": width, "height": 800}, is_mobile=True, has_touch=True,
                                           timezone_id="Europe/Moscow", reduced_motion="reduce")
        context.add_init_script(f"try {{ localStorage.setItem('seos.locale', '{locale}') }} catch (e) {{}}")
        self.addCleanup(context.close)
        return context.new_page()

    def test_task_detail_actions_fit_every_phone_width(self) -> None:
        for locale in LOCALES:
            for width in WIDTHS:
                page = self._page(width, locale)
                for task in TASKS:
                    with self.subTest(locale=locale, width=width, task=task):
                        page.goto(f"{self.origin}/#/task/{task}")
                        page.wait_for_selector(".detail-actions button", timeout=15000)
                        result = page.evaluate(MEASURE_JS, ".detail-actions button, .detail-actions a")
                        self.assertGreaterEqual(result["count"], 3)
                        self.assertEqual(result["scroll"], result["client"])
                        self.assertEqual(result["offscreen"], [])

    def test_no_screen_scrolls_sideways_at_320(self) -> None:
        for locale in LOCALES:
            page = self._page(320, locale)
            for route in ROUTES:
                with self.subTest(locale=locale, route=route):
                    page.goto(f"{self.origin}/#/{route}")
                    page.wait_for_selector("main *", timeout=15000)
                    page.wait_for_timeout(400)
                    result = page.evaluate(MEASURE_JS, "main button")
                    self.assertEqual(result["scroll"], result["client"])
                    self.assertEqual(result["offscreen"], [])


if __name__ == "__main__":
    unittest.main()
