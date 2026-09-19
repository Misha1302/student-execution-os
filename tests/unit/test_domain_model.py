from datetime import datetime, timezone
import unittest

from student_execution_os.domain.errors import ValidationError
from student_execution_os.domain.model import (
    CutoffBoundary,
    CutoffState,
    HalfOpenInterval,
    HardCutoff,
    TemporalPrecision,
)

UTC = timezone.utc


class DomainModelTests(unittest.TestCase):
    def test_half_open_adjacent_intervals_do_not_overlap(self) -> None:
        first = HalfOpenInterval(
            datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
            datetime(2026, 9, 20, 15, 0, tzinfo=UTC),
        )
        second = HalfOpenInterval(
            datetime(2026, 9, 20, 15, 0, tzinfo=UTC),
            datetime(2026, 9, 20, 16, 0, tzinfo=UTC),
        )
        self.assertFalse(first.overlaps(second))
        self.assertFalse(second.overlaps(first))

    def test_half_open_real_overlap_is_detected(self) -> None:
        first = HalfOpenInterval(
            datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
            datetime(2026, 9, 20, 15, 0, tzinfo=UTC),
        )
        second = HalfOpenInterval(
            datetime(2026, 9, 20, 14, 59, tzinfo=UTC),
            datetime(2026, 9, 20, 16, 0, tzinfo=UTC),
        )
        self.assertTrue(first.overlaps(second))

    def test_known_cutoff_retains_boundary_semantics(self) -> None:
        cutoff = HardCutoff.known(
            datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
            CutoffBoundary.EXCLUSIVE,
        )
        self.assertEqual(cutoff.state, CutoffState.KNOWN)
        self.assertEqual(cutoff.boundary, CutoffBoundary.EXCLUSIVE)
        self.assertEqual(cutoff.precision, TemporalPrecision.EXACT_INSTANT)

    def test_unknown_date_only_cutoff_cannot_be_coerced_to_exact_instant(self) -> None:
        cutoff = HardCutoff.unknown(TemporalPrecision.DATE_ONLY)
        self.assertEqual(cutoff.state, CutoffState.UNKNOWN)
        self.assertIsNone(cutoff.at)
        self.assertEqual(cutoff.precision, TemporalPrecision.DATE_ONLY)
        with self.assertRaises(ValidationError):
            HardCutoff(
                CutoffState.UNKNOWN,
                datetime(2026, 9, 21, 23, 59, tzinfo=UTC),
                CutoffBoundary.INCLUSIVE,
                TemporalPrecision.DATE_ONLY,
            )

    def test_naive_datetime_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            HalfOpenInterval(datetime(2026, 1, 1, 10), datetime(2026, 1, 1, 11))


if __name__ == "__main__":
    unittest.main()
