# ADR 0015 — Hosted session authentication and mobile-first client

- Status: Accepted
- Date: 2026-09-24

## Context

ADR 0009 shipped a desktop-oriented browser shell on a loopback-only host whose account is
bound by the operator at start-up. The product is used mostly from a phone, and the next
step is an installable Android app talking to a server on a (not yet chosen) public
domain. That requires:

- an authentication boundary so the host can leave loopback;
- a phone-first interaction model;
- a way to package the client as an APK without forking the UI.

## Decision

### Two host modes

`create_app` runs in exactly one mode:

- **Bound mode** (`account_id=` / `--account`): unchanged ADR 0009 behaviour. One
  operator-chosen account, no login, loopback-only unless explicitly overridden.
- **Session mode** (`auth=AuthConfig(...)`, default when `--account` is omitted): users
  register/log in; every request resolves its account from an opaque bearer session.

In both modes the browser never supplies `account_id` or principal ids; `UiService` is
constructed per request from the session in session mode. Domain owners, expected-version
checks and the ActionIntent gateway are untouched.

### Credentials

Schema v10 adds `auth_users` (login, scrypt password hash) and `auth_sessions` (SHA-256 of
a 256-bit random token, 30-day sliding expiry, revocation on logout). Raw tokens and
passwords are never stored. Login attempts are rate-limited in-process per client IP and
per login; unknown logins spend the same hashing cost as wrong passwords.

The data-lifecycle contract classifies both tables as **account credential tables**: they
are purged by account deletion (which therefore invalidates every session) and are
**excluded** from the user export contract. Full-database backups still contain the
hashes and remain operator artifacts.

Account deletion in session mode may be confirmed by typing the login; the host maps it to
the session's account id before calling the unchanged deletion owner.

### Transport

Tokens travel only in the `Authorization` header — no cookies, so no CSRF surface.
CORS is enabled only in session mode, for the Capacitor WebView origins
(`https://localhost`, `http://localhost`, `capacitor://localhost`) plus operator-configured
origins. TLS is terminated by a reverse proxy (`deploy/` ships Docker + Caddy); the host can
trust forwarded headers from configured proxy addresses.

### Client

The zero-build ES-module client (ADR 0009) is rewritten mobile-first and split into modules
(`js/api.js`, `js/views/*`, `js/i18n/{ru,en}.js`, …):

- bottom tab bar Today · Plan · (+) · Tasks · More; on ≥900px it becomes a side rail;
- bottom sheets for details and forms, Android back-button handling, pull-to-refresh;
- RU/EN dictionaries with a runtime switch; enum/reason codes are localised with a readable
  fallback;
- read-model responses are cached per server+account and shown with an explicit offline
  label when the network is unavailable; mutations always require the server;
- the CSP forbids inline styles, so dynamic sizes are applied through CSSOM.

Semantics preserved from ADR 0009: FEASIBLE/INFEASIBLE/UNKNOWN stay distinct; canonical
facts and derived projections keep separate labels and colours; PlanBlocks are read-only;
destructive agent cancellation goes through preview → explicit confirmation; place
payloads stay alias-only.

New user-facing mutation: "log progress" lowers `remaining_effort_minutes` through the
existing version-checked task update and offers completion at zero.

### Android packaging

`mobile/` wraps the same static files with Capacitor 8. `scripts/sync-web.mjs` copies them
with the host's layout and optionally presets the server URL (`SEOS_SERVER_URL`). The APK
therefore contains no server logic. Debug builds permit cleartext HTTP (LAN development);
release builds allow HTTPS only via `network_security_config.xml`. Because some WebViews
receive Capacitor's native bridge as an inline script, only the bundled copy relaxes
`script-src` to allow inline scripts; the Python host keeps the strict header CSP.

### Fix discovered on the way

`UiService`'s default wall clock now truncates to whole minutes. With seconds present the
planner (correctly) refused sub-minute instants and every live plan was UNKNOWN
(`UNSUPPORTED_SUB_MINUTE_TIME`); fixture-driven tests had masked this.

## Alternatives considered

- **React/TypeScript + bundler**: npm is now reachable, but the UI remains small enough for
  native modules, and keeping zero-build lets the Python host serve the exact APK bundle.
- **Native Kotlin/Compose app**: best platform feel, but a second UI codebase to keep
  semantically aligned with the web shell.
- **Static operator token instead of accounts**: simpler, but the user chose multi-user
  registration.

## Consequences / open items

- Password change/reset, e-mail verification and multi-process rate limiting are not
  implemented.
- The in-app reminder channel (push/local notifications) is not wired to the notification
  outbox yet.
- Production claims still require the external items listed in the handoff (secret
  management, provider integrations, monitoring).
