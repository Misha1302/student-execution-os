from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SETUP = ROOT / "deploy/llm-egress/squid/setup-egress-host.sh"


class SquidEgressContractTests(unittest.TestCase):
    def test_shell_is_syntactically_valid(self) -> None:
        subprocess.run(["sh", "-n", str(SETUP)], check=True)

    def test_proxy_is_source_scoped_exact_host_connect_only_without_interception(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        conf = script.split("<<EOF\n", 1)[1].split("\nEOF\n", 1)[0]
        rules = [line for line in conf.splitlines() if line.startswith("http_access")]

        self.assertEqual(
            rules,
            [
                "http_access deny !seos_server",
                "http_access allow CONNECT SSL_ports llm_hosts",
                "http_access deny all",
            ],
        )
        self.assertIn("http_port 0.0.0.0:3128", conf)
        self.assertIn("acl seos_server src ${SOURCE_IP}/32", conf)
        self.assertIn("acl SSL_ports port 443", conf)
        self.assertIn("acl llm_hosts dstdomain -n ${HOSTS}", conf)
        self.assertIn("is_exact_hostname", script)
        self.assertIn("set -- api.groq.com", script)
        self.assertIn("cache deny all", conf)
        for forbidden in (
            "ssl_bump",
            "ssl-bump",
            "sslcrtd",
            "tls-cert",
            "0.0.0.0/0",
            "all_proxy",
        ):
            self.assertNotIn(forbidden, script.lower())

    def test_host_firewall_is_installed_before_squid_and_is_idempotently_scoped(self) -> None:
        script = SETUP.read_text(encoding="utf-8")
        firewall_install = script.index(
            "apt-get install -y -q --no-install-recommends iptables iptables-persistent"
        )
        squid_install = script.index("apt-get install -y -q --no-install-recommends squid")
        self.assertLess(firewall_install, squid_install)
        self.assertIn('CHAIN=SEOS_SQUID', script)
        self.assertIn(
            'iptables -A "$CHAIN" -p tcp -s "${SOURCE_IP}/32" --dport 3128 -j ACCEPT',
            script,
        )
        self.assertIn(
            'iptables -A "$CHAIN" -p tcp --dport 3128 -j DROP',
            script,
        )
        self.assertIn("netfilter-persistent save", script)

    def test_invalid_source_or_non_exact_host_fails_before_package_changes(self) -> None:
        cases = [
            (["999.1.2.3"], "source must be one valid IPv4 address"),
            (["203.0.113.10", ".api.groq.com"], "allowed host must be one exact DNS hostname"),
            (["203.0.113.10", "*.groq.com"], "allowed host must be one exact DNS hostname"),
            (["203.0.113.10", "api.groq.com:443"], "allowed host must be one exact DNS hostname"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                result = subprocess.run(
                    ["sh", str(SETUP), *args],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("apt-get", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
