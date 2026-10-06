"""The shared recurrence expansion (weekday rules, COUNT/UNTIL, split boundaries)."""
from __future__ import annotations

import itertools
import unittest
from datetime import datetime

from student_execution_os.domain.errors import ValidationError
from student_execution_os.recurrence import (
    RecurrenceRule,
    contains_original,
    iter_original_locals,
    remaining_count,
)


def take(iterator, n):
    return [value.isoformat() for value in itertools.islice(iterator, n)]


class RecurrenceExpansionTests(unittest.TestCase):
    def test_weekdays_start_at_dtstart_and_skip_weekends(self):
        rule = RecurrenceRule.parse("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", allow_weekdays=True)
        self.assertEqual(rule.by_weekdays, (0, 1, 2, 3, 4))
        self.assertEqual(take(iter_original_locals(datetime(2026, 10, 8, 8, 0), rule), 4), [
            "2026-10-08T08:00:00", "2026-10-09T08:00:00", "2026-10-12T08:00:00", "2026-10-13T08:00:00"])

    def test_biweekly_weekdays_and_skip_ahead_agree_with_full_scan(self):
        rule = RecurrenceRule.parse("FREQ=WEEKLY;INTERVAL=2;BYDAY=SU,WE", allow_weekdays=True)
        start = datetime(2026, 10, 7, 18, 0)
        full = list(itertools.islice(iter_original_locals(start, rule), 40))
        probe = datetime(2026, 12, 1)
        jumped = list(itertools.islice(iter_original_locals(start, rule, start_local=probe), 5))
        self.assertEqual(jumped, [value for value in full if value >= probe][:5])
        self.assertTrue(all(value.weekday() in (2, 6) for value in full))

    def test_count_until_and_series_end(self):
        rule = RecurrenceRule.parse("FREQ=WEEKLY;BYDAY=MO,FR;COUNT=3", allow_weekdays=True)
        start = datetime(2026, 10, 6, 9, 0)  # Tuesday: the first week has only Friday
        self.assertEqual(take(iter_original_locals(start, rule), 10),
                         ["2026-10-09T09:00:00", "2026-10-12T09:00:00", "2026-10-16T09:00:00"])
        self.assertEqual(remaining_count(start, rule, datetime(2026, 10, 12, 9, 0)), 2)
        with self.assertRaises(ValidationError):
            remaining_count(start, rule, datetime(2026, 10, 20, 9, 0))
        daily = RecurrenceRule.parse("FREQ=DAILY;UNTIL=2026-10-08T09:00:00")
        self.assertEqual(len(list(iter_original_locals(datetime(2026, 10, 6, 9, 0), daily))), 3)
        self.assertEqual(len(list(iter_original_locals(datetime(2026, 10, 6, 9, 0), RecurrenceRule.parse("FREQ=DAILY"),
                                                       series_end_before=datetime(2026, 10, 9)))), 3)

    def test_contains_original_is_exact(self):
        rule = RecurrenceRule.parse("FREQ=WEEKLY;BYDAY=TU,TH", allow_weekdays=True)
        start = datetime(2026, 10, 6, 7, 30)
        self.assertTrue(contains_original(start, rule, datetime(2026, 10, 8, 7, 30)))
        self.assertFalse(contains_original(start, rule, datetime(2026, 10, 8, 7, 31)))
        self.assertFalse(contains_original(start, rule, datetime(2026, 10, 7, 7, 30)))
        self.assertFalse(contains_original(start, rule, datetime(2026, 9, 29, 7, 30)))

    def test_older_owners_keep_rejecting_weekday_rules(self):
        with self.assertRaisesRegex(ValidationError, "BYDAY"):
            RecurrenceRule.parse("FREQ=WEEKLY;BYDAY=MO")
        with self.assertRaises(ValidationError):
            RecurrenceRule.parse("FREQ=DAILY;BYDAY=MO", allow_weekdays=True)
        with self.assertRaises(ValidationError):
            RecurrenceRule.parse("FREQ=WEEKLY;BYDAY=MO,MO", allow_weekdays=True)
        self.assertEqual(RecurrenceRule.parse("FREQ=WEEKLY;BYDAY=FR,MO", allow_weekdays=True).canonical(),
                         "FREQ=WEEKLY;BYDAY=MO,FR")


if __name__ == "__main__":
    unittest.main()
