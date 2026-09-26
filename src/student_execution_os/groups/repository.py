"""SQLite persistence of the group layer and of the members' private overlays.

Reads and writes here carry no authorization: ``commands.py`` decides who may do
what and calls these inside the caller's transaction (``repo._tx()``), so a group
mutation, its audit row and the client operation record commit together.
Group-owned rows never advance an account's ``server_revision``; the personal
side effects of a group change (a moved preparation deadline) go through the
canonical repository and do.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import Any, Iterable

from student_execution_os.domain.errors import EntityNotFound
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

from .errors import SharedVersionConflict
from .model import (
    AcceptanceState,
    Attendance,
    Criticality,
    Group,
    GroupJoinPolicy,
    GroupSettings,
    GroupStatus,
    GroupType,
    Membership,
    MembershipStatus,
    NotificationBehavior,
    Role,
    SharedKind,
    SubscriptionDefaults,
    UserGroupPreferences,
    UserSharedEventState,
    UserSharedObligationState,
)

TABLES = {
    SharedKind.SHARED_EVENT: "shared_events",
    SharedKind.SHARED_OBLIGATION: "shared_obligations",
    SharedKind.SHARED_ANNOUNCEMENT: "shared_announcements",
}


def _bool(value: Any) -> bool:
    return bool(int(value))


class SQLiteGroupRepository:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection

    # ---- groups -----------------------------------------------------------------------

    @staticmethod
    def _group(row) -> Group:
        return Group(
            id=row["id"], name=row["name"], description=row["description"], type=GroupType(row["type"]),
            owner_account_id=row["owner_account_id"], version=int(row["version"]), status=GroupStatus(row["status"]),
            settings=GroupSettings(
                allow_members_to_publish=_bool(row["allow_members_to_publish"]),
                default_timezone=row["default_timezone"],
                default_subscription_preferences=SubscriptionDefaults(
                    show_regular_classes=_bool(row["default_show_regular_classes"]),
                    show_assessments=_bool(row["default_show_assessments"]),
                    show_deadlines=_bool(row["default_show_deadlines"]),
                    show_announcements=_bool(row["default_show_announcements"]),
                ),
            ),
            join_policy=GroupJoinPolicy(
                invite_links_enabled=_bool(row["invite_links_enabled"]),
                join_codes_enabled=_bool(row["join_codes_enabled"]),
                approval_required=_bool(row["approval_required"]),
                allow_rejoin_after_removal=_bool(row["allow_rejoin_after_removal"]),
            ),
            created_at=_dt(row["created_at"]), updated_at=_dt(row["updated_at"]),
        )

    def find_group(self, group_id: str) -> Group | None:
        row = self.connection.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        return None if row is None else self._group(row)

    def insert_group(self, group: Group) -> None:
        s, p, d = group.settings, group.join_policy, group.settings.default_subscription_preferences
        self.connection.execute(
            "INSERT INTO groups(id,name,description,type,owner_account_id,status,allow_members_to_publish,default_timezone,"
            "default_show_regular_classes,default_show_assessments,default_show_deadlines,default_show_announcements,"
            "invite_links_enabled,join_codes_enabled,approval_required,allow_rejoin_after_removal,version,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (group.id, group.name, group.description, group.type.value, group.owner_account_id, group.status.value,
             int(s.allow_members_to_publish), s.default_timezone, int(d.show_regular_classes), int(d.show_assessments),
             int(d.show_deadlines), int(d.show_announcements), int(p.invite_links_enabled), int(p.join_codes_enabled),
             int(p.approval_required), int(p.allow_rejoin_after_removal), group.version,
             _iso(group.created_at), _iso(group.updated_at)),
        )

    def save_group(self, group: Group, expected_version: int, now: datetime) -> Group:
        """Write every group-owned field of ``group`` if the stored version is ``expected_version``."""
        s, p, d = group.settings, group.join_policy, group.settings.default_subscription_preferences
        cur = self.connection.execute(
            "UPDATE groups SET name=?,description=?,type=?,owner_account_id=?,status=?,allow_members_to_publish=?,"
            "default_timezone=?,default_show_regular_classes=?,default_show_assessments=?,default_show_deadlines=?,"
            "default_show_announcements=?,invite_links_enabled=?,join_codes_enabled=?,approval_required=?,"
            "allow_rejoin_after_removal=?,version=version+1,updated_at=?,"
            "deleted_at=CASE WHEN ?='DELETED' THEN coalesce(deleted_at,?) ELSE deleted_at END "
            "WHERE id=? AND version=?",
            (group.name, group.description, group.type.value, group.owner_account_id, group.status.value,
             int(s.allow_members_to_publish), s.default_timezone, int(d.show_regular_classes), int(d.show_assessments),
             int(d.show_deadlines), int(d.show_announcements), int(p.invite_links_enabled), int(p.join_codes_enabled),
             int(p.approval_required), int(p.allow_rejoin_after_removal), _iso(now), group.status.value, _iso(now),
             group.id, expected_version),
        )
        if cur.rowcount != 1:
            current = self.find_group(group.id)
            raise SharedVersionConflict("group", expected_version, current.version if current else 0)
        return self.find_group(group.id)

    def groups_of(self, account_id: str, statuses: Iterable[MembershipStatus]) -> list[tuple[Group, Membership]]:
        wanted = [s.value for s in statuses]
        marks = ",".join("?" for _ in wanted)
        rows = self.connection.execute(
            f"SELECT g.*,m.role AS m_role,m.status AS m_status,m.version AS m_version,m.joined_at AS m_joined_at,"
            f"m.updated_at AS m_updated_at FROM groups g JOIN group_memberships m ON m.group_id=g.id "
            f"WHERE m.account_id=? AND m.status IN ({marks}) AND g.status!='DELETED' ORDER BY g.name COLLATE NOCASE,g.id",
            (account_id, *wanted),
        ).fetchall()
        return [(self._group(row), Membership(
            group_id=row["id"], account_id=account_id, role=Role(row["m_role"]), status=MembershipStatus(row["m_status"]),
            version=int(row["m_version"]), joined_at=_dt(row["m_joined_at"]), updated_at=_dt(row["m_updated_at"]),
        )) for row in rows]

    # ---- memberships ------------------------------------------------------------------

    @staticmethod
    def _membership(row) -> Membership:
        return Membership(group_id=row["group_id"], account_id=row["account_id"], role=Role(row["role"]),
                          status=MembershipStatus(row["status"]), version=int(row["version"]),
                          joined_at=_dt(row["joined_at"]), updated_at=_dt(row["updated_at"]))

    def membership(self, group_id: str, account_id: str) -> Membership | None:
        row = self.connection.execute(
            "SELECT * FROM group_memberships WHERE group_id=? AND account_id=?", (group_id, account_id)
        ).fetchone()
        return None if row is None else self._membership(row)

    def insert_membership(self, membership: Membership) -> None:
        self.connection.execute(
            "INSERT INTO group_memberships(group_id,account_id,role,status,version,joined_at,updated_at) VALUES (?,?,?,?,?,?,?)",
            (membership.group_id, membership.account_id, membership.role.value, membership.status.value,
             membership.version, _iso(membership.joined_at), _iso(membership.updated_at)),
        )

    def save_membership(self, membership: Membership, expected_version: int, now: datetime) -> Membership:
        joined = membership.joined_at
        cur = self.connection.execute(
            "UPDATE group_memberships SET role=?,status=?,version=version+1,updated_at=?,joined_at=? "
            "WHERE group_id=? AND account_id=? AND version=?",
            (membership.role.value, membership.status.value, _iso(now), _iso(joined), membership.group_id,
             membership.account_id, expected_version),
        )
        if cur.rowcount != 1:
            current = self.membership(membership.group_id, membership.account_id)
            raise SharedVersionConflict("membership", expected_version, current.version if current else 0)
        return self.membership(membership.group_id, membership.account_id)

    def members(self, group_id: str, statuses: Iterable[MembershipStatus]) -> list[dict[str, Any]]:
        wanted = [s.value for s in statuses]
        marks = ",".join("?" for _ in wanted)
        rows = self.connection.execute(
            f"SELECT m.*,u.login FROM group_memberships m LEFT JOIN auth_users u ON u.account_id=m.account_id "
            f"WHERE m.group_id=? AND m.status IN ({marks}) ORDER BY m.joined_at,m.account_id",
            (group_id, *wanted),
        ).fetchall()
        return [{"membership": self._membership(row), "display_name": row["login"] or row["account_id"]} for row in rows]

    def active_member_ids(self, group_id: str) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT account_id FROM group_memberships WHERE group_id=? AND status='ACTIVE' ORDER BY account_id", (group_id,)
        ).fetchall()]

    def display_name(self, account_id: str) -> str:
        row = self.connection.execute("SELECT login FROM auth_users WHERE account_id=?", (account_id,)).fetchone()
        return row["login"] if row is not None else account_id

    # ---- invites ----------------------------------------------------------------------

    def insert_invite(self, values: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO group_invites(id,group_id,kind,token_hash,code,token_hint,created_by_account_id,created_at,"
            "expires_at,max_uses) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (values["id"], values["group_id"], values["kind"], values["token_hash"], values.get("code"),
             values["token_hint"], values["created_by_account_id"], _iso(values["created_at"]),
             _iso(values.get("expires_at")), values.get("max_uses")),
        )

    def invite(self, invite_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM group_invites WHERE id=?", (invite_id,)).fetchone()
        return None if row is None else dict(row)

    def invite_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM group_invites WHERE token_hash=?", (token_hash,)).fetchone()
        return None if row is None else dict(row)

    def invites(self, group_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM group_invites WHERE group_id=? ORDER BY created_at DESC,id", (group_id,)
        ).fetchall()]

    # ---- shared entities --------------------------------------------------------------

    def entity(self, kind: SharedKind, entity_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(f"SELECT * FROM {TABLES[kind]} WHERE id=?", (entity_id,)).fetchone()
        return None if row is None else dict(row)

    def entity_owner_group(self, entity_id: str) -> tuple[SharedKind, str] | None:
        """Which kind of shared entity ``entity_id`` is and its group (ids are unique across kinds)."""
        for kind, table in TABLES.items():
            row = self.connection.execute(f"SELECT group_id FROM {table} WHERE id=?", (entity_id,)).fetchone()
            if row is not None:
                return kind, row["group_id"]
        return None

    def id_taken(self, entity_id: str) -> bool:
        if self.entity_owner_group(entity_id) is not None:
            return True
        return self.connection.execute("SELECT 1 FROM group_proposals WHERE id=?", (entity_id,)).fetchone() is not None

    def insert_entity(self, kind: SharedKind, values: dict[str, Any]) -> None:
        columns = list(values)
        self.connection.execute(
            f"INSERT INTO {TABLES[kind]}({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(_iso(v) if isinstance(v, datetime) else v for v in values.values()),
        )

    def update_entity(self, kind: SharedKind, entity_id: str, expected_version: int, values: dict[str, Any],
                      now: datetime) -> dict[str, Any]:
        assignments = ",".join(f"{column}=?" for column in values)
        prefix = f"{assignments}," if assignments else ""
        cur = self.connection.execute(
            f"UPDATE {TABLES[kind]} SET {prefix}version=version+1,updated_at=? WHERE id=? AND version=?",
            (*[_iso(v) if isinstance(v, datetime) else v for v in values.values()], _iso(now), entity_id, expected_version),
        )
        if cur.rowcount != 1:
            current = self.entity(kind, entity_id)
            raise SharedVersionConflict(kind.value.lower(), expected_version, int(current["version"]) if current else 0)
        return self.entity(kind, entity_id)

    def list_entities(self, kind: SharedKind, group_id: str, *, statuses: Iterable[str], order: str,
                      after: tuple[str, str] | None, limit: int) -> list[dict[str, Any]]:
        """Keyset page ordered by (``order`` column, id)."""
        wanted = list(statuses)
        marks = ",".join("?" for _ in wanted)
        sql = f"SELECT * FROM {TABLES[kind]} WHERE group_id=? AND status IN ({marks})"
        args: list[Any] = [group_id, *wanted]
        if after is not None:
            sql += f" AND ({order}>? OR ({order}=? AND id>?))"
            args += [after[0], after[0], after[1]]
        sql += f" ORDER BY {order},id LIMIT ?"
        args.append(limit)
        return [dict(row) for row in self.connection.execute(sql, tuple(args)).fetchall()]

    def published_duplicate(self, group_id: str, *, title: str, event_kind: str, starts_at: datetime) -> str | None:
        """An already published event with the same title, kind and start (never used to merge)."""
        row = self.connection.execute(
            "SELECT id FROM shared_events WHERE group_id=? AND status='PUBLISHED' AND title=? AND event_kind=? AND starts_at=?",
            (group_id, title, event_kind, _iso(starts_at)),
        ).fetchone()
        return None if row is None else row["id"]

    # ---- proposals ----------------------------------------------------------------------

    def proposal(self, proposal_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM group_proposals WHERE id=?", (proposal_id,)).fetchone()
        return None if row is None else dict(row)

    def pending_proposal_with_hash(self, group_id: str, payload_hash: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM group_proposals WHERE group_id=? AND payload_hash=? AND status='PENDING'", (group_id, payload_hash)
        ).fetchone()
        return None if row is None else dict(row)

    def insert_proposal(self, values: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO group_proposals(id,group_id,proposal_kind,payload_json,payload_hash,author_account_id,status,"
            "version,created_at,updated_at) VALUES (?,?,?,?,?,?,'PENDING',1,?,?)",
            (values["id"], values["group_id"], values["proposal_kind"], json.dumps(values["payload"], sort_keys=True),
             values["payload_hash"], values["author_account_id"], _iso(values["now"]), _iso(values["now"])),
        )

    def review_proposal(self, proposal_id: str, expected_version: int, *, status: str, reviewer: str | None,
                        comment: str | None, approved_kind: str | None, approved_id: str | None,
                        approved_payload: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
        cur = self.connection.execute(
            "UPDATE group_proposals SET status=?,reviewer_account_id=?,review_comment=?,approved_entity_kind=?,"
            "approved_entity_id=?,approved_payload_json=?,version=version+1,updated_at=? "
            "WHERE id=? AND version=? AND status='PENDING'",
            (status, reviewer, comment, approved_kind, approved_id,
             None if approved_payload is None else json.dumps(approved_payload, sort_keys=True), _iso(now),
             proposal_id, expected_version),
        )
        if cur.rowcount != 1:
            current = self.proposal(proposal_id)
            raise SharedVersionConflict("proposal", expected_version, int(current["version"]) if current else 0)
        return self.proposal(proposal_id)

    def list_proposals(self, group_id: str, *, author: str | None, statuses: Iterable[str],
                       after: tuple[str, str] | None, limit: int) -> list[dict[str, Any]]:
        wanted = list(statuses)
        marks = ",".join("?" for _ in wanted)
        sql = f"SELECT * FROM group_proposals WHERE group_id=? AND status IN ({marks})"
        args: list[Any] = [group_id, *wanted]
        if author is not None:
            sql += " AND author_account_id=?"
            args.append(author)
        if after is not None:
            sql += " AND (created_at>? OR (created_at=? AND id>?))"
            args += [after[0], after[0], after[1]]
        sql += " ORDER BY created_at,id LIMIT ?"
        args.append(limit)
        return [dict(row) for row in self.connection.execute(sql, tuple(args)).fetchall()]

    # ---- external bindings ----------------------------------------------------------------

    def active_binding_for_event(self, shared_event_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM external_event_bindings WHERE shared_event_id=? AND status='ACTIVE'", (shared_event_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def binding(self, binding_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM external_event_bindings WHERE id=?", (binding_id,)).fetchone()
        return None if row is None else dict(row)

    def insert_binding(self, values: dict[str, Any]) -> None:
        columns = list(values)
        self.connection.execute(
            f"INSERT INTO external_event_bindings({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(_iso(v) if isinstance(v, datetime) else v for v in values.values()),
        )

    def detach_binding(self, binding_id: str, now: datetime) -> None:
        self.connection.execute(
            "UPDATE external_event_bindings SET status='DETACHED',version=version+1,updated_at=? WHERE id=? AND status='ACTIVE'",
            (_iso(now), binding_id),
        )

    # ---- audit ----------------------------------------------------------------------------

    def audit(self, *, group_id: str, entity_kind: str, entity_id: str, action: str, actor: str, now: datetime,
              previous_version: int | None = None, new_version: int | None = None, mutation_id: str | None = None,
              proposal_id: str | None = None, binding_id: str | None = None,
              changes: dict[str, Any] | None = None) -> None:
        self.connection.execute(
            "INSERT INTO group_audit(group_id,entity_kind,entity_id,action,actor_account_id,at,previous_version,new_version,"
            "mutation_id,proposal_id,binding_id,changes_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (group_id, entity_kind, entity_id, action, actor, _iso(now), previous_version, new_version, mutation_id,
             proposal_id, binding_id, json.dumps(changes or {}, sort_keys=True, default=str)),
        )

    def audit_trail(self, group_id: str, entity_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM group_audit WHERE group_id=?"
        args: list[Any] = [group_id]
        if entity_id is not None:
            sql += " AND entity_id=?"
            args.append(entity_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = self.connection.execute(sql, tuple(args)).fetchall()
        return [{**dict(row), "changes": json.loads(row["changes_json"])} for row in rows]

    def last_changes(self, entity_ids: list[str]) -> dict[str, dict[str, Any]]:
        """The latest recorded field change (reschedule, room, cancellation …) per entity."""
        if not entity_ids:
            return {}
        marks = ",".join("?" for _ in entity_ids)
        rows = self.connection.execute(
            f"SELECT a.* FROM group_audit a JOIN (SELECT entity_id,max(id) AS id FROM group_audit "
            f"WHERE entity_id IN ({marks}) AND action IN ('UPDATE','CANCEL','RETRACT','EXTERNAL_CHANGE') GROUP BY entity_id) "
            f"last ON last.id=a.id",
            tuple(entity_ids),
        ).fetchall()
        return {row["entity_id"]: {"action": row["action"], "at": row["at"], "new_version": row["new_version"],
                                   "previous_version": row["previous_version"], "changes": json.loads(row["changes_json"])}
                for row in rows}

    def actions_since(self, actor: str, actions: Iterable[str], since: datetime, group_id: str | None = None) -> int:
        wanted = list(actions)
        marks = ",".join("?" for _ in wanted)
        sql = f"SELECT count(*) FROM group_audit WHERE actor_account_id=? AND action IN ({marks}) AND at>=?"
        args: list[Any] = [actor, *wanted, _iso(since)]
        if group_id is not None:
            sql += " AND group_id=?"
            args.append(group_id)
        return int(self.connection.execute(sql, tuple(args)).fetchone()[0])

    # ---- personal overlays (only ever read/written for their own account) ----------------

    def preferences(self, account_id: str, group: Group) -> UserGroupPreferences:
        row = self.connection.execute(
            "SELECT * FROM user_group_preferences WHERE account_id=? AND group_id=?", (account_id, group.id)
        ).fetchone()
        if row is None:
            d = group.settings.default_subscription_preferences
            return UserGroupPreferences(account_id=account_id, group_id=group.id, show_regular_classes=d.show_regular_classes,
                                        show_assessments=d.show_assessments, show_deadlines=d.show_deadlines,
                                        show_announcements=d.show_announcements)
        return UserGroupPreferences(
            account_id=account_id, group_id=group.id, muted=_bool(row["muted"]),
            show_regular_classes=_bool(row["show_regular_classes"]), show_assessments=_bool(row["show_assessments"]),
            show_deadlines=_bool(row["show_deadlines"]), show_announcements=_bool(row["show_announcements"]),
            announcements_in_agenda=_bool(row["announcements_in_agenda"]),
            default_attendance_behavior=Attendance(row["default_attendance_behavior"]) if row["default_attendance_behavior"] else None,
            notification_behavior=NotificationBehavior(row["notification_behavior"]), version=int(row["version"]),
        )

    def save_preferences(self, prefs: UserGroupPreferences, now: datetime) -> UserGroupPreferences:
        self.connection.execute(
            "INSERT INTO user_group_preferences(account_id,group_id,muted,show_regular_classes,show_assessments,show_deadlines,"
            "show_announcements,announcements_in_agenda,default_attendance_behavior,notification_behavior,version,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(account_id,group_id) DO UPDATE SET muted=excluded.muted,"
            "show_regular_classes=excluded.show_regular_classes,show_assessments=excluded.show_assessments,"
            "show_deadlines=excluded.show_deadlines,show_announcements=excluded.show_announcements,"
            "announcements_in_agenda=excluded.announcements_in_agenda,"
            "default_attendance_behavior=excluded.default_attendance_behavior,"
            "notification_behavior=excluded.notification_behavior,version=user_group_preferences.version+1,"
            "updated_at=excluded.updated_at",
            (prefs.account_id, prefs.group_id, int(prefs.muted), int(prefs.show_regular_classes), int(prefs.show_assessments),
             int(prefs.show_deadlines), int(prefs.show_announcements), int(prefs.announcements_in_agenda),
             prefs.default_attendance_behavior.value if prefs.default_attendance_behavior else None,
             prefs.notification_behavior.value, _iso(now)),
        )
        row = self.connection.execute(
            "SELECT version FROM user_group_preferences WHERE account_id=? AND group_id=?", (prefs.account_id, prefs.group_id)
        ).fetchone()
        return replace(prefs, version=int(row["version"]))

    def event_state(self, account_id: str, shared_event_id: str) -> UserSharedEventState:
        row = self.connection.execute(
            "SELECT * FROM user_shared_event_states WHERE account_id=? AND shared_event_id=?", (account_id, shared_event_id)
        ).fetchone()
        return UserSharedEventState(account_id, shared_event_id) if row is None else self._event_state(row)

    @staticmethod
    def _event_state(row) -> UserSharedEventState:
        return UserSharedEventState(
            account_id=row["account_id"], shared_event_id=row["shared_event_id"],
            attendance_override=Attendance(row["attendance_override"]) if row["attendance_override"] else None,
            criticality_override=Criticality(row["criticality_override"]) if row["criticality_override"] else None,
            remind_before_minutes=row["remind_before_minutes"], alarm_before_minutes=row["alarm_before_minutes"],
            alarm_reminder_id=row["alarm_reminder_id"], muted=_bool(row["muted"]),
            preparation_task_id=row["preparation_task_id"],
            preparation_deadline_stale=_bool(row["preparation_deadline_stale"]),
            last_seen_version=int(row["last_seen_version"]), version=int(row["version"]),
        )

    def event_states(self, account_id: str, ids: list[str]) -> dict[str, UserSharedEventState]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT * FROM user_shared_event_states WHERE account_id=? AND shared_event_id IN ({marks})", (account_id, *ids)
        ).fetchall()
        return {row["shared_event_id"]: self._event_state(row) for row in rows}

    def event_states_for_event(self, shared_event_id: str) -> list[UserSharedEventState]:
        """Every member's overlay of one event — only for the server's own fan-out, never returned by an API."""
        rows = self.connection.execute(
            "SELECT * FROM user_shared_event_states WHERE shared_event_id=?", (shared_event_id,)
        ).fetchall()
        return [self._event_state(row) for row in rows]

    def save_event_state(self, state: UserSharedEventState, now: datetime) -> UserSharedEventState:
        self.connection.execute(
            "INSERT INTO user_shared_event_states(account_id,shared_event_id,attendance_override,criticality_override,"
            "remind_before_minutes,alarm_before_minutes,alarm_reminder_id,muted,preparation_task_id,preparation_deadline_stale,"
            "last_seen_version,version,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?) "
            "ON CONFLICT(account_id,shared_event_id) DO UPDATE SET attendance_override=excluded.attendance_override,"
            "criticality_override=excluded.criticality_override,remind_before_minutes=excluded.remind_before_minutes,"
            "alarm_before_minutes=excluded.alarm_before_minutes,alarm_reminder_id=excluded.alarm_reminder_id,"
            "muted=excluded.muted,preparation_task_id=excluded.preparation_task_id,"
            "preparation_deadline_stale=excluded.preparation_deadline_stale,"
            "last_seen_version=max(user_shared_event_states.last_seen_version,excluded.last_seen_version),"
            "version=user_shared_event_states.version+1,updated_at=excluded.updated_at",
            (state.account_id, state.shared_event_id,
             state.attendance_override.value if state.attendance_override else None,
             state.criticality_override.value if state.criticality_override else None,
             state.remind_before_minutes, state.alarm_before_minutes, state.alarm_reminder_id, int(state.muted),
             state.preparation_task_id, int(state.preparation_deadline_stale), state.last_seen_version, _iso(now)),
        )
        return self.event_state(state.account_id, state.shared_event_id)

    def obligation_state(self, account_id: str, obligation_id: str) -> UserSharedObligationState:
        row = self.connection.execute(
            "SELECT * FROM user_shared_obligation_states WHERE account_id=? AND shared_obligation_id=?", (account_id, obligation_id)
        ).fetchone()
        return UserSharedObligationState(account_id, obligation_id) if row is None else self._obligation_state(row)

    @staticmethod
    def _obligation_state(row) -> UserSharedObligationState:
        return UserSharedObligationState(
            account_id=row["account_id"], shared_obligation_id=row["shared_obligation_id"],
            acceptance_state=AcceptanceState(row["acceptance_state"]), personal_task_id=row["personal_task_id"],
            personal_deadline_stale=_bool(row["personal_deadline_stale"]),
            criticality_override=Criticality(row["criticality_override"]) if row["criticality_override"] else None,
            remind_before_minutes=row["remind_before_minutes"], muted=_bool(row["muted"]),
            last_seen_version=int(row["last_seen_version"]), version=int(row["version"]),
        )

    def obligation_states(self, account_id: str, ids: list[str]) -> dict[str, UserSharedObligationState]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT * FROM user_shared_obligation_states WHERE account_id=? AND shared_obligation_id IN ({marks})",
            (account_id, *ids),
        ).fetchall()
        return {row["shared_obligation_id"]: self._obligation_state(row) for row in rows}

    def obligation_states_for(self, obligation_id: str) -> list[UserSharedObligationState]:
        rows = self.connection.execute(
            "SELECT * FROM user_shared_obligation_states WHERE shared_obligation_id=?", (obligation_id,)
        ).fetchall()
        return [self._obligation_state(row) for row in rows]

    def save_obligation_state(self, state: UserSharedObligationState, now: datetime) -> UserSharedObligationState:
        self.connection.execute(
            "INSERT INTO user_shared_obligation_states(account_id,shared_obligation_id,acceptance_state,personal_task_id,"
            "personal_deadline_stale,criticality_override,remind_before_minutes,muted,last_seen_version,version,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,?) ON CONFLICT(account_id,shared_obligation_id) DO UPDATE SET "
            "acceptance_state=excluded.acceptance_state,personal_task_id=excluded.personal_task_id,"
            "personal_deadline_stale=excluded.personal_deadline_stale,criticality_override=excluded.criticality_override,"
            "remind_before_minutes=excluded.remind_before_minutes,muted=excluded.muted,"
            "last_seen_version=max(user_shared_obligation_states.last_seen_version,excluded.last_seen_version),"
            "version=user_shared_obligation_states.version+1,updated_at=excluded.updated_at",
            (state.account_id, state.shared_obligation_id, state.acceptance_state.value, state.personal_task_id,
             int(state.personal_deadline_stale), state.criticality_override.value if state.criticality_override else None,
             state.remind_before_minutes, int(state.muted), state.last_seen_version, _iso(now)),
        )
        return self.obligation_state(state.account_id, state.shared_obligation_id)

    def announcement_states(self, account_id: str, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            f"SELECT * FROM user_announcement_states WHERE account_id=? AND announcement_id IN ({marks})", (account_id, *ids)
        ).fetchall()
        return {row["announcement_id"]: {"dismissed": _bool(row["dismissed"]), "last_seen_version": int(row["last_seen_version"]),
                                         "version": int(row["version"])} for row in rows}

    def save_announcement_state(self, account_id: str, announcement_id: str, *, dismissed: bool, last_seen_version: int,
                                now: datetime) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO user_announcement_states(account_id,announcement_id,dismissed,last_seen_version,version,updated_at) "
            "VALUES (?,?,?,?,1,?) ON CONFLICT(account_id,announcement_id) DO UPDATE SET dismissed=excluded.dismissed,"
            "last_seen_version=max(user_announcement_states.last_seen_version,excluded.last_seen_version),"
            "version=user_announcement_states.version+1,updated_at=excluded.updated_at",
            (account_id, announcement_id, int(dismissed), last_seen_version, _iso(now)),
        )
        return self.announcement_states(account_id, [announcement_id])[announcement_id]

    # ---- helpers ----------------------------------------------------------------------------

    def require_group(self, group_id: str) -> Group:
        group = self.find_group(group_id)
        if group is None:
            raise EntityNotFound("group not found")
        return group
