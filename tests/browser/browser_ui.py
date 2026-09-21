from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

from student_execution_os.web.queries import UiService
from tests.ui_fixture import ACCOUNT, NOW, seed_ui_database

CHROMIUM = os.environ.get("CHROMIUM_PATH") or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None)
ORIGIN = "https://student-execution.test"


class BrowserUiTest(unittest.TestCase):
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
            "/api/v1/ask/capabilities": {
                "live_llm_provider": False,
                "explanations": True,
                "destructive_action_preview": True,
                "message": "No live LLM provider is configured in this release. Server-owned explanations remain available.",
            },
        }

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _mount_app(self, page) -> None:
        import json
        def handler(route):
            request = route.request
            relative = request.url.removeprefix(ORIGIN) or "/"
            if relative == "/api/v1/agent/cancel/preview":
                payload = json.loads(request.post_data or "{}")
                target = next(x for x in self.responses["/api/v1/tasks"] + self.responses["/api/v1/events"] if x["id"] == payload["obligation_id"])
                body = {
                    "intent_id": "visual-fixture-intent",
                    "command": "CANCEL_OBLIGATION",
                    "target_entity_id": target["id"],
                    "target_title": target["title"],
                    "expected_version": payload["expected_version"],
                    "requires_confirmation": True,
                    "scope": "one obligation",
                    "effect": "Lifecycle becomes CANCELLED; evidence history remains unchanged; current plan becomes stale.",
                }
                route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
                return
            body = self.responses.get(relative)
            if body is None:
                route.fulfill(status=404, content_type="application/json", body='{"error":{"code":"NOT_FOUND"}}')
                return
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
        page.route(ORIGIN + "/**", handler)
        static = Path("src/student_execution_os/web/static")
        html = (static / "index.html").read_text()
        html = html.replace('<link rel="stylesheet" href="/assets/styles.css">', '')
        html = html.replace('<script type="module" src="/assets/app.js"></script>', '')
        html = html.replace('<head>', f'<head><base href="{ORIGIN}/"><style>{(static / "styles.css").read_text()}</style>')
        page.set_content(html, wait_until="domcontentloaded")
        page.evaluate("history.replaceState = () => {}; history.pushState = () => {}")
        page.add_script_tag(content=(static / "app.js").read_text(), type="module")
        page.wait_for_selector('#workspace[data-view="today"][data-view-state="ready"]')

    def _screenshot(self, page, name: str) -> None:
        root = os.environ.get("UI_QA_SCREENSHOT_DIR")
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(root) / name), full_page=True)

    def test_desktop_product_surfaces_and_agent_confirmation(self):
        page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        self._mount_app(page)
        today = page.locator("#workspace").inner_text()
        self.assertIn("FEASIBLE", today)
        self.assertIn("Discrete homework", today)
        self.assertIn("Leave HOME → HSE", today)
        self._screenshot(page, "desktop-today.png")

        page.locator('[data-nav="plan"]').first.click()
        page.wait_for_selector('#workspace[data-view="plan"][data-view-state="ready"]')
        plan_text = page.locator("#workspace").inner_text()
        self.assertIn("Canonical", plan_text)
        self.assertIn("Derived", plan_text)
        self.assertIn("TRAVEL TRANSITION", plan_text.upper())
        self.assertIn("BUFFER", plan_text.upper())

        page.locator('[data-nav="calendar"]').first.click()
        page.wait_for_selector('#workspace[data-view="calendar"][data-view-state="ready"]')
        calendar = page.locator("#workspace").inner_text()
        self.assertIn("Daily planning review", calendar)
        self.assertIn("CANONICAL RULE", calendar)
        self.assertIn("DERIVED OCCURRENCE", calendar)
        self.assertIn("2026-09-21T17:30:00", calendar)

        page.locator('[data-nav="settings"]').first.click()
        page.wait_for_selector('#workspace[data-view="settings"][data-view-state="ready"]')
        settings = page.locator("#workspace").inner_text()
        self.assertIn("Notifications", settings)
        self.assertIn("Latest Safe Start", settings)
        self.assertIn("PENDING", settings)
        page.locator('[data-action="snooze-notification"]').first.click()
        page.locator("#modal[open]").wait_for()
        self.assertIn("Snooze mutates notification workflow state only", page.locator("#modal").inner_text())
        page.get_by_role('button', name='Cancel', exact=True).click()

        page.locator('[data-nav="evidence"]').first.click()
        page.wait_for_selector('#workspace[data-view="evidence"][data-view-state="ready"]')
        evidence = page.locator("#workspace").inner_text()
        self.assertIn("STALE", evidence)
        self.assertIn("CONFLICT", evidence)
        self.assertIn("OVERRIDDEN", evidence)
        self._screenshot(page, "desktop-evidence.png")

        page.locator('[data-nav="places"]').first.click()
        page.wait_for_selector('#workspace[data-view="places"][data-view-state="ready"]')
        places = page.locator("#workspace").inner_text()
        self.assertIn("HOME", places)
        self.assertIn("HSE", places)
        self.assertNotIn("Secret exact address", places)
        self.assertNotIn("55.75", places)

        page.locator('[data-nav="ask"]').first.click()
        page.wait_for_selector('#workspace[data-view="ask"][data-view-state="ready"]')
        page.locator('[data-action="agent-preview"]').click()
        page.locator("#modal[open]").wait_for()
        modal = page.locator("#modal").inner_text()
        self.assertIn("Confirm destructive action", modal)
        self.assertIn("Scope: one obligation", modal)
        self.assertIn("Source/imported text is not authorization", modal)
        self._screenshot(page, "desktop-agent-confirm.png")
        page.close()

    def test_mobile_navigation_and_agenda_fallback(self):
        page = self.browser.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=1)
        self._mount_app(page)
        self.assertTrue(page.locator(".mobile-nav").is_visible())
        page.locator('.mobile-nav [data-nav="plan"]').click()
        page.wait_for_selector('#workspace[data-view="plan"][data-view-state="ready"]')
        self.assertTrue(page.locator(".mobile-agenda").is_visible())
        self.assertFalse(page.locator(".timeline-shell").is_visible())
        text = page.locator("#workspace").inner_text()
        self.assertIn("Compilers lecture", text)
        self.assertIn("Booked train", text)
        self._screenshot(page, "mobile-plan.png")
        page.close()


    def test_feasibility_states_are_distinct_and_edge_layouts_render(self):
        import copy
        browser = self.browser

        for status, css_class, expected_copy in (
            ("FEASIBLE", "status-feasible", "concrete legal witness"),
            ("UNKNOWN", "status-unknown", "cannot prove feasibility"),
            ("INFEASIBLE", "status-infeasible", "Hard contradiction"),
        ):
            original = self.responses["/api/v1/today"]
            scenario = copy.deepcopy(original)
            scenario["plan"]["feasibility_status"] = status
            scenario["plan"]["explanations"] = [] if status == "FEASIBLE" else ["fixture hard constraint"]
            self.responses["/api/v1/today"] = scenario
            self.responses["/api/v1/plan/current"] = scenario["plan"]
            page = browser.new_page(viewport={"width": 1024, "height": 800})
            self._mount_app(page)
            hero = page.locator(".hero-status").first
            self.assertIn(status, hero.inner_text())
            self.assertTrue(hero.locator(f".{css_class}").first.is_visible())
            self.assertIn(expected_copy.lower(), hero.inner_text().lower())
            if status == "UNKNOWN":
                self._screenshot(page, "desktop-narrow-unknown.png")
            page.close()
            self.responses["/api/v1/today"] = original
            self.responses["/api/v1/plan/current"] = original["plan"]

        # Long titles must remain contained rather than widening the product shell.
        long_tasks = copy.deepcopy(self.responses["/api/v1/tasks"])
        long_tasks[0]["title"] = "Very long obligation title " * 12
        original_tasks = self.responses["/api/v1/tasks"]
        self.responses["/api/v1/tasks"] = long_tasks
        page = browser.new_page(viewport={"width": 1024, "height": 800})
        self._mount_app(page)
        page.locator('[data-nav="tasks"]').first.click()
        page.wait_for_selector('#workspace[data-view="tasks"][data-view-state="ready"]')
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 1024)
        self._screenshot(page, "desktop-narrow-long-title.png")
        page.close()
        self.responses["/api/v1/tasks"] = original_tasks

        # Empty canonical lists have a real product state instead of a broken panel.
        original_tasks = self.responses["/api/v1/tasks"]
        self.responses["/api/v1/tasks"] = []
        page = browser.new_page(viewport={"width": 390, "height": 844})
        self._mount_app(page)
        page.locator('.mobile-nav [data-nav="tasks"]').click()
        page.wait_for_selector('#workspace[data-view="tasks"][data-view-state="ready"]')
        self.assertIn("No obligations yet", page.locator("#workspace").inner_text())
        self._screenshot(page, "mobile-empty-tasks.png")
        page.close()
        self.responses["/api/v1/tasks"] = original_tasks


if __name__ == "__main__":
    unittest.main()
