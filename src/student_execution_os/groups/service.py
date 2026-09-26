"""Read side of the group API. Every read checks the caller's own membership first:
an outsider gets NOT_FOUND (the group's existence is not revealed), a member without
the capability gets FORBIDDEN. Group-facing reads never contain personal overlays."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from student_execution_os.domain.errors import EntityNotFound
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository

from .commands import GroupCommands
from .model import Capability, MembershipStatus, SharedKind
from .projection import PersonalProjection, preferences_view
from .repository import SQLiteGroupRepository
from .views import (
    announcement_view,
    decode_cursor,
    encode_cursor,
    event_view,
    group_view,
    invite_view,
    membership_view,
    obligation_view,
    page,
    page_limit,
    proposal_view,
)

_STATUS_FILTER = {
    SharedKind.SHARED_EVENT: {"all": ("PUBLISHED", "CANCELLED"), "published": ("PUBLISHED",), "cancelled": ("CANCELLED",)},
    SharedKind.SHARED_OBLIGATION: {"all": ("PUBLISHED", "CANCELLED"), "published": ("PUBLISHED",), "cancelled": ("CANCELLED",)},
    SharedKind.SHARED_ANNOUNCEMENT: {"all": ("PUBLISHED", "RETRACTED"), "published": ("PUBLISHED",), "retracted": ("RETRACTED",)},
}
_ORDER = {SharedKind.SHARED_EVENT: "starts_at", SharedKind.SHARED_OBLIGATION: "deadline",
          SharedKind.SHARED_ANNOUNCEMENT: "created_at"}


class GroupReadService:
    def __init__(self, repo: SQLiteCanonicalRepository, account_id: str, now: datetime) -> None:
        self.repo = repo
        self.me = account_id
        self.now = now
        self.groups = SQLiteGroupRepository(repo)
        self.commands = GroupCommands(repo, account_id=account_id, now=now)

    def group(self, group_id: str) -> dict[str, Any]:
        group, membership, caps = self.commands.access(group_id, None, writable=False, allow_pending=True)
        view = group_view(group, membership, caps, member_count=len(self.groups.active_member_ids(group_id)))
        view["preferences"] = preferences_view(self.groups.preferences(self.me, group))
        return view

    def members(self, group_id: str, *, include: str = "active", cursor: str | None = None,
                limit: int | None = None) -> dict[str, Any]:
        _, _, caps = self.commands.access(group_id, Capability.VIEW_MEMBERS, writable=False)
        statuses = [MembershipStatus.ACTIVE]
        if include != "active":
            if Capability.MANAGE_MEMBERS not in caps:
                from student_execution_os.domain.errors import AuthorizationDenied
                raise AuthorizationDenied("your role cannot see pending or blocked members")
            statuses = [MembershipStatus(s) for s in ("PENDING", "BLOCKED", "REMOVED", "LEFT")] if include == "other" \
                else [MembershipStatus(include.upper())]
        size = page_limit(limit)
        after = decode_cursor(cursor)
        rows = [membership_view(m["membership"], m["display_name"]) for m in self.groups.members(group_id, statuses)]
        rows.sort(key=lambda r: (r["joined_at"], r["account_id"]))
        if after is not None:
            rows = [r for r in rows if (r["joined_at"], r["account_id"]) > after]
        return page(rows[:size + 1], size, lambda r: (r["joined_at"], r["account_id"]))

    def entities(self, kind: SharedKind, group_id: str, *, status: str = "all", cursor: str | None = None,
                 limit: int | None = None) -> dict[str, Any]:
        self.commands.access(group_id, Capability.VIEW_SHARED, writable=False)
        statuses = _STATUS_FILTER[kind].get(status)
        if statuses is None:
            raise ValueError("unknown status filter")
        size = page_limit(limit)
        order = _ORDER[kind]
        rows = self.groups.list_entities(kind, group_id, statuses=statuses, order=order, after=decode_cursor(cursor),
                                         limit=size + 1)
        more, rows = len(rows) > size, rows[:size]
        return {"items": [self._entity_view(kind, row) for row in rows],
                "next_cursor": encode_cursor(rows[-1][order], rows[-1]["id"]) if more and rows else None}

    def entity(self, kind: SharedKind, entity_id: str) -> dict[str, Any]:
        row = self.groups.entity(kind, entity_id)
        if row is None:
            raise EntityNotFound("not found")
        self.commands.access(row["group_id"], Capability.VIEW_SHARED, writable=False)
        return self._entity_view(kind, row)

    def _entity_view(self, kind: SharedKind, row: dict[str, Any]) -> dict[str, Any]:
        author = self.groups.display_name(row["author_account_id"])
        if kind is SharedKind.SHARED_EVENT:
            binding = self.groups.binding(row["external_binding_id"]) if row["external_binding_id"] else None
            return event_view(row, binding, author)
        if kind is SharedKind.SHARED_OBLIGATION:
            return obligation_view(row, author)
        return announcement_view(row, author)

    def proposals(self, group_id: str, *, status: str = "pending", cursor: str | None = None,
                  limit: int | None = None) -> dict[str, Any]:
        _, _, caps = self.commands.access(group_id, Capability.VIEW_SHARED, writable=False)
        statuses = {"pending": ("PENDING",), "all": ("PENDING", "APPROVED", "REJECTED", "WITHDRAWN")}.get(status)
        if statuses is None:
            raise ValueError("status must be pending or all")
        # Moderators review everyone's suggestions; a member sees their own.
        author = None if Capability.MODERATE_PROPOSALS in caps else self.me
        size = page_limit(limit)
        rows = self.groups.list_proposals(group_id, author=author, statuses=statuses, after=decode_cursor(cursor),
                                          limit=size + 1)
        views = [proposal_view(row, self.groups.display_name(row["author_account_id"])) for row in rows]
        return page(views, size, lambda v: (v["created_at"], v["id"]))

    def invites(self, group_id: str) -> dict[str, Any]:
        self.commands.access(group_id, Capability.MANAGE_INVITES, writable=False)
        return {"items": [invite_view(row) for row in self.groups.invites(group_id)], "next_cursor": None}

    def audit(self, group_id: str, entity_id: str | None = None) -> dict[str, Any]:
        self.commands.access(group_id, Capability.VIEW_AUDIT, writable=False)
        rows = self.groups.audit_trail(group_id, entity_id)
        for row in rows:
            row.pop("changes_json", None)
            actor = row["actor_account_id"]
            row["actor"] = actor if actor.startswith("external:") else self.groups.display_name(actor)
        return {"items": rows, "next_cursor": None}

    # ---- personal -----------------------------------------------------------------------

    def me_shared(self) -> dict[str, Any]:
        projection = PersonalProjection(self.repo, self.me, self.now)
        return {"now": self.now.isoformat(), "groups": projection.memberships(), "items": projection.items()}

    def me_state(self, kind: SharedKind, entity_id: str) -> dict[str, Any]:
        row = self.groups.entity(kind, entity_id)
        if row is None:
            raise EntityNotFound("not found")
        self.commands.access(row["group_id"], Capability.VIEW_SHARED, writable=False)
        return PersonalProjection(self.repo, self.me, self.now).item(kind, entity_id)

    def me_preferences(self, group_id: str) -> dict[str, Any]:
        group, _, _ = self.commands.access(group_id, None, writable=False)
        return preferences_view(self.groups.preferences(self.me, group))
