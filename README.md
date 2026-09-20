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

Pass 0 establishes the executable skeleton, CI, and acceptance harness. Product/domain semantics intentionally begin in later passes. The normative baseline used by implementation is [docs/SPECIFICATION.md](docs/SPECIFICATION.md), version 2.1.

## Run Pass 0 locally

Requires Python 3.12+ and no third-party runtime dependencies.

```bash
make verify
```

The executable smoke surface is:

```bash
PYTHONPATH=src python -m student_execution_os health
```

## Documentation

- [Normative specification](docs/SPECIFICATION.md)
- [Implementation prompt — first vertical slice](docs/IMPLEMENTATION_PROMPT.md)
- [Implementation stack ADR](docs/adr/0001-implementation-stack.md)
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
6. Recurrence/notifications as demanded by usage.
7. Offline client replication only if product evidence justifies it.

## License

Licensed under the [Apache License 2.0](LICENSE). See [docs/LICENSING.md](docs/LICENSING.md).

## Name

`Student Execution OS` is a working project name and may change before public release.
