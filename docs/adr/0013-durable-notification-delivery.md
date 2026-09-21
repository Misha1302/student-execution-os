# ADR 0013 — Durable notification delivery outbox

Status: Accepted  
Date: 2026-09-21

## Context

Notification workflow state already had stable suppression identity, revision revalidation, quiet hours, snooze and follow-up gating. Direct channel invocation was not a sufficient crash boundary: a process could die around an external send, and retry state was not independently leased/durable.

## Decision

Add a SQLite-backed `notification_delivery_outbox` owned by the notification subsystem.

Each logical notification has one deterministic `delivery_key`. Delivery uses a lease committed before invoking the channel, bounded retry/backoff, an attempt counter, provider message id when available, and terminal `SENT | SUPPRESSED | DEAD` states.

An expired lease may be reclaimed by another worker after restart. Reclamation reuses the same `delivery_key`.

Notification domain/plan revision and completion-prerequisite checks run before a lease is issued. Delivery success commits the notification `DELIVERED` state and outbox `SENT` state atomically.

Snooze cannot race an active lease. Re-computation/snooze resets only non-terminal outbox state for the same logical notification.

## Delivery guarantee

The local guarantee is durable **at-least-once** delivery intent, not exactly-once external delivery. If a provider accepts a message and the process crashes before the local success transaction commits, the expired lease is retried. External channels should bind their provider idempotency mechanism to the stable `delivery_key` where supported.

This limitation is explicit because SQLite cannot prove whether an arbitrary external provider accepted a request after a crash.

## Data lifecycle

The outbox is account-scoped and part of the explicit data-lifecycle table classification. It is covered by SQLite backup/restore and account deletion/export policy. Any future unclassified persistence still fails closed.

## Consequences

- Restart does not lose accepted future notification delivery state.
- A live worker lease prevents concurrent duplicate sends inside the local process fleet.
- Failed sends have deterministic bounded retry state.
- Provider-side duplicate suppression still depends on the external channel honoring the stable delivery key.
- No live channel/provider is introduced by this ADR.
