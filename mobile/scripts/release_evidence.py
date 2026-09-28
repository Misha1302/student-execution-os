#!/usr/bin/env python3
"""Verify a built release APK and write its provenance record.

    release_evidence.py APK --build-tools DIR --output provenance.json
        [--require-fcm] [--require-clean] [--expect-version-name V] [--expect-version-code N]
        [--canary SECRET ...]

The record links source (commit + tree) -> artifact (sha256, size, package, version) ->
signing identity (certificate SHA-256). Fails when the APK is not signed by exactly one
certificate, carries credential material (apk_secret_scan), lacks the Firebase config a
production build needs for push (--require-fcm), or the source tree has tracked changes
(--require-clean). Canary values are only searched for, never written to the record.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("apk_secret_scan", HERE / "apk_secret_scan.py")
apk_secret_scan = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(apk_secret_scan)

# apksigner labels signers "V2 Signer:" (current build-tools), "Signer #1" or
# "Signer (minSdkVersion=24, maxSdkVersion=32)"; one certificate may be listed several times.
_SIGNER = r"^((?:V[\d.]+ )?Signer\b[^:\n]*?):? certificate "
SIGNER_DIGEST = re.compile(_SIGNER + r"SHA-256 digest: ([0-9a-f]{64})\s*$", re.M)
SIGNER_DN = re.compile(_SIGNER + r"DN: (.+?)\s*$", re.M)
BADGING = re.compile(r"^package: name='([^']+)' versionCode='([^']*)' versionName='([^']*)'", re.M)
FCM_RESOURCE = re.compile(r"\bstring/google_app_id\b")


def parse_signers(apksigner_output: str) -> list[dict[str, str]]:
    names = dict(SIGNER_DN.findall(apksigner_output))
    signers: dict[str, dict[str, str]] = {}
    for label, digest in SIGNER_DIGEST.findall(apksigner_output):
        signers.setdefault(digest, {"certificate_sha256": digest, "dn": names.get(label, "")})
    return list(signers.values())


def parse_badging(aapt_output: str) -> dict[str, str]:
    match = BADGING.search(aapt_output)
    if not match:
        raise ValueError("aapt2 badging has no package line")
    return {"package": match[1], "version_code": match[2], "version_name": match[3]}


def has_fcm_config(resources_dump: str) -> bool:
    return bool(FCM_RESOURCE.search(resources_dump))


def _run(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("apk")
    parser.add_argument("--build-tools", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--require-fcm", action="store_true")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--expect-version-name")
    parser.add_argument("--expect-version-code")
    parser.add_argument("--canary", action="append", default=[])
    args = parser.parse_args(argv)
    tools = Path(args.build_tools)
    errors: list[str] = []

    verified = _run(str(tools / "apksigner"), "verify", "--verbose", "--print-certs", args.apk)
    signers = parse_signers(verified)
    if len(signers) != 1:
        print(verified)  # public certificate data only; shows why parsing disagreed
        errors.append(f"expected exactly one signing certificate, found {len(signers)}")
    badging = parse_badging(_run(str(tools / "aapt2"), "dump", "badging", args.apk))
    if args.expect_version_name and badging["version_name"] != args.expect_version_name:
        errors.append(f"versionName {badging['version_name']!r} != {args.expect_version_name!r}")
    if args.expect_version_code and badging["version_code"] != args.expect_version_code:
        errors.append(f"versionCode {badging['version_code']!r} != {args.expect_version_code!r}")
    fcm = has_fcm_config(_run(str(tools / "aapt2"), "dump", "resources", args.apk))
    if args.require_fcm and not fcm:
        errors.append("no Firebase config (string/google_app_id): push would be compiled out")
    if apk_secret_scan.main(args.apk, *args.canary) != 0:
        errors.append("credential material found in the APK")
    changes = [line for line in _run("git", "status", "--porcelain", "--untracked-files=no").splitlines() if line]
    if args.require_clean and changes:
        errors.append("source tree has tracked changes: " + ", ".join(changes))

    data = Path(args.apk).read_bytes()
    record = {
        "source": {"commit": _run("git", "rev-parse", "HEAD").strip(),
                   "tree": _run("git", "rev-parse", "HEAD^{tree}").strip(),
                   "tracked_changes": changes},
        "artifact": {"file": Path(args.apk).name, "sha256": hashlib.sha256(data).hexdigest(),
                     "size": len(data), **badging, "fcm_config": fcm},
        "signing": {"signers": signers},
    }
    Path(args.output).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    for error in errors:
        print(f"::error::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
