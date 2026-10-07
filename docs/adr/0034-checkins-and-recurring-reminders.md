# ADR 0034 — Tracked check-ins and recurring reminders are their own owners (schema v31)

**Status:** Accepted

## Context

botay! planned work (Tasks), fixed time (Events), attention (Reminders) and actual work
(Execution Sessions), and recurring *work* materialized Tasks (ADR 0024). Daily life also
needs small repeated actions whose real outcome matters — a medication at 09:00, «полить
цветы», «20 задач по матану в день» — and repeated attention without an outcome — «каждый
вечер в 22:30 вынести мусор». Neither fits the existing owners:

* making them Tasks (option A, "generalize `work_routines`") would give a pill a 5-minute
  effort, put it into the workload solver and distort capacity; `work_routine_occurrences`
  hard-wires `task_id NOT NULL` and Task lifecycle as the outcome;
* making the Reminder the owner of "taken / not taken" would mix delivery workflow
  (snooze, fired, acknowledged) with the fact of what happened.

## Decision

1. **Check-ins** get a canonical owner, `checkins/` (option B):
   `CheckInTemplate (kind ROUTINE | MEDICATION | QUOTA, recurrence, zone, prompt policy)`
   → `CheckInOccurrence (template_id, original_recurrence_id)` → outcome.
   The occurrence row is the single owner of the outcome:
   `PENDING | DONE | SKIPPED | MISSED | CANCELLED`, `resolved_by USER | POLICY`,
   `occurred_at` (the user's moment), `acted_at`, and for quotas `quantity_done` with the
   day's `target_quantity` snapshot.
2. **Attention stays in `reminders`.** An occurrence that should prompt gets one ordinary
   reminder row (id derived from the identity). Snooze is a reminder operation: the
   occurrence stays PENDING. «Готово» on the reminder itself, dismissing an alarm, or the
   reminder firing never records an outcome. The occurrence owner arms, re-times and
   closes its reminder, never the reverse.
3. **Missed is policy, not a medical statement.** A template's observation window
   (`window_minutes`, default "until the next occurrence or the end of the local day")
   decides when an unanswered day is recorded MISSED by POLICY. A later user answer wins
   over the policy (late offline «Принял»). The medication UI says plainly that the app
   gives no advice about late or missed doses; dose and instructions are the user's text.
4. **Follow-up is delivery policy.** `followup_minutes` re-arms the reminder once after an
   unanswered prompt (a new prompt with its own message, not a delivery retry);
   `followups_sent` is bookkeeping on the occurrence, separate from the outcome columns.
5. **Recurring reminders** (`reminder_series`) are attention only: each occurrence
   materializes one standalone reminder with a stable `(series_id, original_recurrence_id)`
   identity. Push, Android alarms, snooze and «Готово» are unchanged. A deleted occurrence
   stays deleted (`deleted_reminders`). «This and future» replaces later one-day changes
   (as calendars do); an occurrence that already went out is history.
6. **Recurrence is shared, not duplicated.** `recurrence/expansion.py` is the one pure
   expansion (DAILY/WEEKLY, INTERVAL, COUNT, UNTIL, and WEEKLY BYDAY) used by both new
   owners with the existing `recurrence_id`/`resolve_local` (earlier fold, shift forward
   over a gap). Identity is the original local start and never changes when one day is
   moved. Older owners keep their iterators and keep rejecting BYDAY
   (`RecurrenceRule.parse(..., allow_weekdays=False)`).
7. **Writes are sync operations** (`checkin.*`, `reminder_series.*`) with client ids,
   exactly-once replay, tombstones in `deleted_entities`, outcome conflicts returned as
   CONFLICT (Done vs Skip on two devices: the first wins; changing it is an explicit
   «снять отметку»), quantity as deltas that add up across devices.
8. **Assistant** gets typed `CREATE_CHECKIN`, `CREATE_REMINDER_SERIES`, `CHECKIN_OUTCOME`,
   `CHECKIN_PROGRESS`, `MOVE_CHECKIN_OCCURRENCE`. The server, not the model, chooses which
   day's occurrence an answer means (local date, day part, nearest open one); the model
   cannot reference a check-in outside its context. A deterministic RU/EN parser
   (`agent/recurring.py` ⇄ `js/recurring.js`, shared fixture) covers the common phrases
   offline; the medication kind comes from the user's verb, never from a drug list.

## Consequences

* Today shows check-ins as «Принял в 09:04 / Ожидает / Не отмечено», never as Tasks; a
  check-in never consumes planner capacity. A quota with a user-given pace reports its
  remaining minutes next to the day's free time (ADR 0036); without a pace no time is
  invented.
* Account export/deletion/backup cover the four new tables and `deleted_entities`;
  rollback 031 drops them and leaves already-materialized reminders as valid one-shot
  reminders.
* Not done: alarms dismissing a medication prompt do not record an outcome (by design);
  check-in history beyond 35 days is materialized only when a day was in a horizon.
