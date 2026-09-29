from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "artifacts/capture-adversarial"
OUT = ARTIFACTS / "coverage-audit.json"


def read_jsonl(name: str) -> list[dict]:
    path = ARTIFACTS / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def tags(case: dict) -> set[str]:
    return set(case.get("tags", []))


def has(case: dict, *wanted: str) -> bool:
    return set(wanted).issubset(tags(case))


def main() -> None:
    strict = []
    for name in ("seed_regressions.jsonl", "structured_phrases.jsonl", "human_phrases.jsonl",
                 "metamorphic_cases.jsonl", "adaptive_cases.jsonl"):
        strict.extend(read_jsonl(name))
    ambiguous = read_jsonl("ambiguous_phrases.jsonl")

    checks = {
        "EVENT_X_DATE_X_TIME": lambda c: has(c, "EVENT", "DATE", "TIME"),
        "EVENT_X_DURATION_X_REMINDER": lambda c: has(c, "EVENT", "DURATION", "REMINDER_OFFSET"),
        "EVENT_X_WORD_ORDER_X_REMINDER": lambda c: has(c, "EVENT", "WORD_ORDER", "REMINDER_OFFSET"),
        "EVENT_X_TIME_X_CORRECTION": lambda c: has(c, "EVENT", "TIME", "CORRECTION"),
        "TASK_X_DEADLINE_X_REMINDER": lambda c: has(c, "TASK", "DEADLINE", "REMINDER"),
        "TASK_X_EFFORT_X_DEADLINE": lambda c: has(c, "TASK", "EFFORT", "DEADLINE"),
        "TASK_X_DATE_X_REMINDER": lambda c: has(c, "TASK", "DATE", "REMINDER"),
        "KIND_X_DATE_X_TIME": lambda c: bool(tags(c) & {"EVENT", "TASK", "REMINDER"}) and has(c, "DATE", "TIME"),
        "REMINDER_X_OFFSET_X_WORD_ORDER": lambda c: has(c, "REMINDER_OFFSET", "WORD_ORDER"),
    }
    evidence = {}
    missing = []
    for name, predicate in checks.items():
        matches = [c["id"] for c in strict if predicate(c)]
        evidence[name] = {"count": len(matches), "sample_ids": matches[:5], "pass": bool(matches)}
        if not matches:
            missing.append(name)

    ambiguity_matches = [c["id"] for c in ambiguous
                         if any(day in c["utterance"].lower() for day in ("сегодня", "завтра", "послезавтра"))
                         and any(token in c["utterance"].lower() for token in (" в 7", " в 6", "вечером", "днем"))]
    evidence["DATE_X_TIME_X_AMBIGUITY"] = {
        "count": len(ambiguity_matches), "sample_ids": ambiguity_matches[:5], "pass": bool(ambiguity_matches),
    }
    if not ambiguity_matches:
        missing.append("DATE_X_TIME_X_AMBIGUITY")

    kind_counts = {}
    for kind in ("EVENT", "TASK", "REMINDER"):
        kind_counts[kind] = sum(c.get("intent", {}).get("kind") == kind and has(c, "DATE", "TIME") for c in strict)
    if not all(kind_counts.values()):
        if "KIND_X_DATE_X_TIME" not in missing:
            missing.append("KIND_X_DATE_X_TIME")
        evidence["KIND_X_DATE_X_TIME"]["pass"] = False
    evidence["KIND_X_DATE_X_TIME"]["kind_counts"] = kind_counts

    payload = {
        "result": "PASS" if not missing else "FAIL",
        "method": "named acceptance-combination audit over the generated intent-first corpus",
        "strict_cases_read": len(strict),
        "ambiguous_cases_read": len(ambiguous),
        "combinations": evidence,
        "missing_combinations": missing,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": payload["result"], "missing": missing}, ensure_ascii=False))
    if missing:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
