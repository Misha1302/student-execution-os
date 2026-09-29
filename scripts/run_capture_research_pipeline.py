from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts/capture-adversarial/pipeline-run.json"
ENV = {**os.environ, "PYTHONPATH": "src", "TZ": "Europe/Moscow"}

COMMANDS = [
    [sys.executable, "scripts/run_capture_adversarial.py"],
    [sys.executable, "tests/adversarial_capture/coverage_audit.py"],
    [sys.executable, "tests/adversarial_capture/property_probe.py"],
    [sys.executable, "-m", "unittest", "tests.adversarial_capture.capture_e2e_probe.CaptureAdversarialProbe.test_representative_preview_create_api_and_persistence"],
    [sys.executable, "scripts/run_capture_mutations.py"],
    [sys.executable, "tests/adversarial_capture/human_realism_review.py"],
    [sys.executable, "scripts/minimize_capture_failure.py"],
    [sys.executable, "tests/adversarial_capture/generate_report.py"],
]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for command in COMMANDS:
        started = time.monotonic()
        process = subprocess.run(command, cwd=ROOT, env=ENV, text=True, capture_output=True, timeout=900)
        result = {
            "command": command,
            "returncode": process.returncode,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout": process.stdout[-12000:],
            "stderr": process.stderr[-12000:],
        }
        results.append(result)
        payload = {
            "baseline_sha": "7ba92ae0fa99c1526b083cd6bc8afed82dc993fc",
            "research_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "environment": {"PYTHONPATH": "src", "TZ": "Europe/Moscow"},
            "result": "PASS" if all(item["returncode"] == 0 for item in results) and len(results) == len(COMMANDS) else "RUNNING_OR_FAIL",
            "commands": results,
        }
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sys.stdout.write(process.stdout)
        sys.stderr.write(process.stderr)
        if process.returncode:
            payload["result"] = "FAIL"
            OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            raise SystemExit(process.returncode)
    payload["result"] = "PASS"
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": "PASS", "commands": len(results), "artifact": str(OUT)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
