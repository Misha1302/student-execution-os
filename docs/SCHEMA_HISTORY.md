# Schema and product history

What each schema version introduced, oldest narrative first. This is history: the
current state is in the [README](../README.md) and the code
(`persistence/sqlite.py::SCHEMA_VERSION`, `persistence/migrations/`). Decisions are
recorded in the [ADRs](adr/).

## v12 – v22

By schema **v22**, earlier passes established the canonical task/event domain, tri-state feasibility, planning/risk, evidence reconciliation, connector checkpoints, travel, recurrence, hosted auth, backup/export/deletion, and the browser/Android client. V12 completes the execution transition:

- one reminder model (`reminder_states` + `reminder_messages`) replaces the removed v7–v11 notification runtime;
- reminder scheduling reacts to deadline/risk/start/progress/snooze/completion, while delivery retries remain a separate leased outbox concern;
- `started_at` and `last_progress_at` are canonical execution facts;
- `/api/v1/sync` stores client operation results atomically for exactly-once replay and explicit conflicts;
- the mobile client persists cached reads and pending task operations across restart, then reconnects with the same operation ids;
- Assistant providers (OpenAI, Anthropic, and OpenAI-compatible) run server-side with the account's own key and can only return validated proposals that the user applies through the controlled action boundary;
- Capacitor push and speech recognition are native dependencies, with degraded behavior when Firebase/LLM configuration is absent.

Schema v13 makes the student flow the primary path: **"+" → "Что нужно сделать?" → text or
voice → task card → Create**.

- A deterministic RU/EN parser (`agent/nlparse.py`, mirrored on the device in
  `web/static/js/nlparse.js` and held to the same fixtures) turns phrases like
  "В пятницу к шести сдать лабораторную по физике, займёт часа два, это важно" into
  deadline, effort, importance, category, work window, reminder and chunking — offline and
  without an LLM. A configured LLM refines the card; its proposal is validated field by
  field and applied through the same mapping as `task.create`, so nothing is dropped.
- Capture shows a compact interpretation with optional fact editors. Quick tasks without
  an estimate use a clearly provisional 15–60 minute range (30 minute planning nominal),
  not a fabricated user estimate. Explicit unknown values remain supported by the API.
- Conversational corrections and voice follow-ups update the same semantic draft; manual
  edits remain authoritative. Material time disagreements ask about concrete meanings,
  not parser implementations. Accidental closing/reload restores account-scoped drafts
  for up to seven days; successful creation or explicit discard clears them.
- First-run help opens a real guided Capture. Escape does not complete onboarding;
  Skip or creating the first item does. Today leads with the next action.
- Tasks can be fully edited and rescheduled; `remind_at` is an explicit reminder request.
  Snooze (from the app or a notification) schedules the next reminder at that moment.
- The Android app renders reminder pushes itself (data-only FCM for devices declaring
  `reminder-actions-v1`) with working Start / Done / Snooze buttons that run as offline-safe
  `/api/v1/sync` operations in WorkManager.
- Expired Assistant previews (typed/dictated text) are deleted; operation logs and the
  reminder inbox have retention windows (`reliability/retention.py`).
- Technical details (schema, revisions, providers, sources) live under Settings → Advanced.

Schema v14 makes AI **bring your own key** (ADR 0017): each account can add its own
OpenAI, Anthropic or OpenAI-compatible key in Settings → AI. The key is encrypted with a
master key kept outside the database, bound to its account, shown only as `sk-••••abcd`,
and deleted with the account. Without a key everything works with the local parser. The
server-wide `SEOS_LLM_*` key is gone; operator credentials (`SEOS_PLATFORM_LLM_*`) serve
only accounts with a platform-managed entitlement — the seam for a future paid plan
([roadmap](ROADMAP.md)).

Schema v21 ships opt-in **STARTER** AI: with `SEOS_STARTER_LLM_ENABLED=1`, accounts
receive a basic platform-managed entitlement protected by atomic per-account and global
request/token caps. Platform keys stay in API-only files and never enter SQLite or a
client. BYOK always has priority and does not spend STARTER quota; exhausted or
unavailable STARTER falls back to the local parser. This is not a paid billing tier.

Schema v22 turns the product into the **botay! closed-beta capture loop**:

- the same `+` flow proposes **Task / Event / Note** and uses Note as the safe fallback when typed text has no clear scheduling intent;
- Notes are canonical account data with optimistic versions, archive/delete lifecycle, provenance links and the existing exactly-once client operation boundary;
- voice Notes store original audio separately from transcript and user-edited text; transcription failure never destroys the recording;
- Notes participate in backup/restore, account export and account deletion; audio is stored as SQLite BLOB data rather than large base64 request payloads;
- Today exposes every canonical Event intersecting the user's local day and gives a currently-running fixed Event priority over generated work suggestions;
- the user-facing brand, PWA/Android identity and beta feedback surface are **botay!**, while stable technical identifiers remain unchanged.


Schema v15 makes the app **offline-first** and adds fixed-time events (ADR 0018):

- Every task/event change (create, edit, start, done, «не сейчас», reschedule, won't do,
  archive, delete) is queued durably on the device and shown on every screen at once;
  the queue is sent in the background and replayed exactly once after reconnect or restart.
- "Сегодня с 21 до 22 провести занятие по программированию" becomes an event 21:00–22:00
  (1 h) with a clean title and no deadline, with an optional reminder before it.
- Tasks: «Не буду делать», archive/restore and delete (tombstoned against late replays);
  counted progress ("3 из 10 задач").
- Sleep hours (Settings) keep work out of the night and reminders quiet; Plan shows seven
  days with swipe; Today never hides open tasks; reasons are plain sentences.

The normative baseline used by implementation is [docs/SPECIFICATION.md](SPECIFICATION.md), version 2.1.

Schema v18 closes the first actual-execution loop (ADR 0021):

- starting a Task creates a canonical Execution Session rather than treating a single
  `started_at` timestamp as the work history;
- pause/resume creates active segments, so pause time is never counted as work;
- actual work, planned work and remaining effort stay separate — finishing a session
  asks the user whether the Task is done, how much remains, or whether the estimate
  should stay unchanged;
- execution lifecycle commands use the same durable offline queue / exactly-once sync
  boundary as Tasks and Events, including explicit cross-device conflicts;
- Today gives an active session priority, Task detail shows execution history, and
  active execution is cached for offline restart;
- execution history participates in account export/deletion and is covered by Python
  and device-side projection regressions.

Plan Control and Projects build on schema v18 without duplicating planner state:

- pin/move/avoid gestures write canonical `UserTimeConstraint` records and preview the
  real planner result before apply; `PlanBlock` remains derived;
- Projects remain containers over canonical Tasks/Events and Milestones; progress and
  project risk are derived from member state rather than stored as another truth.

Schema v19 adds recurring **work** separately from recurring calendar Events (ADR 0024):

- a work-routine template owns recurrence/timezone/default effort;
- each occurrence has stable recurrence identity and materializes one ordinary Task,
  so planner, execution sessions and progress use the existing Task model;
- one occurrence can be skipped/reopened/edited without changing the series identity;
- "this and future" performs a series split and refuses to rewrite future occurrences
  that already contain user history;
- routine state is offline-safe and included in account export/deletion.

Schema v20 adds Reflection & Calibration (ADR 0025) without creating a second truth
for progress or effort:

- Daily Intent stores at most three explicit Task priorities plus a note and acts only
  as a soft planner tie-break; closing the day removes that signal;
- daily/weekly review derives planned-vs-actual, completion, carry-over and schedule
  churn from saved PlanSnapshots, canonical Tasks and Execution Sessions;
- estimate calibration is suggested only after repeated completed-task evidence and
  never applies automatically;
- an accepted category multiplier changes only the planning/risk projection; the
  Task's canonical estimate and remaining effort remain unchanged;
- intent/calibration mutations are offline-safe, while stale derived WORK blocks are
  hidden until the server replans.

## v23 – v30

- **v23** — class series exceptions with stable external identity: the USER layer of
  an occurrence survives source refreshes ([ADR 0027](adr/0027-series-exceptions-and-external-identity.md)).
- **v24** — academic schedule provider and iCalendar connection ([ADR 0028](adr/0028-academic-schedule-provider.md)).
- **v25** — external capability grants, REST surface and MCP ([ADR 0030](adr/0030-external-capabilities-mcp.md)).
- **v26** — OAuth consent for ChatGPT/Codex that issues capability grants ([ADR 0031](adr/0031-oauth-connect-chatgpt-codex.md)).
- **v27** — collaborative academic groups on the SOURCE/USER model ([ADR 0032](adr/0032-collaborative-groups.md)).
- **v28** — `auth_rate_limits`: login/IP abuse limits persisted in SQLite (hashed scope
  keys, expiry, bounded cleanup), so they survive restarts and are shared by workers.
- **v29** — `assistant_action_history`: the stored inverse and committed version of each
  reversible Assistant action, so «отмени последнее» / [Отменить] can revert the last
  Assistant apply without overwriting newer changes (30-day retention).
- **v30** — `planning_preferences`: canonical soft planning preferences (KEEP_FREE,
  WORK_LIMIT, AVOID_WORK, REST_AFTER_EVENTS) with daily windows and date ranges. The
  planner expands them into the snapshot and uses them only to choose among legal
  placements once the hard model is FEASIBLE; each is reported APPLIED / RELAXED /
  UNSATISFIABLE. Rollback script drops the table (and the preferences in it).

## v31 – v33

- **v31** — tracked check-ins and recurring reminders ([ADR 0034](adr/0034-checkins-and-recurring-reminders.md)):
  `checkin_templates`, `checkin_occurrences` (the outcome owner), `reminder_series`,
  `reminder_series_occurrences` (series occurrence → standalone reminder), and
  `deleted_entities` (tombstones for the kinds added from v31 on). Rollback drops them;
  materialized reminders remain valid one-shot reminders.
- **v32** — places as a product surface, typed location triggers and routing
  bookkeeping ([ADR 0035](adr/0035-places-location-triggers-routing.md)): `places.routing_allowed`,
  `location_triggers`, `route_refresh_state`. Rollback drops the tables and rebuilds
  `places` without the consent column.
- **v33** — checklists inside Tasks ([ADR 0036](adr/0036-checklists-quotas-project-progress.md)):
  `task_subtasks`. Rollback drops it; Tasks are unchanged.

Rolling back across v28–v33 is normally an **application** rollback only: v28–v33 add
tables (v32 also a column with a default), and an older build starts on the newer
database and ignores them. While it runs,
its account export/deletion refuse the tables it cannot classify (422) rather than
skipping them; the rollback scripts are needed only if those must work on the old build.
See deploy/README.md, "Rollback".
