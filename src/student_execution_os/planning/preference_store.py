"""SQLite owner of canonical planning preferences (schema v30)."""
from __future__ import annotations

from datetime import date, time
from zoneinfo import ZoneInfo

from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso
from student_execution_os.planning.preferences import (
    MAX_ACTIVE_PREFERENCES,
    PlanningPreference,
    PreferenceAnchor,
    PreferenceKind,
)

_ENTITY = "PLANNING_PREFERENCE"


def _row(row) -> PlanningPreference:
    return PlanningPreference(
        id=row["id"],
        account_id=row["account_id"],
        kind=PreferenceKind(row["kind"]),
        anchor=PreferenceAnchor(row["anchor"]),
        target=row["target"],
        date_from=date.fromisoformat(row["date_from"]),
        date_until=date.fromisoformat(row["date_until"]) if row["date_until"] else None,
        window_start=time.fromisoformat(row["window_start"]) if row["window_start"] else None,
        window_end=time.fromisoformat(row["window_end"]) if row["window_end"] else None,
        minutes=row["minutes"],
        reason=row["reason"],
        version=int(row["version"]),
    )


class SQLitePlanningPreferenceRepository:
    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical

    def list(self, account_id: str, *, current_on: date | None = None) -> list[PlanningPreference]:
        """Every preference of the account; with ``current_on``, only those not yet over."""
        self.canonical._require_account(account_id)
        sql = "SELECT * FROM planning_preferences WHERE account_id=?"
        args: list[object] = [account_id]
        if current_on is not None:
            sql += " AND (date_until IS NULL OR date_until>=?)"
            args.append(current_on.isoformat())
        rows = self.canonical.connection.execute(sql + " ORDER BY id", args).fetchall()
        return [_row(row) for row in rows]

    def get(self, account_id: str, preference_id: str) -> PlanningPreference:
        row = self.canonical.connection.execute(
            "SELECT * FROM planning_preferences WHERE account_id=? AND id=?", (account_id, preference_id)
        ).fetchone()
        if row is None:
            raise EntityNotFound("planning preference not found")
        return _row(row)

    def owner_of(self, preference_id: str) -> str | None:
        row = self.canonical.connection.execute(
            "SELECT account_id FROM planning_preferences WHERE id=?", (preference_id,)
        ).fetchone()
        return None if row is None else row["account_id"]

    def create(self, preference: PlanningPreference, *, today: date, actor: ActorCategory) -> PlanningPreference:
        if preference.date_until is not None and preference.date_until < today:
            raise ValidationError("preference ends in the past")
        now = _iso(self.canonical.clock.now())
        with self.canonical._tx() as conn:
            self.canonical._require_account(preference.account_id)
            active = conn.execute(
                "SELECT count(*) FROM planning_preferences WHERE account_id=? AND (date_until IS NULL OR date_until>=?)",
                (preference.account_id, today.isoformat()),
            ).fetchone()[0]
            if active >= MAX_ACTIVE_PREFERENCES:
                raise ValidationError(f"at most {MAX_ACTIVE_PREFERENCES} planning preferences can be active")
            payload = preference.payload()
            conn.execute(
                "INSERT INTO planning_preferences(id,account_id,kind,anchor,target,date_from,date_until,window_start,"
                "window_end,minutes,reason,version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (preference.id, preference.account_id, payload["kind"], payload["anchor"], payload["target"],
                 payload["date_from"], payload["date_until"], payload["window_start"], payload["window_end"],
                 preference.minutes, preference.reason, now, now),
            )
            self.canonical._record_change(
                conn, account_id=preference.account_id, entity_type=_ENTITY, entity_id=preference.id,
                action="CREATE_PLANNING_PREFERENCE", actor=actor,
            )
        return self.get(preference.account_id, preference.id)

    def delete(self, account_id: str, preference_id: str, *, expected_version: int, actor: ActorCategory) -> None:
        with self.canonical._tx() as conn:
            current = self.get(account_id, preference_id)
            if current.version != expected_version:
                raise VersionConflict("planning preference version changed")
            cur = conn.execute(
                "DELETE FROM planning_preferences WHERE account_id=? AND id=? AND version=?",
                (account_id, preference_id, expected_version),
            )
            if cur.rowcount != 1:
                raise VersionConflict("planning preference changed before delete")
            self.canonical._record_change(
                conn, account_id=account_id, entity_type=_ENTITY, entity_id=preference_id,
                action="DELETE_PLANNING_PREFERENCE", actor=actor,
            )


def derived_preference_windows(canonical: SQLiteCanonicalRepository, profile, account_id: str):
    """``build_planning_snapshot(derived_preferences=...)`` callback for one account."""
    from student_execution_os.planning.preferences import expand_preferences

    def windows(start, end, events):
        local_start = start.astimezone(ZoneInfo(profile.timezone_name)).date()
        preferences = SQLitePlanningPreferenceRepository(canonical).list(account_id, current_on=local_start)
        return expand_preferences(
            preferences, timezone_name=profile.timezone_name, planning_windows=profile.windows,
            start=start, end=end, events=events,
        )

    return windows
