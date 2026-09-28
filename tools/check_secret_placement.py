#!/usr/bin/env python3
"""Check rendered Compose JSON for inline production secrets without printing values."""
from __future__ import annotations

import json
import sys
from typing import Any
from urllib.parse import urlsplit

ALWAYS_FILE_ONLY = {"SEOS_PLATFORM_LLM_API_KEY", "SEOS_FCM_SERVICE_ACCOUNT_JSON"}
FILE_VARIABLES = {
    "SEOS_CREDENTIAL_KEY_FILE", "SEOS_ACADEMIC_FEED_KEY_FILE",
    "SEOS_PLATFORM_LLM_API_KEY_FILES", "SEOS_FCM_SERVICE_ACCOUNT_FILE",
    "SEOS_LLM_EGRESS_PROXY_FILE", "SEOS_LLM_EGRESS_RELAY_TOKEN_FILE",
}


def check(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict) or not isinstance(config.get("services"), dict):
        raise ValueError("Compose JSON must contain a services object")
    placements: list[dict[str, str]] = []
    violations: list[dict[str, str]] = []
    for service, definition in sorted(config["services"].items()):
        environment = (definition or {}).get("environment")
        if environment is None:
            environment = {}
        if not isinstance(environment, dict):
            raise ValueError(f"service {service!r} environment must be an object")
        for name, value in sorted(environment.items()):
            value = "" if value is None else str(value)
            if name in FILE_VARIABLES and value:
                placements.append({"service": service, "name": name, "path": value})
            if name in ALWAYS_FILE_ONLY and value:
                violations.append({"service": service, "name": name,
                                   "reason": "inline secret must be empty"})
            if name == "SEOS_LLM_EGRESS_PROXY" and value:
                parsed = urlsplit(value)
                if parsed.username is not None or parsed.password is not None:
                    violations.append({"service": service, "name": name,
                                       "reason": "proxy URL contains inline userinfo"})
            if "RELAY_TOKEN" in name and not name.endswith("_FILE") and value:
                violations.append({"service": service, "name": name,
                                   "reason": "relay token must be file-backed"})
    return {"result": "OK" if not violations else "FAIL",
            "file_placements": placements, "violations": violations}


def main() -> int:
    try:
        result = check(json.load(sys.stdin))
    except (ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"result": "FAIL", "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["result"] == "OK" else 1


if __name__ == "__main__":
    sys.exit(main())
