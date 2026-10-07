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
step effort and done share (ADR 0036); milestone progress and a history by completion
dates for projects; daily quotas as quantity check-ins whose user-given pace is reserved
from the day's free time (ADR 0034/0036). Planned:

- Daily quotas placed as plan blocks (today they are reported next to capacity, not
  scheduled), and a stored per-day progress series (today reconstructed).
- Checklist editing through the Assistant.

### Offline and events

- Offline creation of class series (`series.create`) and offline attachments (today
  online-only). Check-ins, reminder series, places, place reminders, an event's place
  and checklists are offline-first (v31–v33).
- Hybrid event location-option selection offline (today an online request); a place for
  imported class series (source-owned templates).

### Daily execution (open items)

- An alarm dismissal on a medication prompt is not an outcome by design; an optional
  «Принял» on the alarm screen is not built yet.
- Current location from place-reminder crossings (opt-in) is not built; the planner uses
  only the user's statement with an expiry.

### Other open items

- Password reset/change and e-mail verification.
- Per-account rate limits shared across several server processes.
- Routing: the provider boundary and Yandex Distance Matrix adapter exist (ADR 0035);
  a live-provider check needs a key in production. OAuth connector providers in
  production.
- Automated FCM credential rotation.
