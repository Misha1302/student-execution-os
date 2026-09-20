import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest


class DomainCliSmokeTests(unittest.TestCase):
    def test_domain_smoke_uses_real_sqlite_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "smoke.sqlite3"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "student_execution_os",
                    "domain-smoke",
                    "--database",
                    str(db),
                ],
                check=True,
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": "src"},
            )
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["server_revision"], 2)
            self.assertEqual(payload["task_version"], 2)
            self.assertEqual(payload["cutoff_state"], "KNOWN")
            self.assertTrue(db.exists())


if __name__ == "__main__":
    unittest.main()
