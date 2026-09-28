# ADR 0027 — Class series exceptions and stable external identity (schema v23)

## Context

Students' timetables are recurring series with exceptions: a class moves, is cancelled or
comes back, changes room or teacher, an extra class appears, holidays cancel a week, and
from some date the whole schedule changes. The same changes arrive from imported
timetables (R4), which update again and again, sometimes late or out of order.

Before v23:
- an occurrence had a single override (CANCEL or MODIFY start/duration);
- a series had no room or teacher;
- series were changed through REST endpoints that bypassed the command boundary (no
  op_id, no idempotency, no offline queue);
- there was no link between an imported item and the local one.

## Decision

1. **Two layers per occurrence.** `occurrence_overrides.layer` is `SOURCE` (what the
   timetable says) or `USER` (what the student changed).
   - The effective class applies the series, then SOURCE, then USER.
   - "Restore" removes only the USER layer.
   - A class the SOURCE cancelled stays cancelled even if the user had moved it: the
     timetable says there is no class. Details the user set still show.
   - A source update never touches the USER layer.
   - MODIFY can change start, length, title, room, teacher or a note, in any combination.
   - `reason` records USER, HOLIDAY or SOURCE.
2. **Room and teacher.** `recurring_templates` gets `location_text` and `teacher`. One-off
   events get them via `event_details`.
3. **Extra classes** are ordinary canonical events (Today, plan and reminders work
   unchanged), linked to their series in `series_extra_events`. Imported one-off events
   keep separate source/user cancellation flags in `external_identities`: a source restore
   cannot erase a personal cancel, and a user restore cannot resurrect an event the source
   still cancels or has removed. Source-owned fields cannot be edited or deleted through
   ordinary event commands; a personal reminder remains independent.
4. **One command boundary.** User changes are typed operations through `/api/v1/sync`:
   - `series.create`, `series.split` ("from this class on"; not for imported series);
   - `series.occurrence.cancel | move | update | restore`;
   - `series.extra.create`;
   - `series.holiday` / `series.holiday.restore` (a date range, all or chosen series).

   They get op_id idempotency and conflict detection, the offline queue and overlay
   projection. The REST write endpoints `/api/v1/recurrence/templates…` are removed.
5. **Stable external identity.** `external_identities` maps
   (source_system_id, UID, RECURRENCE-ID) to a local SERIES, OCCURRENCE or EVENT.
   - Local ids are derived from that key, so re-imports are idempotent.
   - `source_sequence` and `source_updated_at` order updates: an older copy of an update
     is reported as stale and ignored.
   - An occurrence's identity is `(template_id, original local start)`. It never changes
     when the class moves, so both layers keep pointing at the same class.
   - If an imported master shifts its DTSTART without changing cadence, existing USER
     occurrence identities move by the same civil-time delta to remain attached to the
     same ordinal class. An ambiguous cadence rewrite with USER overrides fails the source
     transaction instead of silently detaching them.
6. **Source apply** (`recurrence/source.py`, `SourceApplier`) is the only SOURCE writer. A
   provider turns its data into a `SourceSnapshot` of series (with EXDATE and RECURRENCE-ID
   changes) and one-off events. It is provider-agnostic: nothing names a university.
   - A complete snapshot removes what is missing.
     - A missing series *ends* from now: past classes stay as history.
     - A missing future one-off event is cancelled.
     - Either comes back if the source lists it again.
   - A partial snapshot never removes.
   - Series-wide source changes (room, time) reach every class that has no layer changing
     that field.

## Migration and rollback

- v23 adds columns and tables, and rebuilds `occurrence_overrides` with a `layer` column and
  a wider uniqueness key.
- Every existing override is kept as a USER-layer override with reason USER. This is tested
  on a populated v22 database, together with integrity and foreign-key checks and an
  idempotent re-initialize.
- Rollback runs `persistence/rollback/023_series_exceptions_down.sql`; the script itself
  fails before changing persistent schema unless:
  - no SOURCE-layer overrides, imported series, extra classes, `event_details` rows or
    `external_identities` rows exist;
  - no USER override changes only details.

  Otherwise export first. The script keeps every override the v22 shape can represent. A
  test checks both the fail-closed guard and that a lossless rollback has the same table
  shapes as a fresh v22 database.

## Consequences

- R4 (timetable import) only has to parse its format into a `SourceSnapshot` and schedule it.
  Identity, ordering, removal and user-change precedence are already solved and tested.
- Offline, the calendar shows queued series changes through `overlay.projectCalendar`. The
  device interprets local civil times in its own zone; the server resolves the series zone.
