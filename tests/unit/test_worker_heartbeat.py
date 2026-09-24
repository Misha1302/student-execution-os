from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from student_execution_os.persistence import SQLiteCanonicalRepository
from student_execution_os.reminders.worker import heartbeat, heartbeat_is_fresh

ROOT = Path(__file__).resolve().parents[2]


class WorkerHeartbeatTests(unittest.TestCase):
    def test_health_check_follows_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "hb.sqlite")
            self.assertFalse(heartbeat_is_fresh(db))  # missing file: unhealthy, not created
            self.assertFalse(Path(db).exists())
            with SQLiteCanonicalRepository(db) as repo:
                repo.initialize()
            self.assertFalse(heartbeat_is_fresh(db))
            heartbeat(db, {"push_configured": False})
            self.assertTrue(heartbeat_is_fresh(db))
            result = subprocess.run(
                [sys.executable, "-m", "student_execution_os.reminders.worker", "--database", db, "--check-heartbeat"],
                env={"PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__": unittest.main()
