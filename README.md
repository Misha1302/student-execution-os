# Student Execution OS

Student Execution OS is an experimental workload-planning system for students and other high-load users.

It is **not** designed as another generic TODO list. The core idea is to turn fragmented obligations, fixed events, deadlines, travel constraints, and available calendar capacity into a trusted, explainable, executable plan.

## Product thesis

The system should answer three questions:

1. **What do I need to do?** — capture obligations from manual input, LLMs, LMSs, calendars, documents, and future connectors.
2. **What can I realistically do?** — combine remaining work, fixed events, location/travel, available capacity, and deadlines.
3. **What should I do now?** — produce a small explainable execution queue and warn before the plan becomes infeasible.

Canonical pipeline:

```text
Sources
  -> Capture
  -> Provenance / Deduplication
  -> Reconciliation
  -> Canonical Workload Model
  -> Capacity + Location + Travel
  -> Risk Engine
  -> Planner
  -> Execution Queue
  -> Notifications / User Actions
  -> Feedback
```

## Core design principles

- **Server is canonical state.** Clients, LLMs, and connectors are not independent sources of truth.
- **Source != Extractor != Actor.** A PDF parsed by an LLM is still sourced from the PDF; the LLM is the extractor.
- **Actual cutoff != target != actionable-from != scheduled work != event time.**
- **Travel is a transition between location-bound blocks**, not a duplicate TODO and not a fixed return trip.
- **Importance, urgency, and risk are separate concepts.**
- **Automation must be explainable and auditable.**
- Low-confidence critical facts must not silently become canonical.
- External source changes must be reconciled rather than blindly overwriting user planning decisions.

## Repository status

This repository currently contains the normative product/system specification and project-governance files. Application code has not been started yet.

## Documentation

- [Normative specification](docs/SPECIFICATION.md)
- [Licensing decision](docs/LICENSING.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)

## Planned implementation order

1. Domain core: obligations, tasks, events, milestones, time semantics.
2. Canonical server and sync.
3. Capture API + LLM adapter + provenance/reconciliation.
4. Capacity/risk engine and explainable next actions.
5. Travel-aware planning.
6. One authoritative academic connector brought to high reliability.

## License

Licensed under the [Apache License 2.0](LICENSE).

Apache-2.0 was selected over MIT primarily because this project is expected to expose public APIs, SDKs, connectors, and extension points; Apache-2.0 remains permissive while adding an explicit patent grant and clearer NOTICE/patent terms. See [docs/LICENSING.md](docs/LICENSING.md).

## Name

`Student Execution OS` is a working project name and may change before public release.
