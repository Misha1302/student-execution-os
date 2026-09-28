"""Collaborative academic groups (schema v27, ADR 0032).

    shared academic reality   !=   personal execution state

A group owns shared academic facts: its schedule items (recurring classes and one-off
events such as an exam). Each ACTIVE member receives them as SOURCE state in their own
account through ``SourceApplier`` (one source system per member+group), so the R3
SOURCE/USER model applies unchanged: a member's personal move, cancellation, room note,
reminder or task stays in *their* USER layer and survives every group change; the group
code never reads or writes a member's personal state.

Roles: OWNER (everything, incl. roles and removals), STAROSTA (publish the schedule,
decide proposals, invite, remove MEMBERs), MEMBER (read, propose). Every write here is
idempotent by a client-generated id and schedule edits are guarded by the group's
``schedule_revision`` (optimistic concurrency).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from student_execution_os.domain.errors import (
    AuthorizationDenied,
    EntityNotFound,
    ValidationError,
    VersionConflict,
)
from student_execution_os.domain.model import ActorCategory, ObligationCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso
from student_execution_os.reconciliation import SQLiteReconciliationRepository
from student_execution_os.recurrence.model import RecurrenceRule
from student_execution_os.recurrence.source import (
    SourceApplier,
    SourceEvent,
    SourceOccurrenceChange,
    SourceSeries,
    SourceSnapshot,
)

SOURCE_KIND = "GROUP_SCHEDULE"
MAX_MEMBERS = 300
MAX_ITEMS = 400
_ID = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_CATEGORIES = {ObligationCategory.LESSON, ObligationCategory.EXAM, ObligationCategory.MEETING,
               ObligationCategory.ADMIN, ObligationCategory.GENERAL}
_NS = uuid.UUID("0f6d3a4e-3b1a-4d57-9d77-8c8b9a3b5e10")


class Role(StrEnum):
    OWNER = "OWNER"
    STAROSTA = "STAROSTA"
    MEMBER = "MEMBER"


_PUBLISHERS = {Role.OWNER, Role.STAROSTA}


def group_source_id(account_id: str, group_id: str) -> str:
    """Per member+group source system (source_systems ids are globally unique)."""
    return f"grp-{uuid.uuid5(_NS, f'{account_id}:{group_id}').hex}"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _local(value: Any, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be a local ISO date-time") from exc
    if parsed.tzinfo is not None:
        raise ValidationError(f"{name} must be a local time without an offset")
    return parsed


def _aware(value: Any, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{name} must be an ISO instant with an offset") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{name} must include an offset")
    return parsed


def _text(value: Any, name: str, *, required: bool = False, limit: int = 200) -> str | None:
    if value is None or str(value).strip() == "":
        if required:
            raise ValidationError(f"{name} is required")
        return None
    text = str(value).strip()
    if len(text) > limit:
        raise ValidationError(f"{name} is longer than {limit} characters")
    return text


def _category(value: Any, default: ObligationCategory) -> ObligationCategory:
    try:
        category = ObligationCategory(value or default.value)
    except ValueError as exc:
        raise ValidationError("category is not supported for group items") from exc
    if category not in _CATEGORIES:
        raise ValidationError("category is not supported for group items")
    return category


def normalize_item(kind: str, item: Any, timezone_name: str) -> dict[str, Any]:
    """Validate a shared schedule item and return its canonical JSON form."""
    if not isinstance(item, dict):
        raise ValidationError("item must be an object")
    if kind == "SERIES":
        zone = str(item.get("timezone_name") or timezone_name)
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError("timezone_name is not a known time zone") from exc
        rule = str(item.get("recurrence_rule") or "")
        RecurrenceRule.parse(rule)
        duration = item.get("duration_minutes")
        if isinstance(duration, bool) or not isinstance(duration, int) or not 5 <= duration <= 720:
            raise ValidationError("duration_minutes must be 5-720")
        changes = []
        for change in item.get("changes") or []:
            if not isinstance(change, dict):
                raise ValidationError("changes must be objects")
            changes.append({
                "recurrence_local": _local(change.get("recurrence_local"), "recurrence_local").isoformat(),
                "cancelled": bool(change.get("cancelled", False)),
                "starts_local": None if change.get("starts_local") is None
                else _local(change["starts_local"], "starts_local").isoformat(),
                "location_text": _text(change.get("location_text"), "location_text"),
            })
        if len(changes) > 200:
            raise ValidationError("at most 200 occurrence changes per series")
        return {
            "title": _text(item.get("title"), "title", required=True, limit=180),
            "dtstart_local": _local(item.get("dtstart_local"), "dtstart_local").isoformat(),
            "duration_minutes": duration, "recurrence_rule": rule, "timezone_name": zone,
            "category": _category(item.get("category"), ObligationCategory.LESSON).value,
            "location_text": _text(item.get("location_text"), "location_text"),
            "teacher": _text(item.get("teacher"), "teacher"),
            "exdates_local": sorted(_local(v, "exdates_local").isoformat() for v in item.get("exdates_local") or []),
            "changes": sorted(changes, key=lambda c: c["recurrence_local"]),
        }
    if kind == "EVENT":
        starts, ends = _aware(item.get("starts_at"), "starts_at"), _aware(item.get("ends_at"), "ends_at")
        if not starts < ends <= starts + timedelta(days=7):
            raise ValidationError("ends_at must be after starts_at and within 7 days")
        return {
            "title": _text(item.get("title"), "title", required=True, limit=180),
            "starts_at": starts.isoformat(), "ends_at": ends.isoformat(),
            "category": _category(item.get("category"), ObligationCategory.EXAM).value,
            "location_text": _text(item.get("location_text"), "location_text"),
            "teacher": _text(item.get("teacher"), "teacher"),
            "cancelled": bool(item.get("cancelled", False)),
        }
    raise ValidationError("kind must be SERIES or EVENT")


class GroupService:
    """All group operations for one signed-in account (the actor)."""

    def __init__(self, repo: SQLiteCanonicalRepository, *, account_id: str) -> None:
        self.repo = repo
        self.account_id = account_id

    # ---- reads / authorization --------------------------------------------------------

    def _group(self, group_id: str):
        row = self.repo.connection.execute("SELECT * FROM groups WHERE id=? AND archived_at IS NULL",
                                           (group_id,)).fetchone()
        if row is None:
            raise EntityNotFound("group not found")
        return row

    def _membership(self, group_id: str, account_id: str | None = None):
        return self.repo.connection.execute(
            "SELECT * FROM group_members WHERE group_id=? AND account_id=?",
            (group_id, account_id or self.account_id)).fetchone()

    def _role(self, group_id: str) -> Role:
        """The actor's role; a non-member cannot even learn that the group exists."""
        self._group(group_id)
        self._repair_ownership(group_id)
        row = self._membership(group_id)
        if row is None or row["status"] != "ACTIVE":
            raise EntityNotFound("group not found")
        return Role(row["role"])

    def _require(self, group_id: str, allowed: set[Role]) -> Role:
        role = self._role(group_id)
        if role not in allowed:
            raise AuthorizationDenied(f"this needs one of: {', '.join(sorted(allowed))}")
        return role

    def _repair_ownership(self, group_id: str) -> None:
        # A group whose last owner deleted their account passes to the longest-standing
        # starosta (else member), so it never becomes unmanageable.
        if self.repo.connection.execute(
                "SELECT 1 FROM group_members WHERE group_id=? AND role='OWNER' AND status='ACTIVE'",
                (group_id,)).fetchone():
            return
        heir = self.repo.connection.execute(
            "SELECT account_id FROM group_members WHERE group_id=? AND status='ACTIVE' "
            "ORDER BY CASE role WHEN 'STAROSTA' THEN 0 ELSE 1 END, joined_at, account_id LIMIT 1",
            (group_id,)).fetchone()
        if heir is not None:
            with self.repo._tx() as conn:
                conn.execute("UPDATE group_members SET role='OWNER',updated_at=? WHERE group_id=? AND account_id=?",
                             (_iso(self.repo.clock.now()), group_id, heir["account_id"]))

    def list(self) -> list[dict[str, Any]]:
        rows = self.repo.connection.execute(
            "SELECT g.id,g.name,g.timezone_name,g.schedule_revision,m.role,"
            "(SELECT count(*) FROM group_members x WHERE x.group_id=g.id AND x.status='ACTIVE') AS member_count "
            "FROM groups g JOIN group_members m ON m.group_id=g.id "
            "WHERE m.account_id=? AND m.status='ACTIVE' AND g.archived_at IS NULL ORDER BY g.name,g.id",
            (self.account_id,)).fetchall()
        return [dict(row) for row in rows]

    def detail(self, group_id: str) -> dict[str, Any]:
        role = self._role(group_id)
        group = self._group(group_id)
        members = self.repo.connection.execute(
            "SELECT m.account_id,m.role,m.joined_at,u.login FROM group_members m "
            "LEFT JOIN auth_users u ON u.account_id=m.account_id "
            "WHERE m.group_id=? AND m.status='ACTIVE' ORDER BY CASE m.role WHEN 'OWNER' THEN 0 "
            "WHEN 'STAROSTA' THEN 1 ELSE 2 END, m.joined_at", (group_id,)).fetchall()
        proposals = self.repo.connection.execute(
            "SELECT * FROM group_proposals WHERE group_id=? AND (? OR proposer_account_id=?) "
            "ORDER BY created_at DESC,id LIMIT 200",
            (group_id, int(role in _PUBLISHERS), self.account_id)).fetchall()
        return {
            "id": group["id"], "name": group["name"], "timezone_name": group["timezone_name"],
            "schedule_revision": int(group["schedule_revision"]), "my_role": role.value,
            # Only what membership needs: who, their role, since when. Never personal state.
            "members": [{"member_id": row["account_id"], "login": row["login"], "role": row["role"],
                         "joined_at": row["joined_at"], "me": row["account_id"] == self.account_id}
                        for row in members],
            "schedule": self._items(group_id),
            "proposals": [self._proposal_out(row) for row in proposals],
        }

    def _items(self, group_id: str) -> list[dict[str, Any]]:
        rows = self.repo.connection.execute(
            "SELECT uid,kind,item_json,revision,updated_at FROM group_schedule_items "
            "WHERE group_id=? AND deleted_at IS NULL ORDER BY uid", (group_id,)).fetchall()
        return [{"uid": row["uid"], "kind": row["kind"], "item": json.loads(row["item_json"]),
                 "revision": int(row["revision"]), "updated_at": row["updated_at"]} for row in rows]

    @staticmethod
    def _proposal_out(row) -> dict[str, Any]:
        return {"id": row["id"], "action": row["action"], "uid": row["uid"], "kind": row["kind"],
                "item": None if row["item_json"] is None else json.loads(row["item_json"]),
                "note": row["note"], "status": row["status"], "mine": None,
                "proposer_id": row["proposer_account_id"], "decided_at": row["decided_at"],
                "decision_reason": row["decision_reason"], "created_at": row["created_at"]}

    # ---- materialization (the only path into member accounts) ---------------------------

    def _snapshot(self, group_id: str, account_id: str, *, empty: bool = False) -> SourceSnapshot:
        source = group_source_id(account_id, group_id)
        if empty:
            return SourceSnapshot(source_system_id=source, series=(), events=(), complete=True)
        series: list[SourceSeries] = []
        events: list[SourceEvent] = []
        for row in self.repo.connection.execute(
                "SELECT uid,kind,item_json,revision,updated_at FROM group_schedule_items "
                "WHERE group_id=? AND deleted_at IS NULL", (group_id,)).fetchall():
            item, sequence, updated = json.loads(row["item_json"]), int(row["revision"]), _dt(row["updated_at"])
            if row["kind"] == "SERIES":
                series.append(SourceSeries(
                    uid=row["uid"], title=item["title"], dtstart_local=datetime.fromisoformat(item["dtstart_local"]),
                    duration_minutes=item["duration_minutes"], recurrence_rule=item["recurrence_rule"],
                    timezone_name=item["timezone_name"], category=ObligationCategory(item["category"]),
                    location_text=item["location_text"], teacher=item["teacher"],
                    exdates_local=tuple(datetime.fromisoformat(v) for v in item["exdates_local"]),
                    changes=tuple(SourceOccurrenceChange(
                        recurrence_local=datetime.fromisoformat(c["recurrence_local"]), cancelled=c["cancelled"],
                        starts_local=None if c["starts_local"] is None else datetime.fromisoformat(c["starts_local"]),
                        location_text=c["location_text"], sequence=sequence, updated_at=updated)
                        for c in item["changes"]),
                    sequence=sequence, updated_at=updated))
            else:
                events.append(SourceEvent(
                    uid=row["uid"], title=item["title"], starts_at=datetime.fromisoformat(item["starts_at"]),
                    ends_at=datetime.fromisoformat(item["ends_at"]), category=ObligationCategory(item["category"]),
                    location_text=item["location_text"], teacher=item["teacher"], cancelled=item["cancelled"],
                    sequence=sequence, updated_at=updated))
        return SourceSnapshot(source_system_id=source, series=tuple(series), events=tuple(events), complete=True)

    def _materialize(self, group_id: str, account_id: str, *, empty: bool = False) -> None:
        source = group_source_id(account_id, group_id)
        reconciliation = SQLiteReconciliationRepository(self.repo)
        try:
            reconciliation.get_source_system(account_id, source)
        except EntityNotFound:
            reconciliation.create_source_system(account_id=account_id, kind=SOURCE_KIND,
                                                actor=ActorCategory.CONNECTOR_INGESTION, source_system_id=source,
                                                policy_context={"group_id": group_id})
        SourceApplier(self.repo, account_id=account_id).apply(self._snapshot(group_id, account_id, empty=empty))

    def _fan_out(self, group_id: str) -> None:
        for row in self.repo.connection.execute(
                "SELECT account_id FROM group_members WHERE group_id=? AND status='ACTIVE'", (group_id,)).fetchall():
            self._materialize(group_id, row["account_id"])

    # ---- lifecycle ---------------------------------------------------------------------

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        group_id = str(payload.get("id") or "")
        if not _ID.match(group_id):
            raise ValidationError("id must be a client-generated identifier (8-128 safe characters)")
        name = _text(payload.get("name"), "name", required=True, limit=120)
        zone = str(payload.get("timezone_name") or "Europe/Moscow")
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationError("timezone_name is not a known time zone") from exc
        self.repo._require_account(self.account_id)
        now = _iso(self.repo.clock.now())
        with self.repo._tx() as conn:
            existing = conn.execute("SELECT created_by FROM groups WHERE id=?", (group_id,)).fetchone()
            if existing is not None:
                if existing["created_by"] != self.account_id:
                    raise ValidationError("group id is already in use")
                return self.detail(group_id)  # idempotent retry
            conn.execute("INSERT INTO groups(id,name,timezone_name,created_by,created_at) VALUES (?,?,?,?,?)",
                         (group_id, name, zone, self.account_id, now))
            conn.execute("INSERT INTO group_members(group_id,account_id,role,status,joined_at,updated_at) "
                         "VALUES (?,?,'OWNER','ACTIVE',?,?)", (group_id, self.account_id, now, now))
        return self.detail(group_id)

    def invite(self, group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require(group_id, _PUBLISHERS)
        days = payload.get("expires_in_days", 7)
        uses = payload.get("max_uses", 50)
        for value, name, high in ((days, "expires_in_days", 30), (uses, "max_uses", 500)):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= high:
                raise ValidationError(f"{name} must be an integer from 1 to {high}")
        code = f"BOTAY-{secrets.token_urlsafe(9)}"
        now = self.repo.clock.now()
        invitation_id = uuid.uuid4().hex
        with self.repo._tx() as conn:
            conn.execute("INSERT INTO group_invitations(id,group_id,code_hash,created_by,created_at,expires_at,max_uses) "
                         "VALUES (?,?,?,?,?,?,?)", (invitation_id, group_id, _sha256(code), self.account_id,
                                                    _iso(now), _iso(now + timedelta(days=days)), uses))
        return {"invitation_id": invitation_id, "code": code, "expires_at": _iso(now + timedelta(days=days)),
                "max_uses": uses}

    def revoke_invitation(self, group_id: str, invitation_id: str) -> dict[str, Any]:
        self._require(group_id, _PUBLISHERS)
        with self.repo._tx() as conn:
            conn.execute("UPDATE group_invitations SET revoked_at=? WHERE id=? AND group_id=? AND revoked_at IS NULL",
                         (_iso(self.repo.clock.now()), invitation_id, group_id))
        return {"revoked": True}

    def join(self, payload: dict[str, Any]) -> dict[str, Any]:
        code = str(payload.get("code") or "").strip()
        now = self.repo.clock.now()
        self.repo._require_account(self.account_id)
        with self.repo._tx() as conn:
            invitation = conn.execute("SELECT * FROM group_invitations WHERE code_hash=?", (_sha256(code),)).fetchone()
            if (invitation is None or invitation["revoked_at"] is not None or _dt(invitation["expires_at"]) <= now
                    or invitation["uses"] >= invitation["max_uses"]
                    or not hmac.compare_digest(invitation["code_hash"], _sha256(code))):
                raise ValidationError("the invitation code is invalid or expired")
            group_id = invitation["group_id"]
            self._group(group_id)
            member = self._membership(group_id)
            if member is not None and member["status"] == "ACTIVE":
                return self.detail(group_id)  # already a member: idempotent, uses not consumed
            if member is not None and member["status"] == "REMOVED":
                raise AuthorizationDenied("you were removed from this group; ask its owner")
            active = conn.execute("SELECT count(*) FROM group_members WHERE group_id=? AND status='ACTIVE'",
                                  (group_id,)).fetchone()[0]
            if active >= MAX_MEMBERS:
                raise ValidationError("the group is full")
            conn.execute(
                "INSERT INTO group_members(group_id,account_id,role,status,joined_at,updated_at) "
                "VALUES (?,?,'MEMBER','ACTIVE',?,?) ON CONFLICT(group_id,account_id) DO UPDATE SET "
                "role='MEMBER',status='ACTIVE',updated_at=excluded.updated_at",
                (group_id, self.account_id, _iso(now), _iso(now)))
            conn.execute("UPDATE group_invitations SET uses=uses+1 WHERE id=?", (invitation["id"],))
            self._materialize(group_id, self.account_id)
        return self.detail(group_id)

    def _deactivate(self, group_id: str, account_id: str, status: str) -> None:
        with self.repo._tx() as conn:
            conn.execute("UPDATE group_members SET status=?,updated_at=? WHERE group_id=? AND account_id=?",
                         (status, _iso(self.repo.clock.now()), group_id, account_id))
            # Retract the shared schedule from that account; their USER layer is untouched
            # and comes back if they rejoin (same stable identities).
            self._materialize(group_id, account_id, empty=True)

    def leave(self, group_id: str) -> dict[str, Any]:
        role = self._role(group_id)
        if role is Role.OWNER:
            others = self.repo.connection.execute(
                "SELECT count(*) FROM group_members WHERE group_id=? AND status='ACTIVE' AND account_id<>?",
                (group_id, self.account_id)).fetchone()[0]
            owners = self.repo.connection.execute(
                "SELECT count(*) FROM group_members WHERE group_id=? AND status='ACTIVE' AND role='OWNER'",
                (group_id,)).fetchone()[0]
            if others and owners == 1:
                raise ValidationError("make someone else an owner before leaving")
        self._deactivate(group_id, self.account_id, "LEFT")
        return {"left": True}

    def remove_member(self, group_id: str, member_id: str) -> dict[str, Any]:
        role = self._require(group_id, _PUBLISHERS)
        target = self._membership(group_id, member_id)
        if target is None or target["status"] != "ACTIVE" or member_id == self.account_id:
            raise EntityNotFound("member not found")
        if role is Role.STAROSTA and target["role"] != Role.MEMBER.value:
            raise AuthorizationDenied("a starosta can remove members only")
        self._deactivate(group_id, member_id, "REMOVED")
        return self.detail(group_id)

    def set_role(self, group_id: str, member_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require(group_id, {Role.OWNER})
        try:
            new_role = Role(str(payload.get("role") or ""))
        except ValueError as exc:
            raise ValidationError("role must be OWNER, STAROSTA or MEMBER") from exc
        target = self._membership(group_id, member_id)
        if target is None or target["status"] != "ACTIVE":
            raise EntityNotFound("member not found")
        with self.repo._tx() as conn:
            conn.execute("UPDATE group_members SET role=?,updated_at=? WHERE group_id=? AND account_id=?",
                         (new_role.value, _iso(self.repo.clock.now()), group_id, member_id))
            owners = conn.execute("SELECT count(*) FROM group_members WHERE group_id=? AND status='ACTIVE' "
                                  "AND role='OWNER'", (group_id,)).fetchone()[0]
            if owners == 0:
                raise ValidationError("a group needs at least one owner")
        return self.detail(group_id)

    # ---- the shared schedule ------------------------------------------------------------

    @staticmethod
    def _expected_revision(payload: dict[str, Any]) -> int:
        expected = payload.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise ValidationError("expected_revision is required")
        return expected

    def _write_item(self, group_id: str, uid: str, kind: str | None, item: dict[str, Any] | None,
                    *, expected_revision: int | None = None) -> None:
        """Apply one upsert (item given) or removal (item None) and fan it out.

        With ``expected_revision`` the revision check and the write are one transaction
        (a conditional bump), so two concurrent publishers cannot both win.
        """
        group = self._group(group_id)
        now = _iso(self.repo.clock.now())
        with self.repo._tx() as conn:
            if expected_revision is not None:
                bumped = conn.execute("UPDATE groups SET schedule_revision=schedule_revision+1 "
                                      "WHERE id=? AND schedule_revision=?", (group_id, expected_revision)).rowcount
                if not bumped:
                    raise VersionConflict("the group schedule changed; reload it")
            else:
                conn.execute("UPDATE groups SET schedule_revision=schedule_revision+1 WHERE id=?", (group_id,))
            current = conn.execute("SELECT * FROM group_schedule_items WHERE group_id=? AND uid=?",
                                   (group_id, uid)).fetchone()
            if item is None:
                if current is None or current["deleted_at"] is not None:
                    raise EntityNotFound("schedule item not found")
                conn.execute("UPDATE group_schedule_items SET deleted_at=?,revision=revision+1,updated_by=?,"
                             "updated_at=? WHERE group_id=? AND uid=?", (now, self.account_id, now, group_id, uid))
            else:
                normalized = normalize_item(str(kind), item, group["timezone_name"])
                if current is not None and current["kind"] != kind:
                    raise ValidationError("a schedule item cannot change its kind")
                if current is None:
                    count = conn.execute("SELECT count(*) FROM group_schedule_items WHERE group_id=? "
                                         "AND deleted_at IS NULL", (group_id,)).fetchone()[0]
                    if count >= MAX_ITEMS:
                        raise ValidationError(f"a group schedule holds at most {MAX_ITEMS} items")
                conn.execute(
                    "INSERT INTO group_schedule_items(group_id,uid,kind,item_json,revision,updated_by,updated_at) "
                    "VALUES (?,?,?,?,1,?,?) ON CONFLICT(group_id,uid) DO UPDATE SET item_json=excluded.item_json,"
                    "revision=group_schedule_items.revision+1,updated_by=excluded.updated_by,"
                    "updated_at=excluded.updated_at,deleted_at=NULL",
                    (group_id, uid, kind, json.dumps(normalized, sort_keys=True), self.account_id, now))
            self._fan_out(group_id)

    def publish(self, group_id: str, uid: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require(group_id, _PUBLISHERS)
        if not _ID.match(uid):
            raise ValidationError("uid must be 8-128 safe characters")
        self._write_item(group_id, uid, str(payload.get("kind") or ""), payload.get("item"),
                         expected_revision=self._expected_revision(payload))
        return self.detail(group_id)

    def unpublish(self, group_id: str, uid: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require(group_id, _PUBLISHERS)
        self._write_item(group_id, uid, None, None, expected_revision=self._expected_revision(payload))
        return self.detail(group_id)

    # ---- proposals ---------------------------------------------------------------------

    def propose(self, group_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._role(group_id)  # any active member
        proposal_id = str(payload.get("id") or "")
        if not _ID.match(proposal_id):
            raise ValidationError("id must be a client-generated identifier (8-128 safe characters)")
        action = str(payload.get("action") or "")
        uid = str(payload.get("uid") or "")
        if action not in ("UPSERT", "REMOVE") or not _ID.match(uid):
            raise ValidationError("action must be UPSERT or REMOVE with a valid uid")
        group = self._group(group_id)
        kind, item_json = None, None
        if action == "UPSERT":
            kind = str(payload.get("kind") or "")
            item_json = json.dumps(normalize_item(kind, payload.get("item"), group["timezone_name"]), sort_keys=True)
        elif self.repo.connection.execute("SELECT 1 FROM group_schedule_items WHERE group_id=? AND uid=? "
                                          "AND deleted_at IS NULL", (group_id, uid)).fetchone() is None:
            raise EntityNotFound("schedule item not found")
        note = _text(payload.get("note"), "note", limit=500)
        with self.repo._tx() as conn:
            existing = conn.execute("SELECT group_id,proposer_account_id FROM group_proposals WHERE id=?",
                                    (proposal_id,)).fetchone()
            if existing is not None:
                if existing["group_id"] != group_id or existing["proposer_account_id"] != self.account_id:
                    raise ValidationError("proposal id is already in use")
            else:
                conn.execute("INSERT INTO group_proposals(id,group_id,proposer_account_id,action,uid,kind,item_json,"
                             "note,status,created_at) VALUES (?,?,?,?,?,?,?,?,'PENDING',?)",
                             (proposal_id, group_id, self.account_id, action, uid, kind, item_json, note,
                              _iso(self.repo.clock.now())))
        return self.detail(group_id)

    def decide(self, group_id: str, proposal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._require(group_id, _PUBLISHERS)
        decision = str(payload.get("decision") or "")
        if decision not in ("APPROVE", "REJECT"):
            raise ValidationError("decision must be APPROVE or REJECT")
        reason = _text(payload.get("reason"), "reason", limit=500)
        row = self.repo.connection.execute("SELECT * FROM group_proposals WHERE id=? AND group_id=?",
                                           (proposal_id, group_id)).fetchone()
        if row is None:
            raise EntityNotFound("proposal not found")
        if row["status"] != "PENDING":
            if row["status"] == ("APPROVED" if decision == "APPROVE" else "REJECTED"):
                return self.detail(group_id)  # idempotent repeat of the same decision
            raise VersionConflict(f"the proposal was already {row['status'].lower()}")
        now = _iso(self.repo.clock.now())
        with self.repo._tx() as conn:
            if decision == "APPROVE":
                item = None if row["item_json"] is None else json.loads(row["item_json"])
                self._write_item(group_id, row["uid"], row["kind"], item)
            conn.execute("UPDATE group_proposals SET status=?,decided_by=?,decided_at=?,decision_reason=? "
                         "WHERE id=? AND status='PENDING'",
                         ("APPROVED" if decision == "APPROVE" else "REJECTED", self.account_id, now, reason,
                          proposal_id))
        return self.detail(group_id)

    def withdraw(self, group_id: str, proposal_id: str) -> dict[str, Any]:
        self._role(group_id)
        with self.repo._tx() as conn:
            changed = conn.execute("UPDATE group_proposals SET status='WITHDRAWN',decided_at=? WHERE id=? AND "
                                   "group_id=? AND proposer_account_id=? AND status='PENDING'",
                                   (_iso(self.repo.clock.now()), proposal_id, group_id, self.account_id)).rowcount
        if not changed:
            raise EntityNotFound("pending proposal not found")
        return self.detail(group_id)
