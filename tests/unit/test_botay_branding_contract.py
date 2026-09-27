from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class BotayBrandingContractTests(unittest.TestCase):
    def test_user_facing_shell_and_mobile_label_use_canonical_brand(self):
        index = (ROOT / "src/student_execution_os/web/static/index.html").read_text(encoding="utf-8")
        self.assertIn("<title>botay!</title>", index)
        self.assertIn("manifest.webmanifest", index)
        manifest = json.loads((ROOT / "src/student_execution_os/web/static/manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual((manifest["name"], manifest["short_name"]), ("botay!", "botay!"))
        cap = json.loads((ROOT / "mobile/capacitor.config.json").read_text(encoding="utf-8"))
        self.assertEqual(cap["appName"], "botay!")
        strings = (ROOT / "mobile/android/app/src/main/res/values/strings.xml").read_text(encoding="utf-8")
        self.assertIn("<string name=\"app_name\">botay!</string>", strings)
        self.assertNotIn("Execution OS</string>", strings)

    def test_brand_assets_are_vector_and_legacy_artwork_is_not_shipped(self):
        self.assertTrue((ROOT / "src/student_execution_os/web/static/botay-icon.svg").is_file())
        self.assertTrue((ROOT / "mobile/android/app/src/main/res/drawable/ic_launcher_foreground.xml").is_file())
        old = list((ROOT / "mobile/android/app/src/main/res").glob("**/splash.png"))
        old += list((ROOT / "mobile/android/app/src/main/res").glob("mipmap-*/ic_launcher*.png"))
        self.assertEqual(old, [])


if __name__ == "__main__":
    unittest.main()
