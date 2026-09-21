import json
from pathlib import Path
import unittest


EXPECTED_FIRST_SLICE_IDS = {
    *(f"AT-{number:02d}" for number in range(11, 33)),
    "AT-75",
    "AT-78",
    "AT-79",
    "AT-80",
    "AT-81",
}


class AcceptanceRegistryTests(unittest.TestCase):
    def test_first_slice_acceptance_ids_are_explicitly_tracked(self) -> None:
        path = Path(__file__).with_name("acceptance_registry.json")
        registry = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(registry["specification_version"], "2.1")
        self.assertEqual(set(registry["tests"]), EXPECTED_FIRST_SLICE_IDS)
        self.assertTrue(all(status.startswith("PASS_") for status in registry["tests"].values()))

    def test_pass1_supplemental_acceptance_ids_are_tracked(self) -> None:
        path = Path(__file__).with_name("acceptance_registry.json")
        registry = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(registry["supplemental_pass_tests"]["AT-73"], "PASS_PASS1_DOMAIN")
        self.assertEqual(registry["supplemental_pass_tests"]["AT-83"], "PASS_PASS1_DOMAIN")

    def test_pass4_through_pass9_acceptance_are_explicitly_tracked(self) -> None:
        path = Path(__file__).with_name("acceptance_registry.json")
        registry = json.loads(path.read_text(encoding="utf-8"))
        expected_pass4 = {
            "AT-01", "AT-02", "AT-03", "AT-04", "AT-05", "AT-06", "AT-07", "AT-08", "AT-09", "AT-10",
            "AT-59", "AT-67", "AT-71", "AT-72", "AT-74", "AT-76", "AT-77", "AT-82", "AT-84", "AT-85",
        }
        self.assertEqual(set(registry["pass4_tests"]), expected_pass4)
        self.assertTrue(all(status.startswith("PASS_PASS4_") for status in registry["pass4_tests"].values()))
        self.assertEqual(
            set(registry["pass5_tests"]),
            {"AT-38", "AT-39", "AT-40"},
        )
        self.assertTrue(
            all(status.startswith("PASS_PASS5_") for status in registry["pass5_tests"].values())
        )
        self.assertEqual(
            set(registry["pass6_tests"]),
            {f"AT-{number:02d}" for number in range(41, 50)},
        )
        self.assertTrue(
            all(status.startswith("PASS_PASS6_") for status in registry["pass6_tests"].values())
        )
        self.assertEqual(
            set(registry["pass7_tests"]),
            {"AT-33", "AT-34", "AT-35", "AT-36", "AT-37"},
        )
        self.assertTrue(
            all(status.startswith("PASS_PASS7_") for status in registry["pass7_tests"].values())
        )
        self.assertEqual(
            set(registry["pass8_tests"]),
            {"AT-50", "AT-51", "AT-52", "AT-53", "AT-54", "AT-55", "AT-68"},
        )
        self.assertTrue(
            all(status.startswith("PASS_PASS8_") for status in registry["pass8_tests"].values())
        )
        self.assertEqual(
            set(registry["pass9_tests"]),
            {"AT-61", "AT-62", "AT-63", "AT-64", "AT-69"},
        )
        self.assertTrue(
            all(status.startswith("PASS_PASS9_") for status in registry["pass9_tests"].values())
        )



if __name__ == "__main__":
    unittest.main()
