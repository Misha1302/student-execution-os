"""Location triggers: «когда приду домой, напомни разобрать вещи» (schema v32, ADR 0035).

A trigger is a typed ENTER/EXIT condition on one of the user's places — not a rule
language. The Android device detects the transition with the platform geofencing
service (it alone can, in the background) and reports it as an idempotent sync
operation; the server owns the trigger's lifecycle:

    ARMED ──(device: ENTER/EXIT at the place)──▶ FIRED ──(«Готово»)──▶ DONE
      ▲                                           │
      └───────────── reopen ◀── CANCELLED ◀───────┘ cancel

A repeating trigger stays ARMED and ignores a second firing within the cool-down
(``REFIRE_COOLDOWN``), so GPS jitter at the door is not a second arrival. The
detecting device shows the notification itself (it may be offline); the server
records the fact and de-duplicates late or doubled callbacks.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from student_execution_os.domain.errors import EntityNotFound, ValidationError
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

TRANSITIONS = ("ENTER", "EXIT")
REFIRE_COOLDOWN = timedelta(minutes=30)
DEFAULT_RADIUS_METERS = 150
_COLUMNS = ("id,place_id,transition,title,note,radius_meters,repeat,status,fired_at,fire_count,completed_at,version,"
            "created_at,updated_at")
_EDITABLE = {"place_id", "transition", "title", "note", "radius_meters", "repeat"}


def clean_trigger_fields(payload: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    unknown = set(payload) - _EDITABLE
    if unknown:
        raise ValidationError("location trigger fields are not supported: " + ", ".join(sorted(unknown)))
    fields: dict[str, Any] = {}
    if creating or "title" in payload:
        title = " ".join(str(payload.get("title") or "").split())
        if not title or len(title) > 300:
            raise ValidationError("a location trigger needs a title of up to 300 characters")
        fields["title"] = title
    if creating or "place_id" in payload:
        if not payload.get("place_id"):
            raise ValidationError("a location trigger needs a place")
        fields["place_id"] = str(payload["place_id"])
    if creating or "transition" in payload:
        transition = str(payload.get("transition") or "ENTER")
        if transition not in TRANSITIONS:
            raise ValidationError("transition must be ENTER or EXIT")
        fields["transition"] = transition
    if "note" in payload:
        note = str(payload.get("note") or "").strip() or None
        if note and len(note) > 2000:
            raise ValidationError("note is longer than 2000 characters")
        fields["note"] = note
    if "radius_meters" in payload:
        radius = payload.get("radius_meters")
        if isinstance(radius, bool) or not isinstance(radius, int) or not 50 <= radius <= 5000:
            raise ValidationError("radius_meters must be a whole number from 50 to 5000")
        fields["radius_meters"] = radius
    if "repeat" in payload:
        if not isinstance(payload["repeat"], bool):
            raise ValidationError("repeat must be true or false")
        fields["repeat"] = payload["repeat"]
    return fields


class SQLiteLocationTriggerRepository:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection

    def _payload(self, row) -> dict[str, Any]:
        item = {key: row[key] for key in _COLUMNS.split(",")}
        item["kind"] = "LOCATION_TRIGGER"
        item["repeat"] = bool(item["repeat"])
        place = self.connection.execute(
            "SELECT alias,display_name,latitude,longitude FROM places WHERE id=?", (row["place_id"],)).fetchone()
        item["place_name"] = None if place is None else (place["alias"] or place["display_name"])
        # A device can only watch a place whose position the user saved.
        item["needs_coordinates"] = place is None or place["latitude"] is None
        return item

    def get(self, account_id: str, trigger_id: str) -> dict[str, Any]:
        row = self.connection.execute(f"SELECT {_COLUMNS} FROM location_triggers WHERE account_id=? AND id=?",
                                      (account_id, trigger_id)).fetchone()
        if row is None:
            raise EntityNotFound("location trigger not found")
        return self._payload(row)

    def owner_of(self, trigger_id: str) -> str | None:
        row = self.connection.execute("SELECT account_id FROM location_triggers WHERE id=?", (trigger_id,)).fetchone()
        return None if row is None else row["account_id"]

    def list(self, account_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            f"SELECT {_COLUMNS} FROM location_triggers WHERE account_id=? ORDER BY "
            "CASE status WHEN 'ARMED' THEN 0 WHEN 'FIRED' THEN 1 ELSE 2 END, updated_at DESC", (account_id,)).fetchall()
        return [self._payload(row) for row in rows]

    def armed_for_device(self, account_id: str) -> list[dict[str, Any]]:
        """What the user's own phone must watch: armed triggers with their place's position.

        Coordinates leave the server only here, to the authenticated owner's device, which
        needs them to register the geofence. They are never part of an Assistant context.
        """
        rows = self.connection.execute(
            "SELECT t.id,t.transition,t.title,t.note,t.radius_meters,t.repeat,t.version,p.id AS place_id,"
            "p.latitude,p.longitude,coalesce(p.alias,p.display_name) AS place_name FROM location_triggers t "
            "JOIN places p ON p.account_id=t.account_id AND p.id=t.place_id WHERE t.account_id=? "
            "AND t.status='ARMED' AND p.latitude IS NOT NULL ORDER BY t.id", (account_id,)).fetchall()
        return [{**dict(row), "repeat": bool(row["repeat"])} for row in rows]

    def _require_place(self, account_id: str, place_id: str) -> None:
        if self.connection.execute("SELECT 1 FROM places WHERE account_id=? AND id=?",
                                   (account_id, place_id)).fetchone() is None:
            raise EntityNotFound("place not found")

    def create(self, *, account_id: str, trigger_id: str, fields: dict[str, Any], actor: ActorCategory,
               now: datetime) -> dict[str, Any]:
        self._require_place(account_id, fields["place_id"])
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO location_triggers(id,account_id,place_id,transition,title,note,radius_meters,repeat,status,"
                "actor_category,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,'ARMED',?,1,?,?)",
                (trigger_id, account_id, fields["place_id"], fields["transition"], fields["title"], fields.get("note"),
                 fields.get("radius_meters", DEFAULT_RADIUS_METERS), int(fields.get("repeat", False)), actor.value,
                 _iso(now), _iso(now)))
            self.canonical._record_change(conn, account_id=account_id, entity_type="LOCATION_TRIGGER",
                                          entity_id=trigger_id, action="CREATE_LOCATION_TRIGGER", actor=actor,
                                          payload={"transition": fields["transition"]})
        return self.get(account_id, trigger_id)

    def _write(self, account_id: str, trigger_id: str, action: str, actor: ActorCategory, now: datetime,
               **fields: Any) -> dict[str, Any]:
        assignments = ",".join(f"{key}=?" for key in fields)
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        with self.canonical._tx() as conn:
            conn.execute(f"UPDATE location_triggers SET {assignments},version=version+1,updated_at=? "
                         "WHERE account_id=? AND id=?", (*values, _iso(now), account_id, trigger_id))
            self.canonical._record_change(conn, account_id=account_id, entity_type="LOCATION_TRIGGER",
                                          entity_id=trigger_id, action=action, actor=actor)
        return self.get(account_id, trigger_id)

    def update(self, account_id: str, trigger_id: str, fields: dict[str, Any], *, actor: ActorCategory,
               now: datetime) -> dict[str, Any]:
        self.get(account_id, trigger_id)
        if "place_id" in fields:
            self._require_place(account_id, fields["place_id"])
        return self._write(account_id, trigger_id, "UPDATE_LOCATION_TRIGGER", actor, now, **fields) if fields \
            else self.get(account_id, trigger_id)

    def fire(self, account_id: str, trigger_id: str, *, transition: str, occurred_at: datetime,
             actor: ActorCategory, now: datetime) -> tuple[dict[str, Any], str | None]:
        """The device saw the transition. Returns (trigger, NOOP code or None when recorded)."""
        current = self.get(account_id, trigger_id)
        if transition != current["transition"]:
            return current, "OTHER_TRANSITION"
        if current["status"] in ("DONE", "CANCELLED"):
            return current, "TRIGGER_CLOSED"
        if current["status"] == "FIRED":
            return current, "ALREADY_FIRED"
        last = _dt(current["fired_at"])
        if current["repeat"] and last is not None and abs(occurred_at - last) < REFIRE_COOLDOWN:
            return current, "DUPLICATE_TRANSITION"
        status = "ARMED" if current["repeat"] else "FIRED"
        return self._write(account_id, trigger_id, "FIRE_LOCATION_TRIGGER", actor, now, status=status,
                           fired_at=_iso(occurred_at), fire_count=int(current["fire_count"]) + 1), None

    def done(self, account_id: str, trigger_id: str, *, actor: ActorCategory, now: datetime) -> tuple[dict[str, Any], str | None]:
        current = self.get(account_id, trigger_id)
        if current["status"] == "DONE":
            return current, "ALREADY_DONE"
        if current["status"] == "CANCELLED":
            return current, "TRIGGER_CANCELLED"
        return self._write(account_id, trigger_id, "COMPLETE_LOCATION_TRIGGER", actor, now, status="DONE",
                           completed_at=_iso(now)), None

    def cancel(self, account_id: str, trigger_id: str, *, actor: ActorCategory, now: datetime) -> tuple[dict[str, Any], str | None]:
        current = self.get(account_id, trigger_id)
        if current["status"] == "CANCELLED":
            return current, "ALREADY_CANCELLED"
        return self._write(account_id, trigger_id, "CANCEL_LOCATION_TRIGGER", actor, now, status="CANCELLED"), None

    def reopen(self, account_id: str, trigger_id: str, *, actor: ActorCategory, now: datetime) -> tuple[dict[str, Any], str | None]:
        current = self.get(account_id, trigger_id)
        if current["status"] == "ARMED":
            return current, "ALREADY_OPEN"
        return self._write(account_id, trigger_id, "REARM_LOCATION_TRIGGER", actor, now, status="ARMED",
                           completed_at=None), None

    def delete(self, account_id: str, trigger_id: str, *, actor: ActorCategory, now: datetime) -> None:
        self.get(account_id, trigger_id)
        with self.canonical._tx() as conn:
            conn.execute("DELETE FROM location_triggers WHERE account_id=? AND id=?", (account_id, trigger_id))
            conn.execute("INSERT OR REPLACE INTO deleted_entities(account_id,entity_kind,entity_id,deleted_at) "
                         "VALUES (?,'LOCATION_TRIGGER',?,?)", (account_id, trigger_id, _iso(now)))
            self.canonical._record_change(conn, account_id=account_id, entity_type="LOCATION_TRIGGER",
                                          entity_id=trigger_id, action="DELETE_LOCATION_TRIGGER", actor=actor)
