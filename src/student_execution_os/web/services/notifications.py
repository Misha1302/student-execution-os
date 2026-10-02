"""Reminders, notifications, alarms and devices."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from student_execution_os.reminders import ReminderStore, provider_from_environment

from .common import _jsonify, _dt, TEST_NOTIFICATION_LIMITER

from .base import ApplicationService


class NotificationService(ApplicationService):
    """Reminders, notifications, alarms and devices."""

    def reminders(self) -> list[dict[str, Any]]:
        """Standalone reminders: open ones and those closed in the last 30 days."""
        from student_execution_os.reminders.standalone import SQLiteReminderRepository
        with self._repo() as repo:
            return SQLiteReminderRepository(repo).list(self.account_id, since=self._now() - timedelta(days=30))

    def upcoming_alarms(self) -> dict[str, Any]:
        """What an Android phone must have scheduled (fetched by its background worker)."""
        from student_execution_os.reminders.standalone import SQLiteReminderRepository
        with self._repo() as repo:
            return {"alarms": SQLiteReminderRepository(repo).upcoming_alarms(self.account_id, self._now()),
                    "now": _jsonify(self._now())}

    def notifications(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return ReminderStore(repo).messages(self.account_id, since=self._now() - timedelta(days=30))

    def notification_preferences(self) -> dict[str, Any]:
        with self._repo() as repo:
            store = ReminderStore(repo)
            return store.prefs_payload(store.prefs(self.account_id))

    def update_notification_preferences(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            store = ReminderStore(repo)
            return store.prefs_payload(store.update_prefs(self.account_id, payload))

    def devices(self) -> list[dict[str, Any]]:
        with self._repo() as repo:
            return ReminderStore(repo).devices(self.account_id)

    def register_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return ReminderStore(repo).register_device(
                self.account_id, str(payload.get("token", "")), payload.get("label"), payload.get("capabilities"),
                payload.get("status"),
            )

    def report_device_status(self, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            return ReminderStore(repo).report_device_status(self.account_id, device_id, payload.get("status"))

    def notifications_health(self) -> dict[str, Any]:
        """Can a reminder actually reach this user, and if not, why.

        ``reach`` is what the product may promise: PUSH (a phone will show it),
        IN_APP (only the in-app inbox — no phone can show it), NONE (the reminder
        worker is not running, so nothing is sent at all). ``alarm`` says whether a
        phone can ring a real alarm. The client adds what only it knows (this
        browser's or phone's own permission) on top.
        """
        from student_execution_os.reminders.store import WAKE_ALARM_CAPABILITY
        with self._repo() as repo:
            store = ReminderStore(repo)
            worker = self._worker_status(repo)
            running = worker["state"] == "RUNNING"
            push_configured = worker["push_configured"] if running else provider_from_environment().configured
            devices = [d for d in store.devices(self.account_id) if d["active"]]
            showing = [d for d in devices if d["status"].get("notifications") is not False]
            alarm_devices = [d for d in devices if WAKE_ALARM_CAPABILITY in d["capabilities"]]
            exact = [d for d in alarm_devices if d["status"].get("exact_alarms") is not False]
            prefs = store.prefs(self.account_id)
            problems: list[str] = []
            if not running:
                problems.append("WORKER_" + worker["state"])
            if not push_configured:
                problems.append("PUSH_UNCONFIGURED")
            if not devices:
                problems.append("NO_DEVICE")
            elif not showing:
                problems.append("DEVICE_NOTIFICATIONS_OFF")
            if alarm_devices and not exact:
                problems.append("EXACT_ALARMS_OFF")
            if any(d["status"].get("full_screen") is False for d in alarm_devices):
                problems.append("FULL_SCREEN_OFF")
            if not prefs.enabled:
                problems.append("REMINDERS_OFF")
            reach = "NONE" if not running else "PUSH" if push_configured and showing else "IN_APP"
            return {
                "reach": reach,
                "alarm": "OK" if exact else "INEXACT" if alarm_devices else "NONE",
                "problems": problems,
                "reminders_enabled": prefs.enabled,
                "worker": worker,
                "push_configured": push_configured,
                "devices": devices,
                "recent": store.delivery_summary(self.account_id, self._now() - timedelta(days=7)),
                "checked_at": _jsonify(self._now()),
            }

    def send_test_notification(self) -> dict[str, Any]:
        from student_execution_os.reminders.messages import test_message
        if not TEST_NOTIFICATION_LIMITER.allow(self.account_id):
            from student_execution_os.web.auth import RateLimited
            raise RateLimited("too many test notifications; try again in a minute")
        with self._repo() as repo:
            store = ReminderStore(repo)
            message_id = store.add_service_message(self.account_id, stage="TEST",
                                                   content=test_message(store.prefs(self.account_id).locale),
                                                   now=self._now())
            return store.delivery(self.account_id, message_id)

    def notification_delivery(self, message_id: str) -> dict[str, Any]:
        with self._repo() as repo:
            return ReminderStore(repo).delivery(self.account_id, message_id)

    def revoke_device(self, device_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._repo() as repo:
            expected = int(payload["expected_version"])
            store = ReminderStore(repo)
            if int(store.device(self.account_id, device_id)["version"]) != expected:
                from student_execution_os.domain.errors import VersionConflict
                raise VersionConflict("device registration version changed")
            return store.revoke_device(self.account_id, device_id)

    def snooze_notification(self, notification_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        until = _dt(payload.get("until"))
        if until is None:
            raise ValueError("snooze until is required")
        with self._repo() as repo:
            store = ReminderStore(repo)
            item = store.message(self.account_id, notification_id)
            for task_id in item["task_ids"]:
                store.touch(self.account_id, task_id, self._now(), snooze_until=until, remind_at=until)
            store.mark_acted(self.account_id, notification_id, "SNOOZE", self._now())
            return store.message(self.account_id, notification_id)
