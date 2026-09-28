"""External immutable RC evidence schema and release-identity invariants."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("rc_evidence", Path("tools/rc_evidence.py"))
rc_evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rc_evidence)

SHA = "a" * 40
TREE = "b" * 40


def completed_record() -> dict:
    record = rc_evidence.new_record(SHA, TREE, "release-operator")
    for gate in record["gates"]:
        gate.update({
            "environment": "CI" if gate["gate"] in {"G1", "G4", "G11"} else "LIVE",
            "action": "canonical gate command",
            "safe_result": {"result": "OK"},
            "artifact_identity": [f"evidence:{gate['gate']}"],
            "status": "VERIFIED",
            "blocker": "",
        })
    return record


class ValidationTest(unittest.TestCase):
    def test_complete_record_is_bound_to_one_sha_and_tree(self):
        record = completed_record()
        self.assertEqual(rc_evidence.validate(record, expect_sha=SHA, expect_tree=TREE,
                                              require_all_verified=True), [])
        record["gates"][7]["release_sha"] = "c" * 40
        errors = rc_evidence.validate(record, require_all_verified=True)
        self.assertTrue(any("frozen release_sha" in error for error in errors))

    def test_missing_duplicate_and_unverified_gates_fail_publication(self):
        record = completed_record()
        record["gates"][-1] = dict(record["gates"][0])
        errors = rc_evidence.validate(record, require_all_verified=True)
        self.assertTrue(any("duplicates G1" in error for error in errors))
        self.assertTrue(any("missing gates: G13" in error for error in errors))
        record = completed_record()
        record["gates"][4].update(status="NOT_VERIFIED", blocker="rotation pending")
        self.assertTrue(any("G5 is not VERIFIED" in error for error in
                            rc_evidence.validate(record, require_all_verified=True)))

    def test_secret_bearing_fields_and_values_are_rejected(self):
        record = completed_record()
        record["gates"][8]["safe_result"]["api_key"] = "redacted"
        record["gates"][9]["notes"] = "Bearer " + "x" * 24
        errors = rc_evidence.validate(record)
        self.assertTrue(any("secret-bearing field" in error for error in errors))
        self.assertTrue(any("resembles secret material" in error for error in errors))


class CliTest(unittest.TestCase):
    def test_new_then_validate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rc-evidence.json"
            with redirect_stdout(StringIO()):
                self.assertEqual(rc_evidence.main(["new", "--release-sha", SHA,
                                                   "--release-tree", TREE,
                                                   "--operator", "operator",
                                                   "--output", str(path)]), 0)
            record = json.loads(path.read_text())
            self.assertEqual(len(record["gates"]), 13)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(rc_evidence.main(["validate", str(path),
                                                   "--expect-sha", SHA,
                                                   "--expect-tree", TREE]), 0)
                self.assertEqual(rc_evidence.main(["validate", str(path),
                                                   "--require-all-verified"]), 1)


if __name__ == "__main__":
    unittest.main()
