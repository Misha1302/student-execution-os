"""Push delivery: FCM HTTP v1 provider and the durable outbox dispatcher.

Delivery retry is purely technical: a message the reminder engine already decided
is re-sent after a transient provider failure. Before *every* attempt the message
is re-validated (task still open, not snoozed, not stale), so a retry never
delivers a prompt that stopped making sense. A new user-facing reminder is always
a new message created by the engine, never a retry.

FCM has no request idempotency key, so delivery is at-least-once. Each message
carries its id and a per-task collapse key, and the Android client shows it under a
stable notification id, so a duplicate replaces rather than stacks.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

from .messages import FOLLOW_UP
from .store import REMINDER_ACTIONS_CAPABILITY, SERVICE_STAGES, WAKE_ALARM_CAPABILITY, ReminderStore

log = logging.getLogger("student_execution_os.push")

MAX_ATTEMPTS = 6
STALE_AFTER = timedelta(hours=2)
LEASE = timedelta(seconds=90)
FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


@dataclass(frozen=True)
class SendResult:
    ok: bool
    provider_id: str | None = None
    error: str | None = None
    retryable: bool = False
    token_invalid: bool = False


class PushProvider(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    def send(self, token: str, message: dict[str, Any], *, data_only: bool = False) -> SendResult: ...


class UnconfiguredProvider:
    name = "none"
    configured = False

    def send(self, token: str, message: dict[str, Any], *, data_only: bool = False) -> SendResult:
        return SendResult(False, error="PUSH_UNCONFIGURED")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class FcmV1Provider:
    """Firebase Cloud Messaging HTTP v1 with a service-account OAuth token."""

    name = "fcm-v1"

    def __init__(self, service_account: dict[str, Any], *, http: httpx.Client | None = None,
                 fcm_base_url: str = "https://fcm.googleapis.com") -> None:
        for key in ("client_email", "private_key", "project_id"):
            if not service_account.get(key):
                raise ValueError(f"FCM service account JSON lacks {key}")
        self.account = service_account
        self.project_id = service_account["project_id"]
        self.token_uri = service_account.get("token_uri") or "https://oauth2.googleapis.com/token"
        self.fcm_base_url = fcm_base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=15)
        self._token: str | None = None
        self._token_expires = 0.0
        self._lock = threading.Lock()

    configured = True

    @classmethod
    def from_environment(cls) -> "FcmV1Provider | None":
        raw = os.environ.get("SEOS_FCM_SERVICE_ACCOUNT_JSON", "").strip()
        path = os.environ.get("SEOS_FCM_SERVICE_ACCOUNT_FILE", "").strip()
        if not raw and path:
            file = Path(path)
            if not file.is_file():
                log.error("SEOS_FCM_SERVICE_ACCOUNT_FILE %s does not exist", path)
                return None
            raw = file.read_text(encoding="utf-8")
        if not raw:
            return None
        try:
            return cls(json.loads(raw))
        except (ValueError, TypeError):
            log.exception("invalid FCM service account configuration")
            return None

    def _assertion(self) -> str:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        now = int(time.time())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64url(json.dumps({
            "iss": self.account["client_email"], "scope": FCM_SCOPE, "aud": self.token_uri,
            "iat": now, "exp": now + 3600,
        }).encode())
        signing_input = f"{header}.{claims}".encode()
        key = serialization.load_pem_private_key(self.account["private_key"].encode(), password=None)
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{claims}.{_b64url(signature)}"

    def access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expires - 120:
                return self._token
            response = self.http.post(self.token_uri, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self._assertion(),
            })
            response.raise_for_status()
            payload = response.json()
            self._token = payload["access_token"]
            self._token_expires = time.time() + int(payload.get("expires_in", 3600))
            return self._token

    def send(self, token: str, message: dict[str, Any], *, data_only: bool = False) -> SendResult:
        try:
            bearer = self.access_token()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return SendResult(False, error=f"FCM_AUTH:{type(exc).__name__}", retryable=True)
        body = fcm_message(token, message, data_only=data_only)
        try:
            response = self.http.post(
                f"{self.fcm_base_url}/v1/projects/{self.project_id}/messages:send",
                headers={"Authorization": f"Bearer {bearer}"}, json=body,
            )
        except httpx.HTTPError as exc:
            return SendResult(False, error=f"FCM_NETWORK:{type(exc).__name__}", retryable=True)
        if response.status_code == 200:
            return SendResult(True, provider_id=response.json().get("name"))
        status = ""
        try:
            error = response.json().get("error", {})
            status = error.get("status", "")
            details = [d.get("errorCode", "") for d in error.get("details", []) if isinstance(d, dict)]
        except ValueError:
            details = []
        if response.status_code == 404 or "UNREGISTERED" in details:
            return SendResult(False, error="FCM_UNREGISTERED", token_invalid=True)
        if response.status_code == 400 and ("INVALID_ARGUMENT" in details or status == "INVALID_ARGUMENT"):
            return SendResult(False, error="FCM_INVALID_TOKEN", token_invalid=True)
        if response.status_code == 401:
            self._token = None
        retryable = response.status_code in (401, 429) or response.status_code >= 500
        return SendResult(False, error=f"FCM_HTTP_{response.status_code}:{status}"[:120], retryable=retryable)


    def verify(self, tokens: list[str] | None = None) -> dict[str, Any]:
        """Prove the credential end to end without showing anything on a phone.

        1. The service-account key signs a JWT that Google exchanges for an access token.
        2. A ``validate_only`` send checks project permission; against a placeholder
           token FCM must answer "invalid token", not 401/403/404.
        3. With real registered tokens, ``validate_only`` tells whether each is live.
        Never returns or logs the key or the access token.
        """
        report: dict[str, Any] = {"project_id": self.project_id, "client_email": self.account["client_email"]}
        try:
            self.access_token()
        except Exception as exc:  # noqa: BLE001 - report the class only, never the response
            report.update(oauth="FAILED", error=type(exc).__name__, ok=False)
            return report
        report["oauth"] = "OK"

        def validate(token: str) -> tuple[int, str]:
            body = fcm_message(token, {"type": "check", "title": "check", "body": "check", "collapse_key": "check"},
                               data_only=True)
            body["validate_only"] = True
            response = self.http.post(
                f"{self.fcm_base_url}/v1/projects/{self.project_id}/messages:send",
                headers={"Authorization": f"Bearer {self.access_token()}"}, json=body,
            )
            try:
                error = response.json().get("error", {})
                code = ",".join([error.get("status", "")] + [d.get("errorCode", "") for d in error.get("details", [])
                                                             if isinstance(d, dict)]).strip(",")
            except ValueError:
                code = ""
            return response.status_code, code

        status, code = validate("seos-placeholder-token")
        report["project_permission"] = "OK" if status in (400, 404) and ("INVALID_ARGUMENT" in code or "UNREGISTERED" in code) else f"HTTP_{status}:{code}"
        report["devices"] = []
        for token in tokens or []:
            status, code = validate(token)
            report["devices"].append("VALID" if status == 200 else f"HTTP_{status}:{code}")
        report["ok"] = report["project_permission"] == "OK"
        return report


def fcm_message(token: str, message: dict[str, Any], *, data_only: bool = False) -> dict[str, Any]:
    """The FCM v1 request body for one reminder.

    Clients with native reminder actions get a *data-only* message: the app's own
    messaging service renders the notification with working Start/Done/Snooze
    buttons (a system-rendered notification cannot carry them) and uses the message
    id as a stable notification tag. Older clients get a notification block, which
    the system shows even while the app is killed; tapping it opens the app.
    """
    collapse = str(message.get("collapse_key", "seos"))[:64]
    data = {key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            for key, value in message.items() if key != "collapse_key"}
    android: dict[str, Any] = {"priority": "HIGH", "ttl": f"{int(STALE_AFTER.total_seconds())}s", "collapse_key": collapse}
    body: dict[str, Any] = {"token": token, "data": data, "android": android}
    if data_only:
        data["render"] = "native"
    else:
        body["notification"] = {"title": str(message.get("title", "")), "body": str(message.get("body", ""))}
        # Same tag => an at-least-once duplicate replaces the shown notification.
        android["notification"] = {"tag": collapse}
    return {"message": body}


def provider_from_environment() -> PushProvider:
    return FcmV1Provider.from_environment() or UnconfiguredProvider()


def retry_delay(attempts: int) -> timedelta:
    return timedelta(seconds=min(1800, 30 * 2 ** max(0, attempts - 1)))


class PushDispatcher:
    def __init__(self, database: str, provider: PushProvider) -> None:
        self.database = database
        self.provider = provider

    def run_once(self, now: datetime, worker_id: str = "worker") -> dict[str, int]:
        stats = {"sent": 0, "retry": 0, "cancelled": 0, "no_device": 0, "dead": 0}
        with SQLiteCanonicalRepository(self.database, clock=FrozenClock(now)) as repo:
            repo.initialize()
            for message_id in self._claim(repo, now, worker_id):
                outcome = self._deliver(repo, message_id, now)
                stats[outcome] = stats.get(outcome, 0) + 1
        return stats

    def _claim(self, repo: SQLiteCanonicalRepository, now: datetime, worker_id: str) -> list[str]:
        with repo._tx() as conn:
            rows = conn.execute(
                "SELECT id FROM reminder_messages WHERE (delivery_state='PENDING' AND next_attempt_at<=?) "
                "OR (delivery_state='LEASED' AND lease_expires_at<=?) ORDER BY next_attempt_at LIMIT 50",
                (_iso(now), _iso(now)),
            ).fetchall()
            ids = [row["id"] for row in rows]
            for message_id in ids:
                conn.execute(
                    "UPDATE reminder_messages SET delivery_state='LEASED',lease_owner=?,lease_expires_at=? WHERE id=?",
                    (worker_id, _iso(now + LEASE), message_id),
                )
        return ids

    def _finish(self, repo, message_id: str, state: str, *, error: str | None = None, next_attempt: datetime | None = None,
                attempts: int | None = None, provider_ids: str | None = None, sent_at: datetime | None = None) -> None:
        with repo._tx() as conn:
            conn.execute(
                "UPDATE reminder_messages SET delivery_state=?,lease_owner=NULL,lease_expires_at=NULL,last_error=?,"
                "next_attempt_at=coalesce(?,next_attempt_at),attempts=coalesce(?,attempts),"
                "provider_ids=coalesce(?,provider_ids),sent_at=coalesce(?,sent_at) WHERE id=? AND delivery_state='LEASED'",
                (state, error, _iso(next_attempt), attempts, provider_ids, _iso(sent_at), message_id),
            )

    def _stale_reason(self, repo, row, now: datetime) -> str | None:
        created = _dt(row["created_at"])
        if now - created > STALE_AFTER:
            return "STALE"
        if row["reminder_id"]:
            reminder = repo.connection.execute(
                "SELECT status,fired_at FROM reminders WHERE account_id=? AND id=?", (row["account_id"], row["reminder_id"])
            ).fetchone()
            if reminder is None or reminder["status"] != "FIRED":
                return "REMINDER_CLOSED"
        for task_id in json.loads(row["task_ids_json"]):
            ob = repo.connection.execute(
                "SELECT lifecycle_status FROM obligations WHERE account_id=? AND id=?", (row["account_id"], task_id)
            ).fetchone()
            if ob is None or ob["lifecycle_status"] not in ("ACTIVE", "DRAFT"):
                return "TASK_CLOSED"
            state = repo.connection.execute(
                "SELECT snoozed_until,last_interaction_at FROM reminder_states WHERE account_id=? AND task_id=?",
                (row["account_id"], task_id),
            ).fetchone()
            if state is not None:
                if state["snoozed_until"] and _dt(state["snoozed_until"]) > now:
                    return "SNOOZED"
                if state["last_interaction_at"] and _dt(state["last_interaction_at"]) > created:
                    return "USER_ACTED"
        return None

    def _deliver(self, repo, message_id: str, now: datetime) -> str:
        row = repo.connection.execute("SELECT * FROM reminder_messages WHERE id=?", (message_id,)).fetchone()
        reason = self._stale_reason(repo, row, now)
        if reason is not None:
            self._finish(repo, message_id, "CANCELLED" if reason != "STALE" else "DEAD", error=reason)
            return "cancelled" if reason != "STALE" else "dead"
        prefs = ReminderStore(repo).prefs(row["account_id"])
        quiet_end = prefs.quiet_until(now)
        # The engine only creates a message inside quiet hours when the user asked for
        # that moment explicitly; such a message is delivered, not held.
        explicit = row["stage"] in SERVICE_STAGES or row["stage"] == "REMINDER" and row["reminder_id"]
        if quiet_end is not None and not explicit and prefs.quiet_until(_dt(row["created_at"])) is None:
            self._finish(repo, message_id, "PENDING", error="QUIET_HOURS", next_attempt=quiet_end)
            return "retry"
        if not self.provider.configured:
            self._finish(repo, message_id, "NO_DEVICE", error="PUSH_UNCONFIGURED")
            return "no_device"
        store = ReminderStore(repo)
        devices = store.active_tokens(row["account_id"])
        if row["stage"] == "ALARM_SYNC":
            # A signal for phones that keep local alarms; nobody else needs it.
            devices = [d for d in devices if WAKE_ALARM_CAPABILITY in d[2]]
        if not devices:
            self._finish(repo, message_id, "NO_DEVICE", error="NO_ACTIVE_DEVICE")
            return "no_device"
        task_ids = json.loads(row["task_ids_json"])
        titles = {}
        for task_id in task_ids:
            title_row = repo.connection.execute(
                "SELECT title FROM obligations WHERE account_id=? AND id=?", (row["account_id"], task_id)
            ).fetchone()
            titles[task_id] = title_row["title"] if title_row else ""
        payload = {
            "type": "reminder", "message_id": row["id"], "stage": row["stage"], "title": row["title"],
            "body": row["body"], "deep_link": row["deep_link"], "actions": json.loads(row["actions_json"]),
            "task_ids": task_ids, "task_id": task_ids[0] if len(task_ids) == 1 else "",
            "task_title": titles.get(task_ids[0], "") if len(task_ids) == 1 else "",
            "collapse_key": task_ids[0] if len(task_ids) == 1 else "group",
            "created_at": row["created_at"], "locale": prefs.locale, "timezone": prefs.timezone_name,
            "labels": FOLLOW_UP[prefs.locale],
            # Whose message this is: a phone signed out of (or switched away from) this
            # account ignores it, so an unrevoked push token cannot ring old alarms.
            "account_id": row["account_id"],
        }
        if row["reminder_id"]:
            reminder = repo.connection.execute(
                "SELECT id,title,remind_at,delivery,wake_check,raise_volume FROM reminders WHERE account_id=? AND id=?",
                (row["account_id"], row["reminder_id"]),
            ).fetchone()
            payload.update(reminder_id=row["reminder_id"], delivery=row["delivery"], collapse_key=row["reminder_id"],
                           task_title=reminder["title"] if reminder else row["title"])
            if reminder is not None and row["delivery"] in ("ALARM", "PUSH_AND_ALARM"):
                payload["alarm"] = {"id": reminder["id"], "at": reminder["remind_at"], "title": reminder["title"],
                                    "wake_check": bool(reminder["wake_check"]), "raise_volume": bool(reminder["raise_volume"])}
        if row["stage"] in SERVICE_STAGES:
            payload["type"] = "alarm-sync" if row["stage"] == "ALARM_SYNC" else "reminder"
        attempts = int(row["attempts"]) + 1
        results = []
        for device_id, token, capabilities in devices:
            alarm_phone = WAKE_ALARM_CAPABILITY in capabilities
            if row["stage"] == "ALARM_SYNC":
                result = self.provider.send(token, payload, data_only=True)
            elif REMINDER_ACTIONS_CAPABILITY in capabilities:
                # A phone that cannot ring gets an alarm reminder as a notification.
                message = payload if alarm_phone or "alarm" not in payload else {**payload, "delivery": "PUSH"}
                result = self.provider.send(token, {k: v for k, v in message.items() if alarm_phone or k != "alarm"},
                                            data_only=True)
            else:
                result = self.provider.send(token, {k: v for k, v in payload.items() if k != "alarm"})
            if result.token_invalid:
                store.deactivate_device(device_id)
            results.append(result)
        delivered = [r for r in results if r.ok]
        if delivered:
            self._finish(repo, message_id, "SENT", attempts=attempts, sent_at=now,
                         provider_ids=",".join(r.provider_id or "" for r in delivered))
            return "sent"
        errors = ";".join(sorted({r.error or "" for r in results}))[:200]
        if any(r.retryable for r in results) and attempts < MAX_ATTEMPTS:
            self._finish(repo, message_id, "PENDING", error=errors, attempts=attempts, next_attempt=now + retry_delay(attempts))
            return "retry"
        if all(r.token_invalid for r in results):
            self._finish(repo, message_id, "NO_DEVICE", error=errors, attempts=attempts)
            return "no_device"
        self._finish(repo, message_id, "DEAD", error=errors, attempts=attempts)
        return "dead"
