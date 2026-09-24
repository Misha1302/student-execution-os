# ADR 0016 — Daily product surfaces and schema v11

## Status

Accepted, 2026-09-24.

## Decision

Schema v11 turns the existing planning core into a daily product without changing authority boundaries:

- unknown effort is valid only for a `DRAFT` Task; drafts never enter planning, feasibility, risk, or PlanBlocks;
- Tasks are non-splittable unless the caller explicitly opts in;
- account profiles own timezone, disclosed 08:00–22:00 default windows, week start, and optional-event policy;
- Week and Month are derived from the same snapshot with 7- and 42-day output horizons;
- notification preferences and devices are versioned while delivery keeps the durable, revision-revalidated outbox;
- assistant interpretation creates typed, expiring proposals; apply is principal-scoped, version-checked, confirmed, and idempotent;
- attachments are immutable account-scoped SQLite blobs (10 MiB/file, 50 MiB/account), separately linked and attachment-only on download;
- saved Task views use a strict versioned JSON schema;
- hybrid Event alternatives require a canonical selection before route projection;
- OPTIONAL/PREFERRED omission requires explicit policy and appears in explanations.

## External capabilities

FCM, external LLM, live routing, and OAuth remain `UNCONFIGURED` until deployment providers and secrets are supplied. Deterministic local parsing and injected channel transports are not reported as production provider success.

## Rollback

Migration 011 rebuilds `tasks` and `notifications` to broaden validated states. There is no automatic down-migration. Rollback requires a verified pre-v11 backup because v11-only records cannot be represented losslessly by schema v10.
