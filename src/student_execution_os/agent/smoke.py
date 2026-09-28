"""Operator smoke check of the platform-managed LLM path (``llm-smoke``).

One real ``interpret()`` probe through exactly the provider, credential pool and
egress route production uses. The report holds only classifications: never the key,
relay token, URL, prompt or provider body. The deterministic local parser is never
consulted, so a success here can only mean the provider answered with a typed action.
"""
from __future__ import annotations

import time
from typing import Any

from .providers import (
    PROBE_TEXT,
    ProviderUnavailable,
    check_probe_answer,
    platform_provider_from_environment,
    probe_context,
)


def platform_llm_smoke(provider: Any | None = None, *, now: str = "2026-01-01T09:00:00+00:00") -> dict[str, Any]:
    if provider is None:
        try:
            provider = platform_provider_from_environment()
        except Exception as exc:  # noqa: BLE001 - configuration errors are reported, not raised
            return {"result": "NOT_CONFIGURED", "detail": type(exc).__name__}
    if provider is None:
        return {"result": "NOT_CONFIGURED", "detail": "SEOS_PLATFORM_LLM_* is incomplete"}
    report: dict[str, Any] = {"provider": provider.name, "model": provider.model}
    started = time.monotonic()
    try:
        check_probe_answer(provider.interpret(PROBE_TEXT, probe_context(now)))
    except ProviderUnavailable as failure:
        report.update(result=failure.reason, http_status=failure.http_status, retry_after=failure.retry_after,
                      route=failure.route or getattr(provider, "last_route", None))
    else:
        usage = getattr(provider, "last_usage", None) or {}
        report.update(result="OK", route=getattr(provider, "last_route", None),
                      total_tokens=usage.get("total_tokens"))
    report["latency_ms"] = round((time.monotonic() - started) * 1000)
    credential = getattr(provider, "last_credential", None)
    if credential is not None:
        report["credential"] = credential
    return report
