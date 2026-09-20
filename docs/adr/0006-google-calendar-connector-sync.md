# ADR 0006 — Google Calendar connector sync ownership and checkpoint semantics

- Status: Accepted for Pass 5
- Date: 2026-09-20

## Context

Pass 4 established immutable `SourceRecord` / `Observation` evidence, reversible `SourceBinding`, source availability, explicit source-removal evidence, and reconciliation. Pass 5 must prove one provider-specific connector without letting the connector become a second owner of canonical Task/Event truth.

Google Calendar Events is the first provider because its incremental sync contract exposes the failure modes required by SPEC v2.1:

- `nextSyncToken` is the durable continuation token from the terminal list page;
- incremental sync returns events changed since the previous token, including deleted events;
- an invalid/expired sync token returns HTTP 410 and requires a new full sync;
- deleted events are represented as `status=cancelled`; some tombstones guarantee only `id`.

Current provider references:
- https://developers.google.com/workspace/calendar/api/guides/sync
- https://developers.google.com/workspace/calendar/api/v3/reference/events/list
- https://developers.google.com/workspace/calendar/api/v3/reference/events
- https://developers.google.com/workspace/calendar/api/auth

## Decision

### Provider boundary

`GoogleCalendarHTTPTransport` is a read-only Events.list adapter. It receives an access token through a callback at request time; tokens are not persisted by the connector.

The intended least-privilege OAuth scope is:

`https://www.googleapis.com/auth/calendar.events.readonly`

Pass 5 does not implement OAuth consent, refresh-token storage, credential rotation, or a UI for account connection. Those are deployment/auth concerns around the connector transport.

The raw `calendar_id` is not persisted in connector workflow state. The persisted connector scope uses a deterministic hash-derived calendar scope identifier.

### Sync pipeline

The connector path is:

```text
Google Events.list
→ source-native event id/revision
→ immutable SourceRecord
→ typed Observations / source-removal evidence
→ ingestion receipt
→ durable terminal-page acknowledgement
→ connector checkpoint advance
```

The connector never writes canonical Task/Event fields directly and never resolves cross-source truth.

### Checkpoint atomicity

`connector_states` is the optimistic-concurrency owner for one provider/scope sync stream.

Every `ConnectorSyncSession` records `cursor_before`, `state_version_before`, and terminal `cursor_after` only on successful completion.

A complete session advances checkpoint, health, last-success time, connector-state version, and source availability only if `connector_states.version == state_version_before`.

If another session has already advanced connector state, an older session is terminally marked `CONCURRENT_SYNC_CONFLICT` and cannot overwrite the newer checkpoint. Once a session has a non-null `completed_at`, terminal updates are write-once and replayed `finish_*` calls cannot rewrite its result.

Failure health updates use the same compare-and-swap rule. Therefore an older auth/network failure cannot downgrade a newer successful connector state from `CURRENT` to `STALE`/`UNAVAILABLE`.

Source availability history is written in the same SQLite transaction as the successful connector-state CAS so the two persisted views cannot reorder under concurrent sessions.

### Partial sessions and replay

Evidence pages may be committed before the full request range completes. This is safe because provider revisions have deterministic ingestion receipts and immutable evidence identities.

The checkpoint does not move until the terminal page and its `nextSyncToken` have been durably processed. A retry from the old checkpoint may replay prior pages without duplicating evidence.

A partial or failed session never infers deletion from entities absent from the incomplete response.

### Invalid sync token

For HTTP 410 / invalid sync token:

1. the failed session attempts a CAS from its captured connector-state version;
2. only the current owner may clear the checkpoint and mark the source `STALE`;
3. only after successful invalidation does the connector run a full sync;
4. a stale 410 from an older concurrent session cannot clear a newer checkpoint.

A full sync still produces evidence only. Missing previously active provider entities may become source-removal evidence after the complete snapshot; local obligations are not hard-deleted.

### Provider deletion semantics

`status=cancelled` is source-removal evidence, not local user cancellation or completion.

For ordinary cancelled/deleted events Google guarantees only `id`; the connector therefore does not invent an `updated` timestamp or source revision when the provider omits it. Its replay identity falls back to a deterministic content fingerprint.

Recurring cancelled exceptions retain `recurringEventId` and `originalStartTime` metadata when the provider supplies them. Pass 5 does not yet expand recurrence into canonical Event instances.

### Health and retry

Connector health is `CURRENT | STALE | UNAVAILABLE`.

- successful complete sync → `CURRENT`;
- bounded exhausted transient/provider failure → `STALE`;
- auth failure → `UNAVAILABLE`;
- absence in partial/failed results → no deletion inference.

429, 5xx, and network transport failures use bounded exponential retry. The connector never spins indefinitely.

## Schema

SQLite schema v4 adds `connector_states`, `connector_sync_sessions`, `connector_entities`, and `connector_ingestion_receipts`.

Migration coverage verifies v3 → v4 preservation of existing evidence state.

## Verification contract

Pass 5 executable coverage includes AT-38, AT-39, AT-40, concurrent checkpoint/health/410 regression tests, explicit and minimal tombstone tests, HTTP 410 mapping, and connector smoke in `make verify` / CI.

## Consequences

- Pass 6 can consume provider evidence without granting imported content mutation authority.
- Provider cursor/health workflow state remains separate from canonical local state and planning revisions.
- One connector is implemented deeply rather than introducing a speculative plugin framework.
- OAuth lifecycle, calendar discovery, recurrence expansion, webhooks, and additional providers remain outside Pass 5.
