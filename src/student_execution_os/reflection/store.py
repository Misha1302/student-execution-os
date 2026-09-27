from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.domain.model import ActorCategory, LifecycleStatus, ObligationCategory
from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _dt, _iso


def _day(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("local_date must be YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValidationError("local_date must be canonical YYYY-MM-DD")
    return parsed


def _clip_seconds(start: datetime, end: datetime, lower: datetime, upper: datetime) -> int:
    left, right = max(start, lower), min(end, upper)
    return max(0, int((right - left).total_seconds()))


class SQLiteReflectionStore:
    MIN_CALIBRATION_SAMPLE = 5
    CALIBRATION_WINDOW_DAYS = 90

    def __init__(self, repo: SQLiteCanonicalRepository) -> None:
        self.repo = repo
        self.connection = repo.connection
        self.clock = repo.clock

    def timezone_name(self, account_id: str) -> str:
        row = self.connection.execute(
            "SELECT timezone_name FROM planning_profiles WHERE account_id=?", (account_id,)
        ).fetchone()
        return "UTC" if row is None else str(row["timezone_name"])

    def local_date(self, account_id: str, at: datetime) -> str:
        return at.astimezone(ZoneInfo(self.timezone_name(account_id))).date().isoformat()

    def intent(self, account_id: str, local_date: str) -> dict[str, Any] | None:
        _day(local_date)
        row = self.connection.execute(
            "SELECT * FROM daily_intents WHERE account_id=? AND local_date=?", (account_id, local_date)
        ).fetchone()
        if row is None:
            return None
        return {
            "local_date": row["local_date"],
            "priority_task_ids": json.loads(row["priority_task_ids_json"]),
            "note": row["note"],
            "started_at": row["started_at"],
            "closed_at": row["closed_at"],
            "version": int(row["version"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def set_intent(
        self,
        account_id: str,
        local_date: str,
        priority_task_ids: list[str],
        note: str | None,
        actor: ActorCategory,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        _day(local_date)
        clean: list[str] = []
        for task_id in priority_task_ids:
            value = str(task_id).strip()
            if value and value not in clean:
                clean.append(value)
        if len(clean) > 3:
            raise ValidationError("daily intent supports at most three priority tasks")
        for task_id in clean:
            task = self.repo.get_task(account_id, task_id)
            if task.obligation.lifecycle_status not in {LifecycleStatus.ACTIVE, LifecycleStatus.DRAFT}:
                raise ValidationError("daily intent can only prioritize open tasks")
        note = None if note is None or not str(note).strip() else str(note).strip()
        if note is not None and len(note) > 2000:
            raise ValidationError("daily intent note is too long")
        now = self.clock.now()
        current = self.intent(account_id, local_date)
        if current is None:
            if expected_version not in (None, 0):
                raise VersionConflict("daily intent does not exist at expected version")
        elif expected_version is not None and current["version"] != expected_version:
            raise VersionConflict("daily intent version changed")
        with self.repo._tx() as conn:
            if current is None:
                conn.execute(
                    "INSERT INTO daily_intents(account_id,local_date,priority_task_ids_json,note,started_at,closed_at,version,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,NULL,1,?,?)",
                    (account_id, local_date, json.dumps(clean), note, _iso(now), _iso(now), _iso(now)),
                )
            else:
                cur = conn.execute(
                    "UPDATE daily_intents SET priority_task_ids_json=?,note=?,started_at=COALESCE(started_at,?),"
                    "closed_at=NULL,version=version+1,updated_at=? "
                    "WHERE account_id=? AND local_date=? AND version=?",
                    (json.dumps(clean), note, _iso(now), _iso(now), account_id, local_date, current["version"]),
                )
                if cur.rowcount != 1:
                    raise VersionConflict("daily intent version changed before commit")
            self.repo._record_change(
                conn, account_id=account_id, entity_type="DAILY_INTENT",
                entity_id=local_date, action="SET_DAILY_INTENT", actor=actor,
                payload={"priority_task_ids": clean},
            )
        return self.intent(account_id, local_date)  # type: ignore[return-value]

    def close_intent(
        self, account_id: str, local_date: str, actor: ActorCategory,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        current = self.intent(account_id, local_date)
        if current is None:
            if expected_version not in (None, 0):
                raise VersionConflict("daily intent does not exist at expected version")
            current = self.set_intent(account_id, local_date, [], None, actor, expected_version=0)
        elif expected_version is not None and current["version"] != expected_version:
            raise VersionConflict("daily intent version changed")
        if current["closed_at"] is not None:
            return current
        now = self.clock.now()
        with self.repo._tx() as conn:
            cur = conn.execute(
                "UPDATE daily_intents SET closed_at=?,version=version+1,updated_at=? "
                "WHERE account_id=? AND local_date=? AND version=?",
                (_iso(now), _iso(now), account_id, local_date, current["version"]),
            )
            if cur.rowcount != 1:
                raise VersionConflict("daily intent version changed before close")
            self.repo._record_change(
                conn, account_id=account_id, entity_type="DAILY_INTENT",
                entity_id=local_date, action="CLOSE_DAILY_INTENT", actor=actor,
            )
        return self.intent(account_id, local_date)  # type: ignore[return-value]

    def calibration_preferences(self, account_id: str) -> dict[str, dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM calibration_preferences WHERE account_id=? ORDER BY category", (account_id,)
        ).fetchall()
        return {
            row["category"]: {
                "category": row["category"],
                "safety_multiplier": float(row["safety_multiplier"]),
                "enabled": bool(row["enabled"]),
                "suppress_suggestion": bool(row["suppress_suggestion"]),
                "version": int(row["version"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    def set_calibration(
        self,
        account_id: str,
        category: str,
        multiplier: float,
        enabled: bool,
        suppress_suggestion: bool,
        actor: ActorCategory,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        try:
            normalized = ObligationCategory(category).value
        except ValueError as exc:
            raise ValidationError("unknown task category") from exc
        multiplier = round(float(multiplier), 2)
        if not 1.0 <= multiplier <= 3.0:
            raise ValidationError("safety multiplier must be in [1.0,3.0]")
        current = self.calibration_preferences(account_id).get(normalized)
        if expected_version is not None and (current is None or current["version"] != expected_version):
            raise VersionConflict("calibration preference version changed")
        now = self.clock.now()
        with self.repo._tx() as conn:
            if current is None:
                conn.execute(
                    "INSERT INTO calibration_preferences(account_id,category,safety_multiplier,enabled,suppress_suggestion,version,updated_at) "
                    "VALUES (?,?,?,?,?,1,?)",
                    (account_id, normalized, multiplier, int(enabled), int(suppress_suggestion), _iso(now)),
                )
            else:
                conn.execute(
                    "UPDATE calibration_preferences SET safety_multiplier=?,enabled=?,suppress_suggestion=?,"
                    "version=version+1,updated_at=? WHERE account_id=? AND category=?",
                    (multiplier, int(enabled), int(suppress_suggestion), _iso(now), account_id, normalized),
                )
            self.repo._record_change(
                conn, account_id=account_id, entity_type="CALIBRATION_PREFERENCE",
                entity_id=normalized, action="SET_CALIBRATION", actor=actor,
                payload={"multiplier": multiplier, "enabled": enabled, "suppress_suggestion": suppress_suggestion},
            )
        return self.calibration_preferences(account_id)[normalized]

    def planning_signals(self, account_id: str, at: datetime) -> tuple[tuple[str, ...], dict[str, float]]:
        current = self.intent(account_id, self.local_date(account_id, at))
        priorities = tuple(current["priority_task_ids"]) if current is not None else ()
        prefs = self.calibration_preferences(account_id)
        multipliers = {
            category: float(item["safety_multiplier"])
            for category, item in prefs.items()
            if item["enabled"] and float(item["safety_multiplier"]) > 1.0
        }
        return priorities, multipliers

    def _actual_seconds(self, account_id: str, start: datetime, end: datetime) -> int:
        rows = self.connection.execute(
            "SELECT s.started_at,s.ended_at FROM execution_segments s "
            "JOIN execution_sessions e ON e.account_id=s.account_id AND e.id=s.session_id "
            "WHERE e.account_id=? AND s.started_at<? AND (s.ended_at IS NULL OR s.ended_at>?)",
            (account_id, _iso(end), _iso(start)),
        ).fetchall()
        now = self.clock.now()
        return sum(
            _clip_seconds(_dt(row["started_at"]), _dt(row["ended_at"]) or now, start, end)
            for row in rows
        )

    def _baseline_plan_for_day(self, account_id: str, start: datetime, end: datetime):
        # Prefer the first plan actually generated during the day. This is the
        # observable morning baseline; later replans belong to churn, not "planned".
        row = self.connection.execute(
            "SELECT id FROM plan_snapshots WHERE account_id=? AND generated_at>=? AND generated_at<? "
            "AND horizon_start<? AND horizon_end>? ORDER BY generated_at ASC,id ASC LIMIT 1",
            (account_id, _iso(start), _iso(end), _iso(end), _iso(start)),
        ).fetchone()
        if row is None:
            # If the app never planned during this local day, fall back to the last
            # plan already in force at midnight instead of inventing zero planned work.
            row = self.connection.execute(
                "SELECT id FROM plan_snapshots WHERE account_id=? AND generated_at<? "
                "AND horizon_start<? AND horizon_end>? ORDER BY generated_at DESC,id DESC LIMIT 1",
                (account_id, _iso(start), _iso(end), _iso(start)),
            ).fetchone()
        return None if row is None else row["id"]

    def _planned_minutes(self, account_id: str, start: datetime, end: datetime, zone: ZoneInfo) -> int:
        total = 0
        local = start.astimezone(zone).date()
        last = (end - timedelta(microseconds=1)).astimezone(zone).date()
        while local <= last:
            day_start = datetime.combine(local, time.min, tzinfo=zone).astimezone(timezone.utc)
            day_end = datetime.combine(local + timedelta(days=1), time.min, tzinfo=zone).astimezone(timezone.utc)
            lower, upper = max(start, day_start), min(end, day_end)
            plan_id = self._baseline_plan_for_day(account_id, lower, upper)
            if plan_id is not None:
                rows = self.connection.execute(
                    "SELECT starts_at,ends_at FROM plan_blocks WHERE plan_id=? AND block_type='WORK'",
                    (plan_id,),
                ).fetchall()
                total += sum(_clip_seconds(_dt(r["starts_at"]), _dt(r["ends_at"]), lower, upper) for r in rows)
            local += timedelta(days=1)
        return total // 60

    def _schedule_churn(self, account_id: str, start: datetime, end: datetime) -> int:
        rows = self.connection.execute(
            "SELECT id FROM plan_snapshots WHERE account_id=? AND generated_at>=? AND generated_at<? "
            "ORDER BY generated_at,id",
            (account_id, _iso(start), _iso(end)),
        ).fetchall()
        previous: dict[str, tuple[tuple[str, str], ...]] | None = None
        churn = 0
        for row in rows:
            blocks = self.connection.execute(
                "SELECT obligation_id,starts_at,ends_at FROM plan_blocks "
                "WHERE plan_id=? AND block_type='WORK' ORDER BY obligation_id,starts_at,ends_at",
                (row["id"],),
            ).fetchall()
            current: dict[str, list[tuple[str, str]]] = {}
            for block in blocks:
                current.setdefault(block["obligation_id"], []).append((block["starts_at"], block["ends_at"]))
            frozen = {key: tuple(value) for key, value in current.items()}
            if previous is not None:
                churn += sum(1 for task_id in set(previous) | set(frozen) if previous.get(task_id) != frozen.get(task_id))
            previous = frozen
        return churn

    def _carry_over(self, account_id: str, start: datetime, end: datetime) -> int:
        planned_ids = {
            row["obligation_id"] for row in self.connection.execute(
                "SELECT DISTINCT b.obligation_id FROM plan_blocks b JOIN plan_snapshots p ON p.id=b.plan_id "
                "WHERE p.account_id=? AND b.block_type='WORK' AND b.starts_at<? AND b.ends_at>?",
                (account_id, _iso(end), _iso(start)),
            ).fetchall()
        }
        if not planned_ids:
            return 0
        placeholders = ",".join("?" for _ in planned_ids)
        row = self.connection.execute(
            f"SELECT count(*) FROM obligations WHERE account_id=? AND id IN ({placeholders}) "
            "AND lifecycle_status IN ('ACTIVE','DRAFT')",
            (account_id, *sorted(planned_ids)),
        ).fetchone()
        return int(row[0])

    def _calibration_stats(self, account_id: str, now: datetime) -> list[dict[str, Any]]:
        lower = now - timedelta(days=self.CALIBRATION_WINDOW_DAYS)
        rows = self.connection.execute(
            "SELECT o.id,o.category,o.completed_at,"
            "(SELECT estimated_total_effort_at_start FROM execution_sessions s "
            " WHERE s.account_id=o.account_id AND s.task_id=o.id "
            " AND s.estimated_total_effort_at_start IS NOT NULL ORDER BY s.started_at LIMIT 1) AS estimate "
            "FROM obligations o WHERE o.account_id=? AND o.kind='TASK' AND o.completed_at>=? AND o.completed_at<=?",
            (account_id, _iso(lower), _iso(now)),
        ).fetchall()
        by_category: dict[str, list[float]] = {}
        for row in rows:
            estimate = row["estimate"]
            if estimate is None or int(estimate) <= 0:
                continue
            seconds = self.connection.execute(
                "SELECT COALESCE(SUM(CAST((julianday(COALESCE(sg.ended_at,?))-julianday(sg.started_at))*86400 AS INTEGER)),0) "
                "FROM execution_segments sg JOIN execution_sessions es "
                "ON es.account_id=sg.account_id AND es.id=sg.session_id "
                "WHERE es.account_id=? AND es.task_id=?",
                (_iso(now), account_id, row["id"]),
            ).fetchone()[0]
            actual_minutes = max(0.0, float(seconds) / 60.0)
            if actual_minutes <= 0:
                continue
            by_category.setdefault(row["category"], []).append(actual_minutes / float(estimate))
        prefs = self.calibration_preferences(account_id)
        result = []
        for category, ratios in sorted(by_category.items()):
            median_ratio = statistics.median(ratios)
            mean_ratio = statistics.fmean(ratios)
            suggested = max(1.0, min(3.0, round(median_ratio * 20) / 20))
            pref = prefs.get(category)
            result.append({
                "category": category,
                "sample_size": len(ratios),
                "median_ratio": round(median_ratio, 3),
                "mean_ratio": round(mean_ratio, 3),
                "median_percent_difference": round((median_ratio - 1.0) * 100),
                "suggested_multiplier": suggested if len(ratios) >= self.MIN_CALIBRATION_SAMPLE and not (pref and pref["suppress_suggestion"]) else None,
                "preference": pref,
            })
        return result

    def review(self, account_id: str, start: datetime, end: datetime) -> dict[str, Any]:
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValidationError("review interval must be a non-empty aware interval")
        zone_name = self.timezone_name(account_id)
        zone = ZoneInfo(zone_name)
        completed = int(self.connection.execute(
            "SELECT count(*) FROM obligations WHERE account_id=? AND kind='TASK' AND completed_at>=? AND completed_at<?",
            (account_id, _iso(start), _iso(end)),
        ).fetchone()[0])
        actual_minutes = self._actual_seconds(account_id, start, end) // 60
        planned_minutes = self._planned_minutes(account_id, start, end, zone)
        calibration = self._calibration_stats(account_id, min(end, self.clock.now()))
        worst = max(calibration, key=lambda item: item["median_ratio"], default=None)
        return {
            "start": _iso(start),
            "end": _iso(end),
            "timezone_name": zone_name,
            "planned_work_minutes": planned_minutes,
            "actual_work_minutes": actual_minutes,
            "variance_minutes": actual_minutes - planned_minutes,
            "completed_tasks": completed,
            "schedule_churn": self._schedule_churn(account_id, start, end),
            "carry_over_count": self._carry_over(account_id, start, end),
            "calibration_window_days": self.CALIBRATION_WINDOW_DAYS,
            "calibration": calibration,
            "most_underestimated": worst if worst and worst["median_ratio"] > 1.0 else None,
        }
