"""Credentials must not be committable: ignored by Git and absent from tracked files."""
from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Actual key material: a PEM private-key header followed by a base64 body, either raw
# or JSON-escaped (as in a service-account file). Mentions of the words don't match.
PRIVATE_KEY = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----(?:\\n|\s)+[A-Za-z0-9+/=]{64}")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)


@unittest.skipUnless((ROOT / ".git").exists(), "not a git checkout")
class RepositoryHygieneTest(unittest.TestCase):
    def test_secret_file_names_are_ignored(self):
        for name in ("deadlines-os-firebase-adminsdk-fbsvc-00e9e6be07.json", "prod-firebase-adminsdk-x.json",
                     "fcm-service-account.json", "deploy/secrets/api/credential.key", "deploy/.env",
                     "mobile/android/app/google-services.json", "student-execution-os.db"):
            self.assertEqual(_git("check-ignore", "-q", "--no-index", name).returncode, 0, name)

    def test_no_tracked_file_contains_a_private_key(self):
        files = _git("ls-files", "-z").stdout.split(b"\0")
        for name in filter(None, files):
            path = ROOT / name.decode()
            if not path.is_file() or path.stat().st_size > 5_000_000:
                continue
            self.assertIsNone(PRIVATE_KEY.search(path.read_bytes()), f"{name.decode()} contains a private key")

    def test_detector_finds_a_service_account_key(self):
        sample = b'{"type": "service_account", "private_key": "-----BEGIN PRIVATE KEY-----\\n' + b"A" * 80 + b'\\n"}'
        self.assertIsNotNone(PRIVATE_KEY.search(sample))
        self.assertIsNotNone(PRIVATE_KEY.search(b"-----BEGIN RSA PRIVATE KEY-----\n" + b"B" * 64))
        self.assertIsNone(PRIVATE_KEY.search(b'assertNotIn("PRIVATE KEY", log)'))
        local = ROOT / "deadlines-os-firebase-adminsdk-fbsvc-00e9e6be07.json"
        if local.exists():  # the real, git-ignored credential must be recognised too
            self.assertIsNotNone(PRIVATE_KEY.search(local.read_bytes()))


if __name__ == "__main__":
    unittest.main()
