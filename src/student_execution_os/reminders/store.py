"""SQLite persistence for reminder preferences, per-task reminder state, the
reminder message outbox (also the in-app inbox) and push device registrations.

Reminder rows are workflow state: writing them never advances the account's
canonical ``server_revision``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso

from .policy import ReminderPrefs, ReminderState, parse_clock

MESSAGE_COLUMNS = (
    "id,stage,task_ids_json,title,body,deep_link,actions_json,created_at,delivery_state,attempts,"
    "last_error,sent_at,seen_at,acted_at,acted_action"
)


class ReminderStore:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection

    # ---- preferences ----------------------------------------------------------------

    def prefs(self, account_id: str) -> ReminderPrefs:
        row = self.connection.execute("SELECT * FROM reminder_preferences WHERE account_id=?", (account_id,)).fetchone()
        if row is None:
            return ReminderPrefs()
        return ReminderPrefs(
            enabled=bool(row["enabled"]), intensity=row["intensity"], timezone_name=row["timezone_name"],
            quiet_starts_local=row["quiet_starts_local"], quiet_ends_local=row["quiet_ends_local"],
            locale=row["locale"], version=int(row["version"]),
        )

    def update_prefs(self, account_id: str, payload: dict[str, Any]) -> ReminderPrefs:
        self.canonical._require_account(account_id)
        current = self.prefs(account_id)
        if "expected_version" in payload and int(payload["expected_version"]) != current.version:
            raise VersionConflict("reminder preferences changed")
        quiet = payload.get("quiet_hours") or {}
        if not isinstance(quiet, dict):
            raise ValidationError("quiet_hours must be an object")
        candidate = ReminderPrefs(
            enabled=bool(payload.get("enabled", current.enabled)),
            intensity=str(payload.get("intensity", current.intensity)),
            timezone_name=str(payload.get("timezone", current.timezone_name)),
            quiet_starts_local=str(quiet.get("starts_local", current.quiet_starts_local)),
            quiet_ends_local=str(quiet.get("ends_local", current.quiet_ends_local)),
            locale=str(payload.get("locale", current.locale)),
            version=current.version + 1,
        )
        parse_clock(candidate.quiet_starts_local)
        now = _iso(self.canonical.clock.now())
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO reminder_preferences(account_id,enabled,intensity,timezone_name,quiet_starts_local,"
                "quiet_ends_local,locale,version,updated_at) VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET enabled=excluded.enabled,intensity=excluded.intensity,"
                "timezone_name=excluded.timezone_name,quiet_starts_local=excluded.quiet_starts_local,"
                "quiet_ends_local=excluded.quiet_ends_local,locale=excluded.locale,version=excluded.version,"
                "updated_at=excluded.updated_at",
                (account_id, int(candidate.enabled), candidate.intensity, candidate.timezone_name,
                 candidate.quiet_starts_local, candidate.quiet_ends_local, candidate.locale, candidate.version, now),
            )
        return candidate

    @staticmethod
    def prefs_payload(prefs: ReminderPrefs) -> dict[str, Any]:
        return {
            "enabled": prefs.enabled, "intensity": prefs.intensity, "timezone": prefs.timezone_name,
            "quiet_hours": {"starts_local": prefs.quiet_starts_local, "ends_local": prefs.quiet_ends_local},
            "locale": prefs.locale, "version": prefs.version,
        }

    # ---- per-task state ---------------------------------------------------------------

    def states(self, account_id: str) -> dict[str, ReminderState]:
        rows = self.connection.execute("SELECT * FROM reminder_states WHERE account_id=?", (account_id,)).fetchall()
        return {row["task_id"]: self._state(row) for row in rows}

    @staticmethod
    def _state(row) -> ReminderState:
        return ReminderState(
            episode_key=row["episode_key"], sent_count=int(row["sent_count"]), ignored_count=int(row["ignored_count"]),
            last_sent_at=_dt(row["last_sent_at"]), last_stage=row["last_stage"], last_risk=row["last_risk"],
            stages_sent=frozenset(json.loads(row["stages_sent_json"])),
            last_interaction_at=_dt(row["last_interaction_at"]), snoozed_until=_dt(row["snoozed_until"]),
            closed_reason=row["closed_reason"], next_check_at=_dt(row["next_check_at"]),
        )

    def save_state(self, account_id: str, task_id: str, state: ReminderState) -> None:
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO reminder_states(account_id,task_id,episode_key,sent_count,ignored_count,last_sent_at,last_stage,"
                "last_risk,stages_sent_json,last_interaction_at,snoozed_until,closed_reason,next_check_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,task_id) DO UPDATE SET "
                "episode_key=excluded.episode_key,sent_count=excluded.sent_count,ignored_count=excluded.ignored_count,"
                "last_sent_at=excluded.last_sent_at,last_stage=excluded.last_stage,last_risk=excluded.last_risk,"
                "stages_sent_json=excluded.stages_sent_json,"
                # Interactions/snoozes recorded concurrently by the API must not be overwritten
                # by an engine tick that loaded older state.
                "last_interaction_at=max(coalesce(reminder_states.last_interaction_at,''),coalesce(excluded.last_interaction_at,'')),"
                "snoozed_until=CASE WHEN coalesce(reminder_states.snoozed_until,'') > coalesce(excluded.snoozed_until,'') "
                "THEN reminder_states.snoozed_until ELSE excluded.snoozed_until END,"
                "closed_reason=excluded.closed_reason,next_check_at=excluded.next_check_at,updated_at=excluded.updated_at",
                (account_id, task_id, state.episode_key, state.sent_count, state.ignored_count, _iso(state.last_sent_at),
                 state.last_stage, state.last_risk, json.dumps(sorted(state.stages_sent)), _iso(state.last_interaction_at),
                 _iso(state.snoozed_until), state.closed_reason, _iso(state.next_check_at),
                 _iso(self.canonical.clock.now())),
            )
            conn.execute(
                "UPDATE reminder_states SET last_interaction_at=NULLIF(last_interaction_at,''),snoozed_until=NULLIF(snoozed_until,'') "
                "WHERE account_id=? AND task_id=?", (account_id, task_id),
            )

    def touch(self, account_id: str, task_id: str, at: datetime, *, snooze_until: datetime | None = None) -> None:
        """Record that the user interacted with a task (any mutation, snooze or push action)."""
        with self.canonical._tx() as conn:
            conn.execute(
                "INSERT INTO reminder_states(account_id,task_id,episode_key,last_interaction_at,snoozed_until,updated_at) "
                "VALUES (?,?,'',?,?,?) ON CONFLICT(account_id,task_id) DO UPDATE SET "
                "last_interaction_at=excluded.last_interaction_at,"
                "snoozed_until=coalesce(excluded.snoozed_until,reminder_states.snoozed_until),updated_at=excluded.updated_at",
                (account_id, task_id, _iso(at), _iso(snooze_until), _iso(at)),
            )
            if snooze_until is not None:
                # A snoozed task's undelivered prompts are no longer wanted.
                conn.execute(
                    "UPDATE reminder_messages SET delivery_state='CANCELLED',lease_owner=NULL,lease_expires_at=NULL,"
                    "last_error='SNOOZED' WHERE account_id=? AND delivery_state='PENDING' AND EXISTS ("
                    "SELECT 1 FROM json_each(reminder_messages.task_ids_json) WHERE value=?)",
                    (account_id, task_id),
                )

    # ---- messages ---------------------------------------------------------------------

    def add_message(self, account_id: str, *, stage: str, task_ids: list[str], content: dict[str, Any],
                    dedupe_key: str, now: datetime) -> str | None:
        message_id = f"rem-{uuid4().hex[:24]}"
        with self.canonical._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO reminder_messages(id,account_id,dedupe_key,stage,task_ids_json,title,body,deep_link,"
                "actions_json,created_at,delivery_state,attempts,next_attempt_at) VALUES (?,?,?,?,?,?,?,?,?,?,'PENDING',0,?)",
                (message_id, account_id, dedupe_key, stage, json.dumps(task_ids), content["title"], content["body"],
                 content["deep_link"], json.dumps(content["actions"]), _iso(now), _iso(now)),
            )
        return message_id if cur.rowcount == 1 else None

    def last_message_at(self, account_id: str) -> datetime | None:
        row = self.connection.execute(
            "SELECT max(created_at) FROM reminder_messages WHERE account_id=? AND delivery_state!='CANCELLED'", (account_id,)
        ).fetchone()
        return _dt(row[0]) if row and row[0] else None

    def count_since(self, account_id: str, since: datetime) -> int:
        return int(self.connection.execute(
            "SELECT count(*) FROM reminder_messages WHERE account_id=? AND created_at>=? AND delivery_state!='CANCELLED'",
            (account_id, _iso(since)),
        ).fetchone()[0])

    def messages(self, account_id: str, *, since: datetime, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            f"SELECT {MESSAGE_COLUMNS} FROM reminder_messages WHERE account_id=? AND created_at>=? "
            "AND delivery_state!='CANCELLED' ORDER BY created_at DESC LIMIT ?",
            (account_id, _iso(since), limit),
        ).fetchall()
        return [self._message(row) for row in rows]

    def message(self, account_id: str, message_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            f"SELECT {MESSAGE_COLUMNS} FROM reminder_messages WHERE account_id=? AND id=?", (account_id, message_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("reminder not found")
        return self._message(row)

    @staticmethod
    def _message(row) -> dict[str, Any]:
        item = dict(row)
        item["task_ids"] = json.loads(item.pop("task_ids_json"))
        item["actions"] = json.loads(item.pop("actions_json"))
        return item

    def mark_acted(self, account_id: str, message_id: str, action: str, at: datetime) -> None:
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE reminder_messages SET acted_at=coalesce(acted_at,?),acted_action=coalesce(acted_action,?),"
                "seen_at=coalesce(seen_at,?) WHERE account_id=? AND id=?",
                (_iso(at), action, _iso(at), account_id, message_id),
            )

    def mark_seen(self, account_id: str, at: datetime) -> int:
        with self.canonical._tx() as conn:
            return conn.execute(
                "UPDATE reminder_messages SET seen_at=? WHERE account_id=? AND seen_at IS NULL", (_iso(at), account_id)
            ).rowcount

    def upcoming(self, account_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT task_id,next_check_at,snoozed_until,closed_reason,ignored_count,sent_count FROM reminder_states "
            "WHERE account_id=? AND closed_reason IS NULL AND (next_check_at IS NOT NULL OR snoozed_until IS NOT NULL)",
            (account_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- devices ----------------------------------------------------------------------

    def register_device(self, account_id: str, token: str, label: str | None) -> dict[str, Any]:
        self.canonical._require_account(account_id)
        token = str(token or "").strip()
        if not token or len(token) > 4096:
            raise ValidationError("a valid push token is required")
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = _iso(self.canonical.clock.now())
        with self.canonical._tx() as conn:
            # A physical device belongs to the account signed in on it right now.
            conn.execute(
                "UPDATE mobile_devices SET active=0,token='',version=version+1,updated_at=? "
                "WHERE token_hash=? AND account_id!=? AND active=1", (now, digest, account_id),
            )
            existing = conn.execute(
                "SELECT id FROM mobile_devices WHERE account_id=? AND token_hash=?", (account_id, digest)
            ).fetchone()
            if existing is None:
                device_id = str(uuid4())
                conn.execute(
                    "INSERT INTO mobile_devices(id,account_id,platform,token_hash,token,label,created_at,updated_at) "
                    "VALUES (?,?,'ANDROID',?,?,?,?,?)", (device_id, account_id, digest, token, label, now, now),
                )
            else:
                device_id = existing["id"]
                conn.execute(
                    "UPDATE mobile_devices SET token=?,label=?,active=1,version=version+1,updated_at=? WHERE id=?",
                    (token, label, now, device_id),
                )
        return self.device(account_id, device_id)

    def device(self, account_id: str, device_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT id,platform,label,active,version,created_at,updated_at FROM mobile_devices WHERE account_id=? AND id=?",
            (account_id, device_id),
        ).fetchone()
        if row is None:
            raise EntityNotFound("device not found")
        return dict(row)

    def devices(self, account_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT id,platform,label,active,version,created_at,updated_at FROM mobile_devices "
            "WHERE account_id=? ORDER BY updated_at DESC", (account_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def revoke_device(self, account_id: str, device_id: str) -> dict[str, Any]:
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE mobile_devices SET active=0,token='',version=version+1,updated_at=? WHERE account_id=? AND id=?",
                (_iso(self.canonical.clock.now()), account_id, device_id),
            )
        return self.device(account_id, device_id)

    def active_tokens(self, account_id: str) -> list[tuple[str, str]]:
        rows = self.connection.execute(
            "SELECT id,token FROM mobile_devices WHERE account_id=? AND active=1 AND token!='' ORDER BY id", (account_id,)
        ).fetchall()
        return [(row["id"], row["token"]) for row in rows]

    def deactivate_device(self, device_id: str) -> None:
        with self.canonical._tx() as conn:
            conn.execute(
                "UPDATE mobile_devices SET active=0,token='',version=version+1,updated_at=? WHERE id=?",
                (_iso(self.canonical.clock.now()), device_id),
            )


def state_payload(state: ReminderState) -> dict[str, Any]:
    data = asdict(state)
    data["stages_sent"] = sorted(state.stages_sent)
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in data.items()}


def since_days(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)
