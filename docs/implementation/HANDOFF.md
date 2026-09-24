# Student Execution OS — Current Handoff

> Re-read the current repository and exact commit before using this checkpoint.

## Current architecture

- Schema: v12.
- API/UI: FastAPI plus the shared browser/Capacitor client.
- Execution state: canonical `started_at` and `last_progress_at` on tasks.
- Offline mutations: `/api/v1/sync`, client-generated operation ids, atomic
  `client_operations` results, field-level edits, progress deltas, and explicit
  lifecycle conflicts.
- Reminders: `reminder_states` is per-task execution-loop state;
  `reminder_messages` is both the in-app inbox and leased push outbox. A new user
  reminder is not a technical retry. The v7–v11 runtime notification package was
  removed by v12.
- Assistant: deterministic degraded parser by default; optional server-side OpenAI,
  Anthropic, or OpenAI-compatible provider. Model output is a typed proposal only;
  preview, explicit confirmation, server validation, atomic apply, and idempotency
  remain application-owned.
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
- an OpenAI, Anthropic, or OpenAI-compatible API credential/model;
- routing and OAuth provider configuration for those optional integrations.

Local mocks or compile-time wiring must not be reported as live provider delivery.
