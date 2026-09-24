# Pass 9/10 conformance gap ledger

Fresh baseline for this ledger: post-AT-63 `main` tree `4e248cd059df91c4c08b8d2aeb500df0bb07d53c` / merge commit `64ea121c98d6cc521fc581b99fb5a2fa1086456d`, with exact post-merge CI #104 green. Re-read live state before treating this checkpoint as current.

| Area | Normative acceptance / owner | Baseline | Current Pass-9 slice | Next dependency |
| --- | --- | --- | --- | --- |
| Migration | AT-61 / §25.1 | v6→v7 executable migration exists | tracked as conformant evidence | destructive-migration rollback declaration if one is introduced |
| Backup/restore | AT-62 / §25.2–25.3 | missing | implemented + executable restore proof | production encrypted backup store is deployment-specific |
| Account deletion | AT-63 / §24.2 | missing at baseline | implemented: immediate account purge + 30-day minimal replay/account-id tombstone, revision + typed confirmation, future-table fail-closed | external secret/replica revocation remains deployment-specific |
| Cancel/reopen projection cleanup | AT-64 | partial canonical lifecycle only | closed in current slice: inactive obligations leave planning/travel; pinned-work rows are projection-filtered rather than deleted; undelivered linked notifications/outbox are suppressed; reopen restores planning | in-flight external delivery remains governed by ADR 0013 at-least-once boundary |
| Hybrid occurrence mode | AT-65 | alternative-location representation absent | closed in schema v11: versioned options + explicit selection; route projection fails closed before selection | external routing remains configuration-dependent |
| Optional event policy | AT-66 | fail-closed UNKNOWN for OPTIONAL/PREFERRED | closed in schema v11: explicit profile policy + omission explanation; REQUIRED stays hard | — |
| Older source revision | AT-67 | covered Pass 4 | closed | — |
| Notification storm suppression | AT-68 | covered Pass 8 | closed + durable delivery outbox added in current slice | external channel must honor stable delivery key for provider-side idempotency |
| Account export | AT-69 / §24.2 | missing | implemented explicit account allowlist | deletion/retention policy should share the same data classification |
| Exact feasibility timeout | AT-70 | covered; total RiskEngine budget repaired Pass 8 | closed | expose deployment profile budget in final hardening |
| Single active source binding | AT-71 | covered Pass 4 | closed | — |
| Restart durability | AT-44 / §25.3 | action idempotency + backup/restore covered | durable notification lease/outbox, expired-lease reclaim and retry/backoff added in current slice | external provider exactly-once is not claimed; stable delivery key supports provider idempotency |
| Production auth/TLS | §24 | local loopback guard only | intentionally open | deployment boundary; no fake production claim |
| OAuth secret lifecycle | §24 | no production OAuth/token store | intentionally open | provider integration / secret manager authority required |
| Observability | §25 | audit, plans, connector sessions exist | structured correlation/latency logging, persisted plan/solver/notification metrics, connector/capability diagnostics; no raw payloads | production metrics exporter remains deployment-specific |

## Pass order after this slice

1. Final Pass 10 install/restart/migration/export/deletion/security/observability closure.
2. External provider qualification (FCM credentials, routing, OAuth, optional LLM) without replacing `UNCONFIGURED` with simulated success.
