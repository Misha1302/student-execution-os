# ADR 0014 — Cancellation/reopen projection invalidation

Status: Accepted  
Date: 2026-09-21

## Context

Obligation lifecycle state is canonical, while WORK, travel and notification delivery state are projections/workflow. Cancelling an obligation must remove its future derived effects without deleting historical plans or silently discarding user intent that may matter after reopen.

## Decision

Planning admits only ACTIVE Task/Event obligations. Obligation-bound `PINNED_WORK` constraints remain canonical rows, but the planning state source exposes them only while their referenced obligation is ACTIVE.

For a cancelled Event this also removes route-derived TRAVEL_TRANSITION and BUFFER blocks because travel projection is built only from active Events.

The canonical cancellation transaction suppresses undelivered notification workflow rows whose `entity_ref` is the cancelled obligation and terminalizes corresponding non-SENT outbox rows with reason `OBLIGATION_CANCELLED`. Delivered notification history is preserved.

Historical `PlanSnapshot` / `PlanBlock` rows are immutable and are never deleted by cancellation.

Reopen makes the canonical obligation active again. Preserved PINNED_WORK constraints become planning inputs again. A later notification recomputation may rebind the same logical suppression identity and reset a non-SENT outbox row; a SENT delivery remains terminal.

## External delivery race

Cancellation cannot unsend a channel request already accepted outside the SQLite transaction. An in-flight delivery can therefore cross the cancellation boundary. ADR 0013's stable delivery key / at-least-once contract remains the owner of that race; exactly-once or provider recall is not claimed.

## Consequences

- Future work/travel projections disappear on cancellation.
- User pin intent and plan history are preserved.
- Reopen returns the obligation to ordinary feasibility/risk/planning.
- Cancellation does not physically erase historical derived artifacts.
