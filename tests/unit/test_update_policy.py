from __future__ import annotations

import base64
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from student_execution_os.updates import (
    ArtifactKind,
    MandatoryPolicy,
    MinimumOsVersions,
    PolicySignatureError,
    ReleaseNotes,
    ReleaseSeverity,
    ReleaseStatus,
    RollbackCompatibility,
    Rollout,
    SemVer,
    SignatureMetadata,
    UpdateArtifact,
    UpdateChannel,
    UpdatePolicy,
    UpdateRelease,
    UpdaterMetadata,
    public_key_b64,
    sign_policy,
    verify_policy,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)


def policy(*, channel=UpdateChannel.STABLE, version="1.0.1", status=ReleaseStatus.AVAILABLE,
           rollout=100, sequence=7, expires=NOW + timedelta(days=1)) -> UpdatePolicy:
    release = UpdateRelease(
        version=SemVer.parse(version), build_number=101, channel=channel, published_at=NOW,
        status=status, severity=ReleaseSeverity.NORMAL,
        release_notes={"en": ReleaseNotes("Update", ("Safer updates",)),
                       "ru": ReleaseNotes("Обновление", ("Безопасные обновления",))},
        minimum_os_versions=MinimumOsVersions(24), minimum_api_version=1,
        mandatory_policy=MandatoryPolicy(), rollback_compatibility=RollbackCompatibility.BINARY_ONLY,
        artifacts=(UpdateArtifact(
            "android", "universal", ArtifactKind.APK, "https://updates.example/app.apk", 4,
            "a" * 64, UpdaterMetadata("io.github.misha1302.seos", 101, 24),
        ),),
    )
    return UpdatePolicy(
        1, sequence, NOW, expires, channel, release.version, SemVer.parse("0.9.0"),
        Rollout(rollout), (release,), SignatureMetadata("test-2026"),
    )


class SemVerTests(unittest.TestCase):
    def test_semver_2_ordering_and_build_precedence(self):
        ordered = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
                   "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
        parsed = [SemVer.parse(value) for value in ordered]
        self.assertEqual(sorted(parsed), parsed)
        self.assertEqual(SemVer.parse("1.0.0+one"), SemVer.parse("1.0.0+two"))
        self.assertEqual(
            hash(SemVer.parse("1.0.0+one")),
            hash(SemVer.parse("1.0.0+two")),
        )
        with self.assertRaises(ValueError):
            SemVer.parse("1.0.0-beta.01")

    def test_stable_policy_rejects_prerelease(self):
        with self.assertRaises(ValueError):
            policy(version="1.0.1-beta.1")
        self.assertTrue(policy(channel=UpdateChannel.BETA, version="1.0.1-beta.1"))


class SignedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.trusted = {"test-2026": public_key_b64(self.key)}

    def test_signature_covers_policy_and_artifact_hash(self):
        signed = sign_policy(policy(), self.key)
        verify_policy(signed, self.trusted)
        raw = signed.to_dict()
        raw["releases"][0]["artifacts"][0]["sha256"] = "b" * 64
        with self.assertRaises(PolicySignatureError):
            verify_policy(UpdatePolicy.from_dict(raw), self.trusted)

    def test_invalid_signature_and_unknown_rotation_key_are_rejected(self):
        signed = sign_policy(policy(), self.key)
        bad = replace(signed, signature_metadata=replace(signed.signature_metadata,
                      signature=base64.b64encode(b"x" * 64).decode()))
        with self.assertRaises(PolicySignatureError):
            verify_policy(bad, self.trusted)
        with self.assertRaises(PolicySignatureError):
            verify_policy(signed, {"future-key": public_key_b64(Ed25519PrivateKey.generate())})

    def test_canonical_round_trip_is_stable(self):
        signed = sign_policy(policy(), self.key)
        loaded = UpdatePolicy.from_json(signed.signed_json())
        self.assertEqual(signed.canonical_bytes(), loaded.canonical_bytes())
        verify_policy(loaded, self.trusted)

    def test_signed_schema_rejects_numeric_and_string_coercion(self):
        raw = sign_policy(policy(), self.key).to_dict()
        raw["sequence"] = "7"
        with self.assertRaises(ValueError):
            UpdatePolicy.from_dict(raw)

        raw = sign_policy(policy(), self.key).to_dict()
        raw["releases"][0]["release_notes"]["en"]["changes"] = [123]
        with self.assertRaises(ValueError):
            UpdatePolicy.from_dict(raw)

    def test_rollout_boundaries_and_cohort_are_deterministic(self):
        release_id = policy().latest_release().release_id
        self.assertFalse(Rollout(0).eligible("install-a", release_id))
        self.assertTrue(Rollout(100).eligible("install-a", release_id))
        self.assertEqual(Rollout(25).eligible("install-a", release_id),
                         Rollout(25).eligible("install-a", release_id))
        eligible5 = {i for i in range(1000) if Rollout(5).eligible(f"install-{i}", release_id)}
        eligible25 = {i for i in range(1000) if Rollout(25).eligible(f"install-{i}", release_id)}
        self.assertTrue(eligible5)
        self.assertLess(eligible5, eligible25)

    def test_release_identity_and_artifact_identity_are_unique(self):
        original = policy()
        with self.assertRaises(ValueError):
            replace(original, releases=(original.releases[0], original.releases[0]))
        with self.assertRaises(ValueError):
            replace(original.releases[0], artifacts=(original.releases[0].artifacts[0],) * 2)


class JavaScriptUpdateDomainTests(unittest.TestCase):
    def test_client_policy_service_cases(self):
        result = subprocess.run(
            ["node", str(ROOT / "tests/js/update_cases.mjs")], cwd=ROOT,
            text=True, capture_output=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertGreaterEqual(payload["assertions"], 30)


if __name__ == "__main__":
    unittest.main()
