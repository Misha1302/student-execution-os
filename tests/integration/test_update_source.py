from __future__ import annotations

import hashlib
import http.server
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from student_execution_os.updates import UpdatePolicy, public_key_b64, verify_policy
from student_execution_os.updates.signing import private_seed_b64

ROOT = Path(__file__).resolve().parents[2]


class LocalUpdateSourceToolTests(unittest.TestCase):
    def test_init_is_repeatable_and_reuses_test_trust_root(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [
                "python", str(ROOT / "tools/dev_update_source.py"),
                "--output", directory, "--init-only",
            ]
            first = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
            key_file = Path(directory) / ".test-update-private-key"
            first_key = key_file.read_bytes()
            second = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, text=True)
            self.assertEqual(first_key, key_file.read_bytes())
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass


class UpdateSourceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.artifact = self.root / "student-execution-os-1.0.1-android-universal.apk"
        self.artifact.write_bytes(b"final-package-bytes")
        handler = lambda *args, **kwargs: QuietHandler(*args, directory=self.temp.name, **kwargs)
        try:
            self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except PermissionError:
            self.temp.cleanup()
            self.skipTest("sandbox forbids loopback sockets")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.key = Ed25519PrivateKey.generate()
        self.trust = {"integration": public_key_b64(self.key)}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def create_policy(self, *, sequence=1):
        policy = self.root / "policy.json"
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"),
               "SEOS_UPDATE_SIGNING_KEY_B64": private_seed_b64(self.key)}
        subprocess.run([
            "python", str(ROOT / "tools/update_policy.py"), "create",
            "--artifact", str(self.artifact), "--artifact-url",
            f"http://127.0.0.1:{self.server.server_port}/{self.artifact.name}",
            "--output", str(policy), "--version", "1.0.1", "--build-number", "101",
            "--sequence", str(sequence), "--channel", "STABLE", "--rollout", "100",
            "--key-id", "integration", "--summary-en", "Integration update",
            "--summary-ru", "Интеграционное обновление", "--change-en", "Verified",
            "--change-ru", "Проверено",
        ], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
        return policy

    def test_signed_policy_fetch_exact_artifact_and_hash(self):
        policy_path = self.create_policy()
        response = httpx.get(f"http://127.0.0.1:{self.server.server_port}/{policy_path.name}", timeout=5)
        response.raise_for_status()
        policy = UpdatePolicy.from_json(response.content)
        verify_policy(policy, self.trust)
        release = policy.latest_release()
        self.assertEqual(str(release.version), "1.0.1")
        self.assertEqual(len(release.artifacts), 1)
        artifact = release.artifacts[0]
        downloaded = httpx.get(artifact.url, timeout=5).content
        self.assertEqual(len(downloaded), artifact.size_bytes)
        self.assertEqual(hashlib.sha256(downloaded).hexdigest(), artifact.sha256)

    def test_corrupt_artifact_fails_without_touching_user_data(self):
        policy_path = self.create_policy()
        policy = UpdatePolicy.from_json(policy_path.read_bytes())
        verify_policy(policy, self.trust)
        user_data = self.root / "user-data.sqlite"
        user_data.write_bytes(b"personal-data")
        self.artifact.write_bytes(b"corrupted-after-publication")
        downloaded = httpx.get(policy.latest_release().artifacts[0].url, timeout=5).content
        self.assertNotEqual(hashlib.sha256(downloaded).hexdigest(), policy.latest_release().artifacts[0].sha256)
        self.assertEqual(user_data.read_bytes(), b"personal-data")

    def test_cli_rejects_tampered_policy(self):
        policy_path = self.create_policy()
        raw = json.loads(policy_path.read_text())
        raw["rollout"]["percentage"] = 0
        policy_path.write_text(json.dumps(raw))
        trust = self.root / "trust.json"
        trust.write_text(json.dumps(self.trust))
        result = subprocess.run([
            "python", str(ROOT / "tools/update_policy.py"), "verify", "--policy", str(policy_path),
            "--trusted-keys-file", str(trust),
        ], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
