from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from student_execution_os.agent.semantic import TemporalPrecision, resolve_temporal_transform
from student_execution_os.domain.errors import ValidationError


class AssistantSemanticTest(unittest.TestCase):
    def test_conditional_shift_uses_approximate_next_morning_fallback(self):
        resolved = resolve_temporal_transform({
            "kind": "SHIFT_WITH_GUARD_AND_FALLBACK",
            "delta_minutes": 180,
            "guard": {"not_after_local_time": "22:00"},
            "fallback": {
                "relative_day": "NEXT_MORNING",
                "preferred_local_time": "10:00",
                "precision": "APPROXIMATE",
            },
        }, current=datetime(2026, 10, 1, 20, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            timezone_name="Europe/Moscow")
        self.assertEqual(resolved.when.isoformat(), "2026-10-02T10:00:00+03:00")
        self.assertEqual(resolved.precision, TemporalPrecision.APPROXIMATE)
        self.assertEqual(resolved.reason, "GUARD_TRIGGERED_FALLBACK")

    def test_conditional_shift_keeps_valid_candidate_exact(self):
        resolved = resolve_temporal_transform({
            "kind": "SHIFT_WITH_GUARD_AND_FALLBACK", "delta_minutes": 60,
            "guard": {"not_after_local_time": "22:00"},
            "fallback": {"relative_day": "NEXT_MORNING", "preferred_local_time": "10:00", "precision": "APPROXIMATE"},
        }, current=datetime(2026, 10, 1, 20, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            timezone_name="Europe/Moscow")
        self.assertEqual(resolved.when.isoformat(), "2026-10-01T21:00:00+03:00")
        self.assertEqual(resolved.precision, TemporalPrecision.EXACT)

    def test_transform_schema_fails_closed(self):
        with self.assertRaises(ValidationError):
            resolve_temporal_transform({
                "kind": "SHIFT_WITH_GUARD_AND_FALLBACK", "delta_minutes": 180,
                "guard": {"not_after_local_time": "22:00", "sql": "drop table events"},
                "fallback": {"relative_day": "NEXT_MORNING", "preferred_local_time": "10:00", "precision": "APPROXIMATE"},
            }, current=datetime.now(ZoneInfo("UTC")), timezone_name="UTC")


if __name__ == "__main__":
    unittest.main()
