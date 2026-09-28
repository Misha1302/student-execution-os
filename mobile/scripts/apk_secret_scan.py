#!/usr/bin/env python3
"""Fail if an APK carries credential material.

    python mobile/scripts/apk_secret_scan.py APP.apk [CANARY ...]

Scans every entry (dex, resources, assets) for provider keys, private keys, capability
and session tokens, and for any CANARY string (e.g. the signing passwords used for the
build), which must never be packaged.
"""
from __future__ import annotations

import re
import sys
import zipfile

PATTERNS = {
    "Groq key": re.compile(rb"gsk_[A-Za-z0-9]{20,}"),
    "OpenAI-style key": re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
    "Anthropic key": re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "botay capability token": re.compile(rb"botay_cap_[0-9a-f]{32}_[A-Za-z0-9_-]{20,}"),
    "server secret variable": re.compile(rb"SEOS_(?:CREDENTIAL_KEY|ACADEMIC_FEED_KEY|PLATFORM_LLM_API_KEY)=\S"),
}
JKS_MAGIC = b"\xfe\xed\xfe\xed"


def main(path: str, *canaries: str) -> int:
    findings: list[str] = []
    with zipfile.ZipFile(path) as apk:
        for info in apk.infolist():
            if info.filename.endswith((".jks", ".keystore", ".p12")):
                findings.append(f"{info.filename}: keystore file")
            data = apk.read(info)
            if data.startswith(JKS_MAGIC):
                findings.append(f"{info.filename}: Java keystore content")
            for name, pattern in PATTERNS.items():
                if pattern.search(data):
                    findings.append(f"{info.filename}: {name}")
            for canary in canaries:
                if canary and canary.encode() in data:
                    findings.append(f"{info.filename}: build secret canary")
    if findings:
        print("\n".join(sorted(set(findings))))
        return 1
    print(f"{path}: no credential material found")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
