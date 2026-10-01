from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("android_release", ROOT / "tools/android_release.py")
android_release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = android_release
assert SPEC.loader is not None
SPEC.loader.exec_module(android_release)


class AndroidReleaseTests(unittest.TestCase):
    def test_github_repo_from_remote_accepts_ssh_and_https(self):
        for remote in (
            "git@github.com:Misha1302/student-execution-os.git",
            "https://github.com/Misha1302/student-execution-os.git",
            "ssh://git@github.com/Misha1302/student-execution-os.git",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(
                    android_release.github_repo_from_remote(remote),
                    "Misha1302/student-execution-os",
                )

    def test_github_repo_from_remote_rejects_other_hosts(self):
        with self.assertRaises(android_release.ReleaseError):
            android_release.github_repo_from_remote("git@example.com:owner/repo.git")

    def test_build_number_uses_highest_published_channel(self):
        stable = {"releases": [{"build_number": 6}]}
        beta = {"releases": [{"build_number": 9}]}
        self.assertEqual(android_release.current_build_number((stable, beta)), 9)

    def test_dispatch_args_bind_repo_source_and_all_required_inputs(self):
        values = android_release.ReleaseInputs(
            version="0.8.0",
            build_number=7,
            channel="STABLE",
            policy_sequence=13,
            rollout="5",
            severity="NORMAL",
            mandatory="OPTIONAL",
            required_after="",
            minimum_supported_version="",
            summary_en="Summary",
            summary_ru="Итог",
            change_en="Change",
            change_ru="Изменение",
        )
        args = android_release.dispatch_args(
            "Misha1302/student-execution-os",
            "a" * 40,
            "dispatch-123",
            values,
        )
        joined = "\n".join(args)
        self.assertEqual(args[:7], [
            "workflow", "run", "android-release.yml", "--repo",
            "Misha1302/student-execution-os", "--ref", "main",
        ])
        for expected in (
            f"source_sha={'a' * 40}",
            "dispatch_id=dispatch-123",
            "version=0.8.0",
            "build_number=7",
            "channel=STABLE",
            "policy_sequence=13",
            "summary_en=Summary",
            "summary_ru=Итог",
            "change_en=Change",
            "change_ru=Изменение",
        ):
            self.assertIn(expected, joined)

    def test_current_sequence_requires_integer(self):
        with self.assertRaises(android_release.ReleaseError):
            android_release.current_sequence({"sequence": "12"})


if __name__ == "__main__":
    unittest.main()
