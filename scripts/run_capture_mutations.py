from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / "src/student_execution_os/agent/nlparse.py"
JS = ROOT / "src/student_execution_os/web/static/js/nlparse.js"
CAPTURE = ROOT / "src/student_execution_os/web/static/js/capture.js"
EVENTS = ROOT / "src/student_execution_os/web/static/js/events.js"
OUT = ROOT / "artifacts/capture-adversarial/mutation-testing.json"

PY_TEST = [sys.executable, "-m", "unittest", "tests.unit.test_nl_capture"]
JS_TEST = ["node", "tests/js/capture_kind_cases.mjs"]
BOUNDARY_TEST = ["node", "tests/adversarial_capture/baseline_boundary_cases.mjs"]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_mutant(name: str, path: Path, old: str, new: str, command: list[str]) -> dict[str, object]:
    original = path.read_text(encoding="utf-8")
    if old not in original:
        return {"name": name, "status": "NOT_APPLIED", "reason": "anchor missing"}
    before = digest(path)
    mutated = original.replace(old, new, 1)
    path.write_text(mutated, encoding="utf-8")
    try:
        proc = subprocess.run(
            command,
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": "src", "TZ": "Europe/Moscow"},
            text=True,
            capture_output=True,
            timeout=180,
        )
        status = "SURVIVED" if proc.returncode == 0 else "KILLED"
        excerpt = (proc.stdout + "\n" + proc.stderr)[-4000:]
        return {
            "name": name,
            "status": status,
            "returncode": proc.returncode,
            "command": command,
            "output_excerpt": excerpt,
        }
    finally:
        path.write_text(original, encoding="utf-8")
        if digest(path) != before:
            raise RuntimeError(f"{path} was not restored after {name}")


def main() -> None:
    results = [
        run_mutant(
            "PY_EVENT_REMINDER_GUARD",
            PY,
            "if remind_spans or has_deadline_words or not re.search(_EVENT_WORDS, parser.low) or re.search(_PREPARE_WORDS, parser.low):",
            "if has_deadline_words or not re.search(_EVENT_WORDS, parser.low) or re.search(_PREPARE_WORDS, parser.low):",
            PY_TEST,
        ),
        run_mutant(
            "PY_REMOVE_CALL_EVENT_SIGNAL",
            PY,
            "|созвон\\w*|",
            "|созвон___\\w*|",
            PY_TEST,
        ),
        run_mutant(
            "PY_DEFAULT_EVENT_DURATION_60_TO_61",
            PY,
            "default = 90 if re.search(_LONG_EVENT_WORDS, parser.low) else 60",
            "default = 90 if re.search(_LONG_EVENT_WORDS, parser.low) else 61",
            PY_TEST,
        ),
        run_mutant(
            "JS_EVENT_REMINDER_GUARD",
            JS,
            "if (remindSpans.length || hasDeadlineWords || !rx(EVENT_WORDS).test(parser.low) || rx(PREPARE_WORDS).test(parser.low)) return null;",
            "if (hasDeadlineWords || !rx(EVENT_WORDS).test(parser.low) || rx(PREPARE_WORDS).test(parser.low)) return null;",
            JS_TEST,
        ),
        run_mutant(
            "JS_REMOVE_CALL_EVENT_SIGNAL",
            JS,
            "|созвон\\\\w*|",
            "|созвон___\\\\w*|",
            JS_TEST,
        ),
        run_mutant(
            "JS_DEFAULT_EVENT_DURATION_60_TO_61",
            JS,
            "const fallback = rx(LONG_EVENT_WORDS).test(parser.low) ? 90 : 60;",
            "const fallback = rx(LONG_EVENT_WORDS).test(parser.low) ? 90 : 61;",
            JS_TEST,
        ),
        run_mutant(
            "PY_DEADLINE_ROLE_BECOMES_REMINDER",
            PY,
            '"deadline": "actual_cutoff", "remind": "remind_at", "start": "actionable_from"',
            '"deadline": "remind_at", "remind": "remind_at", "start": "actionable_from"',
            PY_TEST,
        ),
        run_mutant(
            "JS_DEADLINE_ROLE_BECOMES_REMINDER",
            JS,
            "deadline: 'actual_cutoff', remind: 'remind_at', start: 'actionable_from'",
            "deadline: 'remind_at', remind: 'remind_at', start: 'actionable_from'",
            JS_TEST,
        ),
        run_mutant(
            "JS_ACTION_VERB_KIND_SIGNAL_REMOVED",
            JS,
            "return dated || ACTION_VERB.test(text) ? 'TASK' : 'NOTE';",
            "return dated ? 'TASK' : 'NOTE';",
            JS_TEST,
        ),
        run_mutant(
            "JS_ARBITRARY_EVENT_REMINDER_OFFSET_DROPPED",
            EVENTS,
            "if (fields.remind_before_minutes != null) payload.remind_before_minutes = fields.remind_before_minutes;",
            "if (fields.remind_before_minutes != null && fields.remind_before_minutes !== 50) payload.remind_before_minutes = fields.remind_before_minutes;",
            BOUNDARY_TEST,
        ),
        run_mutant(
            "JS_MANUAL_REMINDER_TIME_OVERRIDE_REMOVED",
            CAPTURE,
            "if (old.whenChosen) next.remind_at = old.remind_at;",
            "if (false && old.whenChosen) next.remind_at = old.remind_at;",
            BOUNDARY_TEST,
        ),
        run_mutant(
            "JS_MANUAL_REMINDER_DELIVERY_OVERRIDE_REMOVED",
            CAPTURE,
            "if (old.deliveryChosen) next.delivery = old.delivery;",
            "if (false && old.deliveryChosen) next.delivery = old.delivery;",
            BOUNDARY_TEST,
        ),
    ]
    payload = {
        "baseline_sha": "7ba92ae0fa99c1526b083cd6bc8afed82dc993fc",
        "scope": "targeted natural-capture parser, kind, reminder-role, offset and merge mutation testing",
        "results": results,
        "counts": {
            "killed": sum(x["status"] == "KILLED" for x in results),
            "survived": sum(x["status"] == "SURVIVED" for x in results),
            "not_applied": sum(x["status"] == "NOT_APPLIED" for x in results),
        },
        "restored": True,
        "interpretation": "A surviving mutant is a coverage gap, not evidence that the mutation is correct.",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
