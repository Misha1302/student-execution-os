from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from .providers import ProviderUnavailable


T = TypeVar("T")


@dataclass
class ReliabilityTrace:
    attempts: int = 0
    retries: int = 0
    repair_attempted: bool = False
    repair_succeeded: bool = False


@dataclass(frozen=True)
class ReliabilityPolicy:
    max_transient_retries: int = 1
    max_retry_after_seconds: int = 2
    base_delay_seconds: float = 0.05

    def run(
        self,
        call: Callable[[], T],
        *,
        trace: ReliabilityTrace,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> T:
        while True:
            trace.attempts += 1
            try:
                return call()
            except ProviderUnavailable as exc:
                retryable = exc.reason in {"NETWORK", "RATE_LIMITED", "UPSTREAM"}
                bounded_after = (
                    exc.retry_after is not None and exc.retry_after <= self.max_retry_after_seconds
                    if exc.reason == "RATE_LIMITED" else True
                )
                if not retryable or not bounded_after or trace.retries >= self.max_transient_retries:
                    raise
                trace.retries += 1
                delay = float(exc.retry_after) if exc.retry_after is not None else self.base_delay_seconds * (0.5 + jitter())
                if delay > 0:
                    sleep(delay)


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
