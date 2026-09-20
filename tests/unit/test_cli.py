import io
import json
import unittest
from contextlib import redirect_stdout

from student_execution_os.application.cli import health_payload, main


class CliUnitTests(unittest.TestCase):
    def test_health_payload_is_stable_and_machine_readable(self) -> None:
        payload = health_payload()
        self.assertEqual(payload["service"], "student-execution-os")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["api_version"], "0")
        self.assertTrue(payload["version"])

    def test_version_command_succeeds(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = main(["version"])
        self.assertEqual(result, 0)
        self.assertTrue(stdout.getvalue().strip())

    def test_health_command_emits_json(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = main(["health"])
        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload, health_payload())


if __name__ == "__main__":
    unittest.main()
