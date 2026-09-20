import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest

from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.planning import SQLitePlanStore


class Pass3CliIntegrationTests(unittest.TestCase):
    def run_cli(self, *args):
        completed = subprocess.run(
            [sys.executable, "-m", "student_execution_os", *args],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": "src"},
        )
        return json.loads(completed.stdout)

    def test_manual_capture_plan_complete_replan_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "student.sqlite3")
            self.run_cli("account-init", "--database", db, "--account", "a")
            self.run_cli(
                "task-add",
                "--database", db,
                "--account", "a",
                "--id", "t",
                "--title", "Homework",
                "--minutes", "60",
                "--low-minutes", "45",
                "--high-minutes", "75",
                "--cutoff", "2026-09-20T14:00:00+00:00",
                "--target", "2026-09-20T12:00:00+00:00",
                "--actionable", "2026-09-20T09:00:00+00:00",
                "--splittable",
                "--min-chunk", "30",
                "--max-chunk", "60",
            )
            self.run_cli(
                "event-add",
                "--database", db,
                "--account", "a",
                "--id", "lecture",
                "--title", "Lecture",
                "--starts", "2026-09-20T11:00:00+00:00",
                "--ends", "2026-09-20T12:00:00+00:00",
            )
            first = self.run_cli(
                "plan",
                "--database", db,
                "--account", "a",
                "--now", "2026-09-20T09:00:00+00:00",
                "--analysis-end", "2026-09-20T14:00:00+00:00",
            )
            self.assertEqual(first["feasibility"], "FEASIBLE")
            self.assertTrue(first["next_actions"])
            self.assertTrue(any(b["type"] == "WORK" and b["obligation_id"] == "t" for b in first["blocks"]))
            self.assertTrue(any(b["type"] == "EVENT_PROJECTION" and b["obligation_id"] == "lecture" for b in first["blocks"]))

            self.run_cli("task-complete", "--database", db, "--account", "a", "--id", "t")
            second = self.run_cli(
                "plan",
                "--database", db,
                "--account", "a",
                "--now", "2026-09-20T09:30:00+00:00",
                "--analysis-end", "2026-09-20T14:00:00+00:00",
            )
            self.assertFalse(any(b["type"] == "WORK" and b["obligation_id"] == "t" for b in second["blocks"]))
            self.assertNotEqual(first["plan_id"], second["plan_id"])

            with SQLiteCanonicalRepository(db) as repo:
                repo.initialize()
                self.assertEqual(len(SQLitePlanStore(repo).history_ids("a")), 2)


if __name__ == "__main__":
    unittest.main()
