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
from datetime import datetime, timedelta, timezone
import sys

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url")
    parser.add_argument("--expect-revision")
    parser.add_argument("--expect-worker", action="store_true", help="require a live reminder-worker heartbeat")
    parser.add_argument("--expect-push", action="store_true", help="require the worker to report FCM configured")
    parser.add_argument("--expect-byok", action="store_true",
                        help="require per-account AI key storage and exercise it with a deliberately invalid key")
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
        # Natural-language capture: what the preview shows is what gets stored.
        phrase = "В пятницу к шести сдать лабораторную по физике, займёт часа два, это важно"
        nl = call("POST", "/api/v1/assistant/interpret", json={"text": phrase, "context": {"timezone": "Europe/Moscow"}})
        action = nl["actions"][0]
        check(action["command"] == "CREATE_TASK" and action["payload"]["actual_cutoff"]["state"] == "KNOWN"
              and action["payload"]["estimated_total_effort_minutes"] == 120 and not action["unresolved_fields"],
              "natural-language preview", nl)
        applied = call("POST", "/api/v1/assistant/apply", json={
            "batch_id": nl["batch_id"], "action_ids": [action["id"]], "idempotency_key": f"smoke-nl-{suffix}"})
        stored = call("GET", f"/api/v1/tasks/{applied['results'][0]['entity_id']}")
        check(stored["actual_cutoff"]["state"] == "KNOWN" and stored["importance"] == "HIGH"
              and stored["estimated_total_effort_minutes"] == 120, "applied proposal keeps every field", stored)
        snooze = call("POST", "/api/v1/sync", json={"operations": [{
            "op_id": f"op-snooze-{suffix}", "type": "reminder.snooze", "entity_id": stored["id"],
            "payload": {"minutes": 30}}]})["results"][0]
        check(snooze["status"] == "APPLIED" and snooze["entity"]["remind_at"], "snooze schedules a reminder", snooze)
        # Per-account AI (BYOK): fresh accounts either receive STARTER platform-managed
        # AI or, when STARTER is disabled, fall back to the local parser.
        llm = call("GET", "/api/v1/settings/llm")
        starter_available = llm["source"] == "PLATFORM_MANAGED"
        if starter_available:
            starter = llm.get("platform_managed") or {}
            quota = starter.get("quota") or {}
            check(llm["credential"] is None and starter.get("plan") == "STARTER" and starter.get("available") is True,
                  "new account has STARTER AI", llm)
            check(isinstance(quota.get("requests_remaining"), int)
                  and isinstance(quota.get("tokens_remaining"), int)
                  and quota.get("resets_at"), "STARTER quota is visible", quota)
        else:
            check(llm["source"] == "NONE" and llm["credential"] is None, "new account has no AI key", llm)
            check(capabilities.get("live_llm_provider") is False and preview["provider"] == "deterministic-local-v1",
                  "local parser without a key", capabilities)
        if args.expect_byok:
            check(llm["storage_available"] is True, "AI key storage configured", llm)
            fake = f"sk-smoke-invalid-{secrets.token_hex(12)}"
            saved = call("PUT", "/api/v1/settings/llm", json={"provider": "openai", "model": "gpt-5-mini", "api_key": fake})
            check(saved["source"] == "USER_BYOK" and saved["credential"]["key_hint"] == f"sk-••••{fake[-4:]}",
                  "AI key saved and masked", saved)
            for path in ("/api/v1/settings/llm", "/api/v1/ask/capabilities", "/api/v1/settings/diagnostics",
                         "/api/v1/account/export"):
                body = client.get(path).text
                check(fake not in body and fake[9:] not in body, f"key absent from {path}")
            # The real provider must refuse the fake key (or the deployment egress policy
            # may block provider access); capture keeps working locally either way.
            tested = call("POST", "/api/v1/settings/llm/test")
            check(tested["ok"] is False and tested["status"] in {"INVALID_KEY", "SERVER_BLOCKED"},
                  "invalid key refused by provider path", tested)
            fallback = call("POST", "/api/v1/assistant/interpret", json={"text": "купить молоко"})
            expected_reason = "AUTH" if tested["status"] == "INVALID_KEY" else "SERVER_BLOCKED"
            check(fallback["fallback"] is True and fallback["fallback_reason"] == expected_reason
                  and fallback["actions"][0]["command"] == "CREATE_TASK", "invalid key falls back to local parser", fallback)
            removed = call("DELETE", "/api/v1/settings/llm")
            expected_source = "PLATFORM_MANAGED" if starter_available else "NONE"
            check(removed["source"] == expected_source and removed["credential"] is None, "AI key removed", removed)
            report["byok"] = "ok"
        # Daily execution (schema v31-v33): one pass over each owner, in the smoke account.
        def sync(op_id: str, kind: str, entity_id: str, payload: dict | None = None) -> dict:
            return call("POST", "/api/v1/sync", json={"operations": [
                {"op_id": op_id, "type": kind, "entity_id": entity_id, "payload": payload or {}}]})["results"][0]

        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%dT18:00")
        routine_id = f"routine-smoke-{suffix}"
        routine = sync(f"op-routine-{suffix}", "routine.create", routine_id, {
            "title": "Smoke recurring work", "dtstart_local": tomorrow, "effort_minutes": 30,
            "recurrence_rule": "FREQ=DAILY;COUNT=3", "timezone_name": "UTC"})
        check(routine["status"] == "APPLIED", "create recurring work", routine)
        stopped = sync(f"op-routine-stop-{suffix}", "routine.cancel", routine_id,
                       {"expected_version": routine["entity"]["version"]})
        check(stopped["status"] == "APPLIED" and stopped["entity"]["status"] == "CANCELLED", "stop recurring work", stopped)
        again_stop = sync(f"op-routine-stop2-{suffix}", "routine.cancel", routine_id,
                          {"expected_version": routine["entity"]["version"]})
        check(again_stop["status"] == "NOOP", "stopping twice changes nothing", again_stop)

        start = now.replace(minute=0, second=0, microsecond=0)
        medication_id = f"checkin-smoke-med-{suffix}"
        med = sync(f"op-med-{suffix}", "checkin.create", medication_id, {
            "kind": "MEDICATION", "title": "Smoke vitamin", "dtstart_local": start.strftime("%Y-%m-%dT%H:%M"),
            "recurrence_rule": "FREQ=DAILY", "timezone_name": "UTC", "remind": False})
        check(med["status"] == "APPLIED", "create medication check-in", med)
        rid = start.strftime("%Y-%m-%dT%H:%M:00")
        taken = sync(f"op-med-taken-{suffix}", "checkin.occurrence.done", medication_id,
                     {"original_recurrence_id": rid, "occurred_at": now.isoformat()})
        check(taken["status"] == "APPLIED" and taken["entity"]["status"] == "DONE", "medication taken", taken)

        quota_id = f"checkin-smoke-quota-{suffix}"
        quota = sync(f"op-quota-{suffix}", "checkin.create", quota_id, {
            "kind": "QUOTA", "title": "Smoke quota", "target_quantity": 20, "unit": "tasks", "unit_effort_seconds": 180,
            "dtstart_local": start.strftime("%Y-%m-%dT%H:%M"), "recurrence_rule": "FREQ=DAILY", "timezone_name": "UTC",
            "remind": False})
        check(quota["status"] == "APPLIED", "create quota", quota)
        five = sync(f"op-quota-5-{suffix}", "checkin.occurrence.progress", quota_id,
                    {"original_recurrence_id": rid, "count": 5})
        check(five["status"] == "APPLIED" and five["entity"]["quantity_done"] == 5
              and five["entity"]["remaining_effort_minutes"] == 45, "quota progress and remaining time", five)
        plan = call("GET", "/api/v1/today")["plan"]
        planned = [b for b in plan.get("quota_blocks", []) if b["template_id"] == quota_id]
        unfit = f"QUOTA_DOES_NOT_FIT:{quota_id}" in plan["explanations"]
        check(bool(planned) or unfit or plan["feasibility_status"] != "FEASIBLE", "quota time is planned or explained",
              {"blocks": planned, "explanations": plan["explanations"]})
        if planned:
            check(sum(b["duration_minutes"] for b in planned if b["original_recurrence_id"] == rid) == 45
                  and all(b["ownership"] == "DERIVED" for b in planned), "only the remaining quota is planned", planned)

        step_id = f"subtask-smoke-{suffix}"
        step = sync(f"op-step-{suffix}", "subtask.create", step_id, {"task_id": task_id, "title": "Smoke step"})
        check(step["status"] == "APPLIED", "add checklist step", step)
        ticked = sync(f"op-step-done-{suffix}", "subtask.complete", step_id, {})
        check(ticked["status"] == "APPLIED" and ticked["entity"]["done"] is True, "check off step", ticked)

        place_id = f"place-smoke-{suffix}"
        place = sync(f"op-place-{suffix}", "place.create", place_id, {"display_name": "Smoke place"})
        check(place["status"] == "APPLIED", "create place", place)
        reminder = sync(f"op-reminder-{suffix}", "reminder.create", f"reminder-smoke-{suffix}", {
            "title": "Smoke reminder", "remind_at": (now + timedelta(hours=2)).isoformat()})
        check(reminder["status"] == "APPLIED", "create reminder", reminder)
        report["daily_execution"] = {"plan_status": plan["feasibility_status"],
                                     "quota_planned_minutes": sum(b["duration_minutes"] for b in planned),
                                     "quota_does_not_fit": unfit}

        diagnostics = call("GET", "/api/v1/settings/diagnostics")
        report["diagnostics"] = {k: diagnostics.get(k) for k in ("schema_version", "reminder_worker", "external_capabilities")}
        if args.expect_worker:
            check(diagnostics["reminder_worker"]["state"] == "RUNNING", "reminder worker heartbeat", diagnostics["reminder_worker"])
        if args.expect_push:
            check(diagnostics["external_capabilities"]["fcm"] == "CONFIGURED"
                  and diagnostics["reminder_worker"].get("push_provider") == "fcm-v1", "FCM configured in worker",
                  diagnostics["reminder_worker"])
    finally:
        me = client.get("/api/v1/account/deletion-policy")
        if me.status_code == 200:
            client.post("/api/v1/account/delete", json={
                "expected_server_revision": me.json()["server_revision"], "confirm_login": login})
    print(json.dumps({"status": "ok", **report}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
