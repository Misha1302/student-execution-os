#!/usr/bin/env python3
"""Reject dependency lifecycle scripts in the production Android signing graph."""
from __future__ import annotations

import json
from pathlib import Path

lock = json.loads((Path(__file__).resolve().parents[1] / "mobile/package-lock.json").read_text())
unexpected = []
for path, package in (lock.get("packages") or {}).items():
    if path and (package.get("hasInstallScript") or package.get("scripts")):
        unexpected.append(path)
if unexpected:
    raise SystemExit("unexpected npm lifecycle scripts in signing graph: " + ", ".join(sorted(unexpected)))
print(json.dumps({"valid": True, "packages": len(lock.get("packages") or {}), "lifecycle_scripts": 0}))
