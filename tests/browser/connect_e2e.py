"""Real browser + real server: connect an MCP client with OAuth consent, use it, disconnect.

This is the ChatGPT/Codex path end to end, with the external client simulated by an
HTTP client speaking the same protocol (register -> authorize -> consent in the app ->
token -> MCP tools/call).
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import socket
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from playwright.sync_api import sync_playwright

from student_execution_os.web.app import create_app
from student_execution_os.web.auth import AuthConfig

CHROMIUM = os.environ.get("CHROMIUM_PATH") or ("/usr/bin/chromium" if Path("/usr/bin/chromium").exists() else None)
NOW = datetime(2026, 9, 28, 9, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConnectAppBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = str(Path(cls.temp.name) / "connect.sqlite")
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
        cls.http = httpx.Client(base_url=cls.origin, trust_env=False, timeout=10)
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

    def test_connect_consent_after_sign_in_use_and_disconnect(self):
        self.http.post("/api/v1/auth/register", json={"login": "student", "password": "correct horse"})
        callback = f"{self.origin}/oauth-callback-for-test"
        client_id = self.http.post("/oauth/register", json={"redirect_uris": [callback],
                                                             "client_name": "ChatGPT"}).json()["client_id"]
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        authorize = "/oauth/authorize?" + urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": callback, "state": "s1",
            "code_challenge": challenge, "code_challenge_method": "S256",
            "scope": "today:read tasks:read tasks:write calendar:read"})

        context = self.browser.new_context(viewport={"width": 390, "height": 844}, timezone_id="Europe/Moscow",
                                           reduced_motion="reduce")
        self.addCleanup(context.close)
        context.add_init_script("try { localStorage.setItem('seos.locale', 'ru') } catch (e) {}")
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        # The client sends the student to botay!; they are not signed in yet.
        page.goto(self.origin + authorize)
        page.wait_for_selector('#workspace[data-view="welcome"][data-view-state="ready"]')
        page.locator('[data-chip-group="auth-mode"] [data-value="login"]').click()
        page.fill("input[name=login]", "student")
        page.fill("input[name=password]", "correct horse")
        page.locator("button[type=submit]").click()
        # Signing in resumes the consent, not Today.
        page.wait_for_selector('#workspace[data-view="connect"][data-view-state="ready"]')
        consent = page.locator("[data-connect]")
        self.assertIn("ChatGPT", consent.inner_text())
        self.assertTrue(consent.locator('[data-scope="tasks:write"]').is_checked())
        self.assertFalse(consent.locator('[data-scope="destructive"]').is_checked())
        consent.locator('[data-scope="calendar:read"]').uncheck()  # the student narrows access
        self.assertEqual(page.evaluate("document.documentElement.scrollWidth <= innerWidth"), True)
        with page.expect_navigation(url=lambda url: url.startswith(callback)):
            page.locator('[data-action="connect-allow"]').click()
        query = parse_qs(urlsplit(page.url).query)
        self.assertEqual(query["state"], ["s1"])

        token = self.http.post("/oauth/token", data={
            "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": callback,
            "client_id": client_id, "code_verifier": verifier}).json()
        self.assertEqual(token["scope"], "tasks:read tasks:write today:read")
        bearer = {"Authorization": f"Bearer {token['access_token']}"}
        call = self.http.post("/mcp", headers=bearer, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "create_task", "arguments": {"op_id": "chatgpt-e2e-0001", "title": "Доклад по ИИ"}}})
        self.assertEqual(call.json()["result"]["structuredContent"]["results"][0]["status"], "APPLIED")
        denied = self.http.get("/api/v1/ext/calendar", headers=bearer)
        self.assertEqual(denied.status_code, 403)

        # The student sees the agent's task in the app and the connection in Settings.
        page.goto(f"{self.origin}/#/tasks")
        page.wait_for_selector('#workspace[data-view="tasks"][data-view-state="ready"]')
        self.assertIn("Доклад по ИИ", page.locator("#workspace").inner_text())
        page.goto(f"{self.origin}/#/settings")
        page.wait_for_selector('#workspace[data-view="settings"][data-view-state="ready"]')
        apps = page.locator("[data-connected-apps]")
        self.assertIn("ChatGPT", apps.inner_text())
        self.assertIn("/mcp", apps.locator("[data-mcp-url]").inner_text())
        apps.locator('[data-action="app-revoke"]').click()
        page.locator("dialog.sheet[open] [data-confirm]").click()
        page.locator("[data-connected-apps]", has_text="Отключено").wait_for()
        self.assertEqual(self.http.post("/mcp", headers=bearer, json={"jsonrpc": "2.0", "id": 2, "method": "ping"})
                         .status_code, 401)

        # A token for Codex created in Settings is shown once with the MCP config.
        page.locator('[data-action="app-new"]').click()
        sheet = page.locator("dialog.sheet[open]")
        sheet.locator("[data-label]").fill("Codex")
        sheet.locator('[data-scope="tasks:write"]').check()
        sheet.locator("[data-create]").click()
        shown = page.locator("dialog.sheet[open] [data-token]")
        shown.wait_for()
        codex_token = shown.input_value()
        self.assertTrue(codex_token.startswith("botay_cap_"))
        self.assertIn("bearer_token_env_var", page.locator("dialog.sheet[open] [data-codex]").inner_text())
        tasks = self.http.get("/api/v1/ext/tasks", headers={"Authorization": f"Bearer {codex_token}"})
        self.assertEqual([task["title"] for task in tasks.json()], ["Доклад по ИИ"])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
