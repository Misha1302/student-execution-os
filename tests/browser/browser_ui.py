from __future__ import annotations

import copy
import json
import mimetypes
import os
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from student_execution_os.web.queries import UiService
from tests.ui_fixture import ACCOUNT, NOW, seed_ui_database

CHROMIUM = os.environ.get("CHROMIUM_PATH") or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None)
ORIGIN = "https://student-execution.test"
STATIC = Path("src/student_execution_os/web/static")


class BrowserUiTest(unittest.TestCase):
    """Chromium checks of the mobile-first client against server-shaped fixture payloads.

    Static assets are served from disk exactly as the Python host (and the Android bundle)
    lay them out; API calls are answered from a real UiService over the seeded database.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, executable_path=CHROMIUM)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "ui.sqlite")
        seed_ui_database(self.db)
        service = UiService(self.db, account_id=ACCOUNT, principal_id="browser-user", now=lambda: NOW)
        today = service.today()
        self.health = {"status": "ok", "service": "student-execution-os", "auth_mode": "bound", "registration_open": False}
        self.responses = {
            "/api/v1/today": today,
            "/api/v1/plan/current": today["plan"],
            "/api/v1/tasks": service.tasks(),
            "/api/v1/events": service.events(),
            "/api/v1/calendar": service.calendar(),
            "/api/v1/notifications": service.notifications(),
            "/api/v1/evidence": service.evidence(),
            "/api/v1/places": service.places(),
            "/api/v1/settings/diagnostics": service.diagnostics(),
            "/api/v1/account/deletion-policy": service.account_deletion_policy(),
            "/api/v1/ask/capabilities": {
                "live_llm_provider": False,
                "explanations": True,
                "destructive_action_preview": True,
                "message": "No live LLM provider is configured in this release.",
            },
        }
        self.posts: list[tuple[str, dict]] = []
        self.overrides: dict[tuple[str, str], tuple[int, dict]] = {}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    # ---- harness --------------------------------------------------------------------

    def _handler(self, route):
        request = route.request
        path = request.url.removeprefix(ORIGIN).split("#")[0] or "/"

        def reply(status, body):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

        if path == "/" or not path.startswith(("/api/", "/assets/")):
            route.fulfill(status=200, content_type="text/html", body=(STATIC / "index.html").read_text())
            return
        if path.startswith("/assets/"):
            file = STATIC / path.removeprefix("/assets/")
            if not file.is_file():
                route.fulfill(status=404, body="")
                return
            ctype = "text/javascript" if file.suffix == ".js" else mimetypes.guess_type(file.name)[0] or "text/plain"
            route.fulfill(status=200, content_type=ctype, body=file.read_text())
            return
        payload = json.loads(request.post_data or "{}") if request.method != "GET" else {}
        if request.method != "GET":
            self.posts.append((path, payload))
        override = self.overrides.get((request.method, path))
        if override:
            reply(*override)
            return
        if path == "/api/v1/health":
            reply(200, self.health)
        elif path == "/api/v1/agent/cancel/preview":
            target = next(x for x in self.responses["/api/v1/tasks"] + self.responses["/api/v1/events"] if x["id"] == payload["obligation_id"])
            reply(200, {
                "intent_id": "visual-fixture-intent", "command": "CANCEL_OBLIGATION",
                "target_entity_id": target["id"], "target_title": target["title"],
                "expected_version": payload["expected_version"], "requires_confirmation": True,
                "scope": "one obligation", "effect": "Lifecycle becomes CANCELLED.",
            })
        elif path in self.responses and request.method == "GET":
            reply(200, self.responses[path])
        else:
            reply(404, {"error": {"code": "NOT_FOUND", "message": "not mocked"}})

    def _open(self, *, width=390, height=844, locale="en", hash_="") -> object:
        page = self.browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce")
        self.page_errors: list[str] = []
        page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        page.add_init_script(f"try {{ localStorage.setItem('seos.locale', '{locale}') }} catch (e) {{}}")
        page.route(ORIGIN + "/**", self._handler)
        page.goto(f"{ORIGIN}/{hash_}")
        return page

    @staticmethod
    def _ready(page, view: str) -> None:
        page.wait_for_selector(f'#workspace[data-view="{view}"][data-view-state="ready"]')

    @staticmethod
    def _text(page) -> str:
        return page.locator("#workspace").inner_text()

    def _go(self, page, view: str) -> None:
        page.evaluate(f"location.hash = '#/{view}'")
        self._ready(page, view)

    def _screenshot(self, page, name: str) -> None:
        root = os.environ.get("UI_QA_SCREENSHOT_DIR")
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(root) / name), full_page=True)

    def _assert_no_horizontal_scroll(self, page, width: int) -> None:
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    # ---- tests ---------------------------------------------------------------------

    def test_phone_today_plan_and_tab_navigation(self):
        page = self._open()
        self._ready(page, "today")
        self.assertTrue(page.locator(".tabbar").is_visible())
        self.assertTrue(page.locator(".fab").is_visible())
        today = self._text(page)
        status = self.responses["/api/v1/today"]["plan"]["feasibility_status"]
        self.assertIn(status, today)
        self.assertIn("leave by", today.lower())
        self.assertIn("HOME", today)
        self.assertIn("HSE", today)
        self._assert_no_horizontal_scroll(page, 390)
        self._screenshot(page, "mobile-today.png")

        page.locator('.tabbar [data-nav="plan"]').click()
        self._ready(page, "plan")
        plan = self._text(page)
        self.assertIn("Canonical", plan)
        self.assertIn("Derived", plan)
        self.assertTrue(page.locator(".mobile-agenda").is_visible())
        # Canonical facts and derived projections keep separate visual classes.
        self.assertGreater(page.locator(".agenda-item.canonical").count(), 0)
        self.assertGreater(page.locator(".agenda-item.travel").count(), 0)
        self.assertGreater(page.locator(".agenda-item.buffer").count(), 0)
        self.assertIn("Compilers lecture", plan)
        self.assertIn("Booked train", plan)
        page.locator(".agenda-item.travel").first.click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.wait_for()
        self.assertIn("travel transition", sheet.inner_text().lower())
        self.assertIn("cannot be edited directly", sheet.inner_text())
        page.keyboard.press("Escape")
        self._assert_no_horizontal_scroll(page, 390)
        self._screenshot(page, "mobile-plan.png")

        # Dense agenda rows never overlap vertically.
        boxes = page.eval_on_selector_all(".agenda-item", "els => els.map(e => { const r = e.getBoundingClientRect(); return [r.top, r.bottom]; })")
        for (top_a, bottom_a), (top_b, _) in zip(boxes, boxes[1:]):
            self.assertLessEqual(bottom_a, top_b + 0.5)

        page.locator('.tabbar [data-nav="more"]').click()
        self._ready(page, "more")
        page.locator('.menu-row[data-nav="calendar"]').click()
        self._ready(page, "calendar")
        calendar = self._text(page)
        self.assertIn("Daily planning review", calendar)
        self.assertIn("Canonical rule", calendar)
        self.assertIn("Derived occurrence", calendar)
        self.assertTrue(page.locator("#back-button").is_visible())
        self.assertEqual(self.page_errors, [])
        page.close()

    def test_feasibility_states_are_distinct(self):
        for status, css_class, expected in (
            ("FEASIBLE", "status-feasible", "concrete legal witness"),
            ("UNKNOWN", "status-unknown", "cannot prove feasibility"),
            ("INFEASIBLE", "status-infeasible", "hard contradiction"),
        ):
            original = self.responses["/api/v1/today"]
            scenario = copy.deepcopy(original)
            scenario["plan"]["feasibility_status"] = status
            scenario["plan"]["explanations"] = [] if status == "FEASIBLE" else ["HARD_CUTOFF_PASSED"]
            self.responses["/api/v1/today"] = scenario
            page = self._open(width=1024, height=800)
            self._ready(page, "today")
            hero = page.locator(".hero-status").first
            self.assertIn(status, hero.inner_text())
            self.assertIn(css_class, hero.get_attribute("class"))
            self.assertIn(expected, hero.inner_text().lower())
            page.close()
            self.responses["/api/v1/today"] = original

    def test_task_detail_provenance_progress_and_version_conflict(self):
        page = self._open()
        self._ready(page, "today")
        page.locator('.tabbar [data-nav="tasks"]').click()
        self._ready(page, "tasks")
        self.assertIn("Discrete homework", self._text(page))
        page.locator('.task-card', has_text="Algorithms worksheet").click()
        self._ready(page, "task")
        detail = self._text(page)
        self.assertIn("Overridden", detail)
        page.locator("#back-button").click()
        self._ready(page, "tasks")

        page.locator('.task-card', has_text="Compiler report").click()
        self._ready(page, "task")
        self.assertIn("Conflict", self._text(page))
        task = next(t for t in self.responses["/api/v1/tasks"] if t["id"] == "conflict-task")
        self.overrides[("PATCH", "/api/v1/tasks/conflict-task")] = (
            409, {"error": {"code": "VERSION_CONFLICT", "message": "stale", "retryable": False}},
        )
        page.locator('[data-action="detail-progress"]').click()
        page.locator("dialog.sheet[open] [data-save]").click()
        toast = page.locator(".toast.error")
        toast.wait_for()
        self.assertIn("another device", toast.inner_text())
        path, payload = self.posts[-1]
        self.assertEqual(path, "/api/v1/tasks/conflict-task")
        self.assertEqual(payload["expected_version"], task["version"])
        self._ready(page, "task")
        page.close()

    def test_settings_notifications_evidence_places_and_assistant(self):
        page = self._open(width=1280, height=900)
        self._ready(page, "today")
        # Wide screens show every section in the side rail.
        self.assertTrue(page.locator('.tabbar .tab[data-nav="settings"]').is_visible())
        self.assertFalse(page.locator('.tabbar .tab[data-nav="more"]').is_visible())

        page.locator('.tabbar [data-nav="settings"]').click()
        self._ready(page, "settings")
        settings = self._text(page)
        self.assertIn("includes private account data", settings)
        self.assertIn("30 days", settings)
        self.assertTrue(page.get_by_role("button", name="Download account export").is_visible())
        page.locator('[data-action="account-delete-preview"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.wait_for()
        text = sheet.inner_text()
        self.assertIn("Delete account permanently", text)
        self.assertIn("Type the exact account id", text)
        self.assertIn("not a recoverable copy", text)
        page.get_by_role("button", name="Keep account", exact=True).click()

        page.locator('.tabbar [data-nav="notifications"]').click()
        self._ready(page, "notifications")
        self.assertIn("Pending", self._text(page))
        page.locator('[data-action="snooze-notification"]').first.click()
        self.assertIn("Only the reminder time changes", page.locator("dialog.sheet[open]").inner_text())
        page.keyboard.press("Escape")

        page.locator('.tabbar [data-nav="evidence"]').click()
        self._ready(page, "evidence")
        evidence = self._text(page)
        self.assertIn("Stale", evidence)
        self.assertIn("Conflict", evidence)
        self.assertIn("Overridden", evidence)

        page.locator('.tabbar [data-nav="places"]').click()
        self._ready(page, "places")
        places = self._text(page)
        self.assertIn("HOME", places)
        self.assertIn("HSE", places)
        self.assertNotIn("Secret exact address", page.content())
        self.assertNotIn("55.75", page.content())

        page.locator('.tabbar [data-nav="assistant"]').click()
        self._ready(page, "assistant")
        page.locator('[data-action="agent-preview"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.wait_for()
        modal = sheet.inner_text()
        self.assertIn("Confirm destructive action", modal)
        self.assertIn("Scope: one obligation", modal)
        self.assertIn("Source/imported text is not authorization", modal)
        self._screenshot(page, "desktop-agent-confirm.png")
        self.assertEqual(self.page_errors, [])
        page.close()

    def test_long_titles_and_empty_states(self):
        long_tasks = copy.deepcopy(self.responses["/api/v1/tasks"])
        long_tasks[0]["title"] = "Very long obligation title " * 12
        self.responses["/api/v1/tasks"] = long_tasks
        page = self._open(width=390, height=844, hash_="#/tasks")
        self._ready(page, "tasks")
        self._assert_no_horizontal_scroll(page, 390)
        page.close()

        self.responses["/api/v1/tasks"] = []
        page = self._open(hash_="#/tasks")
        self._ready(page, "tasks")
        self.assertIn("No obligations yet", self._text(page))
        page.close()

    def test_compose_task_sheet_posts_canonical_payload(self):
        page = self._open()
        self._ready(page, "today")
        self.overrides[("POST", "/api/v1/tasks")] = (201, {"id": "new", "title": "Essay", "version": 1})
        page.locator(".fab").click()
        page.locator('[data-choice="task"]').click()
        page.fill('[data-f="title"]', "Essay")
        page.locator('[data-chip-group="effort"] [data-value="90"]').click()
        page.locator('[data-chip-group="deadline"] [data-value="ABSENT"]').click()
        page.locator("dialog.sheet[open] [data-save]").click()
        page.locator(".toast").first.wait_for()
        path, payload = self.posts[-1]
        self.assertEqual(path, "/api/v1/tasks")
        self.assertEqual(payload["title"], "Essay")
        self.assertEqual(payload["estimated_total_effort_minutes"], 90)
        self.assertEqual(payload["actual_cutoff"], {"state": "ABSENT"})
        self.assertNotIn("account_id", payload)
        page.close()

    def test_session_mode_login_flow_and_russian_locale(self):
        self.health = {"status": "ok", "service": "student-execution-os", "auth_mode": "session", "registration_open": True}
        self.overrides[("POST", "/api/v1/auth/login")] = (200, {
            "token": "fixture-token", "expires_at": "2026-10-21T09:00:00+00:00",
            "user": {"login": "student", "account_id": ACCOUNT},
        })
        seen_auth: list[str | None] = []
        page = self._open(locale="ru")
        page.on("request", lambda r: seen_auth.append(r.headers.get("authorization")) if "/api/v1/today" in r.url else None)
        self._ready(page, "welcome")
        self.assertFalse(page.locator(".tabbar").is_visible())
        self.assertIn("Войти", page.locator("#workspace").inner_text())
        page.fill("input[name=login]", "student")
        page.fill("input[name=password]", "correct horse")
        page.locator("button[type=submit]").click()
        self._ready(page, "today")
        self.assertIn("Bearer fixture-token", seen_auth)
        self.assertIn("Сегодня", page.locator(".tabbar").inner_text())
        self._screenshot(page, "mobile-today-ru.png")

        # Switching the language in Settings relabels the shell immediately.
        self._go(page, "settings")
        page.locator('[data-chip-group="locale"] [data-value="en"]').click()
        self._ready(page, "settings")
        self.assertIn("Today", page.locator(".tabbar").inner_text())
        page.close()


if __name__ == "__main__":
    unittest.main()
