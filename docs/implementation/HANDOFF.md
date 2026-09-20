# Student Execution OS — Implementation Handoff

> Checkpoint only. Re-read current repository, current specification, PR state, exact branch refs, and CI before using this file as current state.

## Identity

- Repository: Misha1302/student-execution-os
- Main baseline for Pass 5: `0a6b90f5106c21a34d1dec70acc6ba1b5fdcc8a1`
- Main baseline content: merged Pass 4 / PR #6
- Normative specification: v2.1, blob `9bb0d0934b0810b198dc67fc147b384347f44887`
- Pass 5 branch: `impl/pass-5-google-calendar-connector`
- First pushed Pass 5 implementation checkpoint: `63feafb46b60c5c409f9e7d3f4ee98f7fa9210cb`
- Date: 2026-09-20

A tracked Git file cannot contain the SHA of the commit that contains itself. Re-read the terminal branch/PR head after this handoff commit.

## Completed passes

- [x] Pass 0 — baseline / stack / skeleton / CI
- [x] Pass 1 — canonical local domain / persistence / concurrency
- [x] Pass 2 — immutable PlanningSnapshot / sound tri-state feasibility
- [x] Pass 3 — planner / risk / PlanSnapshot / next actions / first vertical-slice closure
- [x] Pass 4 — evidence / reconciliation / provenance
- [x] Pass 5 — one real provider connector (Google Calendar Events)
- [ ] Pass 6 — LLM extraction / authorized action boundary
- [ ] Pass 7 — travel-aware planning
- [ ] Pass 8 — recurrence / notifications
- [ ] Pass 9 — reliability / security / hardening
- [ ] Pass 10 — conformance closure

## Pass 5 status

IMPLEMENTED AND LOCALLY VERIFIED. Final branch push / PR / exact-head CI must be re-read after this handoff commit.

## Provider

Google Calendar Events API, read-only ingestion.

Provider-specific implementation follows the current Google contracts for Events.list pagination and `nextSyncToken`, incremental deleted events, HTTP 410 invalid-token recovery, `status=cancelled` tombstones, minimal deleted-event payloads, and least-privilege event read scope. Canonical references are recorded in ADR 0006.

## Architecture decisions

- Connector responsibility ends at trustworthy evidence ingestion.
- Google Calendar never directly writes canonical Task/Event fields.
- Access tokens are supplied at request time and are not persisted by the connector.
- Persisted provider scope uses a hash-derived calendar identity rather than raw `calendar_id`.
- SQLite schema v4 owns connector workflow state separately from canonical/evidence/planning state.
- `connector_states.version` is the optimistic-concurrency owner of checkpoint/health transitions.
- Every sync session captures `state_version_before`.
- Success, failure, and invalid-cursor state transitions are compare-and-swap guarded.
- Source availability changes occur in the same transaction as the connector-state CAS.
- A stale concurrent success cannot overwrite a newer checkpoint.
- A stale concurrent failure cannot regress newer health.
- A stale concurrent HTTP 410 cannot clear a newer checkpoint.
- Completed sync sessions are terminal/write-once; repeated finish calls cannot rewrite the recorded result.
- Partial/failed polls do not infer source deletion from absence.
- Terminal-page `nextSyncToken` is persisted only after the corresponding response range has been durably ingested.
- Replayed provider revisions are idempotent through deterministic evidence IDs and ingestion receipts.
- Explicit Google `cancelled` events become source-removal evidence; local obligations survive.
- Deleted events that contain only `id` are accepted without fabricating provider revision time.
- Complete full-snapshot absence may produce source-removal evidence but never local hard deletion.
- Imported content remains data and cannot become an Actor or authorization.

## Implemented capabilities

- Real Google Calendar Events.list HTTP transport using the Python standard library.
- Bounded transient retry for network / 429 / 5xx failures.
- Auth failure → connector/source UNAVAILABLE when the failing session still owns current state.
- Provider failure → STALE without checkpoint advancement.
- Incremental sync using persisted sync token.
- Multi-page sync with terminal-page checkpoint discipline.
- HTTP 410 invalid-token recovery through guarded full resync.
- Explicit cancelled-event deletion evidence.
- Minimal tombstone support where only event id is guaranteed.
- Provider metadata for recurring cancelled exceptions when present.
- Durable connector sessions, health, checkpoints, entity workflow state, and ingestion receipts.
- Schema v4 migration and v3 → v4 preservation test.
- Connector CLI smoke wired into Makefile and GitHub Actions.
- AT-38, AT-39, and AT-40 promoted from deferred to executable PASS coverage.

## Verification checkpoint before final documentation commit

Observed on Fedora against the terminal pre-commit Pass 5 candidate after concurrency hardening:

- full unit/integration/acceptance suite: 118 tests PASS;
- health smoke: PASS;
- canonical-domain smoke: PASS;
- feasibility smoke: PASS;
- planner smoke: PASS;
- reconciliation smoke: PASS;
- connector smoke: PASS;
- first pushed checkpoint GitHub Actions run `35535245792`: SUCCESS on `63feafb46b60c5c409f9e7d3f4ee98f7fa9210cb`.

Do not treat those earlier results as proof for the terminal documentation/concurrency HEAD. Run `make verify` and inspect exact-head GitHub Actions after the final commit.

## Acceptance coverage

Pass 5 adds:
- AT-38 — partial poll no deletion;
- AT-39 — checkpoint atomicity;
- AT-40 — invalid provider cursor / full resync.

Additional provider/concurrency regression coverage:
- old successful session cannot overwrite a newer checkpoint;
- old failed session cannot downgrade newer connector health;
- old 410 cannot clear a newer checkpoint;
- a completed session cannot be rewritten by repeated terminal calls;
- explicit cancelled event is evidence, not local hard delete;
- deleted event with only `id` is supported;
- auth failure preserves checkpoint;
- HTTP transport maps 410 to invalid-sync-token behavior;
- access token is carried only in the Authorization header, not the request URL.

All Pass 0–4 tests remain active.

## Security / privacy boundary

- Intended OAuth scope: `https://www.googleapis.com/auth/calendar.events.readonly`.
- OAuth token acquisition, refresh-token persistence, consent UI, rotation, and revocation are not implemented in Pass 5.
- Access token strings are not stored in connector SQLite state.
- The raw Google calendar identifier is not persisted in connector workflow state.
- Connector content cannot authorize commands or widen scopes.
- No write-capable Google Calendar operation exists in Pass 5.

## Scope explicitly not implemented

- OAuth consent / refresh-token secret store;
- calendar discovery / multi-calendar account UI;
- webhook/watch delivery;
- recurrence expansion into canonical Event instances;
- automatic canonical Event/Task creation from imported Calendar events;
- LLM extraction/action adapter;
- travel routing / location transitions;
- notifications;
- offline replication;
- additional provider connectors;
- generalized plugin framework.

## Known limitations

- Provider integration tests are deterministic HTTP/fixture tests plus a real REST transport; this checkpoint does not claim a live end-user OAuth account test.
- Connector evidence currently captures a bounded event field set (status/summary/start/end/eventType plus provenance metadata); it is not a complete Calendar event mirror.
- Recurring-event semantics remain evidence-level only until recurrence is deliberately implemented.
- Pass 5 is read-only by design.

## Next pass

Pass 6 — LLM extraction / authorized action boundary.

Do not start Pass 6 merely from this checkpoint. First re-read terminal Pass 5 PR/head and CI, and merge Pass 5 only with separate merge authority.

### First concrete actions for Pass 6

1. Keep extraction contexts tool-less and treat imported Calendar/source content as untrusted data.
2. Convert model extraction into typed observations/candidates, not direct canonical mutations.
3. Keep action/tool execution in a separately authenticated and authorized boundary.
4. Bind every mutation to authenticated actor, expected entity version, and idempotency policy.
5. Add prompt-injection negative tests before exposing any LLM-driven mutation path.

## Inspect first

- docs/SPECIFICATION.md
- docs/adr/0005-evidence-reconciliation-provenance.md
- docs/adr/0006-google-calendar-connector-sync.md
- src/student_execution_os/connectors/google_calendar.py
- src/student_execution_os/connectors/model.py
- src/student_execution_os/connectors/repository.py
- src/student_execution_os/persistence/migrations/004_connector_sync.sql
- src/student_execution_os/reconciliation/repository.py
- tests/acceptance/test_pass5_google_calendar_connector.py
- tests/unit/test_google_calendar_transport.py
- tests/integration/test_migration_v4.py
- tests/acceptance/acceptance_registry.json

## Do not trust without fresh verification

- terminal Pass 5 branch HEAD;
- final PR number/state/mergeability;
- exact terminal-head GitHub Actions status;
- current main;
- any PASS statement in this checkpoint without matching executable evidence.
