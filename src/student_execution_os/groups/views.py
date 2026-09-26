"""Group-facing representations. None of them carries any member's personal overlay:
the personal projection (``projection.py``) is built per account and only for it."""
from __future__ import annotations

import base64
import json
from typing import Any

from student_execution_os.domain.errors import ValidationError

from .model import Capability, Group, Membership, is_assessment

MAX_PAGE = 200
DEFAULT_PAGE = 50


def group_view(group: Group, membership: Membership | None, caps: frozenset[Capability] | None = None,
               *, member_count: int | None = None) -> dict[str, Any]:
    s, p = group.settings, group.join_policy
    return {
        "id": group.id, "name": group.name, "description": group.description, "type": group.type.value,
        "status": group.status.value, "version": group.version,
        "owner_account_id": group.owner_account_id,
        "settings": {
            "allow_members_to_publish": s.allow_members_to_publish,
            "default_timezone": s.default_timezone,
            "default_subscription_preferences": {
                "show_regular_classes": s.default_subscription_preferences.show_regular_classes,
                "show_assessments": s.default_subscription_preferences.show_assessments,
                "show_deadlines": s.default_subscription_preferences.show_deadlines,
                "show_announcements": s.default_subscription_preferences.show_announcements,
            },
        },
        "join_policy": {
            "invite_links_enabled": p.invite_links_enabled, "join_codes_enabled": p.join_codes_enabled,
            "approval_required": p.approval_required, "allow_rejoin_after_removal": p.allow_rejoin_after_removal,
        },
        "my_membership": None if membership is None else {
            "role": membership.role.value, "status": membership.status.value, "version": membership.version,
        },
        "capabilities": sorted(c.value for c in (caps or ())),
        "member_count": member_count,
        "created_at": group.created_at.isoformat(), "updated_at": group.updated_at.isoformat(),
    }


def membership_view(membership: Membership, display_name: str) -> dict[str, Any]:
    return {"account_id": membership.account_id, "display_name": display_name, "role": membership.role.value,
            "status": membership.status.value, "version": membership.version,
            "joined_at": membership.joined_at.isoformat()}


def binding_view(binding: dict[str, Any] | None) -> dict[str, Any] | None:
    if binding is None:
        return None
    return {
        "id": binding["id"], "status": binding["status"], "version": int(binding["version"]),
        "relation_kind": binding["relation_kind"], "external_source_key": binding["external_source_key"],
        "external_event_uid": binding["external_event_uid"], "external_occurrence_key": binding["external_occurrence_key"],
        # External-owned values as last observed, shown read-only.
        "official": {"title": binding["official_title"], "starts_at": binding["official_starts_at"],
                     "ends_at": binding["official_ends_at"], "location": binding["official_location"]},
    }


def event_view(row: dict[str, Any], binding: dict[str, Any] | None = None, author: str | None = None) -> dict[str, Any]:
    return {
        "kind": "SHARED_EVENT", "id": row["id"], "group_id": row["group_id"], "title": row["title"],
        "description": row["description"], "event_kind": row["event_kind"], "is_assessment": is_assessment(row["event_kind"]),
        "starts_at": row["starts_at"], "ends_at": row["ends_at"], "timezone": row["timezone"], "location": row["location"],
        "source": row["source"], "external_binding_id": row["external_binding_id"], "external_binding": binding_view(binding),
        "attendance_default": row["attendance_default"], "group_criticality": row["group_criticality"],
        "author_account_id": row["author_account_id"], "author": author, "proposal_id": row["proposal_id"],
        "version": int(row["version"]), "status": row["status"], "created_at": row["created_at"],
        "updated_at": row["updated_at"], "cancelled_at": row["cancelled_at"],
    }


def obligation_view(row: dict[str, Any], author: str | None = None) -> dict[str, Any]:
    return {
        "kind": "SHARED_OBLIGATION", "id": row["id"], "group_id": row["group_id"], "title": row["title"],
        "description": row["description"], "obligation_kind": row["kind"], "deadline": row["deadline"],
        "timezone": row["timezone"], "group_criticality": row["group_criticality"],
        "estimated_effort_hint_minutes": row["estimated_effort_hint_minutes"], "author_account_id": row["author_account_id"],
        "author": author, "proposal_id": row["proposal_id"], "version": int(row["version"]), "status": row["status"],
        "created_at": row["created_at"], "updated_at": row["updated_at"], "cancelled_at": row["cancelled_at"],
    }


def announcement_view(row: dict[str, Any], author: str | None = None) -> dict[str, Any]:
    return {
        "kind": "SHARED_ANNOUNCEMENT", "id": row["id"], "group_id": row["group_id"], "title": row["title"],
        "body": row["body"], "importance": row["importance"], "author_account_id": row["author_account_id"],
        "author": author, "proposal_id": row["proposal_id"], "version": int(row["version"]), "status": row["status"],
        "created_at": row["created_at"], "updated_at": row["updated_at"], "retracted_at": row["retracted_at"],
    }


def proposal_view(row: dict[str, Any], author: str | None = None) -> dict[str, Any]:
    return {
        "id": row["id"], "group_id": row["group_id"], "proposal_kind": row["proposal_kind"],
        "payload": json.loads(row["payload_json"]), "author_account_id": row["author_account_id"], "author": author,
        "status": row["status"], "reviewer_account_id": row["reviewer_account_id"], "review_comment": row["review_comment"],
        "approved_entity_kind": row["approved_entity_kind"], "approved_entity_id": row["approved_entity_id"],
        "approved_payload": None if row["approved_payload_json"] is None else json.loads(row["approved_payload_json"]),
        "version": int(row["version"]), "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def invite_view(row: dict[str, Any]) -> dict[str, Any]:
    """An invite as shown to admins: the link token itself is never stored, only its hint."""
    return {"id": row["id"], "group_id": row["group_id"], "kind": row["kind"], "code": row["code"],
            "token_hint": row["token_hint"], "status": row["status"], "version": int(row["version"]),
            "created_at": row["created_at"], "expires_at": row["expires_at"], "max_uses": row["max_uses"],
            "use_count": int(row["use_count"]), "revoked_at": row["revoked_at"]}


# ---- cursor pagination ------------------------------------------------------------------


def encode_cursor(key: str, entity_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([key, entity_id]).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        key, entity_id = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        return str(key), str(entity_id)
    except (ValueError, TypeError) as exc:
        raise ValidationError("cursor is not valid") from exc


def page_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE
    if not 1 <= int(limit) <= MAX_PAGE:
        raise ValidationError(f"limit must be between 1 and {MAX_PAGE}")
    return int(limit)


def page(items: list[dict[str, Any]], limit: int, key_of) -> dict[str, Any]:
    """``items`` holds up to limit+1 rows; the extra one proves there is a next page."""
    more = len(items) > limit
    shown = items[:limit]
    return {"items": shown, "next_cursor": encode_cursor(*key_of(shown[-1])) if more and shown else None}
