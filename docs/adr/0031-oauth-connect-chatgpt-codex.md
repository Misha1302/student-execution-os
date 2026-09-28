# ADR 0031 — Connecting ChatGPT/Codex: OAuth consent that issues capability grants (schema v26)

**Status:** Accepted for R7

## Context

R6 (ADR 0030) gave external agents scoped capability grants and an MCP endpoint, but a
person had to copy a token by hand, and botay! had no UI for grants. ChatGPT's custom
connectors authenticate remote MCP servers with OAuth (API-key support could not be
confirmed from official documentation during this work); Codex supports a bearer token
from an environment variable and OAuth. Reimplementing agent logic for either would fork
product semantics.

## Decision

- **One credential type.** The OAuth token endpoint returns an ordinary capability
  grant token. Scopes, expiry, revocation, canonical mutation path and provenance are
  exactly R6's. No refresh tokens: when the grant expires the client asks for consent
  again.
- **MCP authorization profile:** protected-resource metadata (RFC 9728) at
  `/.well-known/oauth-protected-resource[/mcp]`, advertised in the `WWW-Authenticate`
  of every 401 from `/mcp`; authorization-server metadata (RFC 8414); dynamic client
  registration (RFC 7591) for public clients only; authorization code + PKCE **S256
  required**; redirect URIs must be `https` or loopback `http` (RFC 8252), exact match;
  `iss` returned on the redirect (RFC 9207).
- **Consent in the app.** App sessions are bearer tokens (not cookies), so
  `/oauth/authorize` validates the request, stores it for 10 minutes and redirects to
  `/#/connect/<id>`; the signed-in person sees client name, the host they return to,
  and the requested scopes, may narrow them and choose 30/90/365 days, then Allow/Deny.
  A consent route survives sign-in. An unknown client or unregistered redirect is an error
  page, never a redirect. Consent needs a session; there is no cookie to forge (no CSRF),
  and the page cannot be framed (`frame-ancestors 'none'`).
- **Codes:** hashed at rest, single use, 5-minute lifetime, bound to client,
  redirect URI and PKCE verifier. Replaying a used code revokes the grant it produced.
- **Abuse bounds:** registration (10/min/IP) and authorization starts (30/min/IP) are
  rate-limited in-process; expired requests are purged; `state` ≤ 500 characters.
- **Settings → Connected apps:** list (label, scopes, last use, expiry, status),
  disconnect, and "Create an access token" for bearer-token clients such as Codex (shown
  once, with the `config.toml` snippet). Only in session mode.
- **Lifecycle:** `oauth_clients` is global; `oauth_authorizations` reference an account
  only after consent and cascade with it. v26 rollback is lossless for user data
  (issued grants stay in v25's table; clients re-register).

## Consequences

ChatGPT and Codex use the same MCP/REST surface and the same canonical operations as
the app; nothing is specific to one vendor. Dynamic registration means any client can
*ask*; the consent screen shows the return host so a person can refuse a lookalike.
Not verified against the live ChatGPT/Codex products from this environment (outbound
access to them is blocked here); the protocol is exercised end to end by an HTTP client
plus a real browser.
