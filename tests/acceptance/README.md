# Acceptance test harness

This directory is keyed to the normative acceptance-test identifiers in `docs/SPECIFICATION.md` v2.1.

Pass 0 establishes the executable harness only. `acceptance_registry.json` lists every acceptance ID required by the first implementation vertical slice (`AT-11`–`AT-32`, `AT-75`, `AT-78`–`AT-81`) and deliberately marks them `PENDING_*`; no product behavior is reported as passing merely because scaffolding exists.

As later passes implement semantics, each acceptance ID should gain an executable test/fixture and the registry status should be updated only with observed evidence.
