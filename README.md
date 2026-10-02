# botay!

**botay!** turns tasks, fixed events and quick captures into a realistic day plan, then keeps actual work separate from what was merely planned.

It is **not** another generic TODO list and it is not intended to replace every LMS or calendar. Its core is a trustworthy interpretation/planning layer over fragmented user and external evidence.

## Product thesis

The system should answer:

1. **What do I need to do?** — reconcile manual input, source systems, calendars, documents, and later LLM extraction into one local model without pretending that external facts belong to the server.
2. **What can I realistically do?** — evaluate hard constraints, effort, fixed events, dependencies, location/travel, and uncertainty using a constraint-aware feasibility contract.
3. **What should I do now?** — produce a small explainable plan/next-action queue and update it safely when reality changes.

Canonical architecture:

```text
External / user evidence
        ↓
Immutable SourceRecords + Observations
        ↓
Identity matching + field reconciliation
        ↓
Server-owned local domain + user intent
        ↓
PlanningSnapshot
        ↓
Feasibility → Planner → Risk / Next actions
```

## Core design principles

- External systems own their assertions; the server owns **local interpretation, user intent, execution state, and projections**.
- **Source != Extractor != Actor.**
- Evidence is immutable; false dedup/matching must be reversible without evidence loss.
- **Actual cutoff != target != actionable-from != event time != planned work.**
- Project is a container, not a schedulable Task/Event subtype.
- User scheduling constraints are canonical inputs; PlanBlocks are derived immutable projections.
- Raw free minutes are not proof of feasibility.
- `FEASIBLE` needs a legal witness; `INFEASIBLE` needs sound proof; heuristic failure alone is `UNKNOWN`.
- Commute is derived plan state; a booked journey is a canonical moving Event.
- LLMs are interfaces/extractors. Imported content is data, never tool authorization.
- Connector/source synchronization is separate from client/offline replication.
- Automation must remain explainable and auditable.

## Main loop

```text
say / type / import something
  → botay! interprets it (language model when available, local parser offline)
  → typed Task / Event / Reminder / Note / constraint / question / command
  → a deterministic preview of the effective change ("было → станет")
  → the user confirms; the server validates authority, scope and version and applies it
  → the planner derives a realistic plan from canonical state
  → actual work is recorded separately from the plan, and future plans adapt
```

## Current status

Schema **v30** (`persistence/sqlite.py::SCHEMA_VERSION`, migrations in
`src/student_execution_os/persistence/migrations/`). What each version added is in
[docs/SCHEMA_HISTORY.md](docs/SCHEMA_HISTORY.md).

Implemented and covered by the test suites:

- canonical Tasks, Events, Reminders, Notes, Projects, recurring work and class series,
  with tri-state feasibility, derived plans, actual Execution Sessions and reflection;
- offline-first clients (browser and Android) with a durable operation queue and
  server-side exactly-once replay (`/api/v1/sync`);
- external sources (academic iCalendar schedules, groups) on the SOURCE/USER model,
  capability grants / MCP and OAuth connect;
- hosted auth with persistent login/IP abuse limits;
- the **Assistant**: typed semantic intents validated server-side; guarded and
  approximate relative rescheduling resolved deterministically; bounded authorized
  target candidates, with the server — not the model — deciding whether a pick is unique
  (several equally plausible items become a choice for the user); multi-turn follow-ups («нет, лучше
  в 10:30») where explicit user edits win over later model output; voice and text in the
  same pipeline; read-only questions answered from server facts; planner-control
  language stored as canonical constraints, and soft wishes («оставь вечером час
  свободным», «день полегче», «после пары полчаса отдыха», «учёбу до девяти», «не
  ставь сложное сразу после подъёма») as canonical planning preferences that steer
  placement but never decide feasibility; dependency-ordered multi-action plans whose
  dependent times are derived by the server; and undo of the last Assistant apply that
  refuses to overwrite newer changes;
- an explicit provider retry policy: a retry only when no (billed) generation can have
  run, never a blind resend after a read timeout, one total latency budget and attempt cap
  per operation, one structured-output repair, BYOK-safe fallback to the local parser, and
  privacy-safe reliability metrics (`agent/reliability.py`).

Known limits:

- Assistant quality with a live provider is evaluated only on demand
  (`python -m student_execution_os llm-eval`, opt-in, needs a configured provider); CI
  uses deterministic fake providers, which prove contracts, not model quality.
- On Android the offline queue is in native SQLite and the bearer token in the Android
  Keystore (migrated from earlier releases at start-up; if that migration fails, the existing
  pre-upgrade token stays in plain app storage until it succeeds); the read-model cache stays in
  WebView storage by design. See [mobile/README.md](mobile/README.md).
- Undo covers Assistant-originated creates, updates, reschedules and snoozes, not
  every manual operation (manual operations have their own short-lived undo).

## Architecture boundaries

| Area | Owner |
| --- | --- |
| Canonical domain and invariants | `domain/`, `persistence/` (SQLite, versioned migrations) |
| Planning | `planning/` (snapshot → feasibility → planner → risk; plan blocks are derived) |
| Writes from clients | `sync/commands.py::SyncService` owns the envelope, op_id replay and transaction; one domain handler per operation type in `sync/handlers/` |
| Reads and app services | `web/queries.py::UiService` composes the owners in `web/services/`; routes in `web/app.py` call them explicitly |
| Assistant | `agent/` — providers and reliability, typed proposals, deterministic temporal resolution, read queries, apply/undo |
| External sources | `connectors/`, `recurrence/`, `academic/`, `groups/` |
| Clients | `web/static/` (no-build ES modules), `mobile/` (Capacitor Android shell) |

## Run the current implementation locally

Requires Python 3.12+. The web/API and browser-verification surfaces use the pinned dependencies in `requirements.txt` / `requirements-dev.txt`.

```bash
make verify
```

The executable smoke surfaces are:

```bash
PYTHONPATH=src python -m student_execution_os health
PYTHONPATH=src python -m student_execution_os domain-smoke
PYTHONPATH=src python -m student_execution_os feasibility-smoke
PYTHONPATH=src python -m student_execution_os planner-smoke
PYTHONPATH=src python -m student_execution_os reconciliation-smoke
PYTHONPATH=src python -m student_execution_os connector-smoke
PYTHONPATH=src python -m student_execution_os agent-smoke
PYTHONPATH=src python -m student_execution_os travel-smoke
PYTHONPATH=src python -m student_execution_os recurrence-notification-smoke
PYTHONPATH=src python -m student_execution_os notification-delivery-smoke
PYTHONPATH=src python -m student_execution_os reliability-smoke
```

The reminder smokes exercise recurrence identity, snooze state, delivery lease recovery after restart, and separation of delivery retry from creation of a new user reminder. `reliability-smoke` verifies backup/restore, export, connector checkpoint, reminder continuity, and account-deletion tombstones. The complete suite also covers fresh-database initialization, task start/progress/complete/completed-open/reopen, sync replay/conflict handling, Assistant schema rejection, native integration contracts, and offline reconnect persistence.

### Run the web UI / API server

Install the pinned runtime/development dependencies once:

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium   # only needed for browser QA/tests
```

**Session mode (default, for hosting and the phone app)** — users register and log in;
every request is bound to the account of its bearer session:

```bash
PYTHONPATH=src python -m student_execution_os.web.server --database student-execution-os.db
# add --host 0.0.0.0 to reach it from a phone on the LAN; --registration closed to stop sign-ups
```

**Bound mode (local, single account, no login)** — as before:

```bash
PYTHONPATH=src python -m student_execution_os.web.server \
  --database student-execution-os.db --account local-user --init-account
```

Open `http://127.0.0.1:8765/`. The client is mobile-first (bottom tabs, sheets, RU/EN) and
becomes a side-rail layout on wide screens. Production deployment behind HTTPS is described
in [deploy/README.md](deploy/README.md), with variants for Docker + Caddy and an existing nginx host.

### Android app

`mobile/` packages the same client with Capacitor into an APK that connects to any
session-mode server (address entered on first launch or preset at build time):

```bash
make apk        # or: cd mobile && npm ci && SEOS_SERVER_URL=https://plan.example.com npm run apk:debug
```

See [mobile/README.md](mobile/README.md) for toolchain, LAN testing, CI artifacts and release signing.

## Documentation

- [Normative specification](docs/SPECIFICATION.md)
- [Schema and product history](docs/SCHEMA_HISTORY.md)
- [Product roadmap](docs/ROADMAP.md)
- [Server deployment](deploy/README.md)
- [Android app](mobile/README.md)
- [Current implementation handoff](docs/implementation/HANDOFF.md)
- [Licensing decision](docs/LICENSING.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

Architecture decisions:

- [ADR 0001 — Initial implementation stack](docs/adr/0001-implementation-stack.md)
- [ADR 0002 — Canonical local state, SQLite persistence, and concurrency ownership](docs/adr/0002-canonical-state-and-concurrency.md)
- [ADR 0003 — Immutable PlanningSnapshot and bounded sound feasibility](docs/adr/0003-planning-snapshot-and-feasibility.md)
- [ADR 0004 — Derived PlanSnapshot, deterministic risk, and first execution slice](docs/adr/0004-planner-risk-and-plan-projection.md)
- [ADR 0005 — Evidence, reconciliation, provenance, and cutoff ownership](docs/adr/0005-evidence-reconciliation-provenance.md)
- [ADR 0006 — Google Calendar connector sync ownership and checkpoint semantics](docs/adr/0006-google-calendar-connector-sync.md)
- [ADR 0007 — LLM extraction and authenticated action boundary](docs/adr/0007-llm-extraction-action-boundary.md)
- [ADR 0008 — Travel-aware planning ownership and feasibility](docs/adr/0008-travel-aware-planning.md)
- [ADR 0009 — Web application boundary and product UI](docs/adr/0009-web-application-boundary-and-product-ui.md)
- [ADR 0010 — Recurrence identity and notification workflow ownership](docs/adr/0010-recurrence-and-notification-workflow.md)
- [ADR 0011 — SQLite backup/restore and account export boundary](docs/adr/0011-backup-restore-and-account-export.md)
- [ADR 0012 — Account deletion, retention, and replay tombstone](docs/adr/0012-account-deletion-retention-and-tombstone.md)
- [ADR 0013 — Durable notification delivery outbox](docs/adr/0013-durable-notification-delivery.md)
- [ADR 0014 — Cancellation/reopen projection invalidation](docs/adr/0014-cancel-reopen-projection-invalidation.md)
- [ADR 0015 — Hosted session authentication and mobile-first client](docs/adr/0015-hosted-auth-and-mobile-client.md)
- [ADR 0016 — Daily product surfaces and schema v11](docs/adr/0016-daily-product-surfaces.md)
- [ADR 0017 — Per-account LLM credentials (BYOK) and the platform-managed seam](docs/adr/0017-per-account-llm-credentials.md)
- [ADR 0018 — Offline-first client, fixed-time events, task lifecycle (schema v15)](docs/adr/0018-offline-first-events-and-lifecycle.md)
- [ADR 0019 — Reminders and wake alarms, text commands, one agenda (schema v16)](docs/adr/0019-reminders-alarms-commands-and-agenda.md)
- [ADR 0020 — Signed policy and Android sideload updates](docs/adr/0020-signed-android-updates.md)
- [ADR 0021 — Actual execution sessions and feedback loop (schema v18)](docs/adr/0021-execution-feedback-loop.md)
- [ADR 0022 — Plan control is canonical constraints, not mutable PlanBlocks](docs/adr/0022-plan-control-canonical-constraints.md)
- [ADR 0023 — Projects are containers over existing obligations](docs/adr/0023-projects-as-containers.md)
- [ADR 0024 — Recurring work materializes canonical Tasks](docs/adr/0024-recurring-work-materialized-tasks.md)
- [ADR 0024 — Recurring work materializes canonical Tasks](docs/adr/0024-recurring-work.md)
- [ADR 0025 — Reflection is derived; calibration is explicit and planning-only](docs/adr/0025-reflection-calibration.md)
- [ADR 0026 — botay! Notes, Capture provenance, and original-audio ownership](docs/adr/0026-botay-notes-capture.md)
- [ADR 0027 — Class series exceptions and stable external identity (schema v23)](docs/adr/0027-series-exceptions-and-external-identity.md)
- [ADR 0028 — Academic schedule provider and iCalendar connection (schema v24)](docs/adr/0028-academic-schedule-provider.md)
- [ADR 0029 — Production LLM path: classification, egress determinism, STARTER accounting](docs/adr/0029-production-llm-path.md)
- [ADR 0030 — External capability grants, REST surface and MCP (schema v25)](docs/adr/0030-external-capabilities-mcp.md)
- [ADR 0031 — Connecting ChatGPT/Codex: OAuth consent that issues capability grants (schema v26)](docs/adr/0031-oauth-connect-chatgpt-codex.md)
- [ADR 0032 — Collaborative academic groups on the SOURCE/USER model (schema v27)](docs/adr/0032-collaborative-groups.md)
- [ADR 0033 — Explicit reminders follow source-driven changes](docs/adr/0033-reminders-follow-source-changes.md)

## License

Licensed under the [Apache License 2.0](LICENSE). See [docs/LICENSING.md](docs/LICENSING.md).

## Name

The product is **botay!** (lowercase, with the exclamation mark). Internal package, API,
environment and deployment identifiers keep the original `student_execution_os` / `SEOS`
names where renaming would add migration or release risk without a user-facing benefit.
