#!/usr/bin/env python3
"""Device check of reminder notification buttons against a real local server.

Starts a session-mode server with a throwaway database, creates an account and a
task that is due soon, lets the reminder engine and push dispatcher produce the
exact FCM data a phone would receive, and runs ReminderNotificationEndToEndTest on
the connected emulator/device with those values. The test renders the reminder,
presses "Snooze" and "Done" on the system notification and reads the task back
from the server. Only Google's FCM transport is not part of this check.

    # emulator running (e.g. `emulator -avd <name>`), JAVA_HOME/ANDROID_HOME set
    python mobile/scripts/native_e2e.py
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from student_execution_os.reminders import ReminderEngine  # noqa: E402
from student_execution_os.reminders.push import PushDispatcher, SendResult, fcm_message  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def main() -> int:
    port = free_port()
    workdir = tempfile.TemporaryDirectory()
    database = str(Path(workdir.name) / "native-e2e.sqlite")
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    server = subprocess.Popen([sys.executable, "-m", "student_execution_os.web.server", "--database", database,
                               "--host", "0.0.0.0", "--port", str(port)], env=env, cwd=ROOT)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                if httpx.get(f"{base}/api/v1/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        client = httpx.Client(base_url=base, timeout=10)
        issued = client.post("/api/v1/auth/register", json={"login": "native-e2e", "password": "native-e2e-password"}).json()
        client.headers["Authorization"] = f"Bearer {issued['token']}"
        client.patch("/api/v1/notification-preferences", json={"timezone": "UTC", "locale": "ru",
                                                               "quiet_hours": {"starts_local": "03:00", "ends_local": "03:01"}})
        # Due tomorrow and not started: the day-before reminder (Start / Snooze / Reschedule).
        due = (datetime.now(timezone.utc) + timedelta(hours=20)).replace(second=0, microsecond=0)
        result = client.post("/api/v1/sync", json={"operations": [{
            "op_id": "native-e2e-create", "type": "task.create", "entity_id": "task-native-e2e",
            "payload": {"title": "Сдать лабораторную по физике", "estimated_total_effort_minutes": 60,
                        "actual_cutoff": {"state": "KNOWN", "at": due.isoformat()}}}]}).json()["results"][0]
        assert result["status"] == "APPLIED", result
        client.post("/api/v1/mobile/devices", json={"token": "emulator", "label": "emulator",
                                                    "capabilities": ["reminder-actions-v1"]})
        now = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=11)
        ReminderEngine(database).tick(issued["user"]["account_id"], now)
        captured: list[dict] = []

        class Capture:
            name, configured = "capture", True

            def send(self, token, message, *, data_only=False):
                captured.append(fcm_message(token, message, data_only=data_only)["message"])
                return SendResult(True, "local")

        PushDispatcher(database, Capture()).run_once(now)
        assert captured and "notification" not in captured[0], captured
        data = captured[0]["data"]
        print("push data:", json.dumps(data, ensure_ascii=False)[:400])
        encoded = base64.b64encode(json.dumps(data, ensure_ascii=False).encode()).decode()
        gradle = ROOT / "mobile/android/gradlew"
        args = [
            f"-Pandroid.testInstrumentationRunnerArguments.class=io.github.misha1302.seos.reminders.ReminderNotificationEndToEndTest",
            f"-Pandroid.testInstrumentationRunnerArguments.seosServer=http://10.0.2.2:{port}",
            f"-Pandroid.testInstrumentationRunnerArguments.seosToken={issued['token']}",
            f"-Pandroid.testInstrumentationRunnerArguments.seosReminder={encoded}",
        ]
        run = subprocess.run([str(gradle), "--no-daemon", "-q", ":app:connectedDebugAndroidTest", *args], cwd=gradle.parent)
        task = client.get("/api/v1/tasks/task-native-e2e").json()
        inbox = client.get("/api/v1/notifications").json()
        print(json.dumps({"task_status": task["status"], "acted": [m.get("acted_action") for m in inbox]}, ensure_ascii=False))
        return run.returncode
    finally:
        server.terminate()
        server.wait(timeout=10)
        workdir.cleanup()


if __name__ == "__main__":
    sys.exit(main())
