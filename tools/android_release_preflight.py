#!/usr/bin/env python3
"""Fail deterministic Android release errors before immutable publication."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from student_execution_os.updates import SemVer, UpdateChannel, UpdatePolicy, verify_policy  # noqa: E402
from student_execution_os.updates.signing import load_private_key, public_key_b64  # noqa: E402


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"release preflight failed: {message}")


def digest(path: Path) -> tuple[int, str]:
    value = path.read_bytes()
    if not value:
        fail("APK is empty")
    return len(value), hashlib.sha256(value).hexdigest()


def load_json(path: str) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read JSON {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"{path} must contain a JSON object")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--provenance", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-number", required=True, type=int)
    parser.add_argument("--channel", choices=[item.value for item in UpdateChannel], required=True)
    parser.add_argument("--policy-sequence", required=True, type=int)
    parser.add_argument("--policy", action="append", default=[])
    parser.add_argument("--published-build", action="append", default=[], type=int)
    parser.add_argument("--current-channel-policy")
    parser.add_argument("--existing-release")
    parser.add_argument("--existing-provenance")
    parser.add_argument("--expected-signer-sha256")
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--mandatory", choices=("OPTIONAL", "REQUIRED_AFTER", "UNSUPPORTED_CLIENT"), required=True)
    parser.add_argument("--required-after", default="")
    parser.add_argument("--minimum-supported-version", default="")
    parser.add_argument("--summary-en", required=True)
    parser.add_argument("--summary-ru", required=True)
    parser.add_argument("--change-en", required=True)
    parser.add_argument("--change-ru", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_sha):
        fail("source SHA must be an exact 40-character commit id")
    version = SemVer.parse(args.version)
    if args.build_number <= 0 or args.policy_sequence <= 0:
        fail("build number and policy sequence must be positive")
    if args.channel == "STABLE" and version.is_prerelease:
        fail("stable cannot publish a prerelease")
    for label, value in (("summary-en", args.summary_en), ("summary-ru", args.summary_ru),
                         ("change-en", args.change_en), ("change-ru", args.change_ru)):
        if not value.strip() or len(value) > 500:
            fail(f"{label} must contain 1..500 characters")
    if (args.mandatory == "REQUIRED_AFTER") != bool(args.required_after):
        fail("required_after must be present only for REQUIRED_AFTER")
    if args.required_after:
        try:
            parsed = datetime.fromisoformat(args.required_after.replace("Z", "+00:00"))
        except ValueError as exc:
            fail(f"required_after is invalid: {exc}")
        if parsed.tzinfo is None:
            fail("required_after must include a timezone")
    if args.minimum_supported_version and SemVer.parse(args.minimum_supported_version) > version:
        fail("minimum supported version cannot be newer than the release")

    trusted_raw = os.environ.get("SEOS_UPDATE_TRUST_KEYS_JSON", "")
    signing_raw = os.environ.get("SEOS_UPDATE_SIGNING_KEY_B64", "")
    try:
        trusted = json.loads(trusted_raw)
    except json.JSONDecodeError as exc:
        fail(f"trusted-key configuration is invalid JSON: {exc}")
    if not isinstance(trusted, dict) or args.key_id not in trusted:
        fail("signing key id is absent from trusted-key configuration")
    if not signing_raw:
        fail("production policy signing key is missing")
    if public_key_b64(load_private_key(signing_raw)) != trusted[args.key_id]:
        fail("policy signing key does not match the configured trusted public key")

    policies: list[UpdatePolicy] = []
    for path in args.policy:
        policy = UpdatePolicy.from_json(Path(path).read_bytes())
        verify_policy(policy, trusted)  # authenticity only: an expired predecessor is valid here
        policies.append(policy)
    max_build = max(
        [release.build_number for policy in policies for release in policy.releases]
        + list(args.published_build), default=0,
    )
    if args.build_number <= max_build:
        fail(f"build number {args.build_number} must be greater than published maximum {max_build}")

    if args.current_channel_policy:
        current = UpdatePolicy.from_json(Path(args.current_channel_policy).read_bytes())
        verify_policy(current, trusted)
        if current.channel.value != args.channel:
            fail("current predecessor channel is wrong")
        if args.policy_sequence <= current.sequence:
            fail(f"policy sequence {args.policy_sequence} must be greater than {current.sequence}")
        if args.channel == "STABLE" and not version > current.latest_release().version:
            fail("stable SemVer must increase; same-SemVer binary revisions are unsupported")

    artifact = Path(args.artifact)
    size, sha256 = digest(artifact)
    provenance = load_json(args.provenance)
    source = provenance.get("source") or {}
    evidence = provenance.get("artifact") or {}
    if source.get("commit") != args.source_sha:
        fail("provenance source SHA differs from requested source")
    if source.get("tracked_changes") != []:
        fail("provenance reports tracked source changes")
    expected_tree = subprocess.run(
        ["git", "rev-parse", f"{args.source_sha}^{{tree}}"], cwd=ROOT,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    if source.get("tree") != expected_tree:
        fail("provenance source tree differs from requested source")
    expected = {
        "file": artifact.name, "sha256": sha256, "size": size,
        "version_name": args.version, "version_code": str(args.build_number),
        "package": "io.github.misha1302.seos", "fcm_config": True,
    }
    for key, value in expected.items():
        if evidence.get(key) != value:
            fail(f"provenance artifact.{key} differs from the candidate APK")
    signers = (provenance.get("signing") or {}).get("signers")
    if not isinstance(signers, list) or len(signers) != 1 or not re.fullmatch(r"[0-9a-f]{64}", str(signers[0].get("certificate_sha256", ""))):
        fail("provenance must contain exactly one valid APK signer identity")
    if args.expected_signer_sha256 and signers[0]["certificate_sha256"] != args.expected_signer_sha256:
        fail("APK signer differs from the established production signing identity")

    if bool(args.existing_release) != bool(args.existing_provenance):
        fail("an existing release must be checked together with its published provenance")
    if args.existing_release:
        release = load_json(args.existing_release)
        if release.get("tag_name") != f"v{args.version}" or release.get("target_commitish") != args.source_sha:
            fail("existing immutable release identity differs from the request")
        if release.get("immutable") is not True or release.get("draft") is not False:
            fail("existing release is not a published immutable release")
        names = sorted(str(item.get("name")) for item in release.get("assets") or [])
        if names != sorted([artifact.name, "provenance.json"]):
            fail(f"existing release assets differ from the exact publication: {names}")
        # Resume is exact or nothing: the published provenance must be these bytes,
        # which binds source SHA/tree, APK SHA-256, size, versionCode and signer.
        if Path(args.existing_provenance).read_bytes() != Path(args.provenance).read_bytes():
            fail("existing release provenance differs from this candidate; never clobber, publish a new SemVer")

    print(json.dumps({"valid": True, "max_published_build": max_build,
                      "version": args.version, "build_number": args.build_number,
                      "policy_sequence": args.policy_sequence, "artifact_sha256": sha256,
                      "artifact_size": size}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
