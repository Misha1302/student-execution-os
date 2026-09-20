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
        self.assertTrue(all(status for status in registry["tests"].values()))

    def test_pass1_supplemental_acceptance_ids_are_tracked(self) -> None:
        path = Path(__file__).with_name("acceptance_registry.json")
        registry = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(registry["supplemental_pass_tests"]["AT-73"], "PASS_PASS1_DOMAIN")
        self.assertEqual(registry["supplemental_pass_tests"]["AT-83"], "PASS_PASS1_DOMAIN")


if __name__ == "__main__":
    unittest.main()
