#!/usr/bin/env python3
"""Create and validate an external release-candidate evidence record.

Live release evidence must describe an already-frozen source revision.  This tool writes
the record wherever the operator asks; the release process deliberately keeps the live
record outside the Git worktree and publishes it as an immutable GitHub Release asset.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SHA = re.compile(r"^[0-9a-f]{40}$")
RFC3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
GATES = tuple(f"G{number}" for number in range(1, 14))
STATUSES = {"VERIFIED", "NOT_VERIFIED"}
FORBIDDEN_KEYS = {
    "password", "secret", "secret_value", "api_key", "private_key", "bearer_token",
    "access_token", "refresh_token", "calendar_url", "subscription_url",
}
SECRET_SHAPES = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\b(?:gsk_|sk-proj-)[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}", re.I),
)


def now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_record(release_sha: str, release_tree: str, operator: str) -> dict[str, Any]:
    timestamp = now_utc()
    return {
        "schema_version": 1,
        "release_sha": release_sha,
        "release_tree": release_tree,
        "created_at": timestamp,
        "operator": operator,
        "gates": [
            {
                "gate": gate,
                "release_sha": release_sha,
                "timestamp": timestamp,
                "operator": operator,
                "environment": "",
                "action": "",
                "safe_result": {},
                "artifact_identity": [],
                "status": "NOT_VERIFIED",
                "notes": "",
                "blocker": "not run",
            }
            for gate in GATES
        ],
    }


def _is_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not RFC3339_UTC.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _check_secret_safety(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in FORBIDDEN_KEYS:
                errors.append(f"{path}.{key}: secret-bearing field name is forbidden")
            _check_secret_safety(child, f"{path}.{key}", errors)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _check_secret_safety(child, f"{path}[{index}]", errors)
    elif isinstance(value, str):
        for pattern in SECRET_SHAPES:
            if pattern.search(value):
                errors.append(f"{path}: value resembles secret material")
                break


def validate(record: Any, *, expect_sha: str | None = None, expect_tree: str | None = None,
             require_all_verified: bool = False) -> list[str]:
    errors: list[str] = []
    if not isinstance(record, dict):
        return ["record must be a JSON object"]
    if record.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    release_sha = record.get("release_sha")
    release_tree = record.get("release_tree")
    if not isinstance(release_sha, str) or not SHA.fullmatch(release_sha):
        errors.append("release_sha must be a lowercase 40-character Git SHA")
    if not isinstance(release_tree, str) or not SHA.fullmatch(release_tree):
        errors.append("release_tree must be a lowercase 40-character Git tree SHA")
    if expect_sha and release_sha != expect_sha:
        errors.append(f"release_sha {release_sha!r} does not match expected SHA {expect_sha}")
    if expect_tree and release_tree != expect_tree:
        errors.append(f"release_tree {release_tree!r} does not match expected tree {expect_tree}")
    if not _is_timestamp(record.get("created_at")):
        errors.append("created_at must be a valid UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)")
    if not isinstance(record.get("operator"), str) or not record["operator"].strip():
        errors.append("operator must be a non-empty string")

    gates = record.get("gates")
    if not isinstance(gates, list):
        errors.append("gates must be a list")
        gates = []
    seen: set[str] = set()
    required_strings = ("operator", "environment", "action", "notes", "blocker")
    for index, gate_record in enumerate(gates):
        path = f"gates[{index}]"
        if not isinstance(gate_record, dict):
            errors.append(f"{path} must be an object")
            continue
        gate = gate_record.get("gate")
        if gate not in GATES:
            errors.append(f"{path}.gate must be one of {', '.join(GATES)}")
        elif gate in seen:
            errors.append(f"{path}.gate duplicates {gate}")
        else:
            seen.add(gate)
        if gate_record.get("release_sha") != release_sha:
            errors.append(f"{path}.release_sha does not match the frozen release_sha")
        if not _is_timestamp(gate_record.get("timestamp")):
            errors.append(f"{path}.timestamp must be a valid UTC timestamp")
        for field in required_strings:
            if not isinstance(gate_record.get(field), str):
                errors.append(f"{path}.{field} must be a string")
        if not isinstance(gate_record.get("safe_result"), dict):
            errors.append(f"{path}.safe_result must be an object")
        artifacts = gate_record.get("artifact_identity")
        if not isinstance(artifacts, list) or any(not isinstance(item, str) for item in artifacts):
            errors.append(f"{path}.artifact_identity must be a list of strings")
        status = gate_record.get("status")
        if status not in STATUSES:
            errors.append(f"{path}.status must be VERIFIED or NOT_VERIFIED")
        if status == "VERIFIED":
            for field in ("operator", "environment", "action"):
                if not isinstance(gate_record.get(field), str) or not gate_record[field].strip():
                    errors.append(f"{path}.{field} must be non-empty when VERIFIED")
            if gate_record.get("blocker"):
                errors.append(f"{path}.blocker must be empty when VERIFIED")
        elif status == "NOT_VERIFIED" and not gate_record.get("blocker"):
            errors.append(f"{path}.blocker must explain why the gate is NOT_VERIFIED")
        if require_all_verified and status != "VERIFIED":
            errors.append(f"{path}: {gate or '?'} is not VERIFIED")

    missing = set(GATES) - seen
    if missing:
        errors.append("missing gates: " + ", ".join(sorted(missing, key=lambda item: int(item[1:]))))
    if len(gates) != len(GATES):
        errors.append(f"gates must contain exactly {len(GATES)} entries")
    _check_secret_safety(record, "record", errors)
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("new", help="write an unverified G1-G13 evidence template")
    create.add_argument("--release-sha", required=True)
    create.add_argument("--release-tree", required=True)
    create.add_argument("--operator", required=True)
    create.add_argument("--output", required=True)
    check = subparsers.add_parser("validate", help="validate a completed evidence record")
    check.add_argument("record")
    check.add_argument("--expect-sha")
    check.add_argument("--expect-tree")
    check.add_argument("--require-all-verified", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "new":
        record = new_record(args.release_sha, args.release_tree, args.operator)
        errors = validate(record, expect_sha=args.release_sha, expect_tree=args.release_tree)
        if errors:
            for error in errors:
                print(f"error: {error}", file=sys.stderr)
            return 1
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        print(output)
        return 0

    record_path = Path(args.record)
    try:
        record = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read evidence record: {exc}", file=sys.stderr)
        return 1
    errors = validate(record, expect_sha=args.expect_sha, expect_tree=args.expect_tree,
                      require_all_verified=args.require_all_verified)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"result": "OK", "release_sha": record["release_sha"],
                      "release_tree": record["release_tree"], "gates": len(record["gates"])},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
