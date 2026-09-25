# ADR 0018 — Offline-first client, fixed-time events, task lifecycle (schema v15)

## Status

Accepted, 2026-09-26. Schema v15.

## Context

Field feedback from the phone app:

- After an action without network (create, start, done, «не сейчас», reschedule) the
  screen did not change. `actions.mutate()` awaited the network flush (up to the 20 s
  request timeout) and then re-fetched the read model, which fell back to the *old*
  cached response. The optimistic patch only touched `/api/v1/tasks`, never Today or
  the plan; events could only be created online.
- "Сегодня с 21 до 22 провести занятие" became a task with `actionable_from` 21:00 and a
  hard deadline at 22:00: the parser had no notion of an interval or of an event.
- Tasks vanished from Today: `build_next_actions` returns nothing unless the plan is
  FEASIBLE, and Today rendered only next actions. The most common cause of a
  non-FEASIBLE plan was any OPTIONAL/PREFERRED event under the default
  `FAIL_CLOSED` policy, which also surfaced raw text such as
  `unsupported optional event policy <id>`.
- The planner ignored the planning-profile windows, so work could land at night; quiet
  hours were read-only.
- Only "cancel" existed; there was no archive and no delete.

## Decision

### Offline-first client

- `sync.js` is a durable operation queue in `localStorage` (per server + account).
  `queueOperation()` persists and returns synchronously; it never waits for the
  network. Delivery runs in the background: immediately, after `online`, on resume /
  visibility, on a 30 s heartbeat while anything is queued, with 2 s → 60 s backoff on
  failure. A change queued during a flush is sent right after it. Repeated identical
  lifecycle taps (start/complete/cancel/…) on the same entity collapse into one queued
  operation. The server's `client_operations` table keeps delivery exactly-once.
- Queue states: `PENDING`, `ACKED` (kept up to 24 h so an older cached read model still
  shows the change), `CONFLICT` / `REJECTED` (shown in the sync sheet, never dropped
  silently).
- `overlay.js` is a pure projection of queued + recently acknowledged operations onto
  cached read models (`/tasks`, `/today`, `/plan/agenda`, `/events`, `/calendar`,
  `/notifications`). Cached server responses are never edited; `store.load()` applies
  the overlay on every read, so the change is visible on every screen immediately and
  after an app restart. Progress deltas are not applied twice when the base already
  contains them.
- After the server confirms, the app refreshes in the background (scroll kept); while
  online it prefetches the main read models so any screen works offline later. A cold
  start shows the saved state first and refreshes afterwards.

### Fixed-time events

- The RU/EN parser (server and device, same fixtures) recognises intervals ("с 21 до
  22", "21:00–22:30", "from 9 to 10 pm", "с 23 до 1") and event words (пара, лекция,
  занятие, встреча, созвон, тренировка, meeting, class, …) with a clock time; it returns
  `kind: "EVENT"` with `starts_at`, `ends_at`, `duration_minutes` and a clean title — no
  deadline and no effort. Default durations: 60 min, 90 min for пара/лекция/семинар.
  An explicit day is never rolled forward.
- Events are created, edited, cancelled, restored and deleted through `event.*` sync
  operations, i.e. offline too. `remind_before_minutes` (0–1440, stored in
  `event_reminders`) sets `reminder_states.remind_at = starts_at − lead`, recomputed when
  the event moves; the reminder engine sends a "Скоро: «…»" message for it.
- The capture card switches between task and event; the event card shows the time span,
  the reminder choice and overlaps with known events.

### Lifecycle

- «Не буду делать» = CANCELLED (restorable). ARCHIVED is reachable from COMPLETED /
  CANCELLED and back. Delete removes the row and everything that cascades from it and
  writes a tombstone (`deleted_obligations`), so a late offline replay for that id is a
  harmless NOOP (`code: DELETED`) and cannot resurrect it.

### Planning

- The planning-profile windows (the user's waking hours, set together with reminder
  quiet hours in Settings → Сон) become derived UNAVAILABLE constraints
  (`off-hours:*`); they are never stored and are shown in the plan as sleep.
- Product default for optional/preferred events (`FAIL_CLOSED` profile value): plan
  around them as if the user attends (`assume_attendance`). Feasibility therefore holds
  under a stated assumption instead of being refused. An optional event that collides
  with another event stays a real question (the engine's fail-closed result), shown as
  "Пойдёте?" with the choices *attend* or *allow skipping optional events*. The engine's
  formal FAIL_CLOSED semantics (acceptance tests) are unchanged.
- Today always shows open tasks: when the plan has no verified next action, a
  "Можно заняться сейчас" card and a "Скоро" list (with the reason: put off until,
  due, not yet planned) appear. Reason codes are turned into sentences about the task or
  event they name; a code without a sentence becomes a generic one — ids never reach the
  screen.
- `GET /api/v1/plan/agenda?days=7` is the same planner over seven local days for the Plan
  screen (swipe between days); it is not stored as the current plan.

### Counted progress

`task_progress_counts` ("3 of 10 problems"): `task.progress {count}` lowers remaining
time proportionally; time stays the planner's unit. Subtasks are on the roadmap.

## Consequences

- The UI no longer blocks on the network for any task/event change.
- Conflicts arrive asynchronously (toast + sync sheet) instead of as the tap's error.
- Schema v15 adds three account-owned tables, included in export/deletion.
