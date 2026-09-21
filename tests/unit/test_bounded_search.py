from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from student_execution_os.domain.model import CutoffBoundary, HalfOpenInterval
from student_execution_os.planning.search import BoundedSearch, SearchState


class BoundedSearchBudgetTests(unittest.TestCase):
    def test_candidate_scan_observes_deadline_before_first_yield(self):
        start = datetime(2026, 9, 21, 9, 0, tzinfo=timezone.utc)
        end = start + timedelta(days=30)
        task = SimpleNamespace(
            actual_cutoff=SimpleNamespace(at=end),
            obligation=SimpleNamespace(id="task"),
        )
        state = SearchState()
        occupied = [HalfOpenInterval(start, end)]

        with patch("student_execution_os.planning.search.monotonic", side_effect=[0.0, 2.0]):
            placements = list(
                BoundedSearch.candidate_blocks(
                    task,
                    1,
                    start,
                    end,
                    CutoffBoundary.INCLUSIVE,
                    occupied,
                    deadline_clock=1.0,
                    state=state,
                )
            )

        self.assertEqual(placements, [])
        self.assertTrue(state.timed_out)


if __name__ == "__main__":
    unittest.main()
