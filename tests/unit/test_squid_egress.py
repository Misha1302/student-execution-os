from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "deploy/llm-egress/squid/setup-egress-host.sh"


class SquidEgressContractTests(unittest.TestCase):
    def test_proxy_is_private_exact_host_connect_only_without_interception(self):
        script = SETUP.read_text(encoding="utf-8")
        conf = script.split("<<EOF\n", 1)[1].split("\nEOF\n", 1)[0]
        rules = [line for line in conf.splitlines() if line.startswith("http_access")]

        self.assertEqual(rules, [
            "http_access deny !seos_server",
            "http_access allow CONNECT SSL_ports llm_hosts",
            "http_access deny all",
        ])
        self.assertIn("acl seos_server src ${SOURCE_IP}/32", conf)
        self.assertIn("acl SSL_ports port 443", conf)
        self.assertIn("acl llm_hosts dstdomain -n $*", conf)
        self.assertIn("set -- api.groq.com", script)
        self.assertIn("cache deny all", conf)
        for forbidden in ("ssl_bump", "ssl-bump", "sslcrtd", "tls-cert", "0.0.0.0/0", "all_proxy"):
            self.assertNotIn(forbidden, script.lower())

    def test_host_firewall_admits_only_the_source(self):
        script = SETUP.read_text(encoding="utf-8")
        self.assertIn('iptables -I INPUT 1 -p tcp -s "${SOURCE_IP}/32" --dport 3128 -j ACCEPT', script)
        self.assertIn("iptables -I INPUT 2 -p tcp --dport 3128 -j DROP", script)
        self.assertIn("netfilter-persistent save", script)


if __name__ == "__main__":
    unittest.main()
