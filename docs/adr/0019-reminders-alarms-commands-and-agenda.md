# ADR 0019 — Reminders and wake alarms, text commands, one agenda (schema v16)

## Status

Accepted, 2026-09-26. Schema v16.

## Context

The product should mostly be driven by what the user says: they state an intent in
text or voice, confirm a preview, and the system manages tasks, events, reminders,
sync and escalation. Field feedback showed where trust broke:

- the AI connection test passed on any HTTP 2xx, and capture fell back to the local
  parser silently when the model failed;
- the app promised reminders that could not arrive (no device, push not configured,
  notifications denied) and had no real alarm;
- "Архив" mixed CANCELLED and ARCHIVED, so a cancelled task offered "В архив" again and
  "restore" could land in another archived state;
- refreshing a screen threw the user back to the top;
- tasks, events and reminders had to be looked for in different places.

## Decision

### AI connection test and visible engine

`provider.check()` is a real `interpret()` round trip (same system prompt and request
shape) that passes only when the model returns a typed create action. Failures carry a
reason — `AUTH`, `NOT_FOUND` (model), `ENDPOINT`, `RATE_LIMITED`, `QUOTA`, `FORMAT`
(refused JSON mode or a non-JSON answer), `MALFORMED` (not the provider's response
shape), `REJECTED`, `UPSTREAM`, `NETWORK`, `BLOCKED_URL` — persisted as a credential
status (v16 widens the CHECK) and shown as the steps that passed (key, address, model,
format). `/assistant/interpret` returns `engine` (`AI`/`LOCAL`), `model`,
`fallback_reason`; the product path also degrades a well-formed but invalid proposal to
the local parser (`INVALID_PROPOSAL`) — nothing invalid is stored or shown either way.
The capture sheet always says which engine read the text.

### Standalone reminders and wake alarms

`reminders` is a first-class entity (not a Task, not an Event): `title, remind_at,
delivery (PUSH | ALARM | PUSH_AND_ALARM), wake_check, raise_volume, obligation_id?,
status (SCHEDULED → FIRED → DONE | CANCELLED)`. Mutations are sync operations
`reminder.create/update/snooze/done/ack/cancel/reopen/delete` (offline, idempotent,
tombstoned in `deleted_reminders`). The engine fires a due reminder once as a message
(`reminder_messages.reminder_id`, `.delivery`), past quiet hours (the user named the
moment).

Alarms ring on the Android device from its own schedule (`alarm/` package):
`AlarmManager.setAlarmClock` (exact, Doze-exempt; needs "Alarms & reminders", otherwise
`setAndAllowWhileIdle` and Settings says so), rescheduled after reboot, update, clock or
time-zone change. Ringing is a foreground service (`systemExempted` when exact alarms
are allowed, else `mediaPlayback`) looping the alarm sound on the alarm stream with a
full-screen screen; `raise_volume` raises the alarm volume to the maximum and restores
it afterwards (the original is persisted first, so a crash/reboot still restores it).
Unanswered ringing stops after 5 min and comes back after 5 min, 3 rounds. A wake alarm
after «Я встал» asks «Вы не уснули?» 25 min later and rings again 10 min after an
unanswered check. Answers are reported with the same WorkManager queue as notification
buttons. The web client hands the device every open alarm (`SeosNative.syncAlarms`);
changes made elsewhere send a silent `ALARM_SYNC` push that makes the phone fetch
`/api/v1/reminders/alarms`; the due push is a backup trigger (deduplicated by
reminder id + moment). Phones without the `wake-alarm-v1` capability get a
notification instead.

### CRITICAL escalation

For a CRITICAL task with a due moment the policy runs a ladder 48h → 24h → 12h → 6h →
3h → 1h → 15m (`Stage.ESCALATION`), replacing the generic 24h/2h prompts. Each rung
fires once per episode; when several were crossed at once (snooze, quiet hours, late
creation) only the latest is sent and the earlier ones count as covered. A reschedule
starts a new ladder (episode key), completion closes it, a snooze postpones it, and
progress is reflected in the text. Inside the ladder window RISK_UP is not sent
separately; the ≤3h rungs are urgent (not held by spacing or the daily cap).

### Notification health

`GET /api/v1/notifications/health` reports what may be promised: `reach` PUSH / IN_APP
/ NONE, `alarm` OK / INEXACT / NONE, and `problems` (worker down, push unconfigured, no
device, notifications off on the phone, exact alarms or full-screen off). Devices report
their permissions at registration and on every resume
(`POST /mobile/devices/{id}/status`). `POST /notifications/test` sends a diagnostic
message (`TEST`; not in the inbox, not counted toward caps). Forms that set a reminder
show a warning when it cannot arrive.

### Text/voice commands

The capture sheet is the command surface: text/voice → intent → preview → confirmation
→ execution. The RU/EN grammar (`agent/commands.py`, device twin `js/commands.js`, one
fixture) and the language model produce the same typed actions: `CREATE_REMINDER`,
`UPDATE_TASK`, `UPDATE_EVENT`, `UPDATE_REMINDER`, `RESCHEDULE`, `SNOOZE`, `LOG_PROGRESS`
(minutes or count), `COMPLETE_OBLIGATION`, `CANCEL_OBLIGATION`, `ARCHIVE_OBLIGATION`.
The server validates each against the account (target exists, right kind, version).
Closing, cancelling and archiving always need explicit confirmation. An unclear target
is chosen by the user from candidates. Server-applied actions run through the same
`sync.commands` handlers as the offline queue; device-parsed commands are queued as
ordinary operations (offline, with Undo). Delete is never offered to the model.

### One agenda

`Commitment` is a read projection over tasks, events and reminders (`web/commitments.py`,
device twin `js/agenda.js`, one fixture): `kind, place (open | done | archive), at,
at_kind`, with the canonical payload untouched under `entity`. «Дела» shows all of them
grouped by day; the same projection powers global search (`/api/v1/commitments?q=`).
Task and Event stay separate canonical entities.

### Lifecycle UX

For the user a task is in work, done, or in the archive; CANCELLED and ARCHIVED are
both "archive". `task.restore` takes an item out of the archive in one step (back to
«Выполнено» if it was done, otherwise back to work). Only a done task offers
«В архив»; swiping it left archives it with Undo for 10 seconds.

### Interaction

Long press (phone) or right click (desktop) opens one action sheet whose actions depend
on the item and its state. Refreshing a screen keeps the viewport (anchor item + offset);
only navigation starts at the top. Settings → «Синхронизация» separates this device's
queue («Отправить сейчас», last send) from external connectors (state, last successful
sync, readable error, «Синхронизировать сейчас» / retry via
`POST /api/v1/connectors/{id}/sync`). Google Calendar without configured access records
an `AUTH_REQUIRED` attempt instead of pretending to sync.

## Rollback

Migration 016 rebuilds `llm_credentials` (same columns, wider CHECK), adds `reminders`,
`deleted_reminders`, and columns on `reminder_messages` and `mobile_devices`. To roll the
code back to v15: stop the stack, take a verified backup, then
`DROP TABLE reminders; DROP TABLE deleted_reminders;
ALTER TABLE reminder_messages DROP COLUMN reminder_id; ALTER TABLE reminder_messages DROP COLUMN delivery;
ALTER TABLE mobile_devices DROP COLUMN status_json; ALTER TABLE mobile_devices DROP COLUMN last_seen_at;
UPDATE llm_credentials SET status='UNTESTED' WHERE status IN ('ENDPOINT_NOT_FOUND','QUOTA_EXCEEDED','UNSUPPORTED_FORMAT','MALFORMED_RESPONSE','PROVIDER_ERROR');
DELETE FROM schema_migrations WHERE version=16;` — or restore the pre-deploy backup.
Standalone reminders are lost by this rollback.
