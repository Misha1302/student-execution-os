# botay! — Current Handoff

> Re-read the current repository and exact commit before using this checkpoint.

## Current architecture

- Schema: v33 (`persistence/sqlite.py::SCHEMA_VERSION`); what each version added is in
  [docs/SCHEMA_HISTORY.md](../SCHEMA_HISTORY.md). v28 persists login/IP abuse limits; v29
  stores Assistant action history (inverse + committed version) for safe undo; v30 stores
  canonical planning preferences; v31 check-ins and reminder series; v32 places/location
  triggers/routing state; v33 task checklists.
- Check-ins (ADR 0034): `checkins/` owns templates and occurrence outcomes; prompts are
  `reminders` rows armed/closed by the owner (`checkins/repository.py`); reminder series
  in `reminders/series.py`; both materialized by `ensure_horizon` from the worker tick
  (`ReminderEngine._advance_recurring`), reads and commands. Shared expansion:
  `recurrence/expansion.py`. Sync: `sync/handlers/checkins.py`. Client:
  `js/checkins.js`, `js/checkin-overlay.js`, views `checkins`/`checkin`. Assistant:
  `agent/checkin_actions.py`, parser `agent/recurring.py` ⇄ `js/recurring.js`.
- Places/triggers/routing (ADR 0035): `travel/repository.py` (CRUD, reference-checked
  delete), `travel/triggers.py`, `travel/routing.py` (provider + refresh, worker pass),
  `sync/handlers/places.py`; client `js/places.js`, `js/place-overlay.js`; Android
  `geofence/` (platform proximity alerts, `GeofenceSyncWorker`, boot restore). Assistant:
  `agent/place_actions.py`, parser `agent/location_phrases.py` ⇄ `js/location-phrases.js`.
- Checklists/progress (ADR 0036): `subtasks.py`, `sync/handlers/subtasks.py`,
  `js/subtask-overlay.js`; project progress in `web/services/projects.py`. Assistant:
  `agent/checklist_actions.py` (typed `CHECKLIST_STEP`; the server picks the step, equally
  fitting steps go back to the user; device phrase detector `js/commands.js`
  `isChecklistStepPhrase`).
- Quota time in the plan: `checkins/demand.py` → `PlanningSnapshot.quota_demands` (hashed into
  the plan identity) → `Planner` engine-only work → `PlanSnapshot.quota_blocks`
  (`PlanBlockType.QUOTA`, never persisted, never a Task; `QUOTA_DOES_NOT_FIT` when obligations
  would not fit) → `plan.quota_blocks` in `/api/v1/today`; Today/Plan show it.
- Medication alarms: `/api/v1/reminders/alarms` carries `checkin {template_id,
  original_recurrence_id, kind}`; Android `AlarmState` keeps it, `AlarmActivity`/ringing
  notification answer it (`AlarmOps.checkin`), and a page sync without check-in prompts
  (`with_checkin_prompts=false`) keeps the phone's check-in alarms.
- Planning preferences: `planning/preferences.py` (model, expansion, placement filter) and
  `planning/preference_store.py`; `Planner._honour_preferences` re-places work only after the
  hard model is FEASIBLE and relaxes preferences one by one; feasibility never reads them.
- Write ownership: `sync/commands.py::SyncService` owns the operation envelope, op_id replay,
  request hashing, the savepoint and result persistence; `Commands` routes each operation type
  to exactly one domain handler in `sync/handlers/` (explicit registration, fail-closed).
- Read ownership: `web/queries.py::UiService` composes the application owners in
  `web/services/`; routes call an explicit owner (`service.planning.today()` …).
- Assistant: `agent/assistant.py` validates typed proposals, resolves temporal transforms and
  `relative_to` anchors deterministically, applies plans atomically in declared order, and
  undoes the last apply against stored versions; the client side of a turn is
  `web/static/js/assistant-turn.js` + `command-preview.js`.
- Assistant targets: `agent/disambiguation.py` — the server decides whether the model's pick
  is materially unique (authorized candidate set, the user's words, kind words, explicit
  dates/times, previous turn); otherwise the target becomes a user choice. With no
  distinguishing evidence ("перенеси её") the pick continues only if it is the one target the
  previous turn established or the only candidate of a fitting kind.
- Provider reliability: `agent/reliability.py` — delivery-aware retry matrix (NOT_SENT /
  ANSWERED / UNKNOWN), total latency budget via `providers.CALL_DEADLINE`, attempt cap.
  Structured output: `providers.supports_json_schema` declares which endpoint/models get
  `json_schema` (OpenAI; Groq gpt-oss); a request-level refusal is re-sent once in JSON mode
  as a separate, metered reliability attempt (ADR 0029).
- Device storage: `web/static/js/device-storage.js` (`OfflineOperationStore`,
  `ReadModelCache`, `CredentialStore`); on Android the queue is native SQLite and the token is
  Keystore-backed through the `SeosStorage` plugin (`mobile/android/.../storage/`); see
  mobile/README.md for the migration and rollback rules.
- Earlier schema notes, kept for context: v13 explicit `remind_at` reminder requests, device
  capabilities, retention indexes; v14 per-account encrypted LLM credentials and the
  platform-managed entitlement seam; v15 delete tombstones, event reminder leads, counted
  progress — ADR 0018; v16 standalone reminders and wake alarms, device health, AI test failure
  states — ADR 0019; v21 opt-in STARTER entitlements and atomic account/global LLM usage
  reservations.
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
- Assistant: `USER_BYOK > PLATFORM_MANAGED STARTER > deterministic parser`; per account, the user's own
  OpenAI, Anthropic, or OpenAI-compatible key (Settings → AI, `agent/credentials.py`,
  ADR 0017) or, for accounts with a `PLATFORM_MANAGED` entitlement, server-file operator credentials. STARTER is quota-protected and falls back locally. Model output is a typed proposal only;
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
- routing (`SEOS_ROUTING_PROVIDER=yandex` + worker key file, deploy/README.md) and OAuth
  provider configuration for those optional integrations.

Local mocks or compile-time wiring must not be reported as live provider delivery.
