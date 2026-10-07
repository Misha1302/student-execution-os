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

Implemented: counted progress per task; checklists inside Tasks with order, optional
step effort and done share (ADR 0036), editable through the Assistant (typed
`CHECKLIST_STEP`, RU/EN phrases without a model); milestone progress and a history by
completion dates for projects; daily quotas as quantity check-ins whose remaining time at
the user's pace is reserved by the planner as derived QUOTA blocks (ADR 0034/0036).
Planned:

- A stored per-day progress series (today reconstructed).
- Quota time in the week/month outlook (today only the 36-hour plan reserves it); the
  plan's quota time follows offline quota progress only after the next online plan.

### Offline and events

- Offline attachments (online-only: the bytes need a durable owner, retry/restart, limits,
  orphan cleanup and upload idempotency before they can be queued). Class series,
  check-ins, reminder series, places, place reminders, an event's place and checklists are
  offline-first.
- Choosing one of a hybrid event's location options: only the REST endpoint exists (no
  screen yet); the screen and its offline sync operation come together. A place for
  imported class series needs a user layer over the source-owned template.

### Daily execution (open items)

- Current location from place-reminder crossings (opt-in) is not built; the planner uses
  only the user's statement with an expiry.
- Check-in history: the detail shows the last 30 days and says so; older days are kept
  but not browsable yet.

### Other open items

- Password reset/change and e-mail verification (hosted-scale: registration is open, but
  accounts are login-based and there is no e-mail channel yet).
- Per-account rate limits shared across several server processes (production runs one
  api process; per-process limits apply).
- Routing: the provider boundary and Yandex Distance Matrix adapter exist (ADR 0035);
  a live-provider check needs a key in production. OAuth connector providers in
  production.
- Automated FCM credential rotation.
