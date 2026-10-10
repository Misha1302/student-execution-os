"""Phone-width layout: no screen may scroll sideways, and every action stays on screen.

Chromium against a real uvicorn + SQLite server seeded with the UI fixture. The
check is the page itself — document scrollWidth equals clientWidth — not a hidden
overflow: a global `overflow-x: hidden` would pass the first assertion but hide
buttons, which is why each action's bounding rect is checked against the viewport.
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
from pathlib import Path
from urllib.request import Request, urlopen

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

WIDE_FONT_JS = """document.addEventListener('DOMContentLoaded', () => {
  const style = document.createElement('style');
  style.textContent = "* { font-family: 'DejaVu Sans', Verdana, sans-serif !important; }";
  document.head.append(style);
});"""

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

# A <dialog> scrolls: an answer row wider than the phone does not widen the page, it lets a
# sideways swipe slide the whole sheet off the screen. So the sheet's own scroll width and
# every element's rect against the sheet are checked, deliberate scrollers excepted.
SHEET_JS = """() => {
  const dialog = [...document.querySelectorAll('dialog[open]')].pop();
  const frame = dialog.querySelector('.sheet-frame').getBoundingClientRect();
  const inScroller = (el) => {
    for (let a = el.parentElement; a && a !== dialog; a = a.parentElement) {
      if (['auto', 'scroll'].includes(getComputedStyle(a).overflowX)) return true;
    }
    return false;
  };
  const outside = [...dialog.querySelectorAll('.sheet-frame *')].filter((el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && (r.left < frame.left - 0.5 || r.right > frame.right + 0.5) && !inScroller(el);
  }).map((el) => el.textContent.trim());
  return {scroll: dialog.scrollWidth, client: dialog.clientWidth, outside, frameLeft: frame.left,
          frameWidth: frame.width, answers: [...dialog.querySelectorAll('.sheet-actions button')].length};
}"""
# A long title, as on the phone where this was found; alarm delivery shows every field.
WIDE_REMINDER = {"title": "Пройти онлайн-регистрацию на рейс и распечатать посадочный",
                 "remind_at": "2030-01-15T09:00:00+00:00", "delivery": "ALARM", "wake_check": True}


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
        operation = {"op_id": "responsive-reminder-0001", "type": "reminder.create", "entity_id": "rem-wide", "payload": WIDE_REMINDER}
        request = Request(f"{cls.origin}/api/v1/sync", data=json.dumps({"operations": [operation]}).encode(),
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            assert json.load(response)["results"][0]["status"] == "APPLIED"
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
                                           timezone_id="Europe/Moscow", reduced_motion="reduce", bypass_csp=True)
        context.add_init_script(f"try {{ localStorage.setItem('seos.locale', '{locale}') }} catch (e) {{}}")
        # Measure with a wide font (CI's Chromium falls back to DejaVu Sans; phones vary), so a
        # label that only just fits on this machine cannot pass here and fail elsewhere.
        context.add_init_script(WIDE_FONT_JS)
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

    def test_sheet_answers_stay_inside_the_sheet_on_every_phone_width(self) -> None:
        # (screen, what opens the sheet, how many answers its footer keeps)
        sheets = (("reminder/rem-wide", "[data-action=rem-edit]", 3),
                  ("places", "[data-action=place-edit]", 3))
        for locale in LOCALES:
            for width in WIDTHS:
                page = self._page(width, locale)
                for route, opener, answers in sheets:
                    with self.subTest(locale=locale, width=width, sheet=route):
                        page.goto(f"{self.origin}/#/{route}")
                        page.locator(opener).first.click(timeout=15000)
                        page.wait_for_selector("dialog[open] .sheet-actions button")
                        result = page.evaluate(SHEET_JS)
                        self.assertEqual(result["answers"], answers)
                        self.assertEqual(result["scroll"], result["client"])
                        self.assertEqual(result["outside"], [])
                        self.assertEqual((result["frameLeft"], result["frameWidth"]), (0, width))
                        page.keyboard.press("Escape")

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
