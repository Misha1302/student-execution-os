"""Soft planning preferences: canonical, explained, and never a feasibility authority."""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from student_execution_os.domain.clock import FrozenClock
from student_execution_os.domain.errors import EntityNotFound, ValidationError, VersionConflict
from student_execution_os.domain.model import (
    ActorCategory,
    HardCutoff,
    Importance,
    ObligationCategory,
    UserTimeConstraintType,
)
from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import SQLitePlanningStateSource, build_planning_snapshot
from student_execution_os.planning.model import FeasibilityStatus
from student_execution_os.planning.outlook import SQLitePlanningProfileRepository, off_hours_constraints
from student_execution_os.planning.planner import Planner
from student_execution_os.planning.preference_store import (
    SQLitePlanningPreferenceRepository,
    derived_preference_windows,
)
from student_execution_os.planning.preferences import (
    LIGHT_DAY_WORK_MINUTES,
    MAX_ACTIVE_PREFERENCES,
    preference_from_payload,
)
from student_execution_os.sync.commands import Commands

UTC = timezone.utc
DAY1 = date(2026, 10, 5)  # Monday; the default profile is UTC with planning windows 08:00-22:00
DAY2 = DAY1 + timedelta(days=1)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


class PlanningPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.now = at(DAY1, 8)
        self.repo = SQLiteCanonicalRepository(":memory:", clock=FrozenClock(self.now))
        self.repo.initialize()
        self.repo.create_account("a")
        self.repo.create_account("b")
        self.prefs = SQLitePlanningPreferenceRepository(self.repo)
        self.counter = 0

    def tearDown(self):
        self.repo.close()

    # -- fixtures ---------------------------------------------------------------------------
    def task(self, tid, minutes, cutoff, *, category=ObligationCategory.GENERAL, importance=Importance.NORMAL,
             splittable=False):
        return self.repo.create_task(
            account_id="a", obligation_id=tid, title=tid, category=category, importance=importance,
            estimated_total_effort_minutes=minutes, remaining_effort_minutes=minutes, splittable=splittable,
            min_chunk_minutes=None, max_chunk_minutes=None, actionable_from=None, target_at=None,
            actual_cutoff=HardCutoff.known(cutoff), actor=ActorCategory.SYSTEM,
        )

    def event(self, eid, starts, ends, category=ObligationCategory.GENERAL):
        self.repo.create_fixed_event(account_id="a", obligation_id=eid, title=eid, starts_at=starts, ends_at=ends,
                                     category=category, actor=ActorCategory.SYSTEM)

    def prefer(self, account="a", **payload):
        self.counter += 1
        payload.setdefault("date_from", DAY1.isoformat())
        preference = preference_from_payload(f"preference-{self.counter:04d}", account, payload)
        return self.prefs.create(preference, today=DAY1, actor=ActorCategory.USER_UI)

    def plan(self, start, end, *, account="a", preferences=True):
        profile = SQLitePlanningProfileRepository(self.repo).get(account)
        snapshot = build_planning_snapshot(
            SQLitePlanningStateSource(self.repo), account_id=account,
            analysis_horizon_start=start, analysis_horizon_end=end,
            derived_constraints=lambda s, e: off_hours_constraints(profile, account, s, e),
            derived_preferences=derived_preference_windows(self.repo, profile, account) if preferences else None,
        )
        return snapshot, Planner().plan(snapshot)

    @staticmethod
    def work(plan, task_id=None):
        return sorted((b.starts_at, b.ends_at) for b in plan.blocks
                      if b.type.value == "WORK" and (task_id is None or b.obligation_id == task_id))

    @staticmethod
    def notes(plan, prefix):
        return [item.split(":", 1)[1] for item in plan.explanations if item.startswith(prefix)]

    # -- phrase scenarios -------------------------------------------------------------------
    def test_finish_study_by_nine_moves_study_but_not_other_work(self):
        # «Учёбу желательно закончить до девяти.»
        self.task("essay", 30, at(DAY2, 12), category=ObligationCategory.HOMEWORK)
        self.task("laundry", 30, at(DAY2, 12), category=ObligationCategory.PERSONAL)
        pref = self.prefer(kind="AVOID_WORK", target="STUDY", window_start="21:00", date_until=None)
        _, plan = self.plan(at(DAY1, 21), at(DAY2, 12))
        self.assertEqual(plan.feasibility_status, FeasibilityStatus.FEASIBLE)
        self.assertEqual(self.work(plan, "essay"), [(at(DAY2, 8), at(DAY2, 8, 30))])
        self.assertEqual(self.work(plan, "laundry"), [(at(DAY1, 21), at(DAY1, 21, 30))])
        self.assertEqual(self.notes(plan, "PREFERENCE_APPLIED"), [pref.id])

    def test_preference_yields_to_a_deadline_and_says_so(self):
        self.task("essay", 30, at(DAY1, 21, 40), category=ObligationCategory.HOMEWORK)
        pref = self.prefer(kind="AVOID_WORK", target="STUDY", window_start="21:00")
        _, plan = self.plan(at(DAY1, 21), at(DAY2, 12))
        self.assertEqual(plan.feasibility_status, FeasibilityStatus.FEASIBLE)
        self.assertEqual(self.work(plan, "essay"), [(at(DAY1, 21), at(DAY1, 21, 30))])
        self.assertEqual(self.notes(plan, "PREFERENCE_RELAXED"), [pref.id])

    def test_keep_an_evening_hour_free(self):
        # «Оставь вечером хотя бы час свободным.»
        for index in range(4):
            self.task(f"t{index}", 60, at(DAY2, 12))
        _, without = self.plan(at(DAY1, 18), at(DAY2, 12), preferences=False)
        evening = [w for w in self.work(without) if w[0] < at(DAY1, 22)]
        self.assertEqual(len(evening), 4)  # the plain planner fills 18:00-22:00
        pref = self.prefer(kind="KEEP_FREE", window_start="18:00", window_end="23:00", minutes=60, date_until=DAY1.isoformat())
        _, plan = self.plan(at(DAY1, 18), at(DAY2, 12))
        evening = [w for w in self.work(plan) if w[0] < at(DAY1, 22)]
        self.assertEqual(len(evening), 3)
        self.assertEqual(self.notes(plan, "PREFERENCE_APPLIED"), [pref.id])

    def test_keep_free_that_events_already_break_is_reported_unsatisfiable(self):
        self.event("party", at(DAY1, 18), at(DAY1, 22))
        self.task("t", 30, at(DAY2, 12))
        pref = self.prefer(kind="KEEP_FREE", window_start="18:00", window_end="22:00", minutes=60)
        _, plan = self.plan(at(DAY1, 18), at(DAY2, 12))
        self.assertEqual(plan.feasibility_status, FeasibilityStatus.FEASIBLE)
        self.assertEqual(self.notes(plan, "PREFERENCE_UNSATISFIABLE"), [pref.id])

    def test_lighter_day_caps_planned_work(self):
        # «Завтра сделай день полегче.»
        for index in range(4):
            self.task(f"t{index}", 60, at(DAY2 + timedelta(days=1), 22))
        payload = {"kind": "WORK_LIMIT", "minutes": LIGHT_DAY_WORK_MINUTES - 60,
                   "date_from": DAY1.isoformat(), "date_until": DAY1.isoformat()}
        self.prefer(**payload)
        _, plan = self.plan(at(DAY1, 8), at(DAY2 + timedelta(days=1), 22))
        day1 = sum((end - start).total_seconds() / 60 for start, end in self.work(plan) if start < at(DAY2, 0))
        self.assertLessEqual(day1, 120)
        self.assertEqual(len(self.work(plan)), 4)

    def test_no_demanding_work_right_after_waking(self):
        # «Не ставь сложное сразу после подъёма.» — the day starts at the profile's first window.
        self.task("exam-prep", 60, at(DAY1, 20), importance=Importance.HIGH)
        self.task("email", 15, at(DAY1, 20))
        self.prefer(kind="AVOID_WORK", anchor="WAKE", target="DEMANDING", minutes=90)
        _, plan = self.plan(at(DAY1, 8), at(DAY1, 20))
        self.assertGreaterEqual(self.work(plan, "exam-prep")[0][0], at(DAY1, 9, 30))
        self.assertEqual(self.work(plan, "email")[0][0], at(DAY1, 8))

    def test_rest_after_classes_only(self):
        # «После пары дай мне полчаса передохнуть.»
        self.event("lecture", at(DAY1, 8), at(DAY1, 9, 30), ObligationCategory.LESSON)
        self.event("meeting", at(DAY1, 11), at(DAY1, 12), ObligationCategory.MEETING)
        self.task("a", 60, at(DAY1, 20))
        self.task("b", 30, at(DAY1, 20))
        self.prefer(kind="REST_AFTER_EVENTS", minutes=30)
        _, plan = self.plan(at(DAY1, 8), at(DAY1, 20))
        self.assertEqual(self.work(plan, "a")[0][0], at(DAY1, 10))      # 9:30-10:00 rest after the class
        self.assertEqual(self.work(plan, "b")[0][0], at(DAY1, 12))      # no rest after a meeting

    # -- soundness --------------------------------------------------------------------------
    def test_preferences_never_change_feasibility(self):
        self.task("too-big", 600, at(DAY1, 12))
        self.prefer(kind="AVOID_WORK", window_start="08:00")
        _, plan = self.plan(at(DAY1, 8), at(DAY1, 12))
        self.assertEqual(plan.feasibility_status, FeasibilityStatus.INFEASIBLE)
        self.assertEqual(self.notes(plan, "PREFERENCE_"), [])
        self.assertEqual(self.work(plan), [])

    def test_unknown_stays_unknown(self):
        self.repo.create_task(
            account_id="a", obligation_id="u", title="u", category=ObligationCategory.GENERAL,
            importance=Importance.NORMAL, estimated_total_effort_minutes=30, remaining_effort_minutes=30,
            splittable=False, min_chunk_minutes=None, max_chunk_minutes=None, actionable_from=None,
            target_at=None, actual_cutoff=HardCutoff.unknown(), actor=ActorCategory.SYSTEM,
        )
        self.prefer(kind="WORK_LIMIT", minutes=60)
        _, plan = self.plan(at(DAY1, 8), at(DAY1, 20))
        self.assertEqual(plan.feasibility_status, FeasibilityStatus.UNKNOWN)

    def test_preferred_witness_stays_inside_hard_constraints(self):
        self.repo.create_time_constraint(account_id="a", type=UserTimeConstraintType.UNAVAILABLE,
                                         starts_at=at(DAY1, 10), ends_at=at(DAY1, 12),
                                         actor=ActorCategory.SYSTEM, constraint_id="busy-0001")
        self.task("t", 60, at(DAY1, 20), splittable=True)
        self.prefer(kind="AVOID_WORK", window_start="08:00", window_end="10:00")
        _, plan = self.plan(at(DAY1, 8), at(DAY1, 20))
        for start, end in self.work(plan):
            self.assertTrue(end <= at(DAY1, 10) or start >= at(DAY1, 12))
            self.assertGreaterEqual(start, at(DAY1, 10))

    def test_plan_hash_changes_only_with_preferences(self):
        self.task("t", 30, at(DAY1, 20))
        plain, _ = self.plan(at(DAY1, 8), at(DAY1, 20), preferences=False)
        empty, _ = self.plan(at(DAY1, 8), at(DAY1, 20))
        self.assertEqual(plain.input_hash, empty.input_hash)
        self.prefer(kind="WORK_LIMIT", minutes=60)
        with_pref, _ = self.plan(at(DAY1, 8), at(DAY1, 20))
        self.assertNotEqual(plain.input_hash, with_pref.input_hash)

    # -- canonical ownership ----------------------------------------------------------------
    def test_validation_rejects_malformed_preferences(self):
        bad = [
            {"kind": "KEEP_FREE", "window_start": "18:00", "window_end": "18:30", "minutes": 60},
            {"kind": "KEEP_FREE", "minutes": 60},
            {"kind": "WORK_LIMIT", "minutes": 60, "window_start": "08:00"},
            {"kind": "AVOID_WORK", "anchor": "WAKE", "minutes": 60, "window_start": "08:00"},
            {"kind": "AVOID_WORK", "target": "CLASSES", "window_start": "08:00"},
            {"kind": "REST_AFTER_EVENTS", "minutes": 500},
            {"kind": "AVOID_WORK", "window_start": "25:00"},
            {"kind": "AVOID_WORK", "window_start": "08:00", "planner_prompt": "be nice"},
            {"kind": "WORK_LIMIT", "minutes": 60, "date_until": "2026-10-01"},
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                payload = {"date_from": DAY1.isoformat(), **payload}
                self.prefs.create(preference_from_payload("preference-bad1", "a", payload), today=DAY1,
                                  actor=ActorCategory.USER_UI)

    def test_active_preferences_are_bounded(self):
        for _ in range(MAX_ACTIVE_PREFERENCES):
            self.prefer(kind="WORK_LIMIT", minutes=60)
        with self.assertRaises(ValidationError):
            self.prefer(kind="WORK_LIMIT", minutes=60)

    def test_sync_operations_are_idempotent_versioned_and_account_isolated(self):
        commands = Commands(self.repo, account_id="a", actor=ActorCategory.USER_UI, now=self.now)
        payload = {"kind": "KEEP_FREE", "window_start": "18:00", "window_end": "23:00", "minutes": 60,
                   "date_from": DAY1.isoformat()}
        first = commands.run("preference.create", "preference-sync-1", payload)
        again = commands.run("preference.create", "preference-sync-1", payload)
        self.assertEqual(first.status, "APPLIED")
        self.assertEqual((again.status, again.code), ("NOOP", "ALREADY_EXISTS"))
        other = Commands(self.repo, account_id="b", actor=ActorCategory.USER_UI, now=self.now)
        with self.assertRaises(ValidationError):
            other.run("preference.create", "preference-sync-1", payload)
        with self.assertRaises(EntityNotFound):
            other.run("preference.delete", "preference-sync-1", {})
        self.assertEqual(self.prefs.list("b"), [])
        with self.assertRaises(VersionConflict):
            commands.run("preference.delete", "preference-sync-1", {"expected_version": 7})
        deleted = commands.run("preference.delete", "preference-sync-1", {"expected_version": 1})
        self.assertEqual(deleted.status, "APPLIED")
        self.assertEqual(self.prefs.list("a"), [])

    def test_other_accounts_preferences_never_shape_a_plan(self):
        self.task("essay", 30, at(DAY2, 12), category=ObligationCategory.HOMEWORK)
        self.prefer(account="b", kind="AVOID_WORK", target="STUDY", window_start="21:00")
        _, plan = self.plan(at(DAY1, 21), at(DAY2, 12))
        self.assertEqual(self.work(plan, "essay"), [(at(DAY1, 21), at(DAY1, 21, 30))])
        self.assertEqual(self.notes(plan, "PREFERENCE_"), [])


if __name__ == "__main__":
    unittest.main()
