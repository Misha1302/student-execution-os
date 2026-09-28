"""Least-authority capability grants for external agents (MCP, ChatGPT, Codex).

A grant lets an external client act for one account with an explicit scope set. It is
an *authorization* layer only: reads call the same ``UiService`` queries as the app, and
every mutation is a typed canonical operation applied through ``UiService.sync`` — the
``/api/v1/sync`` path with op_id idempotency, replay protection, expected-version
conflicts and account isolation. There is no direct database write API here.

Token format: ``botay_cap_<grant id>_<secret>``. Only SHA-256(secret) is stored; the
plaintext is returned once at creation. A grant cannot create, list or revoke grants.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from student_execution_os.domain.errors import DomainError, EntityNotFound, ValidationError
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

TOKEN_PREFIX = "botay_cap_"
MAX_ACTIVE_GRANTS = 20
MAX_LIFETIME = timedelta(days=366)

READ_SCOPES = {
    "today:read": "Today view: plan, due work, classes and events for today",
    "tasks:read": "Tasks and their state",
    "calendar:read": "Events and classes (including imported academic schedule)",
    "notes:read": "Notes",
    "reminders:read": "Reminders",
}
WRITE_SCOPES = {
    "tasks:write": "Create and change tasks (create, update, start, progress, complete, defer, ...)",
    "events:write": "Create and change personal events",
    "notes:write": "Create and change notes",
    "reminders:write": "Create and change reminders",
    "schedule:write": "Personal changes to classes (move, cancel, restore one occurrence)",
}
DESTRUCTIVE_SCOPE = "destructive"
SCOPES = {**READ_SCOPES, **WRITE_SCOPES,
          DESTRUCTIVE_SCOPE: "Permanently delete items (also needs the matching :write scope)"}

# Canonical operation type -> required write scope. Deny by default: an operation type
# that is not listed (projects, routines, constraints, calibration, execution sessions,
# series creation/splitting, holidays, transcripts, ...) cannot be run by a grant.
OPERATION_SCOPES: dict[str, str] = {
    **{f"task.{name}": "tasks:write" for name in (
        "create", "update", "activate", "progress", "start", "complete", "cancel", "reopen",
        "defer", "archive", "unarchive", "restore", "delete")},
    **{f"event.{name}": "events:write" for name in ("create", "update", "cancel", "reopen", "delete")},
    **{f"note.{name}": "notes:write" for name in (
        "create", "update", "archive", "unarchive", "delete", "link")},
    **{f"reminder.{name}": "reminders:write" for name in (
        "create", "update", "done", "ack", "cancel", "reopen", "delete", "snooze")},
    **{f"series.occurrence.{name}": "schedule:write" for name in ("cancel", "move", "update", "restore")},
}
DESTRUCTIVE_OPERATIONS = frozenset(op for op in OPERATION_SCOPES if op.endswith(".delete"))

_TOKEN = re.compile(r"^botay_cap_([0-9a-f]{32})_([A-Za-z0-9_-]{43})$")


class CapabilityDenied(DomainError):
    """The grant exists but does not carry the scope this call needs."""

    def __init__(self, message: str, scope: str | None = None) -> None:
        super().__init__(message)
        self.scope = scope


class InvalidGrant(DomainError):
    """No usable grant: malformed, unknown, revoked or expired (deliberately one error)."""


@dataclass(frozen=True)
class Grant:
    id: str
    account_id: str
    label: str
    scopes: frozenset[str]
    created_at: str
    expires_at: str | None
    revoked_at: str | None
    last_used_at: str | None

    @property
    def principal_id(self) -> str:
        return f"grant:{self.id}"

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise CapabilityDenied(f"this grant does not include {scope}", scope)

    def require_operation(self, op_type: str) -> None:
        scope = OPERATION_SCOPES.get(op_type)
        if scope is None:
            raise CapabilityDenied(f"operation {op_type or '(none)'} is not available to external agents")
        self.require(scope)
        if op_type in DESTRUCTIVE_OPERATIONS:
            self.require(DESTRUCTIVE_SCOPE)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "scopes": sorted(self.scopes),
                "created_at": self.created_at, "expires_at": self.expires_at,
                "revoked_at": self.revoked_at, "last_used_at": self.last_used_at}


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _normalize_scopes(raw: Any) -> frozenset[str]:
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
        raise ValidationError("scopes must be a non-empty list of scope names")
    scopes = frozenset(item.strip() for item in raw)
    unknown = sorted(scopes - SCOPES.keys())
    if unknown:
        raise ValidationError(f"unknown scopes: {', '.join(unknown)}")
    if DESTRUCTIVE_SCOPE in scopes and not scopes & WRITE_SCOPES.keys():
        raise ValidationError("destructive needs at least one :write scope")
    return scopes


class CapabilityStore:
    """Grant lifecycle (session-authenticated owner) and token authentication."""

    def __init__(self, repo: SQLiteCanonicalRepository) -> None:
        self.repo = repo

    @staticmethod
    def _grant(row) -> Grant:
        return Grant(row["id"], row["account_id"], row["label"], frozenset(json.loads(row["scopes_json"])),
                     row["created_at"], row["expires_at"], row["revoked_at"], row["last_used_at"])

    def create(self, account_id: str, created_by: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.repo._require_account(account_id)
        label = str(payload.get("label") or "").strip()
        if not 1 <= len(label) <= 80:
            raise ValidationError("label must be 1-80 characters")
        scopes = _normalize_scopes(payload.get("scopes"))
        now = self.repo.clock.now()
        expires_at: datetime | None = None
        raw_days = payload.get("expires_in_days")
        if raw_days is not None:
            if isinstance(raw_days, bool) or not isinstance(raw_days, int) or not 1 <= raw_days <= MAX_LIFETIME.days:
                raise ValidationError(f"expires_in_days must be an integer from 1 to {MAX_LIFETIME.days}")
            expires_at = now + timedelta(days=raw_days)
        grant_id = uuid4().hex
        secret = secrets.token_urlsafe(32)
        with self.repo._tx() as conn:
            active = conn.execute(
                "SELECT count(*) FROM capability_grants WHERE account_id=? AND revoked_at IS NULL "
                "AND (expires_at IS NULL OR expires_at>?)", (account_id, _iso(now))).fetchone()[0]
            if active >= MAX_ACTIVE_GRANTS:
                raise ValidationError(f"at most {MAX_ACTIVE_GRANTS} active grants; revoke one first")
            conn.execute(
                "INSERT INTO capability_grants(id,account_id,label,token_hash,scopes_json,created_by,created_at,expires_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (grant_id, account_id, label, _hash(secret), json.dumps(sorted(scopes)), created_by, _iso(now),
                 None if expires_at is None else _iso(expires_at)),
            )
        grant = self.get(account_id, grant_id)
        # The only time the token exists outside the client that receives it.
        return {"grant": grant.public(), "token": f"{TOKEN_PREFIX}{grant_id}_{secret}"}

    def get(self, account_id: str, grant_id: str) -> Grant:
        row = self.repo.connection.execute(
            "SELECT * FROM capability_grants WHERE account_id=? AND id=?", (account_id, grant_id)).fetchone()
        if row is None:
            raise EntityNotFound("capability grant not found")
        return self._grant(row)

    def list(self, account_id: str) -> list[dict[str, Any]]:
        rows = self.repo.connection.execute(
            "SELECT * FROM capability_grants WHERE account_id=? ORDER BY created_at DESC,id", (account_id,)).fetchall()
        return [self._grant(row).public() for row in rows]

    def revoke(self, account_id: str, grant_id: str) -> dict[str, Any]:
        with self.repo._tx() as conn:
            changed = conn.execute(
                "UPDATE capability_grants SET revoked_at=? WHERE account_id=? AND id=? AND revoked_at IS NULL",
                (_iso(self.repo.clock.now()), account_id, grant_id)).rowcount
        grant = self.get(account_id, grant_id)  # NOT_FOUND for another account's grant
        return {"grant": grant.public(), "revoked": bool(changed)}

    def authenticate(self, token: str | None) -> Grant:
        match = _TOKEN.match(token or "")
        if match is None:
            raise InvalidGrant("a valid capability token is required")
        grant_id, secret = match.groups()
        row = self.repo.connection.execute("SELECT * FROM capability_grants WHERE id=?", (grant_id,)).fetchone()
        if row is None or not hmac.compare_digest(row["token_hash"], _hash(secret)):
            raise InvalidGrant("a valid capability token is required")
        now = self.repo.clock.now()
        if row["revoked_at"] is not None or (row["expires_at"] is not None and _dt(row["expires_at"]) <= now):
            raise InvalidGrant("a valid capability token is required")
        with self.repo._tx() as conn:
            conn.execute("UPDATE capability_grants SET last_used_at=? WHERE id=?", (_iso(now), grant_id))
        return self._grant(row)


__all__ = [
    "CapabilityDenied", "CapabilityStore", "DESTRUCTIVE_OPERATIONS", "DESTRUCTIVE_SCOPE", "Grant",
    "InvalidGrant", "OPERATION_SCOPES", "READ_SCOPES", "SCOPES", "TOKEN_PREFIX", "WRITE_SCOPES",
]
