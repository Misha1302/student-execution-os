from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.web.app import create_app
from student_execution_os.web.services.assistant import AssistantService


class AssistantThreadpoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_slow_interpret_does_not_block_health_on_the_event_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "app.sqlite")
            with SQLiteCanonicalRepository(database) as repo:
                repo.initialize()
                repo.create_account("account")
            app = create_app(database, account_id="account", principal_id="user")

            def slow_interpret(_service, _payload):
                time.sleep(1.2)
                return {"engine": "LOCAL", "fallback": False}
            transport = httpx.ASGITransport(app=app)
            with patch.object(AssistantService, "assistant_interpret", slow_interpret):
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    started = time.monotonic()
                    interpret = asyncio.create_task(client.post(
                        "/api/v1/assistant/interpret", json={"text": "hello"},
                    ))
                    await asyncio.sleep(0.05)
                    health = await client.get("/api/v1/health")
                    health_elapsed = time.monotonic() - started
                    response = await interpret

            self.assertEqual(health.status_code, 200)
            self.assertEqual(response.status_code, 200)
            # Without the threadpool the health call cannot run until the full 1.2s
            # interpret sleep ends. Leave generous CI headroom while still proving
            # the event loop stayed responsive.
            self.assertLess(health_elapsed, 0.8)


if __name__ == "__main__":
    unittest.main()
