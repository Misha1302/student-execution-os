# Student Execution OS — Product roadmap

This is the canonical list of planned product capabilities that are **not**
implemented yet. Architecture decisions live in `docs/adr/`; the normative baseline
is `docs/SPECIFICATION.md`. An item moves out of this file when it ships (with an ADR
when it changes an ownership or trust boundary).

## Current model (implemented)

- The app is fully usable without AI: capture uses the deterministic RU/EN parser.
- AI is **bring your own key** (BYOK): each account adds its own OpenAI, Anthropic or
  OpenAI-compatible key in Settings → AI; the operator pays for nobody's inference
  (ADR 0017).

## Planned

### Paid / managed AI

A paid plan in which AI works without the user owning an LLM API key.

- The user buys a plan; no personal LLM key is required.
- Student Execution OS uses **platform-managed** credentials for that account.
- Credentials are assigned automatically from the plan (entitlement), never typed
  in by the user. The seam already exists: `llm_entitlements` +
  `SEOS_PLATFORM_LLM_*` resolve to `PLATFORM_MANAGED` (ADR 0017).
- Usage, quota and cost limits are tracked per account (requests, tokens, money)
  and per plan period.
- There is protection against unbounded API spend: per-account and global budget
  caps, rate limits, and a hard stop that degrades to the local parser instead of
  spending further; operator alerts before caps are reached.
- BYOK remains available alongside the paid plan, if that matches the product model
  at the time (today a user's own key takes precedence over the plan).

Prerequisites before any real account is granted platform-managed access: billing /
subscription source of truth that writes and expires entitlements, the usage ledger
and caps above, and choice of default platform model(s).

### Other open items

- Password reset/change and e-mail verification.
- Per-account rate limits shared across several server processes.
- Routing (travel time) and OAuth connector providers in production.
- Automated FCM credential rotation.
