from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.domain.model import ActorCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso


def _day(value: str | date, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValidationError(f"{field} must be YYYY-MM-DD") from exc


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValidationError("unknown IANA timezone") from exc


def _text(value) -> str | None:
    if value is None:
        return None
    out = str(value).strip()
    return out or None


class SQLiteReflectionRepository:
    """User-owned reflection notes plus derived plan/execution calibration.

    Notes and accepted hints are canonical. All metrics are recomputed from source
    facts; no derived ratio is written into Task effort or planning policy.
    """

    def __init__(self, canonical: SQLiteCanonicalRepository) -> None:
        self.canonical = canonical
        self.connection = canonical.connection
        self.clock = canonical.clock

    def _bounds(self, local_date: date, timezone_name: str) -> tuple[datetime, datetime]:
        zone = _zone(timezone_name)
        start = datetime.combine(local_date, time.min, zone).astimezone(timezone.utc)
        end = datetime.combine(local_date + timedelta(days=1), time.min, zone).astimezone(timezone.utc)
        return start, end

    def get_intent(self, account_id: str, local_date: str | date) -> dict | None:
        day = _day(local_date, "local_date")
        row = self.connection.execute(
            "SELECT * FROM daily_intents WHERE account_id=? AND local_date=?",
            (account_id, day.isoformat()),
        ).fetchone()
        if row is None:
            return None
        task_rows = self.connection.execute(
            "SELECT task_id,position FROM daily_intent_tasks "
            "WHERE account_id=? AND local_date=? ORDER BY position",
            (account_id, day.isoformat()),
        ).fetchall()
        return {
            "local_date": row["local_date"],
            "timezone_name": row["timezone_name"],
            "focus_note": row["focus_note"],
            "task_ids": [item["task_id"] for item in task_rows],
            "version": int(row["version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def upsert_intent(
        self,
        *,
        account_id: str,
        local_date: str | date,
        timezone_name: str,
        focus_note: str | None,
        task_ids: list[str] | tuple[str, ...],
        expected_version: int,
        actor: ActorCategory,
    ) -> dict:
        self.canonical._require_account(account_id)
        day = _day(local_date, "local_date")
        _zone(timezone_name)
        unique = list(dict.fromkeys(str(item) for item in task_ids if str(item)))
        if len(unique) > 5:
            raise ValidationError("daily intent supports at most five focus tasks")
        for task_id in unique:
            row = self.connection.execute(
                "SELECT kind FROM obligations WHERE account_id=? AND id=?", (account_id, task_id)
            ).fetchone()
            if row is None or row["kind"] != "TASK":
                raise EntityNotFound("daily intent focus task not found")
        current = self.get_intent(account_id, day)
        current_version = 0 if current is None else int(current["version"])
        if expected_version != current_version:
            raise VersionConflict(f"expected daily intent version {expected_version}, current {current_version}")
        now = self.clock.now()
        with self.canonical._tx() as conn:
            if current is None:
                conn.execute(
                    "INSERT INTO daily_intents(account_id,local_date,timezone_name,focus_note,version,created_at,updated_at) "
                    "VALUES (?,?,?,?,1,?,?)",
                    (account_id, day.isoformat(), timezone_name, _text(focus_note), _iso(now), _iso(now)),
                )
            else:
                cur = conn.execute(
                    "UPDATE daily_intents SET timezone_name=?,focus_note=?,version=version+1,updated_at=? "
                    "WHERE account_id=? AND local_date=? AND version=?",
                    (timezone_name, _text(focus_note), _iso(now), account_id, day.isoformat(), expected_version),
                )
                if cur.rowcount != 1:
                    raise VersionConflict("daily intent changed before commit")
                conn.execute(
                    "DELETE FROM daily_intent_tasks WHERE account_id=? AND local_date=?",
                    (account_id, day.isoformat()),
                )
            for position, task_id in enumerate(unique):
                conn.execute(
                    "INSERT INTO daily_intent_tasks(account_id,local_date,task_id,position) VALUES (?,?,?,?)",
                    (account_id, day.isoformat(), task_id, position),
                )
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="DAILY_INTENT",
                entity_id=day.isoformat(), action="UPSERT_DAILY_INTENT",
                actor=actor, payload={"task_ids": unique},
            )
        return self.get_intent(account_id, day)  # type: ignore[return-value]

    def _get_note(self, table: str, account_id: str, key_name: str, key: str) -> dict | None:
        row = self.connection.execute(
            f"SELECT * FROM {table} WHERE account_id=? AND {key_name}=?", (account_id, key)
        ).fetchone()
        return None if row is None else dict(row)

    def get_daily_reflection(self, account_id: str, local_date: str | date) -> dict | None:
        day = _day(local_date, "local_date").isoformat()
        return self._get_note("daily_reflections", account_id, "local_date", day)

    def upsert_daily_reflection(
        self,
        *,
        account_id: str,
        local_date: str | date,
        timezone_name: str,
        expected_version: int,
        summary: str | None,
        wins: str | None,
        blockers: str | None,
        adjustment: str | None,
        actor: ActorCategory,
    ) -> dict:
        return self._upsert_note(
            table="daily_reflections", key_name="local_date", key=_day(local_date, "local_date").isoformat(),
            entity_type="DAILY_REFLECTION", account_id=account_id, timezone_name=timezone_name,
            expected_version=expected_version, summary=summary, wins=wins, blockers=blockers, adjustment=adjustment,
            actor=actor,
        )

    def get_weekly_review(self, account_id: str, week_starts_on: str | date) -> dict | None:
        day = _day(week_starts_on, "week_starts_on").isoformat()
        return self._get_note("weekly_reviews", account_id, "week_starts_on", day)

    def upsert_weekly_review(
        self,
        *,
        account_id: str,
        week_starts_on: str | date,
        timezone_name: str,
        expected_version: int,
        summary: str | None,
        wins: str | None,
        blockers: str | None,
        adjustment: str | None,
        actor: ActorCategory,
    ) -> dict:
        week = _day(week_starts_on, "week_starts_on")
        if week.weekday() != 0:
            raise ValidationError("week_starts_on must be a Monday")
        return self._upsert_note(
            table="weekly_reviews", key_name="week_starts_on", key=week.isoformat(),
            entity_type="WEEKLY_REVIEW", account_id=account_id, timezone_name=timezone_name,
            expected_version=expected_version, summary=summary, wins=wins, blockers=blockers, adjustment=adjustment,
            actor=actor,
        )

    def _upsert_note(
        self,
        *,
        table: str,
        key_name: str,
        key: str,
        entity_type: str,
        account_id: str,
        timezone_name: str,
        expected_version: int,
        summary: str | None,
        wins: str | None,
        blockers: str | None,
        adjustment: str | None,
        actor: ActorCategory,
    ) -> dict:
        self.canonical._require_account(account_id)
        _zone(timezone_name)
        current = self._get_note(table, account_id, key_name, key)
        current_version = 0 if current is None else int(current["version"])
        if expected_version != current_version:
            raise VersionConflict(f"expected {entity_type.lower()} version {expected_version}, current {current_version}")
        now = self.clock.now()
        values = tuple(_text(v) for v in (summary, wins, blockers, adjustment))
        with self.canonical._tx() as conn:
            if current is None:
                conn.execute(
                    f"INSERT INTO {table}(account_id,{key_name},timezone_name,summary,wins,blockers,adjustment,version,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?,1,?,?)",
                    (account_id, key, timezone_name, *values, _iso(now), _iso(now)),
                )
            else:
                cur = conn.execute(
                    f"UPDATE {table} SET timezone_name=?,summary=?,wins=?,blockers=?,adjustment=?,"
                    f"version=version+1,updated_at=? WHERE account_id=? AND {key_name}=? AND version=?",
                    (timezone_name, *values, _iso(now), account_id, key, expected_version),
                )
                if cur.rowcount != 1:
                    raise VersionConflict(f"{entity_type.lower()} changed before commit")
            self.canonical._record_change(
                conn, account_id=account_id, entity_type=entity_type,
                entity_id=key, action=f"UPSERT_{entity_type}", actor=actor,
            )
        return self._get_note(table, account_id, key_name, key)  # type: ignore[return-value]

    def calibration_preference(self, account_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM effort_calibration_preferences WHERE account_id=?", (account_id,)
        ).fetchone()
        return None if row is None else {
            "multiplier": float(row["multiplier"]),
            "based_on_samples": int(row["based_on_samples"]),
            "accepted_at": row["accepted_at"],
            "version": int(row["version"]),
        }

    def accept_calibration(
        self,
        *,
        account_id: str,
        multiplier: float,
        based_on_samples: int,
        expected_version: int,
        actor: ActorCategory,
    ) -> dict:
        self.canonical._require_account(account_id)
        multiplier = round(float(multiplier), 2)
        if not 0.25 <= multiplier <= 4.0:
            raise ValidationError("calibration multiplier must be in [0.25,4.0]")
        if based_on_samples < 0:
            raise ValidationError("based_on_samples cannot be negative")
        current = self.calibration_preference(account_id)
        current_version = 0 if current is None else int(current["version"])
        if expected_version != current_version:
            raise VersionConflict(f"expected calibration version {expected_version}, current {current_version}")
        now = self.clock.now()
        with self.canonical._tx() as conn:
            if current is None:
                conn.execute(
                    "INSERT INTO effort_calibration_preferences(account_id,multiplier,based_on_samples,accepted_at,version) "
                    "VALUES (?,?,?,?,1)",
                    (account_id, multiplier, based_on_samples, _iso(now)),
                )
            else:
                cur = conn.execute(
                    "UPDATE effort_calibration_preferences SET multiplier=?,based_on_samples=?,accepted_at=?,version=version+1 "
                    "WHERE account_id=? AND version=?",
                    (multiplier, based_on_samples, _iso(now), account_id, expected_version),
                )
                if cur.rowcount != 1:
                    raise VersionConflict("calibration preference changed before commit")
            self.canonical._record_change(
                conn, account_id=account_id, entity_type="EFFORT_CALIBRATION",
                entity_id=account_id, action="ACCEPT_CALIBRATION_HINT", actor=actor,
                payload={"multiplier": multiplier, "based_on_samples": based_on_samples},
            )
        return self.calibration_preference(account_id)  # type: ignore[return-value]

    @staticmethod
    def _overlap_seconds(start: datetime, end: datetime, left: datetime, right: datetime) -> int:
        lo = max(start, left)
        hi = min(end, right)
        return max(0, int((hi - lo).total_seconds()))

    def day_stats(self, account_id: str, local_date: str | date, timezone_name: str, *, now: datetime) -> dict:
        day = _day(local_date, "local_date")
        start, end = self._bounds(day, timezone_name)
        actual_seconds = 0
        segment_rows = self.connection.execute(
            "SELECT s.started_at,s.ended_at FROM execution_segments s "
            "WHERE s.account_id=? ORDER BY s.started_at", (account_id,)
        ).fetchall()
        for row in segment_rows:
            segment_start = _dt(row["started_at"])
            segment_end = _dt(row["ended_at"]) or now
            if segment_start is None or segment_end is None:
                continue
            actual_seconds += self._overlap_seconds(segment_start, segment_end, start, end)

        snapshots = self.connection.execute(
            "SELECT id,generated_at,horizon_start,horizon_end FROM plan_snapshots "
            "WHERE account_id=? ORDER BY generated_at DESC,id DESC", (account_id,)
        ).fetchall()
        selected = None
        for row in snapshots:
            generated = _dt(row["generated_at"])
            horizon_start = _dt(row["horizon_start"])
            horizon_end = _dt(row["horizon_end"])
            if generated is None or horizon_start is None or horizon_end is None:
                continue
            if generated < end and horizon_start < end and start < horizon_end:
                selected = row
                break

        planned_seconds = 0
        if selected is not None:
            block_rows = self.connection.execute(
                "SELECT starts_at,ends_at FROM plan_blocks WHERE plan_id=? AND block_type='WORK'",
                (selected["id"],),
            ).fetchall()
            for row in block_rows:
                block_start = _dt(row["starts_at"])
                block_end = _dt(row["ends_at"])
                if block_start is None or block_end is None:
                    continue
                planned_seconds += self._overlap_seconds(block_start, block_end, start, end)

        completed = int(self.connection.execute(
            "SELECT count(*) FROM obligations WHERE account_id=? AND completed_at>=? AND completed_at<?",
            (account_id, _iso(start), _iso(end)),
        ).fetchone()[0])
        return {
            "local_date": day.isoformat(),
            "timezone_name": timezone_name,
            "planned_work_minutes": planned_seconds // 60,
            "actual_work_minutes": actual_seconds // 60,
            "delta_minutes": actual_seconds // 60 - planned_seconds // 60,
            "completed_obligations": completed,
            "plan_snapshot_id": None if selected is None else selected["id"],
            "plan_basis": "LATEST_SNAPSHOT_EXISTING_BY_DAY_END" if selected is not None else "NO_PLAN_SNAPSHOT",
        }

    def week_stats(self, account_id: str, week_starts_on: str | date, timezone_name: str, *, now: datetime) -> dict:
        week = _day(week_starts_on, "week_starts_on")
        if week.weekday() != 0:
            raise ValidationError("week_starts_on must be a Monday")
        days = [self.day_stats(account_id, week + timedelta(days=i), timezone_name, now=now) for i in range(7)]
        return {
            "week_starts_on": week.isoformat(),
            "timezone_name": timezone_name,
            "planned_work_minutes": sum(int(item["planned_work_minutes"]) for item in days),
            "actual_work_minutes": sum(int(item["actual_work_minutes"]) for item in days),
            "completed_obligations": sum(int(item["completed_obligations"]) for item in days),
            "days": days,
        }

    def calibration(self, account_id: str, *, now: datetime, window_days: int = 90) -> dict:
        window_days = max(7, min(window_days, 3650))
        since = now - timedelta(days=window_days)
        rows = self.connection.execute(
            "SELECT id,task_id,source_plan_block_id,started_at FROM execution_sessions "
            "WHERE account_id=? AND state='FINISHED' AND source_plan_block_id IS NOT NULL AND started_at>=? "
            "ORDER BY started_at DESC,id DESC",
            (account_id, _iso(since)),
        ).fetchall()
        grouped: dict[str, dict] = {}
        for row in rows:
            block_id = row["source_plan_block_id"]
            block = self.connection.execute(
                "SELECT obligation_id,starts_at,ends_at FROM plan_blocks WHERE id=?", (block_id,)
            ).fetchone()
            if block is None or block["obligation_id"] != row["task_id"]:
                continue
            block_start = _dt(block["starts_at"])
            block_end = _dt(block["ends_at"])
            if block_start is None or block_end is None:
                continue
            planned = int((block_end - block_start).total_seconds())
            segment_rows = self.connection.execute(
                "SELECT started_at,ended_at FROM execution_segments "
                "WHERE account_id=? AND session_id=? AND ended_at IS NOT NULL ORDER BY started_at",
                (account_id, row["id"]),
            ).fetchall()
            actual = 0
            for segment in segment_rows:
                left, right = _dt(segment["started_at"]), _dt(segment["ended_at"])
                if left is not None and right is not None:
                    actual += max(0, int((right - left).total_seconds()))
            sample = grouped.setdefault(block_id, {
                "source_plan_block_id": block_id,
                "task_id": row["task_id"],
                "planned_seconds": planned,
                "actual_seconds": 0,
                "session_ids": [],
                "started_at": row["started_at"],
            })
            sample["actual_seconds"] += actual
            sample["session_ids"].append(row["id"])
            if row["started_at"] < sample["started_at"]:
                sample["started_at"] = row["started_at"]

        samples = []
        for item in grouped.values():
            planned = int(item["planned_seconds"])
            actual = int(item["actual_seconds"])
            if planned < 300 or actual < 300:
                continue
            ratio = actual / planned
            samples.append({
                "source_plan_block_id": item["source_plan_block_id"],
                "session_ids": item["session_ids"],
                "task_id": item["task_id"],
                "planned_minutes": planned // 60,
                "actual_minutes": actual // 60,
                "ratio": round(ratio, 3),
                "started_at": item["started_at"],
            })
        samples.sort(key=lambda item: (item["started_at"], item["source_plan_block_id"]), reverse=True)

        ratios = [float(item["ratio"]) for item in samples]
        median_ratio = None if not ratios else round(float(median(ratios)), 2)
        suggested = None if len(ratios) < 3 else max(0.25, min(4.0, median_ratio or 1.0))
        if median_ratio is None:
            bias = "NO_DATA"
        elif len(ratios) < 3:
            bias = "INSUFFICIENT_DATA"
        elif median_ratio > 1.15:
            bias = "UNDER_ESTIMATING"
        elif median_ratio < 0.85:
            bias = "OVER_ESTIMATING"
        else:
            bias = "ALIGNED"
        return {
            "window_days": window_days,
            "sample_count": len(samples),
            "median_actual_to_planned_ratio": median_ratio,
            "suggested_multiplier": suggested,
            "bias": bias,
            "samples": samples[:50],
            "accepted": self.calibration_preference(account_id),
            "automatic_mutation": False,
        }
