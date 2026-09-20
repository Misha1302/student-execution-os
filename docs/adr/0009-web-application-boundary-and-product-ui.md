# ADR 0009 — Web application boundary and product UI

- Status: Accepted for UI product slice
- Date: 2026-09-21

## Context

Passes 0–7 established canonical domain ownership, evidence/reconciliation, planning,
travel and authenticated LLM-action semantics, but the only user-facing application
surface was the CLI. A product UI needs account-scoped query models and mutations
without moving any planner, reconciliation, travel or authorization truth into a
browser client.

The approved UI design also requires a browser-visible distinction between canonical
facts and derived projections, tri-state feasibility, source conflict/override
provenance, travel occupancy and exact scoped confirmation for destructive agent
actions.

## Decision

### Thin HTTP/application façade

`student_execution_os.web.UiService` and the FastAPI host are a transport/query façade
over the existing owners. The façade may aggregate read models for a page, but it does
not reimplement:

- feasibility or witness validation;
- planning/risk;
- reconciliation/conflict policy;
- route/travel projection;
- canonical lifecycle transitions;
- LLM action authorization/idempotency.

Browser requests never supply `account_id`, principal id, or authorization scope.
Those values are bound when the server application is constructed. All canonical
mutations retain the existing expected-version owner.

PlanBlocks remain read-only derived data. The UI does not expose a PlanBlock mutation
endpoint.

### Product surfaces

The shell exposes Today, Plan, Tasks, Calendar, Evidence, Places, Ask and Settings.
Canonical Events and UserTimeConstraints are rendered independently from derived WORK,
EVENT_PROJECTION, TRAVEL_TRANSITION and BUFFER blocks. `FEASIBLE`, `INFEASIBLE` and
`UNKNOWN` remain distinct first-class states.

The Places read model deliberately omits address and coordinates. Exact location stays
behind the existing server-side privacy/grant boundary.

The Ask surface does not fabricate a live model provider. Destructive cancellation is
implemented only through the existing durable ActionIntent gateway:
preview → explicit authenticated confirmation → expected-version/idempotent execution.
Imported text is never authorization.

### Frontend delivery

The first UI is a zero-build native ES-module/CSS application served by the Python
host. This is an intentional implementation choice for this repository slice, not a
change to domain semantics.

The strongest alternative was React + TypeScript + Vite. It remains reasonable for a
larger client, but package acquisition from the npm registry was not available in the
implementation environment while a browser, FastAPI and Playwright were already
available. Shipping a package-lock/build graph that could not be reproduced locally
would have weakened the exact-head verification requirement. The native module is
small enough that the current interaction/state surface does not yet justify that
tradeoff.

If the UI grows beyond this boundary, migrating the presentation layer to a compiled
typed client is allowed by this ADR as long as the HTTP/domain ownership rules above
remain unchanged.

### Runtime and deployment boundary

FastAPI/uvicorn are now explicit runtime dependencies. The included server defaults to
loopback and refuses a non-loopback bind unless the operator supplies an explicit
flag. That flag is not a production-security claim: this slice does not add production
session authentication, TLS termination, secret storage or deployment infrastructure.

Security headers deny framing, geolocation/camera/microphone and third-party script,
style and connect origins by default.

## Verification contract

The UI slice must retain all existing backend tests and additionally cover:

- navigation and real server-backed reads;
- `FEASIBLE / INFEASIBLE / UNKNOWN` presentation primitives;
- optimistic-concurrency failure on task mutation;
- canonical versus derived timeline ownership;
- stale connector state;
- conflict plus active override provenance;
- latest-safe-departure decomposition;
- cross-account isolation at the application boundary;
- default private-location redaction;
- destructive agent preview/confirmation;
- desktop and mobile Chromium smoke;
- dense/short timeline blocks without visual overlap.

Browser fixtures are deterministic and isolated. They may contain synthetic private
addresses specifically so the tests can prove those values never appear in ordinary
UI payloads.

## Consequences

- Product UI can be used against real SQLite/domain state rather than mock product data.
- Existing domain owners remain reusable by CLI and future transports.
- The web layer now has third-party runtime dependencies, so CI installs pinned web/dev
  requirements before `make verify`.
- Production authentication/deployment remains a later hardening concern and must not be
  inferred from the local server.
