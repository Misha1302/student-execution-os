#!/usr/bin/env python3
"""Create, verify, and control signed Student Execution OS update policies.

The private Ed25519 seed is read from ``SEOS_UPDATE_SIGNING_KEY_B64`` or an
explicit protected file. It is never accepted as a command-line value (which
would leak through process listings and CI logs).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from student_execution_os.updates import (  # noqa: E402
    ArtifactKind,
    MandatoryMode,
    MandatoryPolicy,
    MinimumOsVersions,
    ReleaseNotes,
    ReleaseSeverity,
    ReleaseStatus,
    RollbackCompatibility,
    Rollout,
    SemVer,
    SignatureMetadata,
    UpdateArtifact,
    UpdateChannel,
    UpdatePolicy,
    UpdateRelease,
    UpdaterMetadata,
    sign_policy,
    verify_policy,
)
from student_execution_os.updates.signing import load_private_key  # noqa: E402


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def private_key(args):
    if getattr(args, "private_key_file", None):
        raw = Path(args.private_key_file).read_bytes()
    else:
        raw = os.environ.get("SEOS_UPDATE_SIGNING_KEY_B64", "").encode()
    if not raw.strip():
        raise SystemExit("missing Ed25519 signing key: set SEOS_UPDATE_SIGNING_KEY_B64 or --private-key-file")
    return load_private_key(raw)


def trusted_keys(args) -> dict[str, str]:
    if getattr(args, "trusted_keys_file", None):
        raw = Path(args.trusted_keys_file).read_text(encoding="utf-8")
    else:
        raw = os.environ.get("SEOS_UPDATE_TRUST_KEYS_JSON", "")
    if not raw:
        raise SystemExit("missing trusted public keys: set SEOS_UPDATE_TRUST_KEYS_JSON or --trusted-keys-file")
    value = json.loads(raw)
    if not isinstance(value, dict) or not value or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise SystemExit("trusted keys must be a non-empty JSON object of key_id -> base64 public key")
    return value


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def artifact_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            size += len(block)
            digest.update(block)
    if not size:
        raise SystemExit("artifact cannot be empty")
    return size, digest.hexdigest()


def create(args) -> int:
    artifact_path = Path(args.artifact)
    size, sha256 = artifact_digest(artifact_path)
    generated = utcnow()
    mandatory = MandatoryMode(args.mandatory)
    required_after = None
    if mandatory is MandatoryMode.REQUIRED_AFTER:
        if not args.required_after:
            raise SystemExit("--required-after is required for REQUIRED_AFTER")
        required_after = datetime.fromisoformat(args.required_after.replace("Z", "+00:00"))
    release = UpdateRelease(
        version=SemVer.parse(args.version), build_number=args.build_number,
        channel=UpdateChannel(args.channel), published_at=generated,
        status=ReleaseStatus(args.status), severity=ReleaseSeverity(args.severity),
        release_notes={
            "en": ReleaseNotes(args.summary_en, tuple(args.change_en)),
            "ru": ReleaseNotes(args.summary_ru, tuple(args.change_ru)),
        },
        minimum_os_versions=MinimumOsVersions(args.min_android_sdk),
        minimum_api_version=args.minimum_api_version,
        mandatory_policy=MandatoryPolicy(mandatory, required_after),
        rollback_compatibility=RollbackCompatibility(args.rollback_compatibility),
        artifacts=(UpdateArtifact(
            platform="android", architecture=args.architecture, artifact_kind=ArtifactKind.APK,
            url=args.artifact_url, size_bytes=size, sha256=sha256,
            updater_metadata=UpdaterMetadata(args.package_name, args.build_number, args.min_android_sdk),
        ),),
    )
    policy = UpdatePolicy(
        schema_version=1, sequence=args.sequence, generated_at=generated,
        expires_at=generated + timedelta(hours=args.expires_hours), channel=UpdateChannel(args.channel),
        latest_version=release.version,
        minimum_supported_version=SemVer.parse(args.minimum_supported_version) if args.minimum_supported_version else None,
        rollout=Rollout(args.rollout), releases=(release,),
        signature_metadata=SignatureMetadata(args.key_id),
    )
    signed = sign_policy(policy, private_key(args))
    atomic_write(Path(args.output), signed.signed_json())
    print(json.dumps({
        "policy": str(Path(args.output)), "sequence": signed.sequence, "channel": signed.channel,
        "version": str(release.version), "build_number": release.build_number,
        "artifact": artifact_path.name, "size_bytes": size, "sha256": sha256,
        "key_id": signed.signature_metadata.key_id,
    }, sort_keys=True))
    return 0


def verify(args) -> int:
    policy = UpdatePolicy.from_json(Path(args.policy).read_text(encoding="utf-8"))
    verify_policy(policy, trusted_keys(args))
    if not args.allow_expired and policy.expires_at <= utcnow():
        raise SystemExit("policy signature is valid but metadata is expired")
    release = policy.latest_release()
    print(json.dumps({
        "valid": True, "sequence": policy.sequence, "channel": policy.channel,
        "version": str(release.version), "status": release.status,
        "rollout": policy.rollout.percentage, "key_id": policy.signature_metadata.key_id,
    }, sort_keys=True))
    return 0


def control(args) -> int:
    current = UpdatePolicy.from_json(Path(args.policy).read_text(encoding="utf-8"))
    verify_policy(current, trusted_keys(args))
    if args.sequence <= current.sequence:
        raise SystemExit(f"new sequence must be greater than {current.sequence}")
    release = current.latest_release()
    release = replace(
        release,
        status=ReleaseStatus(args.status) if args.status else release.status,
    )
    generated = utcnow()
    updated = replace(
        current, sequence=args.sequence, generated_at=generated,
        expires_at=generated + timedelta(hours=args.expires_hours),
        rollout=Rollout(args.rollout) if args.rollout is not None else current.rollout,
        releases=tuple(release if item.release_id == release.release_id else item for item in current.releases),
        signature_metadata=SignatureMetadata(args.key_id),
    )
    signed = sign_policy(updated, private_key(args))
    atomic_write(Path(args.output), signed.signed_json())
    print(json.dumps({"sequence": signed.sequence, "status": release.status, "rollout": signed.rollout.percentage}, sort_keys=True))
    return 0


def common_key(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--private-key-file", help="protected Ed25519 seed/base64 or PEM file")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    make = commands.add_parser("create", help="hash final APK, create and sign policy")
    make.add_argument("--artifact", required=True)
    make.add_argument("--artifact-url", required=True)
    make.add_argument("--output", required=True)
    make.add_argument("--version", required=True)
    make.add_argument("--build-number", required=True, type=int)
    make.add_argument("--sequence", required=True, type=int)
    make.add_argument("--channel", choices=[item.value for item in UpdateChannel], required=True)
    make.add_argument("--status", choices=[item.value for item in ReleaseStatus], default="AVAILABLE")
    make.add_argument("--severity", choices=[item.value for item in ReleaseSeverity], default="NORMAL")
    make.add_argument("--rollout", type=int, default=5)
    make.add_argument("--mandatory", choices=[item.value for item in MandatoryMode], default="OPTIONAL")
    make.add_argument("--required-after")
    make.add_argument("--minimum-supported-version")
    make.add_argument("--minimum-api-version", type=int)
    make.add_argument("--rollback-compatibility", choices=[item.value for item in RollbackCompatibility], default="BINARY_ONLY")
    make.add_argument("--architecture", choices=("universal", "arm64", "x64"), default="universal")
    make.add_argument("--package-name", default="io.github.misha1302.seos")
    make.add_argument("--min-android-sdk", type=int, default=24)
    make.add_argument("--expires-hours", type=int, default=168)
    make.add_argument("--summary-en", required=True)
    make.add_argument("--summary-ru", required=True)
    make.add_argument("--change-en", action="append", required=True)
    make.add_argument("--change-ru", action="append", required=True)
    common_key(make)
    make.set_defaults(func=create)

    check = commands.add_parser("verify", help="verify signature, schema and freshness")
    check.add_argument("--policy", required=True)
    check.add_argument("--trusted-keys-file")
    check.add_argument("--allow-expired", action="store_true")
    check.set_defaults(func=verify)

    ctl = commands.add_parser("control", help="pause/withdraw/resume or change rollout without a binary")
    ctl.add_argument("--policy", required=True)
    ctl.add_argument("--output", required=True)
    ctl.add_argument("--sequence", required=True, type=int)
    ctl.add_argument("--status", choices=[item.value for item in ReleaseStatus])
    ctl.add_argument("--rollout", type=int)
    ctl.add_argument("--expires-hours", type=int, default=168)
    ctl.add_argument("--trusted-keys-file")
    common_key(ctl)
    ctl.set_defaults(func=control)
    return root


if __name__ == "__main__":
    arguments = parser().parse_args()
    raise SystemExit(arguments.func(arguments))
