# ADR 0033 — Explicit reminders follow source-driven changes

**Status:** Accepted for R9

## Context

The reminder system (ADR 0019 and later) already provides: adaptive prompts with
per-intensity backoff and caps, the CRITICAL escalation ladder, quiet hours, grouping,
acknowledgement/snooze/done actions, standalone reminders and wake alarms, delivery
retries with leases, and stale-message checks at delivery. An event reminder is stored
as a moment, `reminder_states.remind_at = start − lead`, computed when the *user's*
command changes the event.

Since R3/R4/R8, events also change without a user command: an academic feed refresh or
a group's starosta moves or cancels an exam through `SourceApplier`. R9 reproduced that
a student's explicit "remind me 60 minutes before" on an imported exam stayed at the old
moment after the source moved the exam (it would have fired hours early), stayed set
after a source cancellation, and was not recomputed on restore.

## Decision

- `reminders/events.py` is the single owner of "an event's reminder moment is start
  minus the user's lead". The user command path and `SourceApplier` (update, cancel,
  restore, disappearance) both call it.
- A user change counts as interaction (`touch`), as before. A source change only
  **re-times** (`ReminderStore.retime`): prompt spacing is untouched, and undelivered
  messages about the old moment are cancelled (`SOURCE_CHANGED`), so a student is never
  told a wrong time; if the reminder already went out and the event moved later, one new
  reminder fires at the new moment.
- Automation never erases an explicit reminder: the lead (`event_reminders`) survives a
  cancellation or a disconnected feed and is re-applied when the event returns with the
  same stable identity. No reminder is invented for someone who never asked for one.

## Consequences

Per-occurrence reminders for recurring classes remain out of scope (classes surface in
Today/planning; there is no per-class lead to follow). DST: event moments are absolute
instants; quiet hours are local wall-clock and resolved across both DST switches.
