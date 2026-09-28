"""The APK credential scanner used by the Android CI job."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("apk_secret_scan", Path("mobile/scripts/apk_secret_scan.py"))
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


class ApkSecretScanTest(unittest.TestCase):
    def apk(self, entries: dict[str, bytes]) -> str:
        path = Path(tempfile.mkdtemp()) / "app.apk"
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return str(path)

    def run_scan(self, path, *canaries):
        out = StringIO()
        with redirect_stdout(out):
            code = scan.main(path, *canaries)
        return code, out.getvalue()

    def test_clean_apk_passes(self):
        code, _ = self.run_scan(self.apk({"classes.dex": b"dex\n035\x00" + b"\x00" * 1000,
                                          "assets/public/index.html": b"<html>botay</html>"}), "canary-pass")
        self.assertEqual(code, 0)

    def test_each_kind_of_secret_is_reported(self):
        cases = {
            "gsk_" + "a" * 40: "Groq key",
            "sk-proj-" + "b" * 30: "OpenAI-style key",
            "sk-ant-" + "c" * 30: "Anthropic key",
            "-----BEGIN PRIVATE KEY-----": "private key",
            "botay_cap_" + "0" * 32 + "_" + "d" * 43: "botay capability token",
            "SEOS_CREDENTIAL_KEY=abc": "server secret variable",
        }
        for secret, label in cases.items():
            with self.subTest(label=label):
                code, out = self.run_scan(self.apk({"assets/public/config.js": secret.encode()}))
                self.assertEqual(code, 1)
                self.assertIn(label, out)
                self.assertNotIn(secret, out)  # the report never prints the secret itself
        code, out = self.run_scan(self.apk({"res/raw/x": b"\xfe\xed\xfe\xed..."}))
        self.assertIn("Java keystore content", out)
        code, out = self.run_scan(self.apk({"release.jks": b"x"}))
        self.assertIn("keystore file", out)
        code, out = self.run_scan(self.apk({"classes.dex": b"...my-signing-pass-123..."}), "my-signing-pass-123")
        self.assertIn("build secret canary", out)


if __name__ == "__main__":
    unittest.main()
