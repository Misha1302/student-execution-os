# Student Execution OS

Student Execution OS is an experimental workload-planning system for students and other high-load users.

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

## Repository status

Implementation and product specification for Student Execution OS belong in this repository. `Misha1302/chatgpt-knowledge-base` is a separate system and is not an implementation target for this product.

The current MVP uses schema **v15**. Earlier passes established the canonical task/event domain, tri-state feasibility, planning/risk, evidence reconciliation, connector checkpoints, travel, recurrence, hosted auth, backup/export/deletion, and the browser/Android client. V12 completes the execution transition:

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
- Missing values become questions on the card ("Сколько примерно займёт?" · 30 мин · 1 час ·
  2 часа · Не знаю); "don't know" keeps the draft/unknown-deadline lifecycle.
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
([roadmap](docs/ROADMAP.md)).

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

The normative baseline used by implementation is [docs/SPECIFICATION.md](docs/SPECIFICATION.md), version 2.1.

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

The reminder smokes exercise recurrence identity, snooze state, delivery lease recovery after restart, and separation of delivery retry from creation of a new user reminder. `reliability-smoke` verifies backup/restore, export, connector checkpoint, reminder continuity, and account-deletion tombstones. The complete suite also covers fresh-v12 initialization, task start/progress/complete/completed-open/reopen, sync replay/conflict handling, Assistant schema rejection, native integration contracts, and offline reconnect persistence.

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
- [Implementation prompt — first vertical slice](docs/IMPLEMENTATION_PROMPT.md)
- [Implementation stack ADR](docs/adr/0001-implementation-stack.md)
- [Canonical state and concurrency ADR](docs/adr/0002-canonical-state-and-concurrency.md)
- [Planning snapshot and feasibility ADR](docs/adr/0003-planning-snapshot-and-feasibility.md)
- [Planner, risk, and derived plan ADR](docs/adr/0004-planner-risk-and-plan-projection.md)
- [Evidence, reconciliation, provenance, and cutoff ownership ADR](docs/adr/0005-evidence-reconciliation-provenance.md)
- [Google Calendar connector sync and checkpoint ownership ADR](docs/adr/0006-google-calendar-connector-sync.md)
- [LLM extraction and authenticated action boundary ADR](docs/adr/0007-llm-extraction-action-boundary.md)
- [Travel-aware planning ownership and feasibility ADR](docs/adr/0008-travel-aware-planning.md)
- [Web application boundary and product UI ADR](docs/adr/0009-web-application-boundary-and-product-ui.md)
- [Recurrence identity and notification workflow ADR](docs/adr/0010-recurrence-and-notification-workflow.md)
- [Backup/restore and account export ADR](docs/adr/0011-backup-restore-and-account-export.md)
- [Account deletion retention/tombstone ADR](docs/adr/0012-account-deletion-retention-and-tombstone.md)
- [Durable notification delivery ADR](docs/adr/0013-durable-notification-delivery.md)
- [Cancellation/reopen projection ADR](docs/adr/0014-cancel-reopen-projection-invalidation.md)
- [Hosted auth and mobile client ADR](docs/adr/0015-hosted-auth-and-mobile-client.md)
- [Daily product surfaces and schema v11 ADR](docs/adr/0016-daily-product-surfaces.md)
- [Per-account LLM credentials (BYOK) ADR](docs/adr/0017-per-account-llm-credentials.md)
- [Offline-first client, events and lifecycle (v15) ADR](docs/adr/0018-offline-first-events-and-lifecycle.md)
- [Product roadmap](docs/ROADMAP.md)
- [Server deployment](deploy/README.md)
- [Android app](mobile/README.md)
- [Pass 9/10 conformance gap ledger](docs/implementation/PASS9_GAP_LEDGER.md)
- [Current implementation handoff](docs/implementation/HANDOFF.md)
- [Licensing decision](docs/LICENSING.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## Planned implementation order

1. Local Task/Event domain + exact/tri-state feasibility vertical slice.
2. Evidence/reconciliation fixtures and reversible source matching.
3. One reliable real connector with cursor/deletion/staleness semantics.
4. LLM capture/action adapter behind the established evidence/authorization boundaries.
5. Travel-aware planning.
6. Recurrence/notifications as demanded by usage. **Implemented in Pass 8.**
7. Reliability/security/conformance hardening. **Implemented through schema v12.**
8. Offline task-operation replication and execution reminders. **Implemented for the MVP; external FCM/LLM/routing/OAuth providers remain configuration-dependent.**
9. Per-account AI keys (BYOK). **Implemented in schema v14.** Paid/managed AI: see [docs/ROADMAP.md](docs/ROADMAP.md).
10. Offline-first client, fixed-time events, task lifecycle, sleep hours. **Implemented in schema v15.**

## License

Licensed under the [Apache License 2.0](LICENSE). See [docs/LICENSING.md](docs/LICENSING.md).

## Name

`Student Execution OS` is a working project name and may change before public release.
