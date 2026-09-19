import json
import subprocess
import sys
import unittest


class CliSmokeTests(unittest.TestCase):
    def test_module_entrypoint_health(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "student_execution_os", "health"],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["service"], "student-execution-os")
        self.assertEqual(payload["status"], "ok")


if __name__ == "__main__":
    unittest.main()
