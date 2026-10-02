from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class WarpEgressDeploymentContractTests(unittest.TestCase):
    def text(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_overlay_makes_warp_the_only_egress_owner_for_groq(self):
        overlay = self.text("deploy/docker-compose.warp-egress.yml")
        self.assertIn("SEOS_LLM_EGRESS_PROXY: http://seos-groq-warp:40001", overlay)
        self.assertIn("SEOS_LLM_EGRESS_PROXY_HOSTS: api.groq.com\n", overlay)
        for cleared in ("SEOS_LLM_EGRESS_PROXY_FILE", "SEOS_LLM_EGRESS_RELAY_URL",
                        "SEOS_LLM_EGRESS_RELAY_TOKEN_FILE", "SEOS_LLM_EGRESS_RELAY_HOSTS"):
            self.assertIn(f'{cleared}: ""', overlay)

    def test_only_the_api_joins_the_internal_warp_network(self):
        overlay = self.text("deploy/docker-compose.warp-egress.yml")
        services = overlay.split("\nservices:\n", 1)[1].split("\nnetworks:\n", 1)[0]
        self.assertEqual([line for line in services.splitlines() if line.startswith("  ") and not line.startswith("   ")],
                         ["  api:"])
        self.assertIn("      - default\n      - llm-egress-warp", services)
        self.assertIn("external: true\n    name: seos-g9-egress_egress", overlay)
        self.assertNotIn("ports:", overlay)

    def test_warp_project_publishes_nothing_and_keeps_its_registration_outside_the_repo(self):
        compose = self.text("deploy/llm-egress/warp/compose.yml")
        self.assertIn("name: seos-g9-egress\n", compose)
        self.assertIn("container_name: seos-groq-warp", compose)
        self.assertIn("egress:\n    internal: true", compose)
        self.assertIn("restart: unless-stopped", compose)
        self.assertIn("healthcheck:", compose)
        self.assertNotIn("ports:", compose)
        self.assertIn("external: true\n    name: seos-warp-state", compose)
        entrypoint = self.text("deploy/llm-egress/warp/entrypoint.sh")
        self.assertIn("mode proxy", entrypoint)  # never a tunnel for the whole container network


if __name__ == "__main__":
    unittest.main()
