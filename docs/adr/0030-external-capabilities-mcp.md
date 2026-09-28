# ADR 0030 — External capability grants, REST surface and MCP (schema v25)

**Status:** Accepted for R6

## Context

External agents (an MCP client, ChatGPT, Codex) should be able to read a student's
botay! state and make changes for them. The product already has exactly one canonical
mutation path — typed operations applied by `SyncService` behind `/api/v1/sync` with
client `op_id` exactly-once semantics, a savepoint per operation, lifecycle-intent
conflicts and account-scoped entity lookup — and one set of read models (`UiService`).
Session tokens grant everything the app can do, so handing one to an agent is not
acceptable.

## Decision

- **Grant** (`capability_grants`, schema v25): account-bound bearer credential with a
  label, an explicit scope set, optional expiry (≤ 366 days) and revocation. The token
  `botay_cap_<id>_<secret>` is returned once; only SHA-256(secret) is stored and compared
  in constant time. Unknown, malformed, revoked and expired tokens get the same
  `401 INVALID_GRANT`. At most 20 active grants per account.
- **Separation:** grants are created, listed and revoked only with a user session
  (`/api/v1/settings/capabilities`); a grant token is not a session and a session token is
  not a grant. A grant cannot mint grants.
- **Scopes (least authority):** `today:read`, `tasks:read`, `calendar:read`,
  `notes:read`, `reminders:read`; `tasks:write`, `events:write`, `notes:write`,
  `reminders:write`, `schedule:write`; `destructive` (required in addition for any
  `*.delete`). Operation types are mapped to scopes explicitly and **denied by default**:
  projects, routines, constraints, calibration, execution sessions, series
  creation/splitting, holidays and transcripts are not available to grants.
- **Reads** call the existing `UiService` queries. Composite views withhold parts owned
  by a missing scope (`today.inbox_notes` without `notes:read`, listed in `withheld`).
- **Mutations** are authorized for the whole batch first (one denied operation → `403`,
  nothing applied, no `op_id` recorded), then applied by `UiService.sync(actor=
  USER_VIA_LLM)` — the same code the app and its offline outbox use. Provenance:
  `client_operations.principal_id = grant:<id>`, audit actor `USER_VIA_LLM`. Replaying an
  `op_id` returns the recorded result (also across the app and the agent);
  reusing it for different content is `OP_ID_REUSED`.
- **Surfaces:** REST `/api/v1/ext/*` and MCP at `/mcp` (Streamable HTTP, stateless JSON
  mode; protocol 2025-06-18, also 2025-03-26 / 2024-11-05; no server-initiated stream,
  no batches). `tools/list` shows only tools the grant can use. Denials and validation
  failures are tool results with `isError: true` so the model can explain them.
  Convenience creates (`create_task`, `create_note`) derive the entity id from `op_id`,
  so a retried call is the same entity.
- **Lifecycle:** grants are account credentials: purged on account deletion, never
  exported. v25 rollback fails closed while any grant is unrevoked.

## Consequences

No parallel store and no direct database write path for integrations. Conflicts follow
the canonical model (field-level last-writer-wins for edits; lifecycle operations that
disagree with the current state return `CONFLICT`). Not included: OAuth
authorization-code flow for third-party apps (tokens are pasted by the user), per-grant
rate limiting (the in-process limiter is not shared across processes), and a management
UI (added with the ChatGPT/Codex integration stage).
