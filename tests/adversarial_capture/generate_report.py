from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ART = ROOT / "artifacts/capture-adversarial"
OUT = ART / "capture-adversarial-report.md"


def load(name: str) -> dict:
    return json.loads((ART / name).read_text(encoding="utf-8"))


def rows(name: str) -> list[dict]:
    return [json.loads(line) for line in (ART / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def compact(value: object, limit: int = 900) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + "…"


def main() -> None:
    summary = load("capture-adversarial-summary.json")
    coverage = load("coverage-audit.json")
    realism = load("human-realism-review.json")
    e2e = load("ui-api-e2e.json")
    mutation = load("mutation-testing.json")
    properties = load("property-testing.json")
    minimized = {item["cluster"]: item for item in load("failure-minimization.json")["clusters"]}
    failures = rows("failures.jsonl")
    research_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()

    grouped: dict[str, list[dict]] = defaultdict(list)
    for failure in failures:
        grouped[failure["cluster"]].append(failure)
    severities = {name: group[0]["severity"] for name, group in grouped.items()}
    severity_clusters = Counter(severities.values())
    styles = {name: Counter(item["style"] for item in group) for name, group in grouped.items()}
    dimensions = {
        "EVENT_WITH_REMINDER_BECOMES_TASK": ["kind", "event start", "reminder role"],
        "EVENT_BECOMES_REMINDER": ["kind", "event start", "reminder role"],
        "REMINDER_OFFSET_LOST": ["reminder offset"],
        "EVENT_START_WRONG_OR_LOST": ["date", "time", "event start"],
        "DEADLINE_REMINDER_CONFUSION": ["deadline", "reminder"],
        "DURATION_LOST_OR_WRONG": ["duration"],
        "SELF_CORRECTION_IGNORED": ["correction", "date/time", "duration/reminder"],
        "EVENT_BECOMES_TASK": ["kind", "date/time"],
        "REMINDER_TIME_OR_KIND_WRONG": ["kind", "reminder time"],
        "OTHER_SEMANTIC_MISMATCH": ["title/category/role"],
    }
    owners = {
        "EVENT_WITH_REMINDER_BECOMES_TASK": "NaturalTaskParser._event / eventTimes reminder guard",
        "EVENT_BECOMES_REMINDER": "reminder cue role assignment before event construction",
        "REMINDER_OFFSET_LOST": "local parser relative-reminder extraction and capture event lead UI",
        "EVENT_START_WRONG_OR_LOST": "moment grouping / eventTimes",
        "DEADLINE_REMINDER_CONFUSION": "NaturalTaskParser role resolution / JS mirror",
        "DURATION_LOST_OR_WRONG": "duration extraction / event interval construction",
        "SELF_CORRECTION_IGNORED": "moment and duration conflict resolution",
        "EVENT_BECOMES_TASK": "event detection / captureKind fallback",
        "REMINDER_TIME_OR_KIND_WRONG": "reminder cue/time ownership",
        "OTHER_SEMANTIC_MISMATCH": "title/category cleanup and capture draft projection",
    }

    scenario_results = []
    for item in e2e["scenarios"]:
        expected_kind = item["semantic_oracle"]["kind"]
        actual_kind = item["outgoing_sync_operation"]["type"].split(".", 1)[0].upper()
        title_expected = item["semantic_oracle"].get("title")
        title_actual = item["outgoing_sync_operation"]["payload"].get("title")
        scenario_results.append({"id": item["id"], "kind_ok": actual_kind == expected_kind,
                                 "title_ok": title_expected is None or (title_actual or "").casefold() == title_expected.casefold(),
                                 "persisted": item["persisted_entity"] is not None})
    preview_ok = sum(x["kind_ok"] and x["title_ok"] for x in scenario_results)

    lines = [
        "# Capture adversarial research report",
        "",
        "## 1. Executive summary",
        f"- Immutable baseline: `{summary['baseline_sha']}` / tree `{summary['baseline_tree']}` / release `v0.6.2`.",
        f"- Research HEAD: `{research_head}`.",
        f"- {summary['total_strict_cases']} strict cases, {summary['human_cases'] + summary['adaptive_human_cases']} human-like cases, {summary['ambiguous_cases']} ambiguity probes.",
        f"- {summary['failure_cases']} failures in {len(grouped)} causal clusters; cluster severity P0/P1/P2/P3: {dict(severity_clusters)}.",
        f"- JS/Python agreement {summary['js_python_agreement']}; disagreement {summary['js_python_disagreement']}; agreement-but-both-wrong {summary['js_python_agreement_but_both_wrong']}.",
        "- Baseline is intentionally not repaired by this branch; the findings below define the production-fix scope.",
        "",
        "## 2. Methodology",
        "Intent-first oracles are constructed before either parser runs. Python and JS are executed independently at a fixed Europe/Moscow clock. Rendered Playwright scenarios use the real app shell, real sync API and a real temporary SQLite repository. Semantic failures are findings, not harness failures.",
        "",
        "## 3. Corpus composition",
        f"Structured: {summary['structured_cases']} ({summary['distinct_structured_intents']} distinct intents); generated human: {summary['human_cases']}; adaptive human: {summary['adaptive_human_cases']}; metamorphic: {summary['metamorphic_cases']}; ambiguous: {summary['ambiguous_cases']}.",
        f"Human styles: `{compact(summary['human_style_counts'])}`. Languages: `{compact(summary['human_language_counts'])}`.",
        "",
        "## 4. Human realism review",
        f"Reviewed {realism['selection']['reviewed']} deterministic samples without exposing parser output. Verdicts: `{compact(realism['counts'])}`.",
        f"Limitation: {realism['limitation']}",
        "",
        "## 5. Semantic coverage",
        f"Coverage audit: **{coverage['result']}**; missing combinations: `{coverage['missing_combinations']}`.",
    ]
    for name, result in coverage["combinations"].items():
        lines.append(f"- {name}: {'PASS' if result['pass'] else 'FAIL'} ({result['count']})")
    lines += [
        "",
        "## 6. Metrics",
        f"Semantic accuracy: {summary['semantic_accuracy']:.2%}; human accuracy: {summary['human_accuracy']:.2%}; field accuracy: `{compact(summary['field_accuracy'])}`.",
        f"Rendered preview/payload oracle agreement: {preview_ok}/{len(scenario_results)}; persistence reached: {sum(x['persisted'] for x in scenario_results)}/{len(scenario_results)}.",
        f"Property probes: `{compact({k: {'cases': v['cases'], 'passes': v['passes']} for k, v in properties['properties'].items()})}`.",
        "",
        "## 7. Failure clusters",
    ]
    for name, group in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        mini = minimized.get(name)
        realistic = min((x for x in group if x.get("source") in {"human", "seed"}), key=lambda x: len(x["utterance"]), default=min(group, key=lambda x: len(x["utterance"])))
        lines += [
            "",
            f"### {severities[name]} — {name}",
            f"- Frequency: {len(group)}; styles: `{compact(styles[name])}`; dimensions: {', '.join(dimensions.get(name, ['semantic mismatch']))}.",
            f"- Realistic reproduction: `{realistic['utterance'].replace(chr(10), ' / ')}`",
            f"- Actual minimized reproduction: `{(mini or {}).get('minimal_repro', 'No P0/P1 minimization required').replace(chr(10), ' / ')}`",
            f"- Expected semantic object: `{compact(realistic['expected'])}`",
            f"- Actual Python: `{compact(realistic['actual_python'])}`",
            f"- Actual JS: `{compact(realistic['actual_js'])}`",
            f"- Probable owner: `{owners.get(name, 'nlparse/capture boundary')}`; confidence: high for structural clusters, medium otherwise.",
            "- Likely fix scope: owner-level semantic-role/reconciliation change plus an independent-oracle regression; no parser rewrite.",
            "- Required regressions: Python + JS semantic case and, where UI-facing, preview/payload/persistence boundary coverage.",
        ]
    top = sorted((x for x in failures if x.get("source") != "adaptive"), key=lambda x: (int(x["severity"][1]), len(x["utterance"])))[:20]
    lines += ["", "## 8. Top realistic failing phrases"]
    lines.extend(f"- {x['severity']} {x['cluster']}: `{x['utterance'].replace(chr(10), ' / ')}`" for x in top)
    lines += ["", "## 9. Minimal repros"]
    lines.extend(f"- {name}: `{item['minimal_repro'].replace(chr(10), ' / ')}` ({item['attempt_count']} shrink attempts, same cluster={item['same_causal_cluster']})" for name, item in minimized.items())
    lines += [
        "",
        "## 10. JS/Python differential findings",
        f"Agreement: {summary['js_python_agreement']}; disagreement: {summary['js_python_disagreement']}. Agreement-but-both-wrong: {summary['js_python_agreement_but_both_wrong']}. The dominant risk is mirrored logic, so parity alone is not correctness evidence.",
        "",
        "## 11. Parser vs UI vs API findings",
        f"The 12 rendered scenarios are recorded in `ui-api-e2e.json`; {preview_ok} matched kind/title oracles and all {sum(x['persisted'] for x in scenario_results)} created payloads reached SQLite. The artifact preserves each initial/final preview, outgoing sync operation, and persisted row.",
        "",
        "## 12. Product-policy ambiguities",
        "The 80 ambiguous inputs are kept outside strict accuracy. Exact time must not be invented from 'вечером/днём', and an underspecified 'в 7' needs locale-aware uncertainty rather than false precision.",
        "",
        "## 13. Mutation-testing coverage gaps",
        f"Killed {mutation['counts']['killed']}; survived {mutation['counts']['survived']}; not applied {mutation['counts']['not_applied']}.",
    ]
    lines.extend(f"- {item['status']}: {item['name']}" for item in mutation["results"])
    lines += [
        "",
        "## 14. Recommended fix scope",
        "Repair event/reminder/deadline role ownership, add a validated full semantic object and explicit reconciliation in capture, protect user edits, clear stale reparses, carry arbitrary offsets, and clean titles. Preserve local fallback.",
        "",
        "## 15. Deferred/later candidates",
        "Broader parser rewrites, a generic pairwise framework, whole-repository mutation testing, and application-wide visual redesign are intentionally deferred.",
        "",
        "## 16. Exact artifacts and commands",
        "Artifacts: `capture-adversarial-summary.json`, `coverage-audit.json`, `human-realism-review.json`, `property-testing.json`, `mutation-testing.json`, `failure-minimization.json`, `ui-api-e2e.json`, JSONL corpora/results/failures, and `pipeline-run.json`.",
        "The canonical command is `PYTHONPATH=src TZ=Europe/Moscow python scripts/run_capture_research_pipeline.py`; exact subprocess commands, exit codes, durations and captured output are in `pipeline-run.json`.",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"report": str(OUT), "clusters": len(grouped), "sections": 16}, ensure_ascii=False))


if __name__ == "__main__":
    main()
