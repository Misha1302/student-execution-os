"""End-to-end HTTP smoke test against a running session-mode server.

    python deploy/smoke.py https://plan.example.com [--expect-revision <git sha>]

Registers a throwaway account, drives the execution loop through the public API
(create → start → progress → complete → open completed → reopen → reuse), checks
reminders, Assistant capabilities and diagnostics, then deletes the account again.
Exits non-zero on the first failed check.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--expect-revision")
    parser.add_argument("--expect-worker", action="store_true", help="require a live reminder-worker heartbeat")
    parser.add_argument("--insecure", action="store_true", help="skip TLS verification")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    client = httpx.Client(base_url=base, timeout=30, verify=not args.insecure)
    report: dict[str, object] = {}

    def check(condition: bool, label: str, detail: object = None) -> None:
        if not condition:
            raise SystemExit(f"FAIL {label}: {json.dumps(detail, default=str)[:800]}")
        report[label] = "ok"

    def call(method: str, path: str, **kwargs):
        response = client.request(method, path, **kwargs)
        check(response.status_code < 400, f"{method} {path}", {"status": response.status_code, "body": response.text})
        return response.json()

    health = call("GET", "/api/v1/health")
    check(health.get("service") == "student-execution-os" and health.get("status") == "ok", "health", health)
    check(health.get("auth_mode") == "session", "session mode", health)
    report["schema_version"] = health.get("schema_version")
    report["revision"] = health.get("revision")
    if args.expect_revision:
        check(str(health.get("revision", "")).startswith(args.expect_revision[:12]), "deployed revision", health)

    login = f"smoke{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(18)
    issued = call("POST", "/api/v1/auth/register", json={"login": login, "password": password, "device_label": "smoke"})
    client.headers["Authorization"] = f"Bearer {issued['token']}"
    try:
        call("GET", "/api/v1/today")

        def op(op_id: str, kind: str, payload: dict | None = None) -> dict:
            result = call("POST", "/api/v1/sync", json={"operations": [
                {"op_id": op_id, "type": kind, "entity_id": task_id, "payload": payload or {}}]})["results"][0]
            return result

        suffix = secrets.token_hex(4)
        task_id = f"task-smoke-{suffix}"
        created = op(f"op-create-{suffix}", "task.create", {
            "title": "Production smoke task", "estimated_total_effort_minutes": 60,
            "actual_cutoff": {"state": "UNKNOWN"}, "splittable": False})
        check(created["status"] == "APPLIED", "create task", created)
        replay = op(f"op-create-{suffix}", "task.create", {
            "title": "Production smoke task", "estimated_total_effort_minutes": 60,
            "actual_cutoff": {"state": "UNKNOWN"}, "splittable": False})
        check(replay["replayed"] is True, "exactly-once replay", replay)
        fetched = call("GET", f"/api/v1/tasks/{task_id}")
        check(fetched["id"] == task_id and fetched["status"] == "ACTIVE", "get task", fetched)
        started = op(f"op-start-{suffix}", "task.start")
        check(started["status"] == "APPLIED" and started["entity"]["started_at"], "start", started)
        progressed = op(f"op-progress-{suffix}", "task.progress", {"minutes": 20})
        check(progressed["entity"]["remaining_effort_minutes"] == 40 and progressed["entity"]["last_progress_at"], "progress", progressed)
        completed = op(f"op-complete-{suffix}", "task.complete")
        check(completed["entity"]["status"] == "COMPLETED", "complete", completed)
        opened = call("GET", f"/api/v1/tasks/{task_id}")
        check(opened["status"] == "COMPLETED", "open completed task", opened)
        conflict = op(f"op-cancel-{suffix}", "task.cancel")
        check(conflict["status"] == "CONFLICT", "conflict is visible", conflict)
        reopened = op(f"op-reopen-{suffix}", "task.reopen", {"remaining_effort_minutes": 15})
        check(reopened["entity"]["status"] == "ACTIVE", "reopen", reopened)
        again = op(f"op-progress2-{suffix}", "task.progress", {"minutes": 5})
        check(again["status"] == "APPLIED" and again["entity"]["remaining_effort_minutes"] == 10, "reuse after reopen", again)

        reminders = call("GET", "/api/v1/notifications")
        check(isinstance(reminders, list), "reminder inbox", reminders)
        prefs = call("GET", "/api/v1/notification-preferences")
        check("intensity" in prefs, "reminder preferences", prefs)
        capabilities = call("GET", "/api/v1/ask/capabilities")
        check(capabilities.get("structured_actions") is True, "assistant capabilities", capabilities)
        report["llm"] = "live" if capabilities.get("live_llm_provider") else "degraded-local"
        preview = call("POST", "/api/v1/assistant/interpret", json={"text": "task: Smoke assistant 25 min"})
        check(preview["actions"][0]["command"] == "CREATE_TASK" and preview["mutated_canonical_state"] is False,
              "assistant preview does not mutate", preview)
        diagnostics = call("GET", "/api/v1/settings/diagnostics")
        report["diagnostics"] = {k: diagnostics.get(k) for k in ("schema_version", "reminder_worker", "external_capabilities")}
        if args.expect_worker:
            check(diagnostics["reminder_worker"]["state"] == "RUNNING", "reminder worker heartbeat", diagnostics["reminder_worker"])
    finally:
        me = client.get("/api/v1/account/deletion-policy")
        if me.status_code == 200:
            client.post("/api/v1/account/delete", json={
                "expected_server_revision": me.json()["server_revision"], "confirm_login": login})
    print(json.dumps({"status": "ok", **report}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
