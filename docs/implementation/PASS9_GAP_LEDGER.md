# Pass 9/10 conformance gap ledger

Fresh baseline for this ledger: post-Pass-9 recovery/export `main` tree `0a5b3c4e7cf9f9358cfd279c38a298fedc0b465b` / merge commit `8498d67d36bf8cbec1fc03ce84324bc66b87423c`, with exact post-merge CI #100 green. Re-read live state before treating this checkpoint as current.

| Area | Normative acceptance / owner | Baseline | Current Pass-9 slice | Next dependency |
| --- | --- | --- | --- | --- |
| Migration | AT-61 / §25.1 | v6→v7 executable migration exists | tracked as conformant evidence | destructive-migration rollback declaration if one is introduced |
| Backup/restore | AT-62 / §25.2–25.3 | missing | implemented + executable restore proof | production encrypted backup store is deployment-specific |
| Account deletion | AT-63 / §24.2 | missing at baseline | implemented: immediate account purge + 30-day minimal replay/account-id tombstone, revision + typed confirmation, future-table fail-closed | external secret/replica revocation remains deployment-specific |
| Cancel/reopen projection cleanup | AT-64 | partial canonical lifecycle only | open | invalidate future derived travel/notification attributable only to obligation; preserve history |
| Hybrid occurrence mode | AT-65 | alternative-location representation absent | open | domain + planner representation change |
| Optional event policy | AT-66 | fail-closed UNKNOWN for OPTIONAL/PREFERRED | open | explicit omission policy + explanation; REQUIRED stays hard |
| Older source revision | AT-67 | covered Pass 4 | closed | — |
| Notification storm suppression | AT-68 | covered Pass 8 | closed | durable sender lease/outbox still needed for crash boundary |
| Account export | AT-69 / §24.2 | missing | implemented explicit account allowlist | deletion/retention policy should share the same data classification |
| Exact feasibility timeout | AT-70 | covered; total RiskEngine budget repaired Pass 8 | closed | expose deployment profile budget in final hardening |
| Single active source binding | AT-71 | covered Pass 4 | closed | — |
| Restart durability | AT-44 / §25.3 | action idempotency covered | strengthened indirectly by AT-62 restore proof | add notification delivery lease/outbox crash-retry semantics |
| Production auth/TLS | §24 | local loopback guard only | intentionally open | deployment boundary; no fake production claim |
| OAuth secret lifecycle | §24 | no production OAuth/token store | intentionally open | provider integration / secret manager authority required |
| Observability | §25 | audit, plans, connector sessions exist | partial | structured correlation/run telemetry without raw sensitive payloads |

## Pass order after this slice

1. Durable notification delivery lease/outbox and restart/crash retry hardening.
2. Remaining domain/planner conformance (`AT-64`, `AT-65`, `AT-66`).
3. Final Pass 10 install/restart/migration/export/deletion/security/observability closure and documented production blockers.
