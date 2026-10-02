# ADR 0029 — Production LLM path: classification, egress determinism, STARTER accounting

**Status:** Accepted for R5

## Context

ADR 0017 and PRs #27/#28 built the AI path: `USER_BYOK > PLATFORM_MANAGED STARTER >
deterministic local parser` (`agent/credentials.py::resolve`), encrypted per-account
keys, platform primary/standby key files, STARTER reservations/reconciliation
(`agent/usage.py`), and operator egress (host-scoped proxy, Tor/Privoxy, Squid,
Cloudflare Groq relay). R5 reviewed that path against the production shape (Groq
rejecting the VPS, reached through the relay) and found defects in the existing owners,
not missing subsystems:

1. Groq 429 bodies (`code: rate_limit_exceeded`) end with an upsell link to
   `…/settings/billing`; keyword matching classified every Groq rate limit as `QUOTA`.
   That made the BYOK key status stick at `QUOTA` and failed the platform primary over
   to the standby on a transient limit.
2. Groq `model_decommissioned` (400) was `REJECTED`, not `NOT_FOUND`.
3. `MeteredStarterProvider` charged the full conservative reservation on every failure,
   so a `SERVER_BLOCKED` outage drained each student's token budget without a
   generation.
4. The direct route used `httpx.post` with `trust_env=True`: an ambient `HTTPS_PROXY`
   silently changed the egress route of platform and BYOK traffic.
5. A user-supplied OpenAI-compatible address had the same validate-then-re-resolve DNS
   rebinding window fixed for calendars in R4.1, and `is_global` accepted
   NAT64/IPv4-compatible forms of private addresses.
6. No sanitized record of which route a request took; no operator probe of the live path.

## Decision

- `_reason`: a 429 mentioning `rate_limit` (and not `insufficient_quota`) is
  `RATE_LIMITED`; `decommissioned` models are `NOT_FOUND`. `Retry-After` is parsed
  (seconds or HTTP-date, bounded to 1 h) into `ProviderUnavailable.retry_after` and
  returned as `retry_after_seconds` by interpret and the connection test.
- Standby failover stays exactly `AUTH`/`QUOTA` (credential-specific). Transient
  reliability retries stay on the same credential and remain bounded: at most one retry
  for a request operation, and a `RATE_LIMITED` response is retried only when it carries
  an explicit `Retry-After` of at most 30 seconds that fits the 45-second operation budget.
- STARTER: the request counter is never refunded; the token charge is released only
  when the provider certainly generated nothing (401/402/403/404/405/413/415/429, or a
  `REQUEST`/`BLOCKED_URL` failure raised before sending). Timeouts, post-connect network
  errors, 5xx and 400 answers stay fully charged (Groq returns 400
  `json_validate_failed` after generating).
- Egress: one deterministic route per request — `RELAY`/`PROXY` for operator-listed
  exact hosts (both for one host still fails closed with `REQUEST`), otherwise `DIRECT`
  with `trust_env=False`; a user-supplied address is `DIRECT_PINNED` through
  `netguard.PinnedTransport` (shared with the calendar reader). The relay/proxy
  endpoints are operator configuration and are not pinned.
- `student_execution_os.llm` logs one line per request: provider id, route, result
  class, HTTP status, latency. Never URL, key, relay token, prompt or body.
- `llm-smoke` (CLI) runs one real probe through the platform pool and route and prints
  only classifications; it never consults the local parser.
- The deterministic local parser remains the degradation path; every degraded answer
  is labelled `engine: LOCAL` with `fallback_reason`, never as AI.

## Consequences

Relay trust boundary is unchanged (exact path, POST only, hard-coded upstream, token,
1 MiB bound, no redirects, allow-listed response headers, `no-store`), with added
tests for path confusion, streamed oversize bodies and response-header filtering.
A BYOK key that fails keeps precedence (the user chose it): capture degrades to the
local parser and never spends STARTER silently. Live Groq verification needs the
production API container (`llm-smoke`); a sandbox without the platform key or egress
to `api.groq.com` cannot provide it.
