"""«Синхронизировать сейчас» for external connectors (Google Calendar and future ones).

This is separate from sending the device's own offline queue (``/api/v1/sync``): a
connector pulls from an outside system. Each provider has a runner that performs one
sync with the account's credentials. A provider whose credentials are not available
on this server (Google OAuth is not configured yet) still records a failed session
with a clear reason, so the screen shows the real state instead of a spinner.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

from student_execution_os.domain.errors import EntityNotFound
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso
from student_execution_os.reconciliation import SQLiteReconciliationRepository

from .model import ConnectorHealth
from .repository import SQLiteConnectorRepository

# error code → what the user can do about it (the client words it in RU/EN)
AUTH_REQUIRED = "AUTH_REQUIRED"
NOT_SUPPORTED = "NOT_SUPPORTED"
RETRY_LATER = "RETRY_LATER"

Runner = Callable[[SQLiteCanonicalRepository, dict[str, Any]], Any]


def _google_calendar(repo: SQLiteCanonicalRepository, state: dict[str, Any]):
    """Runs the Google Calendar connector when an access token is available.

    Only an operator-provided token (``SEOS_GOOGLE_CALENDAR_ACCESS_TOKEN``, one test
    account) exists today; per-account OAuth is on the roadmap.
    """
    from .google_calendar import GoogleCalendarConnector, GoogleCalendarHTTPTransport
    token = os.environ.get("SEOS_GOOGLE_CALENDAR_ACCESS_TOKEN", "").strip()
    if not token:
        return None
    reconciliation = SQLiteReconciliationRepository(repo)
    connectors = SQLiteConnectorRepository(repo, reconciliation)
    connector = GoogleCalendarConnector(
        account_id=state["account_id"], connector_id=state["id"], source_system_id=state["source_system_id"],
        calendar_id=os.environ.get("SEOS_GOOGLE_CALENDAR_ID", "primary"),
        transport=GoogleCalendarHTTPTransport(lambda: token), reconciliation=reconciliation, connectors=connectors,
    )
    return connector.sync()


RUNNERS: dict[str, Runner] = {"GOOGLE_CALENDAR": _google_calendar}
_MIN_INTERVAL = 10.0
_last_run: dict[tuple[str, str], float] = {}
_lock = threading.Lock()


def connector_payload(row, sessions: list[dict[str, Any]]) -> dict[str, Any]:
    last = sessions[0] if sessions else None
    return {
        "id": row["id"], "provider": row["provider"], "health": row["health_status"],
        "last_successful_sync_at": row["last_successful_complete_sync_at"],
        "last_attempt_at": None if last is None else last["started_at"],
        "last_attempt_status": None if last is None else last["status"],
        "error": row["latest_failure_reason"], "can_sync": row["provider"] in RUNNERS, "version": int(row["version"]),
    }


def list_connectors(repo: SQLiteCanonicalRepository, account_id: str) -> list[dict[str, Any]]:
    rows = repo.connection.execute("SELECT * FROM connector_states WHERE account_id=? ORDER BY id", (account_id,)).fetchall()
    out = []
    for row in rows:
        sessions = [dict(r) for r in repo.connection.execute(
            "SELECT started_at,status,error_code FROM connector_sync_sessions WHERE account_id=? AND connector_id=? "
            "ORDER BY started_at DESC LIMIT 1", (account_id, row["id"]))]
        out.append(connector_payload(row, sessions))
    return out


def sync_now(repo: SQLiteCanonicalRepository, account_id: str, connector_id: str) -> dict[str, Any]:
    row = repo.connection.execute("SELECT * FROM connector_states WHERE account_id=? AND id=?",
                                  (account_id, connector_id)).fetchone()
    if row is None:
        raise EntityNotFound("connector not found")
    key = (account_id, connector_id)
    with _lock:
        if time.monotonic() - _last_run.get(key, -1e9) < _MIN_INTERVAL:
            from student_execution_os.web.auth import RateLimited
            raise RateLimited("this calendar was synced a moment ago; try again in a few seconds")
        _last_run[key] = time.monotonic()
    runner = RUNNERS.get(row["provider"])
    result = runner(repo, dict(row)) if runner else None
    if result is None:
        # Nothing could run: record it as a failed attempt so the state is visible.
        connectors = SQLiteConnectorRepository(repo, SQLiteReconciliationRepository(repo))
        session = connectors.start_session(account_id=account_id, connector_id=connector_id, is_full_sync=False)
        connectors.finish_failure(account_id=account_id, session_id=session.id,
                                  error_code=AUTH_REQUIRED if runner else NOT_SUPPORTED,
                                  page_count=0, record_count=0, deletion_count=0, unavailable=True)
    state = next(item for item in list_connectors(repo, account_id) if item["id"] == connector_id)
    return {"connector": state, "ok": state["health"] == ConnectorHealth.CURRENT.value and state["last_attempt_status"] == "COMPLETE",
            "synced_at": _iso(repo.clock.now())}
