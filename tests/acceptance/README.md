# Acceptance test harness

This directory is keyed to normative acceptance-test identifiers in `docs/SPECIFICATION.md` v2.1.

`acceptance_registry.json` keeps the first implementation vertical-slice IDs (`AT-11`–`AT-32`, `AT-75`, `AT-78`–`AT-81`) explicit and also records supplemental tests pulled forward when a pass owns their invariant.

Pass 1 provides executable acceptance coverage for:

- `AT-11` target vs cutoff independence;
- `AT-16` dependency cycle rejection;
- `AT-17` penalty milestone vs final cutoff separation;
- `AT-73` no duplicate hard-cutoff owner;
- the domain-state half of `AT-75` (ABSENT vs UNKNOWN; risk remains Pass 3);
- `AT-80` aggregate concurrency ownership;
- `AT-81` unsupported flexible Event is rejected, not coerced;
- `AT-83` half-open adjacency.

A status is marked PASS only where the complete acceptance statement is implemented. Partial statuses name the remaining owning pass rather than treating scaffolding or a narrower assertion as full conformance.

Pass 2 adds executable coverage for `AT-12`, `AT-15`, `AT-19`–`AT-21`, `AT-27`, `AT-78`, and `AT-79`, plus supplemental `AT-58`, `AT-70`, and `AT-86`. The engine deliberately returns `UNKNOWN` for unsupported/sub-minute inputs, exhausted exact-search budgets, and no-cutoff horizon exhaustion rather than converting uncertainty into `INFEASIBLE`.
