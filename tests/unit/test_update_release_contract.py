from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class UpdateReleaseContractTests(unittest.TestCase):
    def test_android_runs_for_every_release_push_but_filters_pull_requests(self):
        workflow = (ROOT / ".github/workflows/android.yml").read_text()
        triggers = workflow.split("permissions:", 1)[0]
        push = triggers.split("  push:", 1)[1].split("  pull_request:", 1)[0]
        pull_request = triggers.split("  pull_request:", 1)[1].split("  workflow_dispatch:", 1)[0]
        self.assertNotIn("paths:", push)
        self.assertIn("paths:", pull_request)

    def test_release_pipeline_has_separate_authority_and_metadata_last(self):
        workflow = (ROOT / ".github/workflows/android-release.yml").read_text()
        for phase in ("BUILD", "PACKAGE", "PUBLISH_ARTIFACTS", "PROMOTE_RELEASE"):
            self.assertIn(phase, workflow)
        self.assertLess(workflow.index("name: PUBLISH_ARTIFACTS"), workflow.index("name: PROMOTE_RELEASE"))
        publish = workflow.split("name: PUBLISH_ARTIFACTS", 1)[1].split("name: PROMOTE_RELEASE", 1)[0]
        promote = workflow.split("name: PROMOTE_RELEASE", 1)[1]
        self.assertIn("gh release create", publish)
        self.assertIn("gh release download", publish)
        self.assertIn("sha256sum", publish)
        self.assertIn("Publish channel metadata last", promote)
        self.assertIn("update_policy.py verify", promote)
        self.assertNotIn("pull_request:", workflow)
        self.assertIn("environment: update-production", workflow)

    def test_release_artifacts_are_immutable_and_named_by_version(self):
        workflow = (ROOT / ".github/workflows/android-release.yml").read_text()
        self.assertIn('student-execution-os-${VERSION}-android-universal.apk', workflow)
        self.assertIn("immutable release tag already exists", workflow)
        # Only mutable signed policy is clobbered; versioned APK upload is not.
        publish = workflow.split("name: PUBLISH_ARTIFACTS", 1)[1].split("name: PROMOTE_RELEASE", 1)[0]
        self.assertNotIn("--clobber", publish)

    def test_release_apk_has_push_config_evidence_and_provenance(self):
        workflow = (ROOT / ".github/workflows/android-release.yml").read_text()
        package = workflow.split("name: PACKAGE", 1)[1].split("name: PUBLISH_ARTIFACTS", 1)[0]
        publish = workflow.split("name: PUBLISH_ARTIFACTS", 1)[1].split("name: PROMOTE_RELEASE", 1)[0]
        # Without google-services.json the Gradle build silently compiles push out.
        self.assertIn('test -n "$GOOGLE_SERVICES_B64"', package)
        self.assertIn("mobile/android/app/google-services.json", package)
        self.assertIn("release_evidence.py", package)
        for flag in ("--require-fcm", "--require-clean", "--expect-version-code", "--canary"):
            self.assertIn(flag, package)
        self.assertIn("provenance.json", package)
        self.assertIn('"$apk" provenance.json', publish)

    def test_free_text_dispatch_inputs_never_become_script_text(self):
        workflow = (ROOT / ".github/workflows/android-release.yml").read_text()
        scripts = [block for block in workflow.split("run: |")[1:]]
        for name in ("summary_en", "summary_ru", "change_en", "change_ru", "version",
                     "required_after", "minimum_supported_version"):
            for block in scripts:
                body = block.split("\n      - ", 1)[0]
                self.assertNotIn("${{ inputs.%s }}" % name, body)

    def test_pause_withdraw_requires_new_signed_sequence(self):
        workflow = (ROOT / ".github/workflows/update-release-control.yml").read_text()
        self.assertIn("PAUSED", workflow)
        self.assertIn("WITHDRAWN", workflow)
        self.assertIn("--sequence", workflow)
        self.assertIn("update_policy.py verify", workflow)
        self.assertIn("Discovery metadata is the only mutable asset and is published last", workflow)

    def test_client_has_no_hosting_token_or_signature_bypass(self):
        sources = "\n".join(path.read_text(errors="ignore") for path in [
            ROOT / "src/student_execution_os/web/static/js/update-service.js",
            ROOT / "src/student_execution_os/web/static/js/update-domain.js",
            ROOT / "mobile/android/app/src/main/java/io/github/misha1302/seos/updates/SeosUpdatePlugin.java",
        ])
        self.assertNotIn("GITHUB_TOKEN", sources)
        self.assertNotIn("skip_signature", sources.lower())
        self.assertIn("verifyPolicy", sources)
        self.assertIn("verifyDownloaded", sources)

    def test_android_updater_only_uses_private_update_cache(self):
        plugin = (ROOT / "mobile/android/app/src/main/java/io/github/misha1302/seos/updates/SeosUpdatePlugin.java").read_text()
        self.assertIn('getCacheDir(), "updates"', plugin)
        self.assertNotIn("getFilesDir", plugin)
        self.assertNotIn("/data/", plugin)
        self.assertIn("PackageInstaller", plugin)
        self.assertIn("USER_ACTION_REQUIRED", plugin)

    def test_debug_build_has_no_default_production_update_feed(self):
        gradle = (ROOT / "mobile/android/app/build.gradle").read_text()
        self.assertIn('System.getenv("SEOS_UPDATE_POLICY_URL_TEMPLATE") ?: ""', gradle)
        self.assertIn('System.getenv("SEOS_UPDATE_TRUST_KEYS_JSON") ?: "{}"', gradle)


if __name__ == "__main__":
    unittest.main()
