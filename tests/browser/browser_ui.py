from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta
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
            "/api/v1/outlook?range=week": service.outlook("week", None),
            "/api/v1/outlook?range=month": service.outlook("month", None),
            "/api/v1/settings/diagnostics": service.diagnostics(),
            "/api/v1/account/deletion-policy": service.account_deletion_policy(),
            "/api/v1/ask/capabilities": {
                "live_llm_provider": False,
                "explanations": True,
                "destructive_action_preview": True,
                "message": "No live LLM provider is configured in this release.",
            },
            "/api/v1/settings/llm": self.llm_settings(None),
        }
        self.posts: list[tuple[str, dict]] = []
        self.overrides: dict[tuple[str, str], tuple[int, dict]] = {}
        self.offline: set[tuple[str, str]] = set()
        self.config_js: str | None = None

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def llm_settings(credential):
        return {
            "source": "USER_BYOK" if credential else "NONE", "active": bool(credential), "credential": credential,
            "storage_available": True, "platform_managed": {"entitled": False, "available": False},
            "providers": [{"id": "openai", "label": "OpenAI", "requires_base_url": False},
                          {"id": "anthropic", "label": "Anthropic", "requires_base_url": False},
                          {"id": "openai-compatible", "label": "OpenAI-compatible", "requires_base_url": True}],
        }

    # ---- harness --------------------------------------------------------------------

    def _handler(self, route):
        request = route.request
        path = request.url.removeprefix(ORIGIN).split("#")[0] or "/"

        def reply(status, body):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

        if path == "/" or not path.startswith(("/api/", "/assets/")):
            route.fulfill(status=200, content_type="text/html", body=(STATIC / "index.html").read_text())
            return
        if path == "/assets/config.js" and self.config_js is not None:
            route.fulfill(status=200, content_type="text/javascript", body=self.config_js)
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
        if (request.method, path) in self.offline:
            route.abort("internetdisconnected")
            return
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
        elif path.startswith("/api/v1/attachments?") and request.method == "GET":
            reply(200, [])
        elif path == "/api/v1/sync" and request.method == "POST":
            results = []
            for operation in payload.get("operations", []):
                entity = None
                if operation["type"] == "task.create":
                    entity = {"kind": "TASK", "id": operation["entity_id"], **operation["payload"],
                              "status": "DRAFT" if operation["payload"].get("estimated_total_effort_minutes") is None else "ACTIVE", "version": 1}
                else:
                    known = next((t for t in self.responses["/api/v1/tasks"] if t["id"] == operation["entity_id"]), None)
                    if known is not None:
                        entity = {**known, "kind": "TASK", **{k: v for k, v in operation["payload"].items() if k != "reminder_message_id"}}
                results.append({**operation, "status": "APPLIED", "entity": entity, "replayed": False})
            reply(200, {"results": results, "server_revision": 1})
        elif path in self.responses and request.method == "GET":
            reply(200, self.responses[path])
        else:
            reply(404, {"error": {"code": "NOT_FOUND", "message": "not mocked"}})

    def _open(self, *, width=390, height=844, locale="en", theme="system", hash_="") -> object:
        page = self.browser.new_page(viewport={"width": width, "height": height}, reduced_motion="reduce",
                                     timezone_id="Europe/Moscow")
        self.page_errors: list[str] = []
        page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        page.add_init_script(
            f"try {{ localStorage.setItem('seos.locale', '{locale}'); "
            f"localStorage.setItem('seos.theme', '{theme}') }} catch (e) {{}}"
        )
        page.route(ORIGIN + "/**", self._handler)
        page.goto(f"{ORIGIN}/{hash_}")
        return page

    @staticmethod
    def _ready(page, view: str) -> None:
        page.wait_for_selector(f'#workspace[data-view="{view}"][data-view-state="ready"]')

    PENDING_OPS_JS = ("Object.entries(localStorage).filter(([k]) => k.startsWith('seos.ops.'))"
                      ".flatMap(([, v]) => JSON.parse(v)).filter((x) => x.state === 'PENDING').length")

    def _wait_sync(self, page, *, tries: int = 60) -> None:
        """Changes are queued locally and sent in the background: wait until sent."""
        for _ in range(tries):  # the page CSP forbids wait_for_function's string eval
            if page.evaluate(self.PENDING_OPS_JS) == 0 and self.posts:
                return
            page.wait_for_timeout(100)

    @staticmethod
    def _text(page) -> str:
        return page.locator("#workspace").inner_text()

    def _go(self, page, view: str) -> None:
        page.evaluate(f"location.hash = '#/{view}'")
        self._ready(page, view.split("?", 1)[0])

    def _screenshot(self, page, name: str) -> None:
        root = os.environ.get("UI_QA_SCREENSHOT_DIR")
        if root:
            Path(root).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(root) / name), full_page=True)

    def _assert_no_horizontal_scroll(self, page, width: int) -> None:
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    # ---- tests ---------------------------------------------------------------------

    # Words that belong to the architecture, not to a student's screen.
    INTERNAL = ("CANONICAL", "Canonical", "DERIVED", "Derived", "FEASIBLE", "policy_version", "expected_version",
                "evidence_ids", "CREATE_TASK", "entity_ref", "field_path", "estimated_total_effort", "actual_cutoff",
                "Schema", "schema", "worker", "provider", "Revision", "revision", "PENDING", "deterministic")

    def _assert_human(self, text: str, where: str) -> None:
        for word in self.INTERNAL:
            self.assertNotIn(word, text, f"{word!r} leaked into {where}")
        self.assertNotRegex(text, r"[{}]\s*\"", f"JSON leaked into {where}")

    def test_phone_today_plan_and_tab_navigation(self):
        page = self._open()
        self._ready(page, "today")
        self.assertTrue(page.locator(".tabbar").is_visible())
        self.assertTrue(page.locator(".fab").is_visible())
        today = self._text(page)
        self.assertIn("Everything fits", today)
        self.assertIn("status-feasible", page.locator(".hero-status").get_attribute("class"))
        self.assertIn("leave by", today.lower())
        self.assertIn("HOME", today)
        self.assertIn("HSE", today)
        self._assert_human(today, "Today")
        self._assert_no_horizontal_scroll(page, 390)
        self._screenshot(page, "mobile-today.png")

        page.locator('.tabbar [data-nav="plan"]').click()
        self._ready(page, "plan")
        plan = self._text(page)
        self._assert_human(plan, "Plan")
        self.assertTrue(page.locator(".mobile-agenda").is_visible())
        # Events, travel and buffers keep separate visual classes (no ownership jargon).
        self.assertGreater(page.locator(".agenda-item.canonical").count(), 0)
        self.assertGreater(page.locator(".agenda-item.travel").count(), 0)
        self.assertGreater(page.locator(".agenda-item.buffer").count(), 0)
        self.assertIn("Compilers lecture", plan)
        self.assertIn("Booked train", plan)
        page.locator(".agenda-item.travel").first.click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.wait_for()
        self.assertIn("I picked this time myself", sheet.inner_text())
        self._assert_human(sheet.inner_text(), "plan item sheet")
        page.keyboard.press("Escape")
        self._assert_no_horizontal_scroll(page, 390)
        self._screenshot(page, "mobile-plan.png")

        # Dense agenda rows never overlap vertically.
        boxes = page.eval_on_selector_all(".agenda-item", "els => els.map(e => { const r = e.getBoundingClientRect(); return [r.top, r.bottom]; })")
        for (top_a, bottom_a), (top_b, _) in zip(boxes, boxes[1:]):
            self.assertLessEqual(bottom_a, top_b + 0.5)

        page.locator('.tabbar [data-nav="more"]').click()
        self._ready(page, "more")
        more = self._text(page)
        self.assertNotIn("Assistant", more)
        self.assertNotIn("Sources", more)
        page.locator('.menu-row[data-nav="calendar"]').click()
        self._ready(page, "calendar")
        calendar = self._text(page)
        self.assertIn("Daily planning review", calendar)
        self.assertIn("Every day", calendar)
        self.assertNotIn("FREQ=", calendar)
        self._assert_human(calendar, "Calendar")
        self.assertTrue(page.locator("#back-button").is_visible())
        self.assertEqual(self.page_errors, [])
        page.close()

    def test_plan_states_are_explained_in_plain_words(self):
        tasks = self.responses["/api/v1/today"]["tasks"]
        for status, css_class, reasons, expected in (
            ("FEASIBLE", "status-feasible", [], "fits into your schedule"),
            ("UNKNOWN", "status-unknown", [f"UNKNOWN_HARD_CUTOFF:{tasks[0]['id']}"], f"when “{tasks[0]['title']}” is due"),
            ("INFEASIBLE", "status-infeasible", ["HARD_CUTOFF_PASSED"], "doesn't fit your free time"),
        ):
            original = self.responses["/api/v1/today"]
            scenario = copy.deepcopy(original)
            scenario["plan"]["feasibility_status"] = status
            scenario["plan"]["explanations"] = reasons
            self.responses["/api/v1/today"] = scenario
            page = self._open(width=1024, height=800)
            self._ready(page, "today")
            hero = page.locator(".hero-status").first
            self.assertIn(css_class, hero.get_attribute("class"))
            self.assertIn(expected, hero.inner_text())
            self.assertNotIn(status, hero.inner_text())
            self.assertNotIn("UNKNOWN_HARD_CUTOFF", hero.inner_text())
            if status == "UNKNOWN":
                hero.locator('[data-action="open-task"]').click()
                self._ready(page, "task")
                self.assertIn(tasks[0]["title"], self._text(page))
            page.close()
            self.responses["/api/v1/today"] = original

    def test_task_detail_is_human_and_progress_conflict_is_visible(self):
        page = self._open()
        self._ready(page, "today")
        page.locator('.tabbar [data-nav="tasks"]').click()
        self._ready(page, "tasks")
        self.assertIn("Discrete homework", self._text(page))
        page.locator('.task-card', has_text="Algorithms worksheet").click()
        self._ready(page, "task")
        detail = self._text(page)
        self.assertIn("Deadline", detail)
        self.assertNotIn("Version", detail)
        self._assert_human(detail, "task detail")
        page.locator("#back-button").click()
        self._ready(page, "tasks")

        page.locator('.task-card', has_text="Compiler report").click()
        self._ready(page, "task")
        self.assertIn("sources disagree", self._text(page))
        task = next(t for t in self.responses["/api/v1/tasks"] if t["id"] == "conflict-task")
        self.overrides[("POST", "/api/v1/sync")] = (
            409, {"error": {"code": "VERSION_CONFLICT", "message": "stale", "retryable": False}},
        )
        page.locator('[data-action="detail-progress"]').click()
        page.locator("dialog.sheet[open] [data-save]").click()
        toast = page.locator(".toast.error")
        toast.wait_for()
        self.assertIn("another device", toast.inner_text())
        path, payload = self.posts[-1]
        self.assertEqual(path, "/api/v1/sync")
        self.assertEqual(payload["operations"][0]["entity_id"], task["id"])
        self._ready(page, "task")
        page.close()

    def test_edit_and_reschedule_send_real_changes(self):
        task = next(t for t in self.responses["/api/v1/tasks"] if t["title"].startswith("Discrete"))
        page = self._open()
        self._ready(page, "today")  # aligns the client clock with the fixture's "now"
        page.evaluate(f"location.hash = '#/task/{task['id']}'")
        self._ready(page, "task")
        page.locator('[data-action="detail-edit"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.wait_for()
        sheet.locator('[data-f="title"]').fill("Discrete homework — set 4")
        importance = "CRITICAL" if task["importance"] == "HIGH" else "HIGH"
        sheet.locator(f'[data-chip-group="f-importance"] [data-value="{importance}"]').click()
        sheet.locator('[data-f="category"]').select_option("EXAM")
        sheet.locator("[data-save]").click()
        page.locator(".toast").first.wait_for()
        self._wait_sync(page)
        path, payload = self.posts[-1]
        operation = payload["operations"][0]
        self.assertEqual((path, operation["type"]), ("/api/v1/sync", "task.update"))
        self.assertEqual(operation["payload"], {"title": "Discrete homework — set 4", "importance": importance, "category": "EXAM"})

        # "Reschedule" moves the deadline by a day with one tap …
        self._ready(page, "task")
        page.locator('[data-action="detail-reschedule"]').click()
        page.locator('dialog.sheet[open] [data-deadline="d1"]').click()
        page.locator(".toast").first.wait_for()
        self._wait_sync(page)
        operation = self.posts[-1][1]["operations"][0]
        self.assertEqual(operation["type"], "task.update")
        old = datetime.fromisoformat(task["actual_cutoff"]["at"])
        new = datetime.fromisoformat(operation["payload"]["actual_cutoff"]["at"].replace("Z", "+00:00"))
        self.assertEqual(new - old, timedelta(days=1))
        # … and "later" puts the task off with a reminder at that moment.
        self._ready(page, "task")
        page.locator('[data-action="detail-reschedule"]').click()
        page.locator('dialog.sheet[open] [data-later="morning"]').click()
        page.locator(".toast").first.wait_for()
        self._wait_sync(page)
        operation = self.posts[-1][1]["operations"][0]
        self.assertEqual(operation["type"], "task.defer")
        self.assertTrue(operation["payload"]["until"].endswith("06:00:00.000Z"))  # 09:00 Moscow
        # A notification's "Reschedule" button deep-links straight into this sheet.
        self._go(page, "today")
        page.evaluate(f"location.hash = '#/task/{task['id']}?step=reschedule'")
        page.locator('dialog.sheet[open] [data-deadline="d3"]').wait_for()
        self.assertEqual(self.page_errors, [])
        page.close()

    def test_settings_notifications_and_places(self):
        page = self._open(width=1280, height=900)
        self._ready(page, "today")
        # Wide screens show every section in the side rail.
        self.assertTrue(page.locator('.tabbar .tab[data-nav="settings"]').is_visible())
        self.assertFalse(page.locator('.tabbar .tab[data-nav="more"]').is_visible())
        self.assertEqual(page.locator('.tabbar .tab[data-nav="evidence"]').count(), 0)

        page.locator('.tabbar [data-nav="settings"]').click()
        self._ready(page, "settings")
        settings = self._text(page)
        self.assertIn("includes private account data", settings)
        self._assert_human(settings, "Settings")
        self.assertTrue(page.get_by_role("button", name="Download account export").is_visible())
        # Diagnostics stay available under Advanced.
        page.locator("[data-advanced] summary").click()
        self.assertIn("Schema", self._text(page))
        page.locator('[data-advanced] [data-nav="evidence"]').click()
        self._ready(page, "evidence")
        self.assertIn("Conflict", self._text(page))
        self._go(page, "settings")
        page.locator('[data-action="account-delete-preview"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.wait_for()
        text = sheet.inner_text()
        self.assertIn("Delete account permanently", text)
        self.assertIn("not a recoverable copy", text)
        self.assertNotIn("policy", text.lower())
        page.get_by_role("button", name="Keep account", exact=True).click()

        page.locator('.tabbar [data-nav="notifications"]').click()
        self._ready(page, "notifications")
        inbox = self._text(page)
        self._assert_human(inbox, "Notifications")
        reminder = self.responses["/api/v1/notifications"][0]
        page.locator('[data-action="snooze-notification"]').first.click()
        sheet = page.locator("dialog.sheet[open]")
        self.assertIn("comes back at the chosen time", sheet.inner_text())
        sheet.locator('[data-value="30"]').click()
        sheet.locator("[data-save]").click()
        page.locator(".toast").first.wait_for()
        self._wait_sync(page)
        path, payload = self.posts[-1]
        operation = payload["operations"][0]
        # Snooze goes through the offline queue and names the reminder it answers.
        self.assertEqual((path, operation["type"]), ("/api/v1/sync", "reminder.snooze"))
        self.assertEqual(operation["entity_id"], reminder["task_ids"][0])
        self.assertEqual(operation["payload"]["reminder_message_id"], reminder["id"])

        page.locator('.tabbar [data-nav="places"]').click()
        self._ready(page, "places")
        places = self._text(page)
        self.assertIn("HOME", places)
        self.assertIn("HSE", places)
        self.assertNotIn("Secret exact address", page.content())
        self.assertNotIn("55.75", page.content())
        # The old technical Assistant screen is gone; its link lands on Today.
        page.evaluate("location.hash = '#/assistant'")
        self._ready(page, "today")
        self.assertEqual(self.page_errors, [])
        page.close()

    def test_ai_settings_connect_test_change_and_remove_own_key(self):
        secret = "sk-ant-browser-test-key-7777"
        page = self._open()
        self._ready(page, "today")
        self._go(page, "settings")
        section = page.locator("[data-ai]")
        self.assertIn("Everything works without a key", section.inner_text())
        self._assert_human(self._text(page), "Settings")
        self.assertNotIn("SEOS_", page.content())
        credential = {"provider": "anthropic", "model": "claude-haiku-4-5", "base_url": None, "key_hint": "sk-••••7777",
                      "status": "OK", "last_checked_at": None, "updated_at": None, "version": 1}
        saved = self.llm_settings(credential)
        self.overrides[("PUT", "/api/v1/settings/llm")] = (200, saved)
        self.overrides[("POST", "/api/v1/settings/llm/test")] = (200, {"ok": True, "status": "OK", "settings": saved})
        self.responses["/api/v1/settings/llm"] = saved

        page.locator('[data-action="ai-edit"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.wait_for()
        self.assertTrue(sheet.locator("[data-base-url]").is_hidden())
        sheet.locator('[data-chip-group="ai-provider"] [data-value="openai-compatible"]').click()
        self.assertTrue(sheet.locator("[data-base-url]").is_visible())
        sheet.locator('[data-chip-group="ai-provider"] [data-value="anthropic"]').click()
        self.assertTrue(sheet.locator("[data-base-url]").is_hidden())
        self.assertEqual(sheet.locator("#ai-key").get_attribute("type"), "password")
        sheet.locator("#ai-model").fill("claude-haiku-4-5")
        sheet.locator("#ai-key").fill(secret)
        self._screenshot(page, "settings-ai-connect.png")
        sheet.locator("[data-save]").click()
        page.get_by_text("Connection works").wait_for()
        put = next(payload for path, payload in self.posts if path == "/api/v1/settings/llm")
        self.assertEqual(put, {"provider": "anthropic", "model": "claude-haiku-4-5", "api_key": secret})
        self.assertEqual(self.posts[-1][0], "/api/v1/settings/llm/test")
        page.wait_for_selector("[data-ai] [data-action='ai-delete']")
        text = page.locator("[data-ai]").inner_text()
        self.assertIn("sk-••••7777", text)
        self.assertIn("Working", text)
        self._screenshot(page, "settings-ai-configured.png")
        self.assertNotIn(secret, page.content())
        stored = page.evaluate("JSON.stringify(Object.assign({}, localStorage))")
        self.assertNotIn(secret, stored)

        # Changing only the model keeps the saved key (nothing is re-sent).
        self.posts.clear()
        page.locator('[data-action="ai-edit"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.wait_for()
        self.assertIn("Keep saved key (sk-••••7777)", sheet.locator("#ai-key").get_attribute("placeholder"))
        sheet.locator("#ai-model").fill("claude-sonnet-5")
        sheet.locator("[data-save]").click()
        page.locator(".toast").first.wait_for()
        self.assertEqual(self.posts[0], ("/api/v1/settings/llm", {"provider": "anthropic", "model": "claude-sonnet-5", "expected_version": 1}))

        page.wait_for_selector("dialog.sheet[open]", state="detached")
        page.wait_for_load_state("networkidle")
        self._ready(page, "settings")
        self.overrides[("DELETE", "/api/v1/settings/llm")] = (200, self.llm_settings(None))
        self.responses["/api/v1/settings/llm"] = self.llm_settings(None)
        page.locator('[data-action="ai-delete"]').click()
        page.locator("dialog.sheet[open]").get_by_role("button", name="Remove").click()
        page.get_by_text("Key removed").wait_for()
        page.wait_for_selector("[data-ai] [data-action='ai-edit']")
        self.assertIn("Everything works without a key", page.locator("[data-ai]").inner_text())
        self.assertEqual(self.page_errors, [])
        page.close()

    def test_navigating_with_an_open_sheet_closes_it_instead_of_freezing(self):
        # Regression: <dialog> closes asynchronously and closeAllSheets() spun forever.
        page = self._open()
        self._ready(page, "today")
        self._go(page, "plan")
        page.locator(".agenda-item.work").first.click()
        page.locator("dialog.sheet[open] [data-close-sheet]").click()
        self._ready(page, "task")
        self.assertEqual(page.locator("dialog[open]").count(), 0)
        page.locator(".fab").click()
        page.locator("dialog.sheet[open] #capture-text").wait_for()
        page.evaluate("location.hash = '#/tasks'")
        self._ready(page, "tasks")
        self.assertEqual(page.locator("dialog[open]").count(), 0)
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

    def test_responsive_matrix_week_month_and_dark_theme(self):
        for width, height, locale, theme in (
            (360, 800, "ru", "dark"),
            (768, 1024, "en", "light"),
        ):
            page = self._open(width=width, height=height, locale=locale, theme=theme)
            self._ready(page, "today")
            self._assert_no_horizontal_scroll(page, width)
            self.assertEqual(page.locator("html").get_attribute("data-theme"), theme)
            self._go(page, "plan?step=week")
            self._ready(page, "plan")
            self.assertGreaterEqual(page.locator(".row").count(), 7)
            self._assert_no_horizontal_scroll(page, width)
            self._go(page, "plan?step=month")
            self._ready(page, "plan")
            self.assertGreaterEqual(page.locator(".row").count(), 42)
            self._assert_no_horizontal_scroll(page, width)
            self.assertEqual(self.page_errors, [])
            page.close()

    def test_spoken_style_sentence_becomes_a_task_card_and_create_sends_every_field(self):
        from student_execution_os.agent.nlparse import parse_task

        phrase = "В пятницу к шести сдать лабораторную по физике, займёт часа два, это важно"
        page = self._open(width=360, height=800, locale="ru")
        self._ready(page, "today")
        page.locator(".fab").click()
        sheet = page.locator("dialog.sheet[open]")
        self.assertIn("Что нужно сделать?", sheet.inner_text())
        sheet.locator("#capture-text").fill(phrase)
        card = sheet.locator(".capture-card")
        card.wait_for()
        text = card.inner_text()
        self.assertIn("Сдать лабораторную по физике", text)
        self.assertIn("~2 ч", text)
        self.assertIn("Важно", text)
        self.assertIn("18:00", text)
        self.assertEqual(card.locator(".question").count(), 0)
        self._assert_human(sheet.inner_text(), "capture sheet")
        self._screenshot(page, "mobile-capture.png")
        sheet.locator("[data-create]").click()
        page.locator(".toast").first.wait_for()
        self._wait_sync(page)
        path, payload = self.posts[-1]
        operation = payload["operations"][0]
        self.assertEqual((path, operation["type"]), ("/api/v1/sync", "task.create"))
        expected = parse_task(phrase, now=NOW, timezone_name="Europe/Moscow")
        sent = operation["payload"]
        self.assertEqual(sent["title"], expected["title"])
        self.assertEqual(sent["estimated_total_effort_minutes"], 120)
        self.assertEqual((sent["importance"], sent["category"], sent["splittable"]), ("HIGH", "HOMEWORK", True))
        self.assertEqual(datetime.fromisoformat(sent["actual_cutoff"]["at"].replace("Z", "+00:00")),
                         datetime.fromisoformat(expected["actual_cutoff"]["at"]))
        self.assertEqual(sent["actual_cutoff"]["at"], "2026-09-25T15:00:00.000Z")
        self._assert_no_horizontal_scroll(page, 360)
        page.close()

    def test_missing_values_are_asked_as_questions_and_dont_know_is_allowed(self):
        page = self._open(locale="en")
        self._ready(page, "today")
        page.locator(".fab").click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("#capture-text").fill("buy groceries")
        sheet.locator(".question").first.wait_for()
        self.assertIn("Roughly how long will it take?", sheet.inner_text())
        self.assertIn("When is it due?", sheet.inner_text())
        sheet.locator('[data-answer="effort"][data-value="30"]').click()
        sheet.locator('[data-answer="deadline"][data-value="none"]').click()
        self.assertEqual(sheet.locator(".question").count(), 0)
        self.assertIn("~30m", sheet.locator(".capture-card").inner_text())
        # Manual details stay available and win over the text.
        sheet.locator("[data-more] summary").click()
        sheet.locator('[data-f="description"]').fill("milk, bread")
        sheet.locator('[data-f="description"]').dispatch_event("change")
        sheet.locator("[data-create]").click()
        page.locator(".toast").first.wait_for()
        self._wait_sync(page)
        sent = self.posts[-1][1]["operations"][0]["payload"]
        self.assertEqual(sent["title"], "Buy groceries")
        self.assertEqual((sent["estimated_total_effort_minutes"], sent["actual_cutoff"]), (30, {"state": "ABSENT"}))
        self.assertEqual((sent["category"], sent["description"]), ("ERRAND", "milk, bread"))

        # "Don't know" for both: the task is still created (as a draft without a deadline).
        page.locator(".fab").click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("#capture-text").fill("tidy the room")
        sheet.locator('[data-answer="effort"][data-value="unknown"]').click()
        sheet.locator('[data-answer="deadline"][data-value="unknown"]').click()
        sheet.locator("[data-create]").click()
        page.locator(".toast").first.wait_for()
        self._wait_sync(page)
        sent = self.posts[-1][1]["operations"][0]["payload"]
        self.assertIsNone(sent["estimated_total_effort_minutes"])
        self.assertEqual(sent["actual_cutoff"], {"state": "UNKNOWN"})
        self.assertFalse(sent["splittable"])
        page.close()

        self.responses["/api/v1/tasks"] = []
        page = self._open(hash_="#/tasks")
        self._ready(page, "tasks")
        self.assertIn("No obligations yet", self._text(page))
        page.close()

    def test_capture_offline_is_saved_and_replayed_once_after_reconnect(self):
        page = self._open(locale="en")
        self._ready(page, "today")
        self.offline.add(("POST", "/api/v1/sync"))
        page.locator(".fab").click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("#capture-text").fill("Read chapter 5 tomorrow evening, 45 min")
        sheet.locator(".capture-card").wait_for()
        sheet.locator("[data-create]").click()
        toast = page.locator(".toast").first
        toast.wait_for()
        self.assertIn("Saved on this phone", toast.inner_text())
        queued = page.evaluate("Object.entries(localStorage).filter(([k]) => k.startsWith('seos.ops.')).map(([, v]) => JSON.parse(v)).flat()")
        self.assertEqual(len(queued), 1)
        operation = queued[0]["operation"]
        self.assertEqual((operation["type"], operation["payload"]["estimated_total_effort_minutes"]), ("task.create", 45))
        self.assertIsNotNone(operation["payload"]["actionable_from"])
        # Back online: the same operation id is replayed exactly once.
        self.offline.clear()
        page.evaluate("window.dispatchEvent(new Event('online'))")
        for _ in range(50):  # the page CSP forbids wait_for_function's string eval
            if page.evaluate(self.PENDING_OPS_JS) == 0:
                break
            page.wait_for_timeout(100)
        synced = [p for path, p in self.posts if path == "/api/v1/sync"]
        self.assertEqual([op["op_id"] for p in synced for op in p["operations"]], [operation["op_id"]])
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
        welcome = self._text(page)
        self.assertIn("Напишите или скажите", welcome)
        self.assertNotIn("сервер", welcome.lower())
        page.locator('[data-chip-group="auth-mode"] [data-value="login"]').click()
        self.assertIn("Войти", self._text(page))
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

    def _native_page(self):
        page = self.browser.new_page(viewport={"width": 390, "height": 844}, reduced_motion="reduce", timezone_id="Europe/Moscow")
        self.page_errors = []
        page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        page.add_init_script(
            "try { localStorage.setItem('seos.locale', 'en') } catch (e) {}\n"
            "const prefs = new Map();\n"
            "window.Capacitor = { isNativePlatform: () => true, Plugins: { Preferences: {\n"
            "  get: async ({ key }) => ({ value: prefs.has(key) ? prefs.get(key) : null }),\n"
            "  set: async ({ key, value }) => { prefs.set(key, value) },\n"
            "  remove: async ({ key }) => { prefs.delete(key) } } } };"
        )
        page.route(ORIGIN + "/**", self._handler)
        page.goto(f"{ORIGIN}/")
        return page

    def test_self_hosted_build_asks_for_the_server_then_signs_in(self):
        # A build without a preset server: the probed server stays a candidate until
        # sign-in, and the welcome flow must still advance to sign-in.
        self.health = {"status": "ok", "service": "student-execution-os", "auth_mode": "session",
                       "registration_open": True, "api_version": 1, "sync_protocol": 1}
        self.overrides[("POST", "/api/v1/auth/login")] = (200, {
            "token": "fixture-token", "expires_at": "2026-10-21T09:00:00+00:00",
            "user": {"login": "student", "account_id": ACCOUNT},
        })
        page = self._native_page()
        self._ready(page, "welcome")
        page.fill("input[name=server]", ORIGIN)
        page.locator('[data-form="server"] button[type=submit]').click()
        page.wait_for_selector('[data-form="auth"]')
        self.assertIn("Another server", self._text(page))
        page.locator('[data-chip-group="auth-mode"] [data-value="login"]').click()
        page.fill("input[name=login]", "student")
        page.fill("input[name=password]", "correct horse")
        page.locator('[data-form="auth"] button[type=submit]').click()
        self._ready(page, "today")
        self.assertEqual(page.evaluate("window.Capacitor.Plugins.Preferences.get({ key: 'seos.server' })"), {"value": ORIGIN})
        page.close()

    def test_consumer_build_starts_with_the_product_and_the_first_task(self):
        # The build knows its server: no address step; sign-up leads straight to capture.
        self.config_js = f"window.SEOS_CONFIG = {{ defaultServerUrl: '{ORIGIN}', pushEnabled: false }};"
        self.health = {"status": "ok", "service": "student-execution-os", "auth_mode": "session",
                       "registration_open": True, "api_version": 1, "sync_protocol": 1}
        self.overrides[("POST", "/api/v1/auth/register")] = (201, {
            "token": "fixture-token", "expires_at": "2026-10-21T09:00:00+00:00",
            "user": {"login": "newbie", "account_id": ACCOUNT},
        })
        self.responses["/api/v1/today"] = {**self.responses["/api/v1/today"], "tasks": [], "needs_refinement": [], "next_actions": []}
        page = self._native_page()
        self._ready(page, "welcome")
        welcome = self._text(page)
        self.assertNotIn("Server address", welcome)
        self.assertEqual(page.locator("input[name=server]").count(), 0)
        self.assertIn("Type or say what you need to do", welcome)
        self.assertEqual(page.locator('[data-chip-group="auth-mode"] .on').get_attribute("data-value"), "register")
        page.fill("input[name=login]", "newbie")
        page.fill("input[name=password]", "correct horse")
        page.fill("input[name=password2]", "correct horse")
        page.locator('[data-form="auth"] button[type=submit]').click()
        self._ready(page, "today")
        page.locator("dialog.sheet[open] #capture-text").wait_for()
        self.assertEqual(page.evaluate("window.Capacitor.Plugins.Preferences.get({ key: 'seos.server' })"), {"value": ORIGIN})
        self.assertIn(("/api/v1/auth/register", {"login": "newbie", "password": "correct horse", "device_label": "android"}), self.posts)
        self.assertEqual(self.page_errors, [])
        page.close()


if __name__ == "__main__":
    unittest.main()
