"""Group and personal-overlay operations, executed through the existing sync pipeline.

Every mutation is an operation of ``sync.commands.SyncService``: it carries a
client mutation id (``op_id``), runs inside one transaction together with its
``client_operations`` record, and a replay returns the recorded result. The same
handlers serve ``/api/v1/sync`` and the REST routes under ``/api/v1/groups``.

Two kinds of operations, never mixed:

* group-wide (``GROUP_WIDE_OPS``) — publication, edits, moderation, roles, invites.
  They need the server's confirmation (the client sends them online and the sync
  endpoint refuses them), an explicit ``expected_version`` for every versioned
  entity (a stale one is a CONFLICT with ``current_version``, never
  last-writer-wins) and a capability of the caller's role, checked here.
* personal (``PERSONAL_OPS``) — the member's own overlay. Field-level, offline
  capable, never touching group-owned rows.

A caller who is not an active member gets NOT_FOUND for everything about the group
(existence is not revealed); a member lacking a capability gets FORBIDDEN.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Callable

from student_execution_os.domain.errors import AuthorizationDenied, EntityNotFound, ValidationError
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso
from student_execution_os.sync.commands import APPLIED, CONFLICT, NOOP, REJECTED, Outcome

from . import external, messages
from .errors import ExternalOwnedField, GroupRateLimited, SharedVersionConflict
from .fanout import Fanout, event_critical, lead_moment, notable_event, reminder_key
from .model import (
    AcceptanceState,
    Capability,
    Criticality,
    EventSource,
    Group,
    GroupJoinPolicy,
    GroupSettings,
    GroupStatus,
    GroupType,
    InviteKind,
    Membership,
    MembershipStatus,
    ProposalStatus,
    Role,
    SharedKind,
    SharedStatus,
    can_rejoin,
    capabilities,
    clean_text,
    clean_title,
    parse_announcement_payload,
    parse_event_payload,
    parse_instant,
    parse_obligation_payload,
    parse_payload,
    patch_event_state,
    patch_obligation_state,
    require_group_transition,
    require_membership_transition,
    require_proposal_transition,
    require_shared_transition,
)
from .repository import SQLiteGroupRepository
from .views import announcement_view, event_view, group_view, invite_view, membership_view, obligation_view, proposal_view

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")

GROUP_WIDE_OPS = frozenset({
    "group.create", "group.update", "group.archive", "group.unarchive", "group.delete", "group.join", "group.leave",
    "invite.create", "invite.revoke", "membership.update",
    "shared_event.create", "shared_event.update", "shared_event.cancel", "shared_event.bind_external",
    "shared_event.detach_external",
    "shared_obligation.create", "shared_obligation.update", "shared_obligation.cancel",
    "announcement.create", "announcement.update", "announcement.retract",
    "proposal.create", "proposal.approve", "proposal.reject", "proposal.withdraw",
})
PERSONAL_OPS = frozenset({
    "shared_event_state.update", "shared_obligation_state.update", "announcement_state.update", "group_preferences.update",
})

# Anti-spam: at most N actions of a kind per actor in the window (durable: counted in the audit).
RATE_LIMITS = {
    "publish": (("CREATE",), 60, timedelta(hours=1)),
    "propose": (("PROPOSE",), 10, timedelta(hours=1)),
    "invite": (("CREATE_INVITE",), 30, timedelta(hours=1)),
    "group": (("CREATE_GROUP",), 20, timedelta(days=1)),
    "join": ((), 20, timedelta(minutes=10)),
}
INVITE_TTL_DEFAULT = timedelta(days=14)
_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def _expected(payload: dict[str, Any]) -> int:
    raw = payload.get("expected_version")
    if raw is None or raw == "":
        raise ValidationError("expected_version is required for a change of shared data")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError("expected_version must be a whole number") from exc


def _check_version(entity: str, expected: int, current: int) -> None:
    if expected != current:
        raise SharedVersionConflict(entity, expected, current)


def _event_raw(row: dict[str, Any]) -> dict[str, Any]:
    return {"kind": SharedKind.SHARED_EVENT.value, "title": row["title"], "event_kind": row["event_kind"],
            "starts_at": row["starts_at"], "ends_at": row["ends_at"], "timezone": row["timezone"],
            "attendance_default": row["attendance_default"], "group_criticality": row["group_criticality"],
            "description": row["description"], "location": row["location"]}


def _obligation_raw(row: dict[str, Any]) -> dict[str, Any]:
    return {"kind": SharedKind.SHARED_OBLIGATION.value, "title": row["title"], "obligation_kind": row["kind"],
            "deadline": row["deadline"], "timezone": row["timezone"], "group_criticality": row["group_criticality"],
            "description": row["description"], "estimated_effort_hint_minutes": row["estimated_effort_hint_minutes"]}


def _announcement_raw(row: dict[str, Any]) -> dict[str, Any]:
    return {"kind": SharedKind.SHARED_ANNOUNCEMENT.value, "title": row["title"], "body": row["body"],
            "importance": row["importance"]}


def _diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[Any]]:
    return {key: [before.get(key), value] for key, value in after.items() if before.get(key) != value and key != "kind"}


_EVENT_COLUMNS = {"title": "title", "event_kind": "event_kind", "starts_at": "starts_at", "ends_at": "ends_at",
                  "timezone": "timezone", "attendance_default": "attendance_default",
                  "group_criticality": "group_criticality", "description": "description", "location": "location"}
_OBLIGATION_COLUMNS = {"title": "title", "obligation_kind": "kind", "deadline": "deadline", "timezone": "timezone",
                       "group_criticality": "group_criticality", "description": "description",
                       "estimated_effort_hint_minutes": "estimated_effort_hint_minutes"}
_ANNOUNCEMENT_COLUMNS = {"title": "title", "body": "body", "importance": "importance"}
# Fields of a bound event owned by the external source.
EXTERNAL_OWNED = frozenset({"starts_at", "ends_at", "timezone", "location"})


class GroupCommands:
    def __init__(self, repo: SQLiteCanonicalRepository, *, account_id: str, now: datetime) -> None:
        self.repo = repo
        self.me = account_id
        self.now = now
        self.groups = SQLiteGroupRepository(repo)
        self.fanout = Fanout(repo, self.groups, now)
        self.mutation_id: str | None = None
        self.handlers: dict[str, Callable[[str, dict[str, Any]], Outcome]] = {
            "group.create": self.group_create,
            "group.update": self.group_update,
            "group.archive": lambda gid, p: self._group_status(gid, p, GroupStatus.ARCHIVED),
            "group.unarchive": lambda gid, p: self._group_status(gid, p, GroupStatus.ACTIVE),
            "group.delete": lambda gid, p: self._group_status(gid, p, GroupStatus.DELETED),
            "group.join": self.group_join,
            "group.leave": self.group_leave,
            "invite.create": self.invite_create,
            "invite.revoke": self.invite_revoke,
            "membership.update": self.membership_update,
            "shared_event.create": self.event_create,
            "shared_event.update": self.event_update,
            "shared_event.cancel": self.event_cancel,
            "shared_event.bind_external": self.event_bind_external,
            "shared_event.detach_external": self.event_detach_external,
            "shared_obligation.create": self.obligation_create,
            "shared_obligation.update": self.obligation_update,
            "shared_obligation.cancel": self.obligation_cancel,
            "announcement.create": self.announcement_create,
            "announcement.update": self.announcement_update,
            "announcement.retract": self.announcement_retract,
            "proposal.create": self.proposal_create,
            "proposal.approve": self.proposal_approve,
            "proposal.reject": self.proposal_reject,
            "proposal.withdraw": self.proposal_withdraw,
            "shared_event_state.update": self.event_state_update,
            "shared_obligation_state.update": self.obligation_state_update,
            "announcement_state.update": self.announcement_state_update,
            "group_preferences.update": self.preferences_update,
        }

    # ---- access ---------------------------------------------------------------------------

    def access(self, group_id: str, capability: Capability | None = None, *, writable: bool = True,
               allow_pending: bool = False) -> tuple[Group, Membership, frozenset[Capability]]:
        """The caller's active membership in the group, or NOT_FOUND; then the capability, or FORBIDDEN."""
        group = self.groups.find_group(group_id) if group_id else None
        membership = None if group is None else self.groups.membership(group_id, self.me)
        allowed = {MembershipStatus.ACTIVE} | ({MembershipStatus.PENDING} if allow_pending else set())
        if group is None or group.status is GroupStatus.DELETED or membership is None or membership.status not in allowed:
            raise EntityNotFound("group not found")
        caps = capabilities(membership.role, group.settings) if membership.status is MembershipStatus.ACTIVE else frozenset()
        if capability is not None and capability not in caps:
            raise AuthorizationDenied(f"your role in this group cannot {capability.value.lower().replace('_', ' ')}")
        if writable and capability is not None and group.status is GroupStatus.ARCHIVED:
            raise AuthorizationDenied("the group is archived")
        return group, membership, caps

    def _entity(self, kind: SharedKind, entity_id: str, capability: Capability | None,
                *, writable: bool = True) -> tuple[dict[str, Any], Group, Membership, frozenset[Capability]]:
        row = self.groups.entity(kind, entity_id) if entity_id else None
        if row is None:
            raise EntityNotFound(f"{kind.value.lower().replace('_', ' ')} not found")
        group, membership, caps = self.access(row["group_id"], capability, writable=writable)
        return row, group, membership, caps

    def _rate(self, name: str, group_id: str | None = None) -> None:
        actions, limit, window = RATE_LIMITS[name]
        if name == "join":
            # Every attempt counts, including guesses of unknown codes (recorded as REJECTED operations).
            used = int(self.repo.connection.execute(
                "SELECT count(*) FROM client_operations WHERE account_id=? AND op_type='group.join' AND created_at>=?",
                (self.me, _iso(self.now - window))).fetchone()[0])
        else:
            used = self.groups.actions_since(self.me, actions, self.now - window, group_id)
        if used >= limit:
            raise GroupRateLimited("too many group actions in a short time; try again later")

    def _audit(self, group_id: str, kind: str, entity_id: str, action: str, **kwargs) -> None:
        self.groups.audit(group_id=group_id, entity_kind=kind, entity_id=entity_id, action=action, actor=self.me,
                          now=self.now, mutation_id=self.mutation_id, **kwargs)

    def _new_id(self, entity_id: str, prefix: str) -> str:
        if entity_id:
            if not _ID.match(entity_id):
                raise ValidationError("id must be a client-generated identifier (8-128 safe characters)")
            return entity_id
        seed = f"{self.me}|{self.mutation_id or secrets.token_hex(8)}|{prefix}"
        return f"{prefix}-{_hash(seed)[:28]}"

    def _author(self, account_id: str) -> str:
        return self.groups.display_name(account_id)

    def _group_out(self, group: Group, status: str = APPLIED, code: str | None = None, message: str | None = None) -> Outcome:
        membership = self.groups.membership(group.id, self.me)
        caps = capabilities(membership.role, group.settings) if membership and membership.status is MembershipStatus.ACTIVE else frozenset()
        count = len(self.groups.active_member_ids(group.id))
        return Outcome(status, group_view(group, membership, caps, member_count=count), code, message)

    # ---- groups -----------------------------------------------------------------------------

    def group_create(self, group_id: str, payload: dict[str, Any]) -> Outcome:
        group_id = self._new_id(group_id, "grp")
        existing = self.groups.find_group(group_id)
        if existing is not None:
            if existing.owner_account_id == self.me:
                return self._group_out(existing, NOOP, "ALREADY_EXISTS")
            raise ValidationError("group id is already in use")
        self._rate("group")
        unknown = set(payload) - {"name", "description", "type", "settings", "join_policy"}
        if unknown:
            raise ValidationError("unknown fields: " + ", ".join(sorted(unknown)))
        from student_execution_os.reminders.store import ReminderStore
        settings_raw = dict(payload.get("settings") or {})
        settings_raw.setdefault("default_timezone", ReminderStore(self.repo).prefs(self.me).timezone_name)
        settings = GroupSettings().patched(settings_raw)
        policy = GroupJoinPolicy().patched(payload.get("join_policy") or {})
        try:
            group_type = GroupType(payload.get("type") or GroupType.ACADEMIC.value)
        except ValueError as exc:
            raise ValidationError("type must be ACADEMIC, PROJECT or OTHER") from exc
        group = Group(id=group_id, name=clean_title(payload.get("name"), "name", 120),
                      description=clean_text(payload.get("description"), "description", 2000), type=group_type,
                      owner_account_id=self.me, version=1, status=GroupStatus.ACTIVE, settings=settings,
                      join_policy=policy, created_at=self.now, updated_at=self.now)
        self.groups.insert_group(group)
        self.groups.insert_membership(Membership(group_id, self.me, Role.OWNER, MembershipStatus.ACTIVE, 1, self.now, self.now))
        self._audit(group_id, "GROUP", group_id, "CREATE_GROUP", new_version=1)
        return self._group_out(group)

    def group_update(self, group_id: str, payload: dict[str, Any]) -> Outcome:
        group, _, _ = self.access(group_id, Capability.MANAGE_SETTINGS)
        expected = _expected(payload)
        _check_version("group", expected, group.version)
        unknown = set(payload) - {"expected_version", "name", "description", "type", "settings", "join_policy"}
        if unknown:
            raise ValidationError("fields cannot be changed here: " + ", ".join(sorted(unknown)))
        updated = group
        if "name" in payload:
            updated = replace(updated, name=clean_title(payload["name"], "name", 120))
        if "description" in payload:
            updated = replace(updated, description=clean_text(payload["description"], "description", 2000))
        if "type" in payload:
            try:
                updated = replace(updated, type=GroupType(payload["type"]))
            except ValueError as exc:
                raise ValidationError("type must be ACADEMIC, PROJECT or OTHER") from exc
        if "settings" in payload:
            updated = replace(updated, settings=updated.settings.patched(payload["settings"] or {}))
        if "join_policy" in payload:
            updated = replace(updated, join_policy=updated.join_policy.patched(payload["join_policy"] or {}))
        before, after = group_view(group, None), group_view(updated, None)
        changes = {k: [before[k], after[k]] for k in ("name", "description", "type", "settings", "join_policy")
                   if before[k] != after[k]}
        if not changes:
            return self._group_out(group, NOOP, "NOTHING_TO_CHANGE")
        saved = self.groups.save_group(updated, expected, self.now)
        self._audit(group_id, "GROUP", group_id, "UPDATE_GROUP", previous_version=expected, new_version=saved.version,
                    changes=changes)
        return self._group_out(saved)

    def _group_status(self, group_id: str, payload: dict[str, Any], target: GroupStatus) -> Outcome:
        capability = Capability.DELETE_GROUP if target is GroupStatus.DELETED else Capability.ARCHIVE_GROUP
        group, _, _ = self.access(group_id, capability, writable=False)
        if group.status is target:
            return self._group_out(group, NOOP, f"ALREADY_{target.value}")
        expected = _expected(payload)
        _check_version("group", expected, group.version)
        require_group_transition(group.status, target)
        saved = self.groups.save_group(replace(group, status=target), expected, self.now)
        if target is GroupStatus.DELETED:
            self.repo.connection.execute(
                "UPDATE group_invites SET status='REVOKED',revoked_at=?,version=version+1 WHERE group_id=? AND status='ACTIVE'",
                (_iso(self.now), group_id))
        self._audit(group_id, "GROUP", group_id, {GroupStatus.ARCHIVED: "ARCHIVE_GROUP", GroupStatus.ACTIVE: "UNARCHIVE_GROUP",
                                                  GroupStatus.DELETED: "DELETE_GROUP"}[target],
                    previous_version=expected, new_version=saved.version,
                    changes={"status": [group.status.value, target.value]})
        return Outcome(APPLIED, group_view(saved, self.groups.membership(group_id, self.me)))

    # ---- joining ----------------------------------------------------------------------------

    def group_join(self, _entity_id: str, payload: dict[str, Any]) -> Outcome:
        self._rate("join")
        token, code = payload.get("token"), payload.get("code")
        if bool(token) == bool(code):
            raise ValidationError("give either an invite token or a join code")
        lookup = _hash(str(token)) if token else _hash("code:" + normalize_code(code))
        invite = self.groups.invite_by_hash(lookup)
        group = None if invite is None else self.groups.find_group(invite["group_id"])
        if invite is None or group is None or not self._invite_usable(invite, group):
            # The refusal is recorded as the operation's result, which the join rate limit counts.
            return Outcome(REJECTED, None, "INVITE_UNAVAILABLE", "this invite is not valid (revoked, expired or used up)")
        current = self.groups.membership(group.id, self.me)
        target = MembershipStatus.PENDING if group.join_policy.approval_required else MembershipStatus.ACTIVE
        if current is not None:
            if current.status in (MembershipStatus.ACTIVE, MembershipStatus.PENDING):
                return self._group_out(group, NOOP, "ALREADY_" + current.status.value)
            if not can_rejoin(current.status, group.join_policy):
                return Outcome(REJECTED, None, "JOIN_NOT_ALLOWED", "you cannot join this group with an invite")
            require_membership_transition(current.status, target)
            self.groups.save_membership(replace(current, status=target, role=Role.MEMBER, joined_at=self.now),
                                        current.version, self.now)
        else:
            self.groups.insert_membership(Membership(group.id, self.me, Role.MEMBER, target, 1, self.now, self.now))
        self.repo.connection.execute("UPDATE group_invites SET use_count=use_count+1 WHERE id=?", (invite["id"],))
        self._audit(group.id, "MEMBERSHIP", self.me, "JOIN", changes={"status": [None if current is None else current.status.value,
                                                                                  target.value], "invite_id": invite["id"]})
        outcome = self._group_out(group)
        return replace(outcome, entity={**outcome.entity, "subscription_setup": target is MembershipStatus.ACTIVE})

    def _invite_usable(self, invite: dict[str, Any], group: Group) -> bool:
        if invite["status"] != "ACTIVE" or group.status is not GroupStatus.ACTIVE:
            return False
        if invite["expires_at"] and _dt(invite["expires_at"]) <= self.now:
            return False
        if invite["max_uses"] is not None and int(invite["use_count"]) >= int(invite["max_uses"]):
            return False
        enabled = group.join_policy.invite_links_enabled if invite["kind"] == InviteKind.LINK.value else group.join_policy.join_codes_enabled
        return enabled

    def group_leave(self, group_id: str, payload: dict[str, Any]) -> Outcome:
        group, membership, _ = self.access(group_id, None, writable=False, allow_pending=True)
        if membership.role is Role.OWNER:
            return Outcome(REJECTED, None, "OWNER_CANNOT_LEAVE", "hand the group over to another member first")
        require_membership_transition(membership.status, MembershipStatus.LEFT)
        saved = self.groups.save_membership(replace(membership, status=MembershipStatus.LEFT), membership.version, self.now)
        self._audit(group_id, "MEMBERSHIP", self.me, "LEAVE", previous_version=membership.version, new_version=saved.version,
                    changes={"status": [membership.status.value, "LEFT"]})
        # Personal overlays stay: coming back later restores them.
        return Outcome(APPLIED, {"group_id": group_id, "status": "LEFT"})

    # ---- invites ------------------------------------------------------------------------------

    def invite_create(self, group_id: str, payload: dict[str, Any]) -> Outcome:
        group, _, _ = self.access(group_id, Capability.MANAGE_INVITES)
        self._rate("invite", group_id)
        kind = InviteKind(payload.get("kind") or InviteKind.LINK.value)
        if kind is InviteKind.LINK and not group.join_policy.invite_links_enabled:
            raise ValidationError("invite links are turned off for this group")
        if kind is InviteKind.CODE and not group.join_policy.join_codes_enabled:
            raise ValidationError("join codes are turned off for this group")
        hours = payload.get("expires_in_hours")
        ttl = INVITE_TTL_DEFAULT if hours in (None, "") else timedelta(hours=int(hours))
        if not timedelta(hours=1) <= ttl <= timedelta(days=90):
            raise ValidationError("expires_in_hours must be between 1 and 2160")
        max_uses = payload.get("max_uses")
        max_uses = None if max_uses in (None, "") else int(max_uses)
        invite_id = self._new_id("", "inv")
        if self.groups.invite(invite_id) is not None:
            return Outcome(NOOP, invite_view(self.groups.invite(invite_id)), "ALREADY_CREATED",
                           "the invite was created by this request before; its secret is shown only once")
        secret: dict[str, Any] = {}
        if kind is InviteKind.LINK:
            token = secrets.token_urlsafe(24)
            values = {"token_hash": _hash(token), "code": None, "token_hint": token[-4:]}
            secret = {"token": token, "path": f"/join/{token}"}
        else:
            prefix = re.sub(r"[^A-Z0-9]", "", group.name.upper())[:6] or "G"
            random_part = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
            code = f"{prefix}-{random_part[:4]}-{random_part[4:]}"
            values = {"token_hash": _hash("code:" + normalize_code(code)), "code": code, "token_hint": random_part[-4:]}
        self.groups.insert_invite({"id": invite_id, "group_id": group_id, "kind": kind.value, **values,
                                   "created_by_account_id": self.me, "created_at": self.now, "expires_at": self.now + ttl,
                                   "max_uses": max_uses})
        self._audit(group_id, "INVITE", invite_id, "CREATE_INVITE", new_version=1, changes={"kind": kind.value})
        # The link token is returned once and never stored (not even in the operation log).
        return Outcome(APPLIED, invite_view(self.groups.invite(invite_id)), transient=secret or None)

    def invite_revoke(self, invite_id: str, payload: dict[str, Any]) -> Outcome:
        invite = self.groups.invite(invite_id) if invite_id else None
        if invite is None:
            raise EntityNotFound("invite not found")
        self.access(invite["group_id"], Capability.MANAGE_INVITES, writable=False)
        if invite["status"] == "REVOKED":
            return Outcome(NOOP, invite_view(invite), "ALREADY_REVOKED")
        expected = _expected(payload)
        _check_version("invite", expected, int(invite["version"]))
        self.repo.connection.execute(
            "UPDATE group_invites SET status='REVOKED',revoked_at=?,version=version+1 WHERE id=? AND version=?",
            (_iso(self.now), invite_id, expected))
        self._audit(invite["group_id"], "INVITE", invite_id, "REVOKE_INVITE", previous_version=expected, new_version=expected + 1)
        return Outcome(APPLIED, invite_view(self.groups.invite(invite_id)))

    # ---- membership / roles --------------------------------------------------------------------

    def membership_update(self, group_id: str, payload: dict[str, Any]) -> Outcome:
        group, me, caps = self.access(group_id, None, writable=True)
        unknown = set(payload) - {"account_id", "expected_version", "role", "status"}
        if unknown:
            raise ValidationError("unknown fields: " + ", ".join(sorted(unknown)))
        target_id = str(payload.get("account_id") or "")
        if target_id == self.me:
            raise ValidationError("use leave to change your own membership")
        target = self.groups.membership(group_id, target_id)
        if target is None:
            raise EntityNotFound("member not found")
        expected = _expected(payload)
        _check_version("membership", expected, target.version)
        updated = target
        changes: dict[str, list[Any]] = {}
        if payload.get("role"):
            role = Role(payload["role"])
            if Capability.MANAGE_ROLES not in caps:
                raise AuthorizationDenied("only the owner manages roles")
            if target.status is not MembershipStatus.ACTIVE:
                raise ValidationError("only an active member can get a role")
            if role is Role.OWNER:
                # Handing the group over: the previous owner stays as an admin.
                self.groups.save_membership(replace(me, role=Role.ADMIN), me.version, self.now)
                self.groups.save_group(replace(group, owner_account_id=target_id), group.version, self.now)
            updated = replace(updated, role=role)
            changes["role"] = [target.role.value, role.value]
        if payload.get("status"):
            status = MembershipStatus(payload["status"])
            if Capability.MANAGE_MEMBERS not in caps:
                raise AuthorizationDenied("your role cannot manage members")
            if target.role is Role.OWNER:
                raise AuthorizationDenied("the owner cannot be removed or blocked")
            if target.role is Role.ADMIN and Capability.MANAGE_ROLES not in caps:
                raise AuthorizationDenied("only the owner can remove or block an admin")
            if status is not target.status:
                require_membership_transition(target.status, status)
                updated = replace(updated, status=status)
                if status is not MembershipStatus.ACTIVE and updated.role is not Role.MEMBER:
                    updated = replace(updated, role=Role.MEMBER)  # a removed admin keeps no role
                changes["status"] = [target.status.value, status.value]
        if not changes:
            return Outcome(NOOP, membership_view(target, self._author(target_id)), "NOTHING_TO_CHANGE")
        saved = self.groups.save_membership(updated, expected, self.now)
        self._audit(group_id, "MEMBERSHIP", target_id, "UPDATE_MEMBERSHIP", previous_version=expected,
                    new_version=saved.version, changes=changes)
        if changes.get("status") == [MembershipStatus.PENDING.value, MembershipStatus.ACTIVE.value]:
            self.fanout.notify(group, kind=SharedKind.SHARED_ANNOUNCEMENT, entity={"id": group_id},
                               dedupe=f"{group_id}:admitted:{target_id}", critical=False, exclude=None,
                               recipients=[target_id], respect_behavior=False,
                               compose=lambda locale, _tz, _critical: messages.membership_approved(group.name, locale=locale))
        return Outcome(APPLIED, membership_view(saved, self._author(target_id)))

    # ---- shared events --------------------------------------------------------------------------

    def _event_out(self, row: dict[str, Any], status: str = APPLIED, code: str | None = None,
                   message: str | None = None) -> Outcome:
        binding = self.groups.binding(row["external_binding_id"]) if row["external_binding_id"] else None
        return Outcome(status, event_view(row, binding, self._author(row["author_account_id"])), code, message)

    def _publish_event(self, group: Group, entity_id: str, raw: dict[str, Any], *, proposal_id: str | None = None,
                       allow_duplicate: bool = False, local_event_id: str | None = None) -> Outcome:
        identity = official = None
        if local_event_id:
            identity = external.identity_of_local_event(self.repo, self.me, local_event_id)
            official = external.official_fields(self.repo, self.me, local_event_id)
            raw = {**raw, "starts_at": official.starts_at.isoformat(), "ends_at": official.ends_at.isoformat(),
                   "location": None}
            if not raw.get("title"):
                raw["title"] = official.title
        parsed = parse_event_payload(raw, default_timezone=group.settings.default_timezone)
        if not allow_duplicate:
            duplicate = self._external_duplicate(group.id, identity, parsed.event_kind.value) if identity else \
                self.groups.published_duplicate(group.id, title=parsed.title, event_kind=parsed.event_kind.value,
                                                starts_at=parsed.starts_at)
            if duplicate:
                return Outcome(REJECTED, {"duplicate_of": duplicate}, "POSSIBLE_DUPLICATE",
                               "the same event is already published in this group")
        binding_id = None if identity is None else f"bind-{_hash(entity_id)[:28]}"
        self.groups.insert_entity(SharedKind.SHARED_EVENT, {
            "id": entity_id, "group_id": group.id, "title": parsed.title, "description": parsed.description,
            "event_kind": parsed.event_kind.value, "starts_at": _iso(parsed.starts_at), "ends_at": _iso(parsed.ends_at),
            "timezone": parsed.timezone, "location": parsed.location,
            "source": (EventSource.EXTERNAL_ANNOTATION if identity else EventSource.GROUP_MANUAL).value,
            "external_binding_id": binding_id, "attendance_default": parsed.attendance_default.value,
            "group_criticality": parsed.group_criticality.value, "author_account_id": self.me, "proposal_id": proposal_id,
            "version": 1, "status": SharedStatus.PUBLISHED.value, "created_at": _iso(self.now), "updated_at": _iso(self.now),
        })
        if identity is not None:
            self._insert_binding(binding_id, group.id, entity_id, identity, official)
        row = self.groups.entity(SharedKind.SHARED_EVENT, entity_id)
        self._audit(group.id, SharedKind.SHARED_EVENT.value, entity_id, "CREATE", new_version=1, proposal_id=proposal_id,
                    binding_id=binding_id, changes={"payload": parsed.to_json()})
        if notable_event(row):
            tz = row["timezone"]
            self.fanout.notify(group, kind=SharedKind.SHARED_EVENT, entity=row, dedupe=f"{entity_id}:v1",
                               critical=event_critical(row), exclude=self.me,
                               compose=lambda locale, zone, critical: messages.event_published(row, locale=locale, timezone_name=zone or tz,
                                                                                     critical=event_critical(row)))
        return self._event_out(row)

    def _external_duplicate(self, group_id: str, identity: external.ExternalIdentity, event_kind: str) -> str | None:
        row = self.repo.connection.execute(
            "SELECT e.id FROM external_event_bindings b JOIN shared_events e ON e.id=b.shared_event_id "
            "WHERE b.group_id=? AND b.status='ACTIVE' AND b.external_source_key=? AND b.external_event_uid=? "
            "AND coalesce(b.external_occurrence_key,'')=? AND e.status='PUBLISHED' AND e.event_kind=?",
            (group_id, identity.source_key, identity.event_uid, identity.occurrence_key or "", event_kind),
        ).fetchone()
        return None if row is None else row["id"]

    def _insert_binding(self, binding_id: str, group_id: str, event_id: str, identity: external.ExternalIdentity,
                        official: external.OfficialFields) -> None:
        self.groups.insert_binding({
            "id": binding_id, "group_id": group_id, "shared_event_id": event_id, "external_source_key": identity.source_key,
            "external_event_uid": identity.event_uid, "external_occurrence_key": identity.occurrence_key,
            "relation_kind": "ANNOTATES", "bound_by_account_id": self.me, "official_title": official.title,
            "official_starts_at": _iso(official.starts_at), "official_ends_at": _iso(official.ends_at),
            "official_location": official.location, "version": 1, "status": "ACTIVE",
            "created_at": _iso(self.now), "updated_at": _iso(self.now),
        })

    def event_create(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        entity_id = self._new_id(entity_id, "sev")
        existing = self.groups.entity(SharedKind.SHARED_EVENT, entity_id)
        if existing is not None:
            self.access(existing["group_id"], Capability.VIEW_SHARED, writable=False)
            return self._event_out(existing, NOOP, "ALREADY_EXISTS")
        if self.groups.id_taken(entity_id):
            raise ValidationError("id is already in use")
        raw = dict(payload)
        group_id = str(raw.pop("group_id", "") or "")
        allow_duplicate = bool(raw.pop("allow_duplicate", False))
        local_event_id = raw.pop("external_local_event_id", None)
        group, _, caps = self.access(group_id, None)
        if Capability.PUBLISH_SHARED not in caps:
            if Capability.CREATE_PROPOSAL in caps:
                return Outcome(REJECTED, None, "PROPOSAL_REQUIRED", "members suggest events; a scheduler publishes them")
            raise AuthorizationDenied("your role cannot publish")
        if local_event_id and Capability.BIND_EXTERNAL not in caps:
            raise AuthorizationDenied("your role cannot annotate external events")
        self._rate("publish", group_id)
        raw.setdefault("kind", SharedKind.SHARED_EVENT.value)
        return self._publish_event(group, entity_id, raw, allow_duplicate=allow_duplicate, local_event_id=local_event_id)

    def event_update(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _, _ = self._entity(SharedKind.SHARED_EVENT, entity_id, Capability.PUBLISH_SHARED)
        expected = _expected(payload)
        patch = {k: v for k, v in payload.items() if k != "expected_version"}
        unknown = set(patch) - set(_EVENT_COLUMNS)
        if unknown:
            raise ValidationError("fields cannot be changed here: " + ", ".join(sorted(unknown)))
        if row["status"] != SharedStatus.PUBLISHED.value:
            return self._event_out(row, CONFLICT, "ENTITY_CANCELLED", "a cancelled event cannot be edited")
        _check_version("shared event", expected, int(row["version"]))
        bound = row["external_binding_id"] is not None
        if bound and EXTERNAL_OWNED & set(patch):
            raise ExternalOwnedField("time, time zone and room of this event come from its external calendar")
        before = _event_raw(row)
        merged = {**before, **patch}
        if "starts_at" in patch and "ends_at" not in patch:
            # Moving the start keeps the duration.
            duration = _dt(row["ends_at"]) - _dt(row["starts_at"])
            merged["ends_at"] = (parse_instant(patch["starts_at"], "starts_at") + duration).isoformat()
        after = parse_event_payload(merged, default_timezone=group.settings.default_timezone).to_json()
        normalized_before = parse_event_payload(before, default_timezone=group.settings.default_timezone).to_json()
        changes = _diff(normalized_before, after)
        if not changes:
            return self._event_out(row, NOOP, "NOTHING_TO_CHANGE")
        values = {_EVENT_COLUMNS[k]: (_iso(_dt(v[1])) if k in ("starts_at", "ends_at") else v[1]) for k, v in changes.items()}
        saved = self.groups.update_entity(SharedKind.SHARED_EVENT, entity_id, expected, values, self.now)
        self._audit(group.id, SharedKind.SHARED_EVENT.value, entity_id, "UPDATE", previous_version=expected,
                    new_version=int(saved["version"]), changes=changes)
        self._after_event_change(group, row, saved, changes)
        return self._event_out(saved)

    def _after_event_change(self, group: Group, before: dict[str, Any], after: dict[str, Any], changes: dict[str, Any]) -> None:
        old_start, new_start = _dt(before["starts_at"]), _dt(after["starts_at"])
        if old_start != new_start:
            self.fanout.event_moved(after, old_start, new_start)
        critical = event_critical(after)
        self.fanout.notify(group, kind=SharedKind.SHARED_EVENT, entity=after, dedupe=f"{after['id']}:v{after['version']}",
                           critical=critical, exclude=self.me,
                           compose=lambda locale, zone, critical: messages.event_changed(after, changes, locale=locale,
                                                                               timezone_name=zone, critical=critical))

    def event_cancel(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _, _ = self._entity(SharedKind.SHARED_EVENT, entity_id, Capability.PUBLISH_SHARED)
        if row["status"] == SharedStatus.CANCELLED.value:
            return self._event_out(row, NOOP, "ALREADY_CANCELLED")
        expected = _expected(payload)
        _check_version("shared event", expected, int(row["version"]))
        require_shared_transition(SharedKind.SHARED_EVENT, SharedStatus.PUBLISHED, SharedStatus.CANCELLED)
        saved = self.groups.update_entity(SharedKind.SHARED_EVENT, entity_id, expected,
                                          {"status": SharedStatus.CANCELLED.value, "cancelled_at": _iso(self.now)}, self.now)
        reason = clean_text(payload.get("reason"), "reason", 500)
        self._audit(group.id, SharedKind.SHARED_EVENT.value, entity_id, "CANCEL", previous_version=expected,
                    new_version=int(saved["version"]), changes={"status": ["PUBLISHED", "CANCELLED"], "reason": reason})
        self.fanout.event_cancelled(saved)
        critical = event_critical(saved)
        self.fanout.notify(group, kind=SharedKind.SHARED_EVENT, entity=saved, dedupe=f"{entity_id}:cancelled",
                           critical=critical, exclude=self.me,
                           compose=lambda locale, zone, critical: messages.event_cancelled(saved, locale=locale, timezone_name=zone,
                                                                                 critical=critical))
        return self._event_out(saved)

    def event_bind_external(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _, caps = self._entity(SharedKind.SHARED_EVENT, entity_id, Capability.BIND_EXTERNAL)
        if Capability.PUBLISH_SHARED not in caps:
            raise AuthorizationDenied("your role cannot change shared events")
        if row["status"] != SharedStatus.PUBLISHED.value:
            return self._event_out(row, CONFLICT, "ENTITY_CANCELLED", "a cancelled event cannot be bound")
        expected = _expected(payload)
        _check_version("shared event", expected, int(row["version"]))
        local_event_id = str(payload.get("local_event_id") or "")
        identity = external.identity_of_local_event(self.repo, self.me, local_event_id)
        current = self.groups.active_binding_for_event(entity_id)
        if current is not None:
            same = (current["external_source_key"], current["external_event_uid"], current["external_occurrence_key"]) == \
                (identity.source_key, identity.event_uid, identity.occurrence_key)
            if same:
                return self._event_out(row, NOOP, "ALREADY_BOUND")
            self.groups.detach_binding(current["id"], self.now)
        official = external.official_fields(self.repo, self.me, local_event_id)
        binding_id = f"bind-{_hash(entity_id + '|' + (self.mutation_id or ''))[:28]}"
        self._insert_binding(binding_id, group.id, entity_id, identity, official)
        values = {"external_binding_id": binding_id, "source": EventSource.EXTERNAL_ANNOTATION.value,
                  "starts_at": _iso(official.starts_at), "ends_at": _iso(official.ends_at), "location": None}
        saved = self.groups.update_entity(SharedKind.SHARED_EVENT, entity_id, expected, values, self.now)
        changes = {k: [row[k], saved[k]] for k in ("starts_at", "ends_at", "location") if row[k] != saved[k]}
        self._audit(group.id, SharedKind.SHARED_EVENT.value, entity_id, "BIND_EXTERNAL", previous_version=expected,
                    new_version=int(saved["version"]), binding_id=binding_id, changes=changes)
        if changes:
            self._after_event_change(group, row, saved, changes)
        return self._event_out(saved)

    def event_detach_external(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _, caps = self._entity(SharedKind.SHARED_EVENT, entity_id, Capability.BIND_EXTERNAL)
        binding = self.groups.active_binding_for_event(entity_id)
        if binding is None:
            return self._event_out(row, NOOP, "NOT_BOUND")
        expected = _expected(payload)
        _check_version("shared event", expected, int(row["version"]))
        official = external.resolve_official(self.repo, binding)
        self.groups.detach_binding(binding["id"], self.now)
        # The event keeps the last official time as its own; nothing external or personal is deleted.
        saved = self.groups.update_entity(SharedKind.SHARED_EVENT, entity_id, expected, {
            "external_binding_id": None, "source": EventSource.GROUP_MANUAL.value,
            "starts_at": _iso(official.starts_at), "ends_at": _iso(official.ends_at),
            "location": official.location}, self.now)
        self._audit(group.id, SharedKind.SHARED_EVENT.value, entity_id, "DETACH_EXTERNAL", previous_version=expected,
                    new_version=int(saved["version"]), binding_id=binding["id"])
        return self._event_out(saved)

    # ---- shared obligations ---------------------------------------------------------------------

    def _obligation_out(self, row: dict[str, Any], status: str = APPLIED, code: str | None = None,
                        message: str | None = None) -> Outcome:
        return Outcome(status, obligation_view(row, self._author(row["author_account_id"])), code, message)

    def _publish_obligation(self, group: Group, entity_id: str, raw: dict[str, Any], *,
                            proposal_id: str | None = None) -> Outcome:
        parsed = parse_obligation_payload(raw, default_timezone=group.settings.default_timezone)
        self.groups.insert_entity(SharedKind.SHARED_OBLIGATION, {
            "id": entity_id, "group_id": group.id, "title": parsed.title, "description": parsed.description,
            "kind": parsed.obligation_kind.value, "deadline": _iso(parsed.deadline), "timezone": parsed.timezone,
            "group_criticality": parsed.group_criticality.value,
            "estimated_effort_hint_minutes": parsed.estimated_effort_hint_minutes, "author_account_id": self.me,
            "proposal_id": proposal_id, "version": 1, "status": SharedStatus.PUBLISHED.value,
            "created_at": _iso(self.now), "updated_at": _iso(self.now),
        })
        row = self.groups.entity(SharedKind.SHARED_OBLIGATION, entity_id)
        self._audit(group.id, SharedKind.SHARED_OBLIGATION.value, entity_id, "CREATE", new_version=1, proposal_id=proposal_id,
                    changes={"payload": parsed.to_json()})
        critical = row["group_criticality"] == Criticality.CRITICAL.value
        self.fanout.notify(group, kind=SharedKind.SHARED_OBLIGATION, entity=row, dedupe=f"{entity_id}:v1", critical=critical,
                           exclude=self.me,
                           compose=lambda locale, zone, critical: messages.obligation_published(row, locale=locale, timezone_name=zone,
                                                                                      critical=critical))
        return self._obligation_out(row)

    def obligation_create(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        entity_id = self._new_id(entity_id, "sob")
        existing = self.groups.entity(SharedKind.SHARED_OBLIGATION, entity_id)
        if existing is not None:
            self.access(existing["group_id"], Capability.VIEW_SHARED, writable=False)
            return self._obligation_out(existing, NOOP, "ALREADY_EXISTS")
        if self.groups.id_taken(entity_id):
            raise ValidationError("id is already in use")
        raw = dict(payload)
        group_id = str(raw.pop("group_id", "") or "")
        group, _, caps = self.access(group_id, None)
        if Capability.PUBLISH_SHARED not in caps:
            if Capability.CREATE_PROPOSAL in caps:
                return Outcome(REJECTED, None, "PROPOSAL_REQUIRED", "members suggest deadlines; a scheduler publishes them")
            raise AuthorizationDenied("your role cannot publish")
        self._rate("publish", group_id)
        raw.setdefault("kind", SharedKind.SHARED_OBLIGATION.value)
        return self._publish_obligation(group, entity_id, raw)

    def obligation_update(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _, _ = self._entity(SharedKind.SHARED_OBLIGATION, entity_id, Capability.PUBLISH_SHARED)
        expected = _expected(payload)
        patch = {k: v for k, v in payload.items() if k != "expected_version"}
        unknown = set(patch) - set(_OBLIGATION_COLUMNS)
        if unknown:
            raise ValidationError("fields cannot be changed here: " + ", ".join(sorted(unknown)))
        if row["status"] != SharedStatus.PUBLISHED.value:
            return self._obligation_out(row, CONFLICT, "ENTITY_CANCELLED", "a cancelled deadline cannot be edited")
        _check_version("shared obligation", expected, int(row["version"]))
        tz = group.settings.default_timezone
        before = parse_obligation_payload(_obligation_raw(row), default_timezone=tz).to_json()
        after = parse_obligation_payload({**_obligation_raw(row), **patch}, default_timezone=tz).to_json()
        changes = _diff(before, after)
        if not changes:
            return self._obligation_out(row, NOOP, "NOTHING_TO_CHANGE")
        values = {_OBLIGATION_COLUMNS[k]: (_iso(_dt(v[1])) if k == "deadline" else v[1]) for k, v in changes.items()}
        saved = self.groups.update_entity(SharedKind.SHARED_OBLIGATION, entity_id, expected, values, self.now)
        self._audit(group.id, SharedKind.SHARED_OBLIGATION.value, entity_id, "UPDATE", previous_version=expected,
                    new_version=int(saved["version"]), changes=changes)
        if "deadline" in changes:
            self.fanout.obligation_moved(saved, _dt(row["deadline"]), _dt(saved["deadline"]))
        critical = saved["group_criticality"] == Criticality.CRITICAL.value
        self.fanout.notify(group, kind=SharedKind.SHARED_OBLIGATION, entity=saved,
                           dedupe=f"{entity_id}:v{saved['version']}", critical=critical, exclude=self.me,
                           compose=lambda locale, zone, critical: messages.obligation_changed(saved, changes, locale=locale,
                                                                                    timezone_name=zone, critical=critical))
        return self._obligation_out(saved)

    def obligation_cancel(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _, _ = self._entity(SharedKind.SHARED_OBLIGATION, entity_id, Capability.PUBLISH_SHARED)
        if row["status"] == SharedStatus.CANCELLED.value:
            return self._obligation_out(row, NOOP, "ALREADY_CANCELLED")
        expected = _expected(payload)
        _check_version("shared obligation", expected, int(row["version"]))
        require_shared_transition(SharedKind.SHARED_OBLIGATION, SharedStatus.PUBLISHED, SharedStatus.CANCELLED)
        saved = self.groups.update_entity(SharedKind.SHARED_OBLIGATION, entity_id, expected,
                                          {"status": SharedStatus.CANCELLED.value, "cancelled_at": _iso(self.now)}, self.now)
        self._audit(group.id, SharedKind.SHARED_OBLIGATION.value, entity_id, "CANCEL", previous_version=expected,
                    new_version=int(saved["version"]), changes={"status": ["PUBLISHED", "CANCELLED"]})
        self.fanout.obligation_cancelled(saved)
        critical = saved["group_criticality"] == Criticality.CRITICAL.value
        self.fanout.notify(group, kind=SharedKind.SHARED_OBLIGATION, entity=saved, dedupe=f"{entity_id}:cancelled",
                           critical=critical, exclude=self.me,
                           compose=lambda locale, _zone, _critical: messages.obligation_cancelled(saved, locale=locale, critical=critical))
        return self._obligation_out(saved)

    # ---- announcements ------------------------------------------------------------------------------

    def _announcement_out(self, row: dict[str, Any], status: str = APPLIED, code: str | None = None,
                          message: str | None = None) -> Outcome:
        return Outcome(status, announcement_view(row, self._author(row["author_account_id"])), code, message)

    def _publish_announcement(self, group: Group, entity_id: str, raw: dict[str, Any], *,
                              proposal_id: str | None = None) -> Outcome:
        parsed = parse_announcement_payload(raw)
        self.groups.insert_entity(SharedKind.SHARED_ANNOUNCEMENT, {
            "id": entity_id, "group_id": group.id, "title": parsed.title, "body": parsed.body,
            "importance": parsed.importance.value, "author_account_id": self.me, "proposal_id": proposal_id,
            "version": 1, "status": SharedStatus.PUBLISHED.value, "created_at": _iso(self.now), "updated_at": _iso(self.now),
        })
        row = self.groups.entity(SharedKind.SHARED_ANNOUNCEMENT, entity_id)
        self._audit(group.id, SharedKind.SHARED_ANNOUNCEMENT.value, entity_id, "CREATE", new_version=1,
                    proposal_id=proposal_id, changes={"payload": parsed.to_json()})
        self.fanout.notify(group, kind=SharedKind.SHARED_ANNOUNCEMENT, entity=row, dedupe=f"{entity_id}:v1",
                           critical=row["importance"] == "URGENT", exclude=self.me,
                           compose=lambda locale, _zone, _critical: messages.announcement(row, locale=locale))
        return self._announcement_out(row)

    def announcement_create(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        entity_id = self._new_id(entity_id, "san")
        existing = self.groups.entity(SharedKind.SHARED_ANNOUNCEMENT, entity_id)
        if existing is not None:
            self.access(existing["group_id"], Capability.VIEW_SHARED, writable=False)
            return self._announcement_out(existing, NOOP, "ALREADY_EXISTS")
        if self.groups.id_taken(entity_id):
            raise ValidationError("id is already in use")
        raw = dict(payload)
        group_id = str(raw.pop("group_id", "") or "")
        group, _, caps = self.access(group_id, None)
        if Capability.PUBLISH_SHARED not in caps:
            if Capability.CREATE_PROPOSAL in caps:
                return Outcome(REJECTED, None, "PROPOSAL_REQUIRED", "members suggest announcements; a scheduler publishes them")
            raise AuthorizationDenied("your role cannot publish")
        self._rate("publish", group_id)
        raw.setdefault("kind", SharedKind.SHARED_ANNOUNCEMENT.value)
        return self._publish_announcement(group, entity_id, raw)

    def announcement_update(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _, _ = self._entity(SharedKind.SHARED_ANNOUNCEMENT, entity_id, Capability.PUBLISH_SHARED)
        expected = _expected(payload)
        patch = {k: v for k, v in payload.items() if k != "expected_version"}
        unknown = set(patch) - set(_ANNOUNCEMENT_COLUMNS)
        if unknown:
            raise ValidationError("fields cannot be changed here: " + ", ".join(sorted(unknown)))
        if row["status"] != SharedStatus.PUBLISHED.value:
            return self._announcement_out(row, CONFLICT, "ENTITY_RETRACTED", "a retracted announcement cannot be edited")
        _check_version("announcement", expected, int(row["version"]))
        before = parse_announcement_payload(_announcement_raw(row)).to_json()
        after = parse_announcement_payload({**_announcement_raw(row), **patch}).to_json()
        changes = _diff(before, after)
        if not changes:
            return self._announcement_out(row, NOOP, "NOTHING_TO_CHANGE")
        saved = self.groups.update_entity(SharedKind.SHARED_ANNOUNCEMENT, entity_id, expected,
                                          {_ANNOUNCEMENT_COLUMNS[k]: v[1] for k, v in changes.items()}, self.now)
        self._audit(group.id, SharedKind.SHARED_ANNOUNCEMENT.value, entity_id, "UPDATE", previous_version=expected,
                    new_version=int(saved["version"]), changes=changes)
        return self._announcement_out(saved)

    def announcement_retract(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _, _ = self._entity(SharedKind.SHARED_ANNOUNCEMENT, entity_id, Capability.PUBLISH_SHARED)
        if row["status"] == SharedStatus.RETRACTED.value:
            return self._announcement_out(row, NOOP, "ALREADY_RETRACTED")
        expected = _expected(payload)
        _check_version("announcement", expected, int(row["version"]))
        require_shared_transition(SharedKind.SHARED_ANNOUNCEMENT, SharedStatus.PUBLISHED, SharedStatus.RETRACTED)
        saved = self.groups.update_entity(SharedKind.SHARED_ANNOUNCEMENT, entity_id, expected,
                                          {"status": SharedStatus.RETRACTED.value, "retracted_at": _iso(self.now)}, self.now)
        self._audit(group.id, SharedKind.SHARED_ANNOUNCEMENT.value, entity_id, "RETRACT", previous_version=expected,
                    new_version=int(saved["version"]), changes={"status": ["PUBLISHED", "RETRACTED"]})
        self.fanout.retract_messages(entity_id)
        return self._announcement_out(saved)

    # ---- proposals ------------------------------------------------------------------------------------

    def _proposal_out(self, row: dict[str, Any], status: str = APPLIED, code: str | None = None,
                      message: str | None = None) -> Outcome:
        return Outcome(status, proposal_view(row, self._author(row["author_account_id"])), code, message)

    def proposal_create(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        entity_id = self._new_id(entity_id, "prp")
        existing = self.groups.proposal(entity_id)
        if existing is not None:
            self.access(existing["group_id"], None, writable=False)
            if existing["author_account_id"] != self.me:
                raise ValidationError("id is already in use")
            return self._proposal_out(existing, NOOP, "ALREADY_EXISTS")
        if self.groups.id_taken(entity_id):
            raise ValidationError("id is already in use")
        group_id = str(payload.get("group_id") or "")
        group, _, _ = self.access(group_id, Capability.CREATE_PROPOSAL)
        parsed = parse_payload(payload.get("payload"), default_timezone=group.settings.default_timezone)
        canonical = parsed.to_json()
        payload_hash = _hash(json.dumps(canonical, sort_keys=True))
        duplicate = self.groups.pending_proposal_with_hash(group_id, payload_hash)
        if duplicate is not None:
            return self._proposal_out(duplicate, NOOP, "DUPLICATE_PROPOSAL", "the same suggestion is already waiting for review")
        self._rate("propose", group_id)
        self.groups.insert_proposal({"id": entity_id, "group_id": group_id, "proposal_kind": parsed.kind.value,
                                     "payload": canonical, "payload_hash": payload_hash, "author_account_id": self.me,
                                     "now": self.now})
        self._audit(group_id, "PROPOSAL", entity_id, "PROPOSE", new_version=1, proposal_id=entity_id,
                    changes={"payload": canonical})
        moderators = [m["membership"].account_id for m in self.groups.members(group_id, [MembershipStatus.ACTIVE])
                      if Capability.MODERATE_PROPOSALS in capabilities(m["membership"].role, group.settings)]
        self.fanout.notify(group, kind=SharedKind.SHARED_ANNOUNCEMENT, entity={"id": entity_id},
                           dedupe=f"{entity_id}:proposed", critical=False, exclude=self.me, recipients=moderators,
                           compose=lambda locale, _zone, _critical: messages.proposal_waiting(canonical["title"], group.name, locale=locale))
        return self._proposal_out(self.groups.proposal(entity_id))

    def _proposal(self, proposal_id: str, capability: Capability | None) -> tuple[dict[str, Any], Group, frozenset[Capability]]:
        row = self.groups.proposal(proposal_id) if proposal_id else None
        if row is None:
            raise EntityNotFound("proposal not found")
        group, _, caps = self.access(row["group_id"], capability)
        return row, group, caps

    def proposal_approve(self, proposal_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _ = self._proposal(proposal_id, Capability.MODERATE_PROPOSALS)
        if row["status"] == ProposalStatus.APPROVED.value:
            # A second approval (another moderator, a retry, a replay after reconnect) returns
            # the one entity the first approval created.
            return self._proposal_out(row, NOOP, "ALREADY_APPROVED")
        if row["status"] != ProposalStatus.PENDING.value:
            return self._proposal_out(row, CONFLICT, "PROPOSAL_CLOSED", f"the proposal is {row['status'].lower()}")
        expected = _expected(payload)
        _check_version("proposal", expected, int(row["version"]))
        require_proposal_transition(ProposalStatus.PENDING, ProposalStatus.APPROVED)
        unknown = set(payload) - {"expected_version", "edits", "comment", "allow_duplicate"}
        if unknown:
            raise ValidationError("unknown fields: " + ", ".join(sorted(unknown)))
        kind = SharedKind(row["proposal_kind"])
        original = json.loads(row["payload_json"])
        edits = payload.get("edits") or {}
        if not isinstance(edits, dict) or "kind" in edits:
            raise ValidationError("edits must be an object of payload fields")
        approved = parse_payload({**original, **edits}, default_timezone=group.settings.default_timezone,
                                 expected_kind=kind).to_json()
        entity_id = f"pub-{_hash(proposal_id)[:28]}"
        publish = {SharedKind.SHARED_EVENT: self._publish_event, SharedKind.SHARED_OBLIGATION: self._publish_obligation,
                   SharedKind.SHARED_ANNOUNCEMENT: self._publish_announcement}[kind]
        extra = {"allow_duplicate": bool(payload.get("allow_duplicate"))} if kind is SharedKind.SHARED_EVENT else {}
        outcome = publish(group, entity_id, approved, proposal_id=proposal_id, **extra)
        if outcome.status != APPLIED:
            return outcome
        saved = self.groups.review_proposal(
            proposal_id, expected, status=ProposalStatus.APPROVED.value, reviewer=self.me,
            comment=clean_text(payload.get("comment"), "comment", 1000), approved_kind=kind.value, approved_id=entity_id,
            approved_payload=approved if approved != original else None, now=self.now)
        self._audit(group.id, "PROPOSAL", proposal_id, "APPROVE", previous_version=expected, new_version=int(saved["version"]),
                    proposal_id=proposal_id,
                    changes={"edited": _diff(original, approved), "approved_entity_id": entity_id})
        self.fanout.notify(group, kind=SharedKind.SHARED_ANNOUNCEMENT, entity={"id": proposal_id},
                           dedupe=f"{proposal_id}:approved", critical=False, exclude=self.me,
                           recipients=[row["author_account_id"]], respect_behavior=False,
                           compose=lambda locale, _zone, _critical: messages.proposal_reviewed(True, approved["title"], locale=locale))
        return self._proposal_out(saved)

    def proposal_reject(self, proposal_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _ = self._proposal(proposal_id, Capability.MODERATE_PROPOSALS)
        return self._close_proposal(row, group, payload, ProposalStatus.REJECTED)

    def proposal_withdraw(self, proposal_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _ = self._proposal(proposal_id, None)
        if row["author_account_id"] != self.me:
            raise AuthorizationDenied("only the author can withdraw a proposal")
        return self._close_proposal(row, group, payload, ProposalStatus.WITHDRAWN)

    def _close_proposal(self, row: dict[str, Any], group: Group, payload: dict[str, Any], target: ProposalStatus) -> Outcome:
        if row["status"] == target.value:
            return self._proposal_out(row, NOOP, f"ALREADY_{target.value}")
        if row["status"] != ProposalStatus.PENDING.value:
            return self._proposal_out(row, CONFLICT, "PROPOSAL_CLOSED", f"the proposal is {row['status'].lower()}")
        expected = _expected(payload)
        _check_version("proposal", expected, int(row["version"]))
        require_proposal_transition(ProposalStatus.PENDING, target)
        saved = self.groups.review_proposal(
            row["id"], expected, status=target.value, reviewer=self.me if target is ProposalStatus.REJECTED else None,
            comment=clean_text(payload.get("comment"), "comment", 1000), approved_kind=None, approved_id=None,
            approved_payload=None, now=self.now)
        self._audit(group.id, "PROPOSAL", row["id"], {ProposalStatus.REJECTED: "REJECT", ProposalStatus.WITHDRAWN: "WITHDRAW"}[target],
                    previous_version=expected, new_version=int(saved["version"]), proposal_id=row["id"])
        if target is ProposalStatus.REJECTED:
            title = json.loads(row["payload_json"])["title"]
            self.fanout.notify(group, kind=SharedKind.SHARED_ANNOUNCEMENT, entity={"id": row["id"]},
                               dedupe=f"{row['id']}:rejected", critical=False, exclude=self.me,
                               recipients=[row["author_account_id"]], respect_behavior=False,
                               compose=lambda locale, _zone, _critical: messages.proposal_reviewed(False, title, locale=locale))
        return self._proposal_out(saved)

    # ---- personal overlay ---------------------------------------------------------------------------------

    def _personal(self, kind: SharedKind, entity_id: str) -> dict[str, Any] | None:
        from .projection import PersonalProjection
        return PersonalProjection(self.repo, self.me, self.now).item(kind, entity_id)

    def event_state_update(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        row, group, _, _ = self._entity(SharedKind.SHARED_EVENT, entity_id, Capability.VIEW_SHARED, writable=False)
        state = self.groups.event_state(self.me, entity_id)
        updated = patch_event_state(state, payload)
        if updated == state:
            return Outcome(NOOP, self._personal(SharedKind.SHARED_EVENT, entity_id), "NOTHING_TO_CHANGE")
        from .projection import official_interval
        start = official_interval(self.repo, row)[0]
        live = row["status"] == SharedStatus.PUBLISHED.value
        if "remind_before_minutes" in payload:
            self.fanout.set_reminder(self.me, reminder_key(SharedKind.SHARED_EVENT, entity_id),
                                     lead_moment(start, updated.remind_before_minutes, self.now) if live else None)
        if "alarm_before_minutes" in payload:
            updated = replace(updated, alarm_reminder_id=self.fanout.set_alarm(updated, row["title"], start if live else None))
        self.groups.save_event_state(updated, self.now)
        return Outcome(APPLIED, self._personal(SharedKind.SHARED_EVENT, entity_id))

    def obligation_state_update(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        row, _, _, _ = self._entity(SharedKind.SHARED_OBLIGATION, entity_id, Capability.VIEW_SHARED, writable=False)
        state = self.groups.obligation_state(self.me, entity_id)
        updated = patch_obligation_state(state, payload)
        if updated == state:
            return Outcome(NOOP, self._personal(SharedKind.SHARED_OBLIGATION, entity_id), "NOTHING_TO_CHANGE")
        if "remind_before_minutes" in payload:
            live = row["status"] == SharedStatus.PUBLISHED.value
            self.fanout.set_reminder(self.me, reminder_key(SharedKind.SHARED_OBLIGATION, entity_id),
                                     lead_moment(_dt(row["deadline"]), updated.remind_before_minutes, self.now) if live else None)
        self.groups.save_obligation_state(updated, self.now)
        return Outcome(APPLIED, self._personal(SharedKind.SHARED_OBLIGATION, entity_id))

    def announcement_state_update(self, entity_id: str, payload: dict[str, Any]) -> Outcome:
        self._entity(SharedKind.SHARED_ANNOUNCEMENT, entity_id, Capability.VIEW_SHARED, writable=False)
        unknown = set(payload) - {"dismissed", "last_seen_version"}
        if unknown:
            raise ValidationError("fields cannot be set: " + ", ".join(sorted(unknown)))
        current = self.groups.announcement_states(self.me, [entity_id]).get(entity_id, {"dismissed": False, "last_seen_version": 0})
        dismissed = bool(payload["dismissed"]) if "dismissed" in payload else current["dismissed"]
        seen = max(current["last_seen_version"], int(payload.get("last_seen_version") or 0))
        self.groups.save_announcement_state(self.me, entity_id, dismissed=dismissed, last_seen_version=seen, now=self.now)
        return Outcome(APPLIED, self._personal(SharedKind.SHARED_ANNOUNCEMENT, entity_id))

    def preferences_update(self, group_id: str, payload: dict[str, Any]) -> Outcome:
        group, _, _ = self.access(group_id, None, writable=False)
        prefs = self.groups.preferences(self.me, group)
        if payload.get("expected_version") not in (None, "") and int(payload["expected_version"]) != prefs.version:
            raise SharedVersionConflict("preferences", int(payload["expected_version"]), prefs.version)
        updated = prefs.patched(payload)
        saved = self.groups.save_preferences(updated, self.now)
        from .projection import preferences_view
        return Outcome(APPLIED, preferences_view(saved))

    # ---- preparation (called from task.create) -------------------------------------------------------

    def link_personal_task(self, kind: SharedKind, entity_id: str, task_id: str) -> dict[str, Any]:
        """Attach the caller's new task to a shared item as its preparation / own follow-up."""
        row, _, _, _ = self._entity(kind, entity_id, Capability.VIEW_SHARED, writable=False)
        if kind is SharedKind.SHARED_EVENT:
            state = self.groups.event_state(self.me, entity_id)
            self.groups.save_event_state(replace(state, preparation_task_id=task_id, preparation_deadline_stale=False), self.now)
        elif kind is SharedKind.SHARED_OBLIGATION:
            state = self.groups.obligation_state(self.me, entity_id)
            self.groups.save_obligation_state(replace(state, personal_task_id=task_id, personal_deadline_stale=False,
                                                      acceptance_state=AcceptanceState.ACCEPTED), self.now)
        else:
            raise ValidationError("an announcement cannot have a personal task")
        return row


def validate_link(payload: Any) -> tuple[SharedKind, str]:
    if not isinstance(payload, dict):
        raise ValidationError("prepares must be an object")
    try:
        kind = SharedKind(payload.get("kind"))
    except ValueError as exc:
        raise ValidationError("prepares.kind must be SHARED_EVENT or SHARED_OBLIGATION") from exc
    entity_id = str(payload.get("id") or "")
    if not entity_id:
        raise ValidationError("prepares.id is required")
    return kind, entity_id

