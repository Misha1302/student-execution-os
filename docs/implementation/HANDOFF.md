# Student Execution OS — Implementation Handoff

> Checkpoint only. Re-read current repository, specification, PR/branch state, exact commit refs and CI before treating this file as current truth.

## Identity

- Repository: `Misha1302/student-execution-os`
- Baseline: post-AT-63 merge commit `64ea121c98d6cc521fc581b99fb5a2fa1086456d`
- Baseline tree: `4e248cd059df91c4c08b8d2aeb500df0bb07d53c`
- Normative specification: v2.1
- Current Pass 9 branch: `impl/pass-9-notification-outbox`
- Schema candidate: v9
- Package candidate: `0.6.0.dev3`
- Date: 2026-09-21

## Completed passes

- [x] Pass 0–7 — executable core through travel-aware planning
- [x] Product UI stage — PR #10 merged; post-merge CI green
- [x] Pass 8 — recurrence / notification workflow; PR #11 merged; post-merge CI green
- [x] Pass 9 recovery/export — PR #12 merged; post-merge CI green
- [x] Pass 9 account deletion/retention — PR #13 merged; post-merge CI #104 green
- [ ] Pass 9 durable notification delivery — current candidate
- [x] Pass 9 AT-64 cancellation/reopen projection cleanup — current candidate
- [ ] Pass 9 remaining domain/planner conformance — AT-65 / AT-66
- [ ] Pass 10 — final closure and production-boundary documentation

## Current candidate — durable notification delivery

The notification workflow remains the owner of notification truth. Schema v9 adds `notification_delivery_outbox` as durable sender state, not a second source of Task/Event truth.

- Delivery has explicit `READY | LEASED | RETRY_WAIT | SENT | SUPPRESSED | DEAD` state.
- A lease is committed before the external channel call.
- Worker crash before completion leaves a durable lease; after expiry a later worker can reclaim the same notification.
- Reclaim preserves the same stable `delivery_key` and increments durable attempt state.
- Failed sends use bounded exponential retry/backoff and survive repository restart.
- Stale domain/plan revisions and failed completion prerequisites are suppressed before a channel call.
- Snooze is rejected while an active delivery lease exists; outside a lease it reschedules the same logical notification/outbox rather than creating a duplicate.
- Delivery success updates notification + outbox atomically.
- The contract is intentionally **at-least-once** at the external boundary. A crash after a provider accepts a message but before local commit can cause a retry. Channels should use the stable `delivery_key` as their provider-side idempotency key where supported; exactly-once is not claimed.
- Data lifecycle classification includes the outbox, so backup/restore, account export and deletion remain fail-closed as schema evolves.

### Verification checkpoint

Hosted push run #108 on intermediate exact head `ac390830b50dde2b593310903759d376ff4a89e9` is fully green across core, API, Chromium and all pre-existing smoke surfaces. New acceptance tests specifically prove:

- expired lease reclaim after restart with the same delivery key;
- retry/backoff state survives restart;
- stale notification is suppressed before channel invocation;
- active delivery lease blocks snooze;
- v8 → v9 migration preserves notification state and adds the outbox.

A final candidate-bound CI run is still required after this documentation + dedicated smoke commit, followed by PR exact-head CI, fresh mergeability/base check, guarded merge and post-merge CI.

## AT-64 cancellation/reopen projection cleanup

- PlanningSnapshot already admits only ACTIVE Task/Event obligations; this slice extends that invariant to obligation-bound PINNED_WORK constraints.
- Cancellation does not delete canonical pinned-work rows. They are hidden from planning while the obligation is inactive and become effective again on reopen.
- Required Event travel/buffer projections disappear automatically because inactive Events are removed before travel projection.
- Cancellation suppresses undelivered notifications whose `entity_ref` is the obligation and terminalizes their non-SENT delivery outbox rows with `OBLIGATION_CANCELLED`.
- Delivered notifications and immutable historical PlanSnapshots/PlanBlocks are never deleted or rewritten.
- Reopening returns the obligation and its preserved pin to planning. Recomputed logical notifications may rebind the same stable suppression identity; SENT delivery remains terminal.
- An already in-flight external channel call cannot be retroactively unsent; that race remains explicitly governed by ADR 0013's at-least-once boundary.

Hosted push run #113 on the implementation commit is fully green across core, API, Chromium and all smoke surfaces. A final hosted run after this documentation commit and normal PR/merge gates remains required.

## Hosted auth + mobile client (ADR 0015)

- Branch `feat/mobile-client-auth`, schema v10 (`auth_users`, `auth_sessions`).
- Session mode is the server default; bound mode (`--account`) is unchanged for local use.
- Credentials are purged by account deletion and excluded from account export.
- Web client rewritten mobile-first (modules under `web/static/js`), RU/EN.
- `mobile/` Capacitor 8 Android project; `make apk` builds `app-debug.apk`; CI workflow `android` uploads it.
- `deploy/` Docker + Caddy; only `SEOS_DOMAIN` is missing.
- Fixed: live wall clock with seconds made every plan UNKNOWN (`UNSUPPORTED_SUB_MINUTE_TIME`).
- Open: password change/reset, push/local reminders from the notification outbox, multi-process rate limiting.

## Remaining local conformance

1. AT-65 hybrid occurrence mode.
2. AT-66 optional-event omission policy.
3. Pass 10 observability/configuration/install-restart-security closure.

## External production blockers

The repository still does **not** claim production deployment readiness. External capabilities remain required for:

- authenticated TLS deployment boundary;
- production OAuth consent/token encryption/revocation/rotation and secret manager;
- live notification provider/channel (including provider-side idempotency behavior);
- live routing/maps provider;
- live LLM provider.

Do not convert absence of these external integrations into fake local success.
