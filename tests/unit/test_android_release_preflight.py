from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from student_execution_os.updates import public_key_b64
from student_execution_os.updates.signing import private_seed_b64

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/android_release_preflight.py"
SIGNER = "c" * 64


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


class AndroidReleasePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sha = git("rev-parse", "HEAD")
        key = Ed25519PrivateKey.generate()
        self.env = {
            **os.environ,
            "SEOS_UPDATE_SIGNING_KEY_B64": private_seed_b64(key),
            "SEOS_UPDATE_TRUST_KEYS_JSON": json.dumps({"test-2026": public_key_b64(key)}),
        }
        self.apk = self.dir / "student-execution-os-0.7.4-android-universal.apk"
        self.apk.write_bytes(b"signed apk bytes")
        self.provenance = self.dir / "provenance.json"
        self.provenance.write_text(json.dumps({
            "source": {"commit": self.sha, "tree": git("rev-parse", f"{self.sha}^{{tree}}"), "tracked_changes": []},
            "artifact": {
                "file": self.apk.name, "sha256": hashlib.sha256(self.apk.read_bytes()).hexdigest(),
                "size": self.apk.stat().st_size, "version_name": "0.7.4", "version_code": "10",
                "package": "io.github.misha1302.seos", "fcm_config": True,
            },
            "signing": {"signers": [{"certificate_sha256": SIGNER}]},
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_tool(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([
            sys.executable, str(TOOL), "--artifact", str(self.apk), "--provenance", str(self.provenance),
            "--source-sha", self.sha, "--version", "0.7.4", "--build-number", "10", "--channel", "STABLE",
            "--policy-sequence", "18", "--key-id", "test-2026", "--mandatory", "OPTIONAL",
            "--summary-en", "Safer updates", "--summary-ru", "Безопасные обновления",
            "--change-en", "Installer recovery", "--change-ru", "Восстановление установщика",
            "--expected-signer-sha256", SIGNER, *extra,
        ], cwd=ROOT, env=self.env, capture_output=True, text=True)

    def existing_release(self, **overrides) -> Path:
        path = self.dir / "existing-release.json"
        release = {"tag_name": "v0.7.4", "target_commitish": self.sha, "immutable": True, "draft": False,
                   "assets": [{"name": self.apk.name}, {"name": "provenance.json"}], **overrides}
        path.write_text(json.dumps(release), encoding="utf-8")
        return path

    def test_valid_candidate_passes(self) -> None:
        result = self.run_tool("--published-build", "9")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])

    def test_version_code_must_exceed_every_published_build(self) -> None:
        result = self.run_tool("--published-build", "9", "--published-build", "10")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be greater than published maximum 10", result.stderr)

    def test_signer_must_match_established_identity(self) -> None:
        result = self.run_tool("--expected-signer-sha256", "d" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signer differs", result.stderr)

    def test_resume_requires_byte_identical_provenance(self) -> None:
        same = self.dir / "existing-provenance.json"
        same.write_bytes(self.provenance.read_bytes())
        release = str(self.existing_release())
        ok = self.run_tool("--existing-release", release, "--existing-provenance", str(same))
        self.assertEqual(ok.returncode, 0, ok.stderr)

        same.write_bytes(self.provenance.read_bytes().replace(b'"size"', b'"size" ', 1))
        changed = self.run_tool("--existing-release", release, "--existing-provenance", str(same))
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("never clobber", changed.stderr)

    def test_resume_rejects_extra_assets_and_missing_provenance(self) -> None:
        same = self.dir / "existing-provenance.json"
        same.write_bytes(self.provenance.read_bytes())
        extra = self.existing_release(assets=[{"name": self.apk.name}, {"name": "provenance.json"}, {"name": "x"}])
        result = self.run_tool("--existing-release", str(extra), "--existing-provenance", str(same))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("assets differ", result.stderr)
        alone = self.run_tool("--existing-release", str(self.existing_release()))
        self.assertNotEqual(alone.returncode, 0)


if __name__ == "__main__":
    unittest.main()
