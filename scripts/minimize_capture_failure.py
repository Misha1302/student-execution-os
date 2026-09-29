from __future__ import annotations

import json
import re
from pathlib import Path

from run_capture_adversarial import norm, run


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts/capture-adversarial"
OUT = ARTIFACTS / "failure-minimization.json"


def read_jsonl(name: str) -> list[dict]:
    return [json.loads(line) for line in (ARTIFACTS / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def surface_still_expresses(case: dict, text: str) -> bool:
    """Keep the independent oracle plausible while shrinking; never infer it from parser output."""
    low = norm(text)
    expected = case["expected"]
    if len(low) < 3:
        return False
    if expected.get("title_tokens") and not all(norm(token) in low for token in expected["title_tokens"]):
        return False
    def contains_clock(value: str | None) -> bool:
        if not value:
            return True
        hh, mm = value[-5:].split(":")
        hour = str(int(hh))
        return any(form in low for form in (f"{hh} {mm}", f"{hour} {mm}", f"{hh}:{mm}", f"{hour}:{mm}", f"{hh}.{mm}", f"{hour}.{mm}"))

    def contains_date(value: str | None) -> bool:
        if not value:
            return True
        return any(cue in low for cue in ("сегодня", "завтра", "послезавтра", "сред", "пятниц", "сентябр", "today", "tomorrow", "friday"))

    def contains_minutes(value: int | None) -> bool:
        if value is None:
            return True
        return (str(value) in low
                or value == 30 and "полчас" in low
                or value == 60 and bool(re.search(r"(?:на|за|about) час|one hour", low))
                or value == 90 and "полтора часа" in low
                or value == 120 and bool(re.search(r"(?:на|за) 2 часа|two hours", low)))

    if expected["kind"] == "EVENT":
        if not re.search(r"\b(?:встреч|созвон|лекц|семинар|прием|трениров|call|meeting|lecture|seminar|appointment|workout)", low):
            return False
        if not contains_date(expected.get("starts")) or not contains_clock(expected.get("starts")):
            return False
        if not contains_minutes(expected.get("duration")):
            return False
        if expected.get("lead") is not None and not re.search(r"напом|пни|пинг|remind|reminder|заранее|до начала|before", low):
            return False
        if not contains_minutes(expected.get("lead")):
            return False
    if expected["kind"] == "TASK":
        if expected.get("deadline") and (not re.search(r"\bдо\b|due|deadline|сдать", low)
                                         or not contains_date(expected["deadline"])
                                         or not contains_clock(expected["deadline"])):
            return False
        if expected.get("effort") is not None and not contains_minutes(expected["effort"]):
            return False
        if expected.get("remind") and (not re.search(r"напом|пни|remind", low)
                                       or not contains_date(expected["remind"])
                                       or not contains_clock(expected["remind"])):
            return False
    if expected["kind"] == "REMINDER":
        if (not re.search(r"напом|пни|remind", low)
                or not contains_date(expected.get("remind"))
                or not contains_clock(expected.get("remind"))):
            return False
    if "CORRECTION" in case.get("tags", []) and not re.search(r"ой нет|нет|хотя лучше", low):
        return False
    return True


def candidates(text: str, stage: str) -> list[str]:
    if stage == "sentence_or_clause":
        parts = [p for p in re.split(r"(?<=[.!?])\s+|\n+|\s*[,;—]\s*", text) if p]
    elif stage == "fragment":
        parts = [p for p in re.split(r"\s+(?=(?:напомни|пни|ой|нет|хотя|до|завтра|сегодня|в\s+\d))", text, flags=re.I) if p]
    else:
        parts = text.split()
    if len(parts) < 2:
        return []
    out = []
    for i in range(len(parts)):
        joiner = " "
        value = joiner.join(parts[:i] + parts[i + 1:]).strip(" ,.;:—\n")
        if value and value not in out:
            out.append(value)
    return out


def minimize(case: dict, cluster: str) -> dict:
    current = case["utterance"]
    attempts = []
    for stage in ("sentence_or_clause", "fragment", "token"):
        changed = True
        while changed:
            changed = False
            for candidate in candidates(current, stage):
                if not surface_still_expresses(case, candidate):
                    attempts.append({"stage": stage, "candidate": candidate, "accepted": False, "reason": "oracle_surface_not_preserved"})
                    continue
                probe = {**case, "id": f"min-{cluster}", "utterance": candidate}
                result = run(ROOT, [probe])[0]
                accepted = result.get("cluster") == cluster
                attempts.append({"stage": stage, "candidate": candidate, "accepted": accepted,
                                 "observed_cluster": result.get("cluster")})
                if accepted:
                    current = candidate
                    changed = True
                    break
    final = run(ROOT, [{**case, "id": f"min-final-{cluster}", "utterance": current}])[0]
    return {
        "cluster": cluster,
        "severity": case.get("severity"),
        "realistic_repro": case["utterance"],
        "minimal_repro": current,
        "expected_semantics": case["expected"],
        "final_actual_python": final["actual_python"],
        "final_actual_js": final["actual_js"],
        "same_causal_cluster": final.get("cluster") == cluster,
        "attempt_count": len(attempts),
        "accepted_reductions": [a for a in attempts if a["accepted"]],
    }


def main() -> None:
    failures = read_jsonl("failures.jsonl")
    clusters = sorted({f["cluster"] for f in failures if f.get("severity") in {"P0", "P1"}})
    minimized = []
    for cluster in clusters:
        pool = [f for f in failures if f["cluster"] == cluster and f.get("source") in {"human", "seed"}]
        if not pool:
            pool = [f for f in failures if f["cluster"] == cluster and f.get("source") != "adaptive"]
        base = min(pool, key=lambda f: (0 if f.get("source") == "seed" else 1, len(f["utterance"])))
        minimized.append(minimize(base, cluster))
    payload = {
        "method": ["remove sentence/clause and rerun", "remove fragment and rerun", "remove token and rerun"],
        "acceptance": "same causal cluster plus independent-oracle surface constraints",
        "clusters": minimized,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"clusters": len(minimized), "all_preserved": all(x["same_causal_cluster"] for x in minimized)}, ensure_ascii=False))
    if not all(x["same_causal_cluster"] for x in minimized):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
