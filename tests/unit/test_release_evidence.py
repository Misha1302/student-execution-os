"""Release APK evidence: signer, version, Firebase config, credential scan, provenance."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("release_evidence", Path("mobile/scripts/release_evidence.py"))
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)

DIGEST = "ab" * 32
APKSIGNER = f"""Verifies
Verified using v2 scheme (APK Signature Scheme v2): true
Number of signers: 1
Signer #1 certificate DN: CN=botay release
Signer #1 certificate SHA-256 digest: {DIGEST}
Signer #1 certificate SHA-1 digest: {"cd" * 20}
"""
BADGING = ("package: name='io.github.misha1302.seos' versionCode='42' versionName='1.2.0' "
           "platformBuildVersionName='14'\nsdkVersion:'24'\n")
RESOURCES = "Package name=io.github.misha1302.seos id=7f\n  type string id=0e\n    resource 0x7f0e0042 string/google_app_id\n"


class ParsingTest(unittest.TestCase):
    def test_signers_badging_and_fcm(self):
        self.assertEqual(evidence.parse_signers(APKSIGNER), [{"certificate_sha256": DIGEST, "dn": "CN=botay release"}])
        self.assertEqual(evidence.parse_badging(BADGING),
                         {"package": "io.github.misha1302.seos", "version_code": "42", "version_name": "1.2.0"})
        self.assertTrue(evidence.has_fcm_config(RESOURCES))
        self.assertFalse(evidence.has_fcm_config("resource 0x7f0e0043 string/google_app_id_hint"))
        with self.assertRaises(ValueError):
            evidence.parse_badging("nothing")


class MainTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.tools = self.dir / "build-tools"
        self.tools.mkdir()
        self.apk = self.dir / "app.apk"
        with zipfile.ZipFile(self.apk, "w") as archive:
            archive.writestr("classes.dex", b"dex\n035\x00" + b"\x00" * 64)

    def tool(self, name: str, output: str) -> None:
        (self.dir / f"{name}.out").write_text(output)
        script = self.tools / name
        script.write_text(f"#!/bin/sh\nif [ \"$2\" = resources ]; then cat '{self.dir}/{name}.res'; "
                          f"else cat '{self.dir}/{name}.out'; fi\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def run_main(self, resources: str = RESOURCES, *extra: str) -> tuple[int, str, dict]:
        self.tool("apksigner", APKSIGNER)
        self.tool("aapt2", BADGING)
        (self.dir / "aapt2.res").write_text(resources)
        out = StringIO()
        with redirect_stdout(out):
            code = evidence.main([str(self.apk), "--build-tools", str(self.tools),
                                  "--output", str(self.dir / "provenance.json"), *extra])
        return code, out.getvalue(), json.loads((self.dir / "provenance.json").read_text())

    def test_record_links_source_artifact_and_signer(self):
        code, _out, record = self.run_main(RESOURCES, "--require-fcm", "--expect-version-name", "1.2.0",
                                           "--expect-version-code", "42", "--canary", "never-in-apk")
        self.assertEqual(code, 0)
        head = subprocess.run(["git", "rev-parse", "HEAD", "HEAD^{tree}"], capture_output=True, text=True,
                              check=True).stdout.split()
        self.assertEqual([record["source"]["commit"], record["source"]["tree"]], head)
        self.assertEqual(record["signing"]["signers"][0]["certificate_sha256"], DIGEST)
        self.assertEqual(record["artifact"]["size"], os.path.getsize(self.apk))
        self.assertTrue(record["artifact"]["fcm_config"])
        self.assertNotIn("never-in-apk", json.dumps(record))

    def test_release_without_firebase_config_fails_when_required(self):
        code, out, record = self.run_main("resource 0x7f0e0001 string/app_name\n", "--require-fcm")
        self.assertEqual(code, 1)
        self.assertIn("push would be compiled out", out)
        self.assertFalse(record["artifact"]["fcm_config"])
        self.assertEqual(self.run_main("resource 0x7f0e0001 string/app_name\n")[0], 0)

    def test_wrong_version_or_leaked_secret_fails(self):
        self.assertEqual(self.run_main(RESOURCES, "--expect-version-code", "43")[0], 1)
        with zipfile.ZipFile(self.apk, "a") as archive:
            archive.writestr("assets/leak.txt", "gsk_" + "a" * 40)
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("credential material", out)


if __name__ == "__main__":
    unittest.main()
