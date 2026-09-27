# Student Execution OS — Product roadmap

This is the canonical list of planned product capabilities that are **not**
implemented yet. Architecture decisions live in `docs/adr/`; the normative baseline
is `docs/SPECIFICATION.md`. An item moves out of this file when it ships (with an ADR
when it changes an ownership or trust boundary).

## Current model (implemented)

- The app is fully usable without AI: capture uses the deterministic RU/EN parser.
- AI is **bring your own key** (BYOK): each account adds its own OpenAI, Anthropic or
  OpenAI-compatible key in Settings → AI.
- Opt-in STARTER platform AI is shipped in schema v21: new/existing accounts receive
  the non-destructive `STARTER` entitlement when enabled, with atomic per-account and
  global request/token caps. BYOK always wins and consumes no STARTER quota; exhausted
  or unavailable STARTER falls back to the local parser (ADR 0017).

## Planned

### Paid managed AI (not implemented)

A paid plan in which AI works without the user owning an LLM API key.

- The user buys a plan; no personal LLM key is required.
- Student Execution OS would use **platform-managed** credentials for that account.
- Credentials are assigned automatically from the plan (entitlement), never typed
  in by the user. The seam already exists: `llm_entitlements` +
  `SEOS_PLATFORM_LLM_*` resolve to `PLATFORM_MANAGED` (ADR 0017); STARTER already
  exercises this seam without claiming billing.
- Reuse the shipped request/token ledger and hard caps, then add monetary accounting,
  billing reconciliation, subscription lifecycle, operator alerts, and plan-specific
  policy.
- BYOK remains available alongside the paid plan, if that matches the product model
  at the time (today a user's own key takes precedence over the plan).

Prerequisites for a paid tier: billing/subscription source of truth that writes and
expires entitlements, monetary reconciliation, plan policy, and operator alerts.

### Progress beyond time

Implemented: counted progress per task (x of y with a unit, percentage; remaining time
shrinks proportionally). Planned:

- Subtasks / checklists with their own effort and order, progress = done share.
- Milestone-based progress for long projects (per-milestone deadlines already exist in
  the domain) and progress history charts.
- Recurring "N units per day" goals (reading, problem sets) planned as daily quotas.

### Offline and events

- Offline creation of recurring series and offline attachments (today online-only).
- A native (SQLite) store for the Android queue instead of WebView localStorage.
- Event location/travel choices and hybrid selection offline.

### Other open items

- Password reset/change and e-mail verification.
- Per-account rate limits shared across several server processes.
- Routing (travel time) and OAuth connector providers in production.
- Automated FCM credential rotation.
