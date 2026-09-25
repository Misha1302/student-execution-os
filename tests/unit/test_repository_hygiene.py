"""Credentials must not be committable: ignored by Git and absent from tracked files."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET_MARKERS = (b"-----BEGIN PRIVATE KEY-----", b"-----BEGIN RSA PRIVATE KEY-----", b'"type": "service_account"')


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
            if not path.is_file() or path.stat().st_size > 5_000_000 or path.name == Path(__file__).name:
                continue
            content = path.read_bytes()
            for marker in SECRET_MARKERS:
                self.assertNotIn(marker, content, f"{name.decode()} looks like it contains a credential")


if __name__ == "__main__":
    unittest.main()
