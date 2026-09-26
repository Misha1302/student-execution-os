from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class TorDeploymentContractTests(unittest.TestCase):
    def text(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_tor_is_opt_in_and_overlay_sets_only_the_llm_proxy(self):
        overlay = self.text("deploy/docker-compose.tor.yml")
        for base in ("deploy/docker-compose.yml", "deploy/docker-compose.nginx.yml"):
            text = self.text(base)
            self.assertNotIn("\n  tor:", text)
            self.assertNotIn("\n  llm-egress-proxy:", text)

        self.assertIn("SEOS_LLM_EGRESS_PROXY: http://llm-egress-proxy:8118", overlay)
        self.assertIn(
            "SEOS_LLM_EGRESS_PROXY_HOSTS: ${SEOS_LLM_EGRESS_PROXY_HOSTS:-api.groq.com}",
            overlay,
        )
        self.assertIn('SEOS_LLM_EGRESS_PROXY_FILE: ""', overlay)

    def test_privoxy_has_no_direct_internet_network_and_no_ports_are_published(self):
        overlay = self.text("deploy/docker-compose.tor.yml")
        proxy_block = overlay.split("\n  llm-egress-proxy:", 1)[1].split("\n  tor:", 1)[0]
        tor_block = overlay.split("\n  tor:", 1)[1].split("\nnetworks:", 1)[0]

        self.assertIn("\n      - llm-egress", proxy_block)
        self.assertNotIn("tor-uplink", proxy_block)
        self.assertIn("\n      - llm-egress", tor_block)
        self.assertIn("\n      - tor-uplink", tor_block)
        self.assertNotIn("\n    ports:", overlay)
        self.assertIn("llm-egress:\n    internal: true", overlay)

    def test_privoxy_uses_tor_remote_dns_without_interception_or_request_logging(self):
        config = self.text("deploy/llm-egress/privoxy/config")
        self.assertIn("forward-socks5t / tor:9050 .", config)
        self.assertIn("accept-intercepted-requests 0", config)
        self.assertIn("enable-edit-actions 0", config)
        self.assertNotIn("logfile ", config)
        self.assertNotIn("ca-cert", config.lower())
        self.assertNotIn("https-inspection", config.lower())

    def test_tor_healthcheck_requires_completed_bootstrap_and_control_is_local(self):
        torrc = self.text("deploy/llm-egress/tor/torrc")
        health = self.text("deploy/llm-egress/tor/healthcheck.sh")
        self.assertIn("ControlPort 127.0.0.1:9051", torrc)
        self.assertIn("CookieAuthentication 1", torrc)
        self.assertIn("GETINFO status/bootstrap-phase", health)
        self.assertIn("PROGRESS=100", health)

    def test_provider_transport_security_invariants_are_still_explicit(self):
        providers = self.text("src/student_execution_os/agent/providers.py")
        self.assertNotIn("verify=False", providers)
        self.assertIn("follow_redirects=False", providers)
        self.assertIn("host not in allowed", providers)


if __name__ == "__main__":
    unittest.main()
