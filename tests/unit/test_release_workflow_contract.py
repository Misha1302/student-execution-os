from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ReleaseWorkflowContractTests(unittest.TestCase):
    def test_signed_channel_metadata_is_not_published_as_mutable_release_asset(self):
        release = (ROOT / ".github/workflows/android-release.yml").read_text(encoding="utf-8")
        control = (ROOT / ".github/workflows/update-release-control.yml").read_text(encoding="utf-8")
        for workflow in (release, control):
            self.assertIn('branch="update-$channel"', workflow)
            self.assertIn("raw.githubusercontent.com/$repo/$branch/policy.json", workflow)
            self.assertNotIn('gh release upload "$tag" policy.json', workflow)
            self.assertIn("python tools/update_policy.py verify-client-policy --policy published/policy.json", workflow)
        self.assertIn("group: update-channel-${{ inputs.channel }}", release)
        self.assertIn("group: update-channel-${{ inputs.channel }}", control)

    def test_scheduled_refresh_authenticates_expired_predecessor_and_serializes_by_channel(self):
        workflow = (ROOT / ".github/workflows/update-policy-refresh.yml").read_text(encoding="utf-8")
        self.assertIn("schedule:", workflow)
        self.assertIn("verify-authenticity --policy current-policy.json", workflow)
        self.assertIn("verify-client-policy --policy policy.json", workflow)
        self.assertIn("group: update-channel-${{ matrix.channel }}", workflow)
        self.assertIn('sha="$current_sha"', workflow)

    def test_release_publisher_rejects_non_increasing_sequence(self):
        release = (ROOT / ".github/workflows/android-release.yml").read_text(encoding="utf-8")
        self.assertIn('if test "$POLICY_SEQUENCE" -le "$current_sequence"; then', release)

    def test_release_dispatch_is_bound_to_exact_source_sha(self):
        release = (ROOT / ".github/workflows/android-release.yml").read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("source_sha:", release)
        self.assertIn("dispatch_id:", release)
        self.assertIn("run-name: android-release ${{ inputs.version }} · ${{ inputs.dispatch_id }}", release)
        self.assertIn("EXPECTED_SOURCE_SHA: ${{ inputs.source_sha }}", release)
        self.assertIn('test "$GITHUB_SHA" != "$EXPECTED_SOURCE_SHA"', release)
        self.assertIn("apk-release:\n\tpython tools/android_release.py", makefile)
        self.assertNotIn("gh workflow run", makefile.split("apk-release:", 1)[1])

    def test_documented_client_template_points_to_signed_metadata_branch(self):
        docs = (ROOT / "docs/updates.md").read_text(encoding="utf-8")
        mobile = (ROOT / "mobile/README.md").read_text(encoding="utf-8")
        expected = "https://raw.githubusercontent.com/OWNER/REPOSITORY/update-{channel}/policy.json"
        self.assertIn(expected, docs)
        self.assertIn(
            "https://raw.githubusercontent.com/OWNER/REPO/update-{channel}/policy.json",
            mobile,
        )


if __name__ == "__main__":
    unittest.main()
