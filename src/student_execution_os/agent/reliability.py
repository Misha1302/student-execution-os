from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from .providers import CALL_DEADLINE, ProviderUnavailable


T = TypeVar("T")


@dataclass
class ReliabilityTrace:
    attempts: int = 0
    retries: int = 0
    repair_attempted: bool = False
    repair_succeeded: bool = False
    repair_reason: str | None = None
    # Monotonic deadline of the whole operation (set on the first attempt).
    deadline: float | None = None
    # Why the last failure was not retried (see ``retry_decision``), for metrics.
    stop_reason: str | None = None


# Retry decisions. Every failed attempt gets exactly one of these codes.
RETRY = "RETRY"
NOT_TRANSIENT = "NOT_TRANSIENT"            # auth, quota, format, model missing, ... — retrying cannot help
UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"        # delivered, no answer: a paid generation may already have run
PROVIDER_SAID_NO = "PROVIDER_SAID_NO"      # explicit x-should-retry: false
RETRY_AFTER_TOO_LONG = "RETRY_AFTER_TOO_LONG"
RETRIES_EXHAUSTED = "RETRIES_EXHAUSTED"
ATTEMPTS_EXHAUSTED = "ATTEMPTS_EXHAUSTED"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"


@dataclass(frozen=True)
class ReliabilityPolicy:
    """The one retry policy for language-model calls of an Assistant operation.

    * Only a failure that cannot have produced a (billed) generation is retried:
      the request never reached the provider (NOT_SENT), or the provider itself
      answered "rate limited" / "temporarily failing" (ANSWERED 429/5xx). A read
      timeout, a connection dropped after sending or a gateway 502/504 has an
      UNKNOWN outcome and is never re-sent; the caller degrades to the local
      parser instead (no blind duplicate paid generation).
    * The provider's explicit ``x-should-retry: false`` always wins.
    * At most ``max_transient_retries`` retries and ``max_attempts`` provider calls
      per operation (the structured-output repair call included), all inside one
      ``total_budget_seconds``; each call's own timeout is cut to what is left, and
      a retry is skipped when less than ``min_attempt_seconds`` remain.
    * A Retry-After is honoured only up to ``max_retry_after_seconds`` and only
      when it fits the remaining budget.
    * httpx does no retries of its own and the egress relay does not retry, so
      there is no nested retry amplification. A BYOK failure never switches to the
      managed provider (SQLiteAssistantService only falls back to the local parser).
    """

    max_transient_retries: int = 1
    # Groq free-tier TPM pressure commonly returns an explicit 10-20s Retry-After.
    # One provider-advised wait is cheaper and more truthful than silently replacing
    # the neural interpretation with the deterministic parser. The 45s operation
    # budget and one-retry cap still bound latency and prevent retry storms.
    max_retry_after_seconds: int = 30
    base_delay_seconds: float = 0.05
    max_attempts: int = 3
    total_budget_seconds: float = 45.0
    min_attempt_seconds: float = 3.0

    def retry_decision(self, exc: ProviderUnavailable, trace: ReliabilityTrace, remaining: float) -> str:
        if exc.reason == "TIMEOUT" and exc.delivery == "NOT_SENT":
            return BUDGET_EXHAUSTED  # refused locally: no time left for another call
        if exc.should_retry is False:
            return PROVIDER_SAID_NO
        if exc.delivery == "UNKNOWN" or exc.reason == "TIMEOUT":
            return UNKNOWN_OUTCOME
        if exc.reason == "NETWORK" and exc.delivery != "NOT_SENT":
            return UNKNOWN_OUTCOME
        if exc.reason not in {"NETWORK", "RATE_LIMITED", "UPSTREAM"}:
            return NOT_TRANSIENT
        if exc.reason == "RATE_LIMITED" and (
            exc.retry_after is None or exc.retry_after > self.max_retry_after_seconds
        ):
            # Without a short explicit Retry-After the wait is unknown: degrade instead.
            return RETRY_AFTER_TOO_LONG
        if trace.retries >= self.max_transient_retries:
            return RETRIES_EXHAUSTED
        if trace.attempts >= self.max_attempts:
            return ATTEMPTS_EXHAUSTED
        wait = float(exc.retry_after or 0)
        if remaining - wait < self.min_attempt_seconds:
            return BUDGET_EXHAUSTED
        return RETRY

    def run(
        self,
        call: Callable[[], T],
        *,
        trace: ReliabilityTrace,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> T:
        if trace.deadline is None:
            trace.deadline = clock() + self.total_budget_seconds
        while True:
            remaining = trace.deadline - clock()
            if trace.attempts >= self.max_attempts or remaining <= 0:
                trace.stop_reason = ATTEMPTS_EXHAUSTED if trace.attempts >= self.max_attempts else BUDGET_EXHAUSTED
                raise ProviderUnavailable("the assistant retry budget is exhausted", "TIMEOUT", delivery="NOT_SENT")
            trace.attempts += 1
            token = CALL_DEADLINE.set(trace.deadline)
            try:
                result = call()
                trace.stop_reason = None
                return result
            except ProviderUnavailable as exc:
                decision = self.retry_decision(exc, trace, trace.deadline - clock())
                if decision != RETRY:
                    trace.stop_reason = decision
                    raise
                trace.retries += 1
                delay = float(exc.retry_after) if exc.retry_after is not None else self.base_delay_seconds * (0.5 + jitter())
                if delay > 0:
                    sleep(delay)
            finally:
                CALL_DEADLINE.reset(token)


def sanitized_validation_feedback(exc: Exception) -> str:
    message = str(exc).casefold()
    if "unsupported field" in message:
        return "UNKNOWN_FIELD"
    if "lacks" in message or "required" in message:
        return "MISSING_REQUIRED_FIELD"
    if "enum" in message:
        return "WRONG_ENUM"
    if "time" in message or "instant" in message or "temporal" in message or "deadline" in message:
        return "INVALID_TEMPORAL_REPRESENTATION"
    if "unknown obligation" in message or "target" in message or "address" in message:
        return "TARGET_RESOLUTION"
    if isinstance(exc, ProviderUnavailable) and exc.reason == "FORMAT":
        return "JSON_SCHEMA"
    return "ACTION_SCHEMA"
