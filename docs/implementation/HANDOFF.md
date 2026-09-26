# Student Execution OS — Current Handoff

> Re-read the current repository and exact commit before using this checkpoint.

## Current architecture

- Schema: v16 (v13: explicit `remind_at` reminder requests, device capabilities, retention
  indexes; v14: per-account encrypted LLM credentials and the platform-managed entitlement seam;
  v15: delete tombstones, event reminder leads, counted progress — ADR 0018; v16: standalone
  reminders and wake alarms, device health, AI test failure states — ADR 0019).
- v16 (ADR 0019): `reminders/standalone.py` + `reminder.*` sync ops; CRITICAL escalation ladder
  in `reminders/policy.py`; `/notifications/health`, `/notifications/test`; RU/EN command
  grammar `agent/commands.py` ⇄ `js/commands.js` (fixture `nl_command_cases.json`) and typed
  Assistant actions executed through `sync.commands`; `Commitment` projection
  `web/commitments.py` ⇄ `js/agenda.js` («Дела», search); `task.restore`; Android `alarm/`
  package (AlarmManager alarm clocks, ringing service, awake check, `SeosNative` plugin).
- Client is offline-first: `js/sync.js` (durable queue, background delivery, backoff,
  duplicate-tap collapse) + `js/overlay.js` (pure projection of queued/acked operations onto
  cached read models, applied by `js/store.js` on every read). No task/event change awaits
  the network. Tests: `tests/unit/test_offline_overlay.py` (Node) and
  `tests/browser/offline_e2e.py` (Chromium against a real uvicorn + SQLite server).
- Events: parser `kind: "EVENT"` for intervals/event words; `event.create/update/cancel/
  reopen/delete` sync ops; `remind_before_minutes` → `reminder_states.remind_at`; engine
  `event_facts` → "Скоро" messages. Lifecycle: `task.archive/unarchive/delete`.
- Planning: planning-profile windows → derived `off-hours:*` UNAVAILABLE constraints;
  `assume_attendance` for non-colliding optional events (product default); `/api/v1/plan/agenda`.
- API/UI: FastAPI plus the shared browser/Capacitor client.
- Execution state: canonical `started_at` and `last_progress_at` on tasks.
- Offline mutations: `/api/v1/sync`, client-generated operation ids, atomic
  `client_operations` results, field-level edits, progress deltas, and explicit
  lifecycle conflicts.
- Reminders: `reminder_states` is per-task execution-loop state;
  `reminder_messages` is both the in-app inbox and leased push outbox. A new user
  reminder is not a technical retry. The v7–v11 runtime notification package was
  removed by v12.
- Assistant: deterministic degraded parser by default; per account, the user's own
  OpenAI, Anthropic, or OpenAI-compatible key (Settings → AI, `agent/credentials.py`,
  ADR 0017) or, for accounts with a `PLATFORM_MANAGED` entitlement, operator credentials. Model output is a typed proposal only;
  preview, explicit confirmation, server validation, atomic apply, and idempotency
  remain application-owned.
- Capture: `agent/nlparse.py` (server) and `web/static/js/nlparse.js` (device) parse RU/EN
  text identically (fixture `tests/fixtures/nl_capture_cases.json`, parity test runs Node).
  The capture card creates tasks through the `task.create` sync operation; an LLM proposal
  only refines the card. `assistant/apply` accepts `edits` to answer unresolved fields.
- Reminders: a user-requested moment (`remind_at`, set by snooze, "not now" and
  "напомни …") fires once, past one-shot stage limits and quiet hours.
- Android: `reminders/` Java package renders data-only reminder pushes with Start/Done/Snooze
  buttons executed by WorkManager through `/api/v1/sync` (op ids derived from reminder + button).
  `mobile/scripts/native_e2e.py` checks it on an emulator against a local server.
- Android: native push and speech plugins are installed. FCM delivery additionally
  requires a matching Firebase Android configuration and server service account.

## Verification contract

`make verify` covers Python unit/integration/acceptance tests, API tests, Chromium UI
tests, static checks, and all executable smokes. `make apk` performs Capacitor sync and
builds the debug APK. Critical v12 regressions cover fresh database initialization,
task start/progress/complete/completed-open/reopen, exactly-once replay, conflict
visibility, offline reload/reconnect persistence, reminder snooze/termination, delivery
retry separation, Assistant invalid-action rejection, backend server identity isolation,
and native push/voice wiring.

## External boundaries

The application runs without external credentials and reports degraded capability.
Actual external delivery/inference still requires:

- FCM service-account credentials plus Android `google-services.json`;
- per user: their own OpenAI, Anthropic, or OpenAI-compatible key (the server needs the
  `credential.key` master key file to store them);
- routing and OAuth provider configuration for those optional integrations.

Local mocks or compile-time wiring must not be reported as live provider delivery.
