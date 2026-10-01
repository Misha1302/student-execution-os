from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Any

from student_execution_os.domain.errors import ValidationError

from .providers import ProviderUnavailable


DEFAULT_CORPUS = Path(__file__).resolve().parents[3] / "tests/fixtures/assistant_semantic_corpus_v1.json"
_TRANSPORT_REASONS = {"NETWORK", "TIMEOUT", "RATE_LIMITED", "UPSTREAM"}
_FORMAT_REASONS = {"FORMAT", "MALFORMED"}


def load_semantic_corpus(path: str | Path = DEFAULT_CORPUS) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("cases"), list):
        raise ValidationError("assistant semantic corpus must be version 1 with a cases list")
    identifiers: set[str] = set()
    for case in value["cases"]:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) \
                or not isinstance(case.get("capability"), str) or not isinstance(case.get("turns"), list) \
                or not case["turns"]:
            raise ValidationError("assistant semantic corpus case is invalid")
        if case["id"] in identifiers:
            raise ValidationError("assistant semantic corpus case ids must be unique")
        identifiers.add(case["id"])
        for turn in case["turns"]:
            if not isinstance(turn, dict) or not isinstance(turn.get("text"), str) \
                    or not turn["text"].strip() or not isinstance(turn.get("expected"), dict):
                raise ValidationError("assistant semantic corpus turn is invalid")
    return value


def _subset(expected: object, actual: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and _subset(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) <= len(actual) \
            and all(_subset(value, actual[index]) for index, value in enumerate(expected))
    return expected == actual


def _classify(expected: dict[str, Any], actual: object) -> str:
    if not isinstance(actual, dict):
        return "SCHEMA_FAILURE"
    if expected.get("kind") == "ACTION":
        actions = actual.get("actions")
        if not isinstance(actions, list) or not actions or not isinstance(actions[0], dict):
            return "SCHEMA_FAILURE"
        action = actions[0]
        if action.get("command") != expected.get("command"):
            return "SEMANTIC_MISMATCH"
        payload = action.get("payload")
        if not isinstance(payload, dict):
            return "SCHEMA_FAILURE"
        target = expected.get("target")
        if isinstance(target, dict) and not _subset(target, payload):
            return "TARGET_RESOLUTION_MISMATCH"
        if not _subset(expected.get("payload_subset", {}), payload):
            return "SEMANTIC_MISMATCH"
        return "CORRECT"
    return "CORRECT" if _subset(expected.get("output_subset", {}), actual) else "SEMANTIC_MISMATCH"


def evaluate_semantic_corpus(provider: Any, corpus: dict[str, Any], *, limit: int | None = None) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    cases = corpus["cases"][:limit] if limit is not None else corpus["cases"]
    for case in cases:
        context: dict[str, object] = {
            "now": "2026-10-02T09:00:00+03:00",
            "timezone": "Europe/Moscow",
            "locale": case.get("locale", "ru"),
            "obligations": [],
            "reminders": [],
            **(case.get("context") or {}),
        }
        previous: dict[str, Any] | None = None
        for turn_index, turn in enumerate(case["turns"], start=1):
            if previous is not None:
                context["assistant_session"] = {"previous_result": previous}
            started = perf_counter()
            try:
                actual = provider.interpret(turn["text"], context)
            except ProviderUnavailable as exc:
                classification = (
                    "TRANSPORT_FAILURE" if exc.reason in _TRANSPORT_REASONS
                    else "FORMAT_FAILURE" if exc.reason in _FORMAT_REASONS
                    else "PROVIDER_FAILURE"
                )
                actual = None
                reason = exc.reason
            except (ValidationError, ValueError, TypeError):
                classification = "SCHEMA_FAILURE"
                actual = None
                reason = "VALIDATION"
            else:
                classification = _classify(turn["expected"], actual)
                reason = None
                previous = actual
            results.append({
                "case_id": case["id"],
                "capability": case["capability"],
                "turn": turn_index,
                "classification": classification,
                "reason": reason,
                "latency_ms": round((perf_counter() - started) * 1000),
            })
    counts: dict[str, int] = {}
    for result in results:
        counts[result["classification"]] = counts.get(result["classification"], 0) + 1
    return {
        "corpus_version": corpus["version"],
        "provider": getattr(provider, "name", "unknown"),
        "model": getattr(provider, "model", None),
        "counts": counts,
        "results": results,
    }
