"""STARTER quota policy, atomic reservations, and provider usage reconciliation.

This is the single owner of STARTER limits.  The ledger deliberately stores only
counts and timestamps: prompts and model responses never enter usage accounting.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from student_execution_os.persistence.sqlite import SQLiteCanonicalRepository, _iso

from .providers import ProviderUnavailable, SYSTEM_PROMPT


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _positive_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class StarterQuotaPolicy:
    """All STARTER defaults and environment overrides, in one configuration owner."""

    enabled: bool = False
    period_seconds: int = 30 * 24 * 60 * 60
    account_request_limit: int = 100
    account_token_limit: int = 100_000
    global_request_limit: int = 10_000
    global_token_limit: int = 10_000_000
    max_output_tokens: int = 1_200
    token_reservation_overhead: int = 256

    @classmethod
    def from_environment(cls) -> "StarterQuotaPolicy":
        return cls(
            enabled=_enabled(os.environ.get("SEOS_STARTER_LLM_ENABLED")),
            period_seconds=_positive_env("SEOS_STARTER_LLM_PERIOD_SECONDS", cls.period_seconds),
            account_request_limit=_positive_env(
                "SEOS_STARTER_LLM_ACCOUNT_REQUEST_LIMIT", cls.account_request_limit),
            account_token_limit=_positive_env(
                "SEOS_STARTER_LLM_ACCOUNT_TOKEN_LIMIT", cls.account_token_limit),
            global_request_limit=_positive_env(
                "SEOS_STARTER_LLM_GLOBAL_REQUEST_LIMIT", cls.global_request_limit),
            global_token_limit=_positive_env(
                "SEOS_STARTER_LLM_GLOBAL_TOKEN_LIMIT", cls.global_token_limit),
            max_output_tokens=_positive_env(
                "SEOS_STARTER_LLM_MAX_OUTPUT_TOKENS", cls.max_output_tokens),
            token_reservation_overhead=_positive_env(
                "SEOS_STARTER_LLM_TOKEN_RESERVATION_OVERHEAD", cls.token_reservation_overhead),
        )

    def period(self, now: datetime) -> tuple[datetime, datetime]:
        utc = now.astimezone(timezone.utc)
        start_epoch = int(utc.timestamp()) // self.period_seconds * self.period_seconds
        start = datetime.fromtimestamp(start_epoch, timezone.utc)
        return start, start + timedelta(seconds=self.period_seconds)

    def reservation_tokens(self, text: str, context: dict[str, object]) -> int:
        # A UTF-8 byte is a conservative upper bound for a tokenizer token. Include
        # the exact semantic prompt inputs, output ceiling, and protocol overhead.
        user = json.dumps({"text": text, "context": context}, ensure_ascii=False, default=str)
        prompt_bound = len(SYSTEM_PROMPT.encode("utf-8")) + len(user.encode("utf-8"))
        return prompt_bound + self.max_output_tokens + self.token_reservation_overhead


@dataclass(frozen=True)
class StarterReservation:
    id: str
    account_id: str
    period_start: datetime
    period_end: datetime
    reserved_tokens: int


class StarterQuotaExceeded(Exception):
    """The account or global STARTER hard cap refused a reservation."""


class StarterUsageStore:
    def __init__(self, repo: SQLiteCanonicalRepository, policy: StarterQuotaPolicy | None = None) -> None:
        self.repo = repo
        self.policy = policy or StarterQuotaPolicy.from_environment()

    def backfill_entitlements(self) -> int:
        """Grant STARTER only where no entitlement exists; safe and repeatable."""
        if not self.policy.enabled:
            return 0
        now = _iso(self.repo.clock.now())
        with self.repo._tx() as conn:
            before = conn.total_changes
            conn.execute(
                "INSERT OR IGNORE INTO llm_entitlements(account_id,source,plan,granted_at,expires_at) "
                "SELECT id,'PLATFORM_MANAGED','STARTER',?,NULL FROM accounts",
                (now,),
            )
            return conn.total_changes - before

    def grant_new_account(self, conn, account_id: str, now: datetime) -> None:
        if self.policy.enabled:
            conn.execute(
                "INSERT OR IGNORE INTO llm_entitlements(account_id,source,plan,granted_at,expires_at) "
                "VALUES (?,'PLATFORM_MANAGED','STARTER',?,NULL)",
                (account_id, _iso(now)),
            )

    def reserve(self, account_id: str, tokens: int) -> StarterReservation:
        if tokens <= 0:
            raise ValueError("STARTER token reservation must be positive")
        now = self.repo.clock.now()
        start, end = self.policy.period(now)
        start_s, end_s, now_s = _iso(start), _iso(end), _iso(now)
        reservation = StarterReservation(str(uuid4()), account_id, start, end, tokens)
        with self.repo._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO starter_llm_account_usage"
                "(account_id,period_start,period_end,request_count,token_count,updated_at) VALUES (?,?,?,0,0,?)",
                (account_id, start_s, end_s, now_s),
            )
            conn.execute(
                "INSERT OR IGNORE INTO starter_llm_global_usage"
                "(period_start,period_end,request_count,token_count,updated_at) VALUES (?,?,0,0,?)",
                (start_s, end_s, now_s),
            )
            account = conn.execute(
                "SELECT request_count,token_count FROM starter_llm_account_usage "
                "WHERE account_id=? AND period_start=?", (account_id, start_s)).fetchone()
            global_row = conn.execute(
                "SELECT request_count,token_count FROM starter_llm_global_usage WHERE period_start=?",
                (start_s,),
            ).fetchone()
            if (int(account["request_count"]) + 1 > self.policy.account_request_limit
                    or int(account["token_count"]) + tokens > self.policy.account_token_limit
                    or int(global_row["request_count"]) + 1 > self.policy.global_request_limit
                    or int(global_row["token_count"]) + tokens > self.policy.global_token_limit):
                raise StarterQuotaExceeded("STARTER quota is exhausted")
            conn.execute(
                "UPDATE starter_llm_account_usage SET request_count=request_count+1,token_count=token_count+?,"
                "updated_at=? WHERE account_id=? AND period_start=?",
                (tokens, now_s, account_id, start_s),
            )
            conn.execute(
                "UPDATE starter_llm_global_usage SET request_count=request_count+1,token_count=token_count+?,"
                "updated_at=? WHERE period_start=?", (tokens, now_s, start_s),
            )
            conn.execute(
                "INSERT INTO starter_llm_reservations"
                "(id,account_id,period_start,period_end,reserved_tokens,status,created_at) "
                "VALUES (?,?,?,?,?,'RESERVED',?)",
                (reservation.id, account_id, start_s, end_s, tokens, now_s),
            )
        return reservation

    def reconcile(self, reservation: StarterReservation, usage: dict[str, int] | None) -> None:
        """Replace the conservative token charge with provider-reported usage.

        If no trustworthy usage is available the reservation remains fully charged
        but is marked reconciled; that fail-closed behavior prevents both an
        unmetered spend path and a misleading forever-in-flight row.
        """
        prompt = None if usage is None else max(0, int(usage.get("prompt_tokens", 0)))
        completion = None if usage is None else max(0, int(usage.get("completion_tokens", 0)))
        total = None if usage is None else max(
            prompt + completion, int(usage.get("total_tokens", prompt + completion)))
        # The request was sent with a completion ceiling and a conservative prompt
        # bound. Never allow an anomalous provider counter to increase past the hard
        # cap that was reserved before the request.
        charged = reservation.reserved_tokens if total is None else min(total, reservation.reserved_tokens)
        delta = charged - reservation.reserved_tokens
        now_s, start_s = _iso(self.repo.clock.now()), _iso(reservation.period_start)
        with self.repo._tx() as conn:
            row = conn.execute(
                "SELECT status FROM starter_llm_reservations WHERE id=? AND account_id=?",
                (reservation.id, reservation.account_id),
            ).fetchone()
            if row is None or row["status"] != "RESERVED":
                return
            conn.execute(
                "UPDATE starter_llm_account_usage SET token_count=token_count+?,updated_at=? "
                "WHERE account_id=? AND period_start=?",
                (delta, now_s, reservation.account_id, start_s),
            )
            conn.execute(
                "UPDATE starter_llm_global_usage SET token_count=token_count+?,updated_at=? WHERE period_start=?",
                (delta, now_s, start_s),
            )
            conn.execute(
                "UPDATE starter_llm_reservations SET prompt_tokens=?,completion_tokens=?,total_tokens=?,"
                "status='RECONCILED',reconciled_at=? WHERE id=?",
                (prompt, completion, total, now_s, reservation.id),
            )

    def public_quota(self, account_id: str) -> dict[str, Any]:
        now = self.repo.clock.now()
        start, end = self.policy.period(now)
        row = self.repo.connection.execute(
            "SELECT request_count,token_count FROM starter_llm_account_usage "
            "WHERE account_id=? AND period_start=?", (account_id, _iso(start))).fetchone()
        requests = int(row["request_count"]) if row else 0
        tokens = int(row["token_count"]) if row else 0
        return {
            "requests_limit": self.policy.account_request_limit,
            "requests_remaining": max(0, self.policy.account_request_limit - requests),
            "tokens_limit": self.policy.account_token_limit,
            "tokens_remaining": max(0, self.policy.account_token_limit - tokens),
            "resets_at": _iso(end),
        }


# Provider answers that refuse a request before any generation: nothing was spent.
_NOT_GENERATED_STATUSES = frozenset({401, 402, 403, 404, 405, 413, 415, 429})
# Failures raised before the request left this server (configuration, SSRF refusal).
_NEVER_SENT_REASONS = frozenset({"REQUEST", "BLOCKED_URL"})
_NO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _definitely_not_generated(failure: ProviderUnavailable) -> bool:
    """True only when the provider cannot have produced (and billed) a completion.

    Timeouts, network errors after connect, 5xx and 400-class answers (Groq returns
    400 ``json_validate_failed`` *after* generating) stay fully charged: fail closed.
    """
    return failure.reason in _NEVER_SENT_REASONS or failure.http_status in _NOT_GENERATED_STATUSES


class MeteredStarterProvider:
    """Quota-protected PLATFORM_MANAGED provider; one reservation per attempt.

    The request counter is never refunded (it bounds attempts against the operator's
    credential); the token charge is released only for a definite no-generation
    refusal, so an operator-side outage such as SERVER_BLOCKED does not drain every
    student's STARTER token budget.
    """

    def __init__(self, provider: Any, usage: StarterUsageStore, account_id: str) -> None:
        self._provider = provider
        self._usage = usage
        self._account_id = account_id
        self.name = provider.name
        self.model = provider.model

    @property
    def last_usage(self) -> dict[str, int] | None:
        return getattr(self._provider, "last_usage", None)

    @property
    def last_route(self) -> str | None:
        return getattr(self._provider, "last_route", None)

    def _invoke(self, call, text: str, context: dict[str, object]):
        tokens = self._usage.policy.reservation_tokens(text, context)
        try:
            reservation = self._usage.reserve(self._account_id, tokens)
        except StarterQuotaExceeded:
            raise ProviderUnavailable("STARTER quota is exhausted", "STARTER_QUOTA") from None
        try:
            result = call()
        except ProviderUnavailable as failure:
            usage = getattr(self._provider, "last_usage", None)
            self._usage.reconcile(reservation, _NO_USAGE if _definitely_not_generated(failure) else usage)
            raise
        except BaseException:
            self._usage.reconcile(reservation, getattr(self._provider, "last_usage", None))
            raise
        self._usage.reconcile(reservation, getattr(self._provider, "last_usage", None))
        return result

    def interpret(self, text: str, context: dict[str, object]):
        return self._invoke(lambda: self._provider.interpret(text, context), text, context)

    def repair(self, text: str, context: dict[str, object], feedback: str):
        repair = getattr(self._provider, "repair", None)
        if not callable(repair):
            raise ProviderUnavailable("assistant provider cannot repair structured output", "FORMAT")
        return self._invoke(lambda: repair(text, context, feedback), text, context)
