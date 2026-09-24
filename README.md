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

Pass 0 established the executable skeleton, CI, and acceptance harness. Pass 1 implements the canonical local domain, SQLite schema/migrations, account-scoped repository boundary, optimistic concurrency, server revisions, and audit/change records. Pass 2 adds immutable revision/hash-bound `PlanningSnapshot` inputs plus a sound tri-state feasibility core. Pass 3 completes the first executable vertical slice with immutable `PlanSnapshot` history, derived WORK/Event projections, deterministic risk and colour projections, constraint-aware latest-safe-start, 1–5 explainable next actions, and a persistent CLI capture/plan flow. Pass 4 adds immutable source/evidence records, typed observations, reversible source binding, versioned field reconciliation, explicit override/conflict history, provenance-aware conservative cutoff projections, and reconciliation-bound plan invalidation. Pass 5 adds a read-only Google Calendar Events connector with durable sync tokens, connector health, bounded retry, explicit deletion evidence, HTTP 410 full-resync recovery, idempotent replay, and optimistic concurrency that prevents stale sessions from overwriting newer checkpoint/health state. Pass 6 adds a tool-less typed LLM extraction boundary plus a server-bound authenticated action gateway with durable intents, expected-version checks, atomic idempotency replay, prompt-injection fail-closed behavior, private-place alias redaction, and cross-account isolation. Pass 7 adds canonical Places/current-location inputs, source-backed TravelEstimate history, location-bearing Event semantics, derived travel/buffer PlanBlocks, route staleness and unknown-origin fail-closed behavior, and travel-aware hard feasibility without converting booked MOVE Events into derived commute. The first product UI slice adds a loopback-safe FastAPI application boundary and responsive browser shell for Today, Plan, Tasks, Calendar, Evidence, Places, Ask and Settings while preserving those same owners. Pass 8 adds canonical recurring templates, stable occurrence identity and overrides, deterministic local-civil/DST expansion into the ordinary planning path, plus revision-bound notification workflow state with suppression, quiet hours, snooze and completion-follow-up gating. The first Pass 9 reliability slice adds verified SQLite backup/restore, an explicit account-scoped JSON export contract, private file permissions and a sensitive-data export surface in Settings without conflating backup with user export. The next Pass 9 slice adds an explicit account-deletion policy: immediate account-scoped purge plus a 30-day minimal replay/account-id tombstone, revision/typed confirmation, and fail-closed handling of future unclassified data stores. The notification hardening slice adds a durable SQLite delivery outbox with leases, restart-safe retry/backoff and a stable channel idempotency key; delivery is explicitly at-least-once unless the external channel honors that key. AT-64 lifecycle hardening makes cancellation immediately disappear from future work/travel/notification projections while preserving immutable plan history and canonical pinned-work intent for a later reopen. The hosted/mobile slice (schema v10) adds registration/login with hashed bearer sessions, a mobile-first RU/EN client and a Capacitor Android app built from the same client. The normative baseline used by implementation is [docs/SPECIFICATION.md](docs/SPECIFICATION.md), version 2.1.

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

`domain-smoke` verifies canonical persistence/versioning. `feasibility-smoke` exercises repository → immutable snapshot → tri-state feasibility. `planner-smoke` exercises the full first vertical slice: canonical Task/Event → snapshot → plan → risk → next actions → persisted derived plan history. `reconciliation-smoke` exercises immutable evidence → conflicting cutoff reconciliation → separately labelled conservative planning projection while preserving conflict truth. `connector-smoke` exercises Google Calendar-shaped provider data → immutable evidence → durable complete-session checkpoint and health state without live OAuth/network access. `agent-smoke` exercises server-minted explicit intent → version-checked cancellation → durable idempotent replay without exposing a generic LLM mutation surface. `travel-smoke` exercises current location + a fresh route estimate + a location-bound Event → immutable travel projection → 15:05 latest-safe-departure and hard arrival buffer. `recurrence-notification-smoke` exercises stable recurrence identity across a moved occurrence, DST-aware local-civil expansion and versioned notification snooze workflow (introduced in schema v7 and preserved through later schemas). `notification-delivery-smoke` proves lease persistence across restart, expired-lease reclamation with the same delivery key, and durable delivered state. `reliability-smoke` takes a consistent SQLite backup, exports one account, performs a separate account deletion, restores to a new database, and verifies canonical state, connector checkpoint, notification workflow continuity and deletion-tombstone policy. A persistent manual flow is available through `account-init`, `task-add`, `event-add`, `plan`, and `task-complete`; operator data-lifecycle commands are `backup`, `restore`, `account-export`, `account-delete`, and `deletion-tombstones-purge`.

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
7. Reliability/security/conformance hardening before any production claim. **In progress: backup/restore, account export, account deletion/tombstone policy, and durable notification delivery are implemented in Pass 9.**
8. Offline client replication only if product evidence justifies it.

## License

Licensed under the [Apache License 2.0](LICENSE). See [docs/LICENSING.md](docs/LICENSING.md).

## Name

`Student Execution OS` is a working project name and may change before public release.
