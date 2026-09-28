# botay! Release Candidate — implementation ledger

One entry per stage. Each claim links to executed evidence (tests, CI runs, commands);
"not run" is written as such.

Live G1–G13 results for a frozen release are not committed here: doing so would change the
revision they describe. `RELEASE_CHECKLIST.md` defines the external immutable GitHub
Release evidence record keyed to the frozen source SHA.

## R1 — Notes, voice, Capture Task/Event/Note, Today events (PR #29)

- **STATUS:** DONE, merged to main (c4d4e20).
- **BASELINE:** PR #29 draft at d54dbfa; schema v22 (`022_botay_notes.sql`).
- **OBSERVED PROBLEM:** the Capture Task/Note decision used an ASCII word boundary, so
  Cyrillic verbs never matched; the upgrade test did not cover a populated v21 database.
- **OWNER:** `web/static/js/nlparse.js` (`captureKind`), `tests/integration/test_v22_botay_notes.py`.
- **CHANGE:** 21dfe5e — Unicode-aware `captureKind`, 11 JS cases; populated v21→v22 upgrade
  test (tasks/events kept, notes link to them, op_id reuse → `OP_ID_REUSED`, re-initialize
  idempotent, `foreign_key_check` clean).
- **TESTS:** full unit suite 388 OK; API + browser OK; `make smoke` OK; a copy of the local
  v9 dev database upgraded to v22 with integrity ok and row counts unchanged; Android
  `assembleDebug :app:testDebugUnitTest :app:lintDebug` OK locally; CI apk ×2 and verify ×2
  green on 21dfe5e.
- **RESULT:** merged.
- **REMAINING RISK:** production has not applied v22 yet (deploy is a separate step).
- **NEXT STEP:** deploy with the standard pre-deploy backup.

## R2 — Capture kind, Task Detail layout, DurationPicker, Today "Soon" (PR #30)

- **STATUS:** DONE, merged to main (`d85ff2f`).
- **OBSERVED PROBLEMS:**
  - Task Detail scrolled sideways at 320px in RU (scrollWidth 324): the `1fr 1fr` action
    grid has a min-content floor. The sweep of every route found the same problem in the
    calendar quick actions and the routines header.
  - `+` had no explicit Task/Event/Reminder/Note choice; Notes had no "new note" button.
  - Durations were chosen by nine different chip sets and minute-number inputs.
  - "Soon" listed tasks only; events lived in a separate day list, so a 00:30 class was
    invisible at 23:00.
  - Found while testing: `/api/v1/today` returned 500 when the plan input returned to an
    earlier state (A→B→A). This bug is in main as well.
  - Found while testing: "1ч35м" was not parsed, and "1.5 часа" was read as the date 1 May,
    leaving "часа" in the title.
- **OWNERS / CHANGES:**
  - `styles.css`: `minmax(0, 1fr)` grids, wrapping labels. No global `overflow-x: hidden`.
  - `capture.js`: kind switch; a chosen kind is never switched by later parses; the
    reminder time is editable.
  - `views/notes.js`: New note / voice note buttons.
  - `duration.js` (new): used by capture, task detail, execution finish, progress, project
    tasks, routines and class series.
  - `nlparse.js` + `agent/nlparse.py`: the same grammar fix in both, in parity.
  - `planning/store.py`: the transition note `REPLAN_INPUT_CHANGED` is not plan content.
  - `upcoming.js` (new, pure) + `views/today.js`; `web/queries.py` adds `upcoming_events`,
    a rolling 12-hour window.
- **TESTS:**
  - `tests/browser/responsive_e2e.py`: real server; 320/360/375/390/412 × ru/en; asserts
    scrollWidth == clientWidth and every action rect in the viewport. Verified that it fails
    without the CSS fix.
  - `browser_ui`: kind switch and New note; DurationPicker (1 ч 35 мин → 95).
  - `tests/web/test_today_upcoming.py`: event in 1 hour, running, late evening, crossing
    midnight, after midnight, recurring, moved occurrence, cancelled occurrence.
  - `tests/js/upcoming_cases.mjs`: tasks + events + reminders merge; a running event goes
    to Now or leads Soon.
  - `tests/browser/today_e2e.py`: "Сегодня в 18:00 созвон с Ариадной" typed into `+` →
    event row in SQLite at 18:00 → Today Soon.
  - `tests/integration/test_plan_store.py`: A→B→A; verified that it fails without the fix.
  - `nl_capture_cases.json`: 4 new cases, server/device parity.
  - Full suites: unit OK, API OK, browser 32 OK, `make static` OK, `make smoke` OK.
- **REMAINING RISK:**
  - The "HSE-imported event in Soon" regression needs the R4 importer. The Google Calendar
    connector records observations bound to tasks and creates no canonical events.
  - Recurring event templates are still edited through REST, not the command boundary.
    That moves in R3.
- **NEXT STEP:** R3 — recurrence exceptions on the command boundary with stable external identity.

## R3 — Class series exceptions and stable external identity (schema v23, PR #31)

- **STATUS:** DONE, merged to main as `e1087cb` (PR #31). Review fixes are `51fd474`;
  documentation follow-up is `4a468b8`.
- **BASELINE:**
  - One override per occurrence (CANCEL, or MODIFY start/duration).
  - No room or teacher.
  - Series mutated through REST endpoints outside the command boundary.
  - No external identity.
- **OWNERS / CHANGES** (ADR 0027):
  - `023_series_exceptions.sql`:
    - override `layer` (SOURCE/USER), detail fields and `reason`;
    - template `location_text` / `teacher` / `source_system_id`;
    - new tables `series_extra_events`, `event_details`, `external_identities`.
  - `rollback/023_series_exceptions_down.sql`.
  - `recurrence/`: layered expansion; `remove_override` (restore), `update_template`,
    `end_series`.
  - `recurrence/source.py`: `SourceApplier`, the provider-agnostic SOURCE writer with
    identity, stale-update ordering and removal.
  - `sync/commands.py`: 9 `series.*` operations. The REST write endpoints are removed.
  - `web/queries.py`: occurrence details and series identity in the calendar, Today and
    events payloads.
  - Client:
    - `compose.js`: `series.create` with room and teacher;
    - `views/calendar.js`: edit / move / cancel / restore, "from this class on",
      extra class, days off;
    - `overlay.projectCalendar` for offline.
- **TESTS:**
  - `test_v23_series_exceptions.py`:
    - exactly-once `series.create` (replay, op_id reuse, id reuse);
    - move + room + cancel + restore of one class; a non-member occurrence is rejected;
    - extra class in Today Soon; holiday range and its undo; split from a date;
    - import + idempotent resync (no version churn);
    - source move / cancel / restore / room change / series-time change;
    - the USER layer survives source updates; a SOURCE cancel survives a user restore;
    - imported series cannot be split;
    - late update ignored; removal keeps history; re-added series restored; a partial
      snapshot never removes;
    - populated v22 → v23 keeps overrides as the USER layer (integrity, FK check,
      idempotent init); rollback gives a v22-identical shape.
  - `tests/js/series_overlay_cases.mjs`.
  - `tests/browser/series_e2e.py` (real server): create series with room → change one
    class's room → cancel → restore → day off. Every step is asserted in SQLite and
    `client_operations`.
  - Migration tests v14/v22 chain the v23 rollback.
  - Full suites: unit 400 OK, API OK, browser 33 OK, `make static` OK, `make smoke` OK.
- **INDEPENDENT HANDOFF REVIEW / FIX:** the PR was not merged on green CI alone. A
  production-shaped reproducer found that a source-cancelled one-off event did not reopen
  when the source restored it. Review also found that a source-wide DTSTART shift detached
  USER overrides and that the documented rollback preconditions were not enforced.
  The follow-up keeps source/user cancellation intent separately for imported one-offs,
  remaps USER identities on an unambiguous civil DTSTART shift, advances occurrence
  sequence metadata even when content is unchanged, applies source timezone changes,
  makes event-detail refresh idempotent, adds child FKs, and makes v23 rollback fail closed.
  Follow-up commit: `51fd474`.
- **HANDOFF VALIDATION:** `make verify` on `51fd474`: static + relay 12/12;
  unit/integration/acceptance 404/404; API 29/29; real-browser 33/33; all smoke
  commands including schema-v23 reliability backup/restore passed. Focused v23 suite
  13/13. Remote PR CI is recorded after push.
- **REMAINING RISK:**
  - Offline projection reads local civil times in the device zone. It is exact when device
    zone = series zone (the default: the composer uses the device zone).
  - Series-level user edits (rename, delete a whole series) are not in scope. Users can
    split or cancel classes.
- **NEXT STEP:** R4 — `AcademicScheduleProvider` + iCalendar/HSE provider feeding `SourceApplier`.

## R4 — AcademicScheduleProvider + iCalendar connection (schema v24)

- **STATUS:** MERGED. PR #32 (`feature/r4-academic-ical`), final head `5a36b93`, merge
  commit `a3d45c6` on `main`. Remote CI on `5a36b93` green: `verify` (static, unit/
  integration, API, smoke, full browser suite) and `apk`. Live HSE feed NOT validated.
- **OBSERVED REALITY:** official current HSE material points students to ЕЛК / HSE App X
  and calendar integration; RUZ is internal/VPN-only. No current public HSE API or live
  student feed was available. A direct HSE login integration is therefore not claimed.
- **OWNER:** `academic/AcademicScheduleProvider` normalizes a source;
  `recurrence/SourceApplier` remains the only canonical SOURCE writer; `/api/v1/sync`
  remains the USER mutation boundary (ADR 0028).
- **IMPLEMENTATION:**
  - RFC 5545 provider for UID, TZID, DTSTART/DTEND/DURATION, DAILY/WEEKLY RRULE,
    EXDATE, RECURRENCE-ID move/cancel, all-day, location, teacher, sequence/update;
  - deterministic per-account connector/source identity and schema-v24 connection state;
  - HTTPS URL (encrypted dedicated key) and `.ics` upload UI in Settings;
  - manual and scheduled refresh, safe status/diagnostics, disconnect;
  - SSRF/redirect/body/timeout/retry controls; URL absent from APIs, logs and export;
  - deploy key mounted only into API + worker; worker receives no LLM credential.
- **FOCUSED TESTS EXECUTED:**
  - `tests.unit.test_academic_ical`: 6/6 (duplicate/reorder/revision conflict,
    unsupported RRULE, SSRF, redirects/auth, bounded retry, 5 MiB limit);
  - `tests.integration.test_v24_academic_schedule`: 10/10 (realistic fixture,
    repeat zero duplicates, move/cancel/room/source-wide/stale ordering, USER survival,
    disappearance/restore, scheduled failure/recovery, v23→v24 + rollback,
    concurrent stale completion ordering, backup→clean restore, export secret exclusion,
    and account-deletion purge with another account preserved);
  - `tests.web.test_academic_schedule_api`: 2/2 (provider→SourceApplier→Today,
    account isolation, malformed/oversize preservation);
  - `tests.browser.academic_schedule_e2e`: 1/1 (390px RU, real server/database,
    Settings upload→Today, repeat import, disconnect, no horizontal overflow).
- **HANDOFF REVIEW / FIX (`63a7f87`):** the first commit (`41eb3cb`) was re-read
  adversarially before PR. Found and fixed:
  - `RECURRENCE-ID`/`EXDATE` written in UTC named the wrong local instance (DST/zone
    semantics); now converted to the series zone, duplicate keys normalized to UTC;
  - unexpected httpx transport errors escaped the reader and could echo the feed URL;
    now `PROVIDER_PROTOCOL_ERROR` without details; unexpected fetch failures close the
    sync session instead of leaving it open;
  - the worker refreshed feeds *before* the reminder tick in the same error boundary
    (a slow provider delayed reminders); now after the tick, own boundary, ≤20 feeds/pass,
    per-account isolation;
  - connect/refresh endpoints blocked the event loop with network I/O; now threadpool;
  - Cyrillic `.ics` file names broke the upload header (Latin-1); now percent-encoded;
  - `sync_interval_minutes: null` gave 500; now 422.
  Reviewed and unchanged: SOURCE/USER precedence stays owned by R3 `SourceApplier`;
  identities are deterministic uuid5 per account; connection/state version preconditions
  inside `BEGIN IMMEDIATE` make a disconnect during refresh win (new test); v24 rollback
  fails closed while a connection exists.
- **FULL VERIFICATION (`63a7f87`, Python 3.13):** `make static` + relay 12/12 OK;
  `make test` 427/427 OK (includes v14/v22/v23/v24 migration + rollback chains);
  `make api` 29/29 OK; `make smoke` OK (incl. reliability backup/restore on v24);
  Compose config (default/Tor/nginx/nginx+Tor) OK; focused R4 25/25.
  `make browser` 33/34 locally: the one failure
  (`test_explicit_alarm_survives_model_omission_and_conflict_up_to_the_queued_create`)
  also fails 2/3 on unmodified `main` in this container (Chromium 1194 vs Playwright's
  expected 1200); `academic_schedule_e2e` passes. Remote CI is authoritative for it.
- **ACCEPTANCE REGISTRY:** the previously reported
  `AcceptanceRegistryTests.test_first_slice_acceptance_ids_are_explicitly_tracked`
  failure does not reproduce: registry and test are untouched by R4 and pass in full-suite
  runs on both `41eb3cb` (422/422) and `63a7f87`. No test was weakened.
- **REMAINING RISK (at merge):** HTTP client re-resolved DNS after validation
  (rebinding window) and honoured ambient proxy variables — closed by R4.1 below.
- **EXTERNAL BLOCKER:** live HSE authentication/subscription validation requires a
  consenting student account or sanitized current feed. Fixture validation is not
  represented as live HSE evidence.
- **NEXT DEPENDENCY:** R4.1 DNS pinning, then R5 production Groq path.

## R4.1 — Academic calendar SSRF / DNS-rebinding hardening

- **STATUS:** MERGED. PR #33 (`fix/r4-academic-dns-pinning`), head `3d4352b`, merge
  `ff0867c` on `main`. Remote CI `verify` green on `3d4352b` (both push and PR runs);
  `android`/`apk` not triggered (path-filtered to `mobile/**` and static web assets).
- **BASELINE (`a3d45c6`), reproduced, not assumed:** `HttpIcsReader` validated
  `getaddrinfo` answers, then handed the *hostname* to a default `httpx.Client`. A
  socket-level spy showed the transport connecting to `('calendar.example', 443)` — i.e.
  resolving again — and, with `HTTPS_PROXY` in the environment, connecting to the proxy
  instead (the proxy resolves the name; the address check is bypassed entirely).
  `ipaddress.is_global` also accepted `::127.0.0.1` and `64:ff9b::7f00:1`.
- **OWNER:** `academic/http.py` (the only calendar egress path; ADR 0028).
- **IMPLEMENTATION:** per attempt resolve once → validate the complete answer set →
  `PinnedTransport` (httpcore pool, verified TLS only) over `PinnedNetworkBackend`, which
  connects only to the validated literals and refuses other hosts/ports/Unix sockets;
  SNI, certificate hostname and `Host` stay the original name; `trust_env=False`;
  embedded-IPv4 (IPv4-compatible, NAT64 well-known and local-use) checked; redirects still
  refused; errors remain fixed codes without URL/cause chain. `httpcore==1.0.9` pinned
  (already the transitive version; now a direct import).
- **TESTS (`tests.unit.test_academic_ical`, 21/21, 13 new):** public address succeeds;
  loopback / RFC1918 / link-local / metadata / CGNAT / IPv6 ULA / mapped / compatible /
  NAT64-embedded-private / mixed public+private rejected before any connection; DNS change
  after validation not consulted; real socket layer (patched `create_connection` +
  rebinding `getaddrinfo` + hostile `HTTPS_PROXY`) connects only to the literal; retries
  use only their own validated set and stop on a rebinding answer; in-attempt failover
  stays inside the set; redirect not followed; real TLS handshake against a local server
  proves SNI + `Host` = original name (IDN: its punycode wire name) and a certificate for another name is rejected;
  unverified TLS contexts refused; URL token absent from `str`/`repr`, no cause chain.
  Before the fix the reproducer connected by hostname / via the ambient proxy; the IDN test
  fails if the pin uses the Unicode name instead of the wire name.
- **LOCAL VERIFICATION (Python 3.13):** `make static` OK; relay `node --test` 12/12;
  `make test` 440/440 OK; `make api` 29/29 OK; `make smoke` OK;
  `academic_schedule_e2e` 1/1; focused R4+R4.1 32/32.
- **KNOWN LIMITATION:** calendar egress is always direct; an operator cannot route it
  through a proxy (by design — the proxy would perform its own resolution).

## R5 — Production Groq / AI path

- **STATUS:** MERGED (code/CI). PR #34 (`feature/r5-production-groq`), head `cab7931`,
  merge `dab7081`. Remote CI on `cab7931`: `verify` ×2 green, `apk` ×2 green.
  LIVE GROQ: **NOT VERIFIED** (exact blocker below).
- **CI FINDING FIXED IN THIS PR (`cab7931`):** the PR run on `08d90f9` failed in
  `series_e2e` (class saved as `МатанализR205`, no room). Root cause: sheets focused their
  first field from `setTimeout(80)`; on a slow runner (or a quick user) the timer fired
  after another field was focused and stole the rest of the typing. `ui.js::focusSoon`
  yields to a text control already chosen in the same sheet; used by all six
  auto-focusing sheets; new browser regression test fails on the old code (`'R2' != 'R205'`).
- **BASELINE (`ff0867c`), observed in code, not assumed:** resolution is
  `USER_BYOK > PLATFORM_MANAGED (STARTER) > deterministic local parser`
  (`agent/credentials.py::resolve`); a stored BYOK key keeps precedence even when it
  fails. Platform primary/standby via `SEOS_PLATFORM_LLM_API_KEY_FILES`, standby only
  after `AUTH`/`QUOTA` (`PlatformProviderPool`). STARTER limits/reservation/reconciliation
  in `agent/usage.py` (atomic `BEGIN IMMEDIATE`). Egress: direct, host-scoped proxy
  (Tor/Privoxy, Squid) or Cloudflare relay, per exact host, both-for-one-host = `REQUEST`.
  Degraded answers already labelled `engine: LOCAL` + `fallback_reason`.
- **DEFECTS FOUND (each reproduced by a test that fails on `ff0867c`):**
  1. Groq 429 (`rate_limit_exceeded` + `…/settings/billing` upsell) → `QUOTA`: sticky key
     status and platform standby failover on a transient limit;
  2. Groq `model_decommissioned` (400) → `REJECTED` instead of `NOT_FOUND`;
  3. STARTER charged the full reservation on refusals that generated nothing, so a
     `SERVER_BLOCKED` outage drained every student's token budget;
  4. direct egress used `trust_env=True`: an ambient `HTTPS_PROXY` silently rerouted
     platform/BYOK traffic (route not deterministic);
  5. user-supplied OpenAI-compatible address: validate-then-re-resolve DNS rebinding
     window, and NAT64/IPv4-compatible private forms accepted;
  6. hostile `Retry-After: 1e309` would raise `OverflowError` (HTTP 500) once parsed;
  7. no sanitized route/result record and no operator live probe.
- **OWNERS / IMPLEMENTATION (no new subsystem):** `agent/providers.py` (classification,
  `Retry-After`, deterministic routes `DIRECT`/`DIRECT_PINNED`/`PROXY`/`RELAY`,
  `student_execution_os.llm` log line without URL/key/token/prompt/body);
  `agent/usage.py` (token refund only for certain no-generation: 401/402/403/404/405/
  413/415/429 or never-sent; request counter never refunded; timeouts/5xx/400 stay
  charged); `netguard.py` (pinning shared with R4.1 calendar reader);
  `agent/smoke.py` + CLI `llm-smoke`; `retry_after_seconds` in interpret and
  connection-test responses (additive). ADR 0029; deploy/README updated.
- **TESTS (MOCKED unless stated):**
  - `tests.unit.test_r5_llm_egress_matrix` 12 new: success + usage + route log;
    401/403 (JSON and HTML)/404/decommissioned/429 Groq/429 insufficient_quota/500/503/
    400 json_validate_failed/302; Retry-After (seconds, date, garbage, nan/inf/1e309,
    bounds); connect timeout/read timeout/DNS/protocol error → `NETWORK` without cause
    chain; empty choices/no choices/HTML 200/null content → `MALFORMED`, non-JSON/no
    actions/actions not list/truncated (`finish_reason=length`) → `FORMAT`; standby only
    for 401/402; operator smoke report (OK/`SERVER_BLOCKED`/`FORMAT`, standby named,
    NOT_CONFIGURED); real socket layer: platform DIRECT ignores `HTTPS_PROXY`/`ALL_PROXY`,
    BYOK custom address resolved once and connected only to the validated literal under a
    rebinding resolver; embedded-private IPv6/CGNAT refused; relay route + relay-vs-Groq
    error classes; secrets (key, relay token, prompt, URL) absent from errors/repr/logs.
  - `tests.integration.test_v21_starter_llm` 17 (7 new, LOCAL INTEGRATION through the
    real API + SQLite): Groq 429 → `RATE_LIMITED`, `retry_after_seconds=7`, no failover,
    tokens 0, labelled LOCAL; `SERVER_BLOCKED` ×3 → no failover, 3 requests / 0 tokens;
    read-timeout/503/400 stay fully charged; standby used for 401 and 402 only, its
    429/success accounted exactly; revoked BYOK → LOCAL `AUTH`, status `INVALID_KEY`, key
    absent, no platform call, no STARTER row; off-switch → no outbound, entitlement kept,
    re-enable restores; 8-thread account-cap race → exactly 3 reservations, counters ==
    ledger, global == account. Existing: global-cap race, period reset, secrets never in
    API/log/DB/export, deletion.
  - `tests.web.test_llm_credentials_api` fake now enforces `trust_env=False` and
    public-only pinned addresses; relay Worker `node --test` 16/16 (4 new: path
    confusion, streamed oversize body, response-header allowlist, token length/case).
- **LOCAL VERIFICATION (Python 3.13):** `make static` OK; relay 16/16; `make test`
  459/459; `make api` 29/29; `make smoke` OK; focused AI suites 80/80. `make browser`
  33/34: `test_explicit_alarm_survives_model_omission_and_conflict_up_to_the_queued_create`
  (page-stubbed API; "timed out waiting for a fresh /api/v1/sync request") fails
  identically 2/2 on unmodified `main` in this container — environmental; CI decides.
- **LIVE GROQ: NOT VERIFIED.** Blockers: (a) this build container's network policy
  refuses `CONNECT api.groq.com:443` (agent proxy `connect_rejected`) and holds no
  platform key; (b) the production API container (which holds the key files and relay
  settings) was not accessed — the only SSH credential available was pasted into chat
  and is treated as compromised, so it was not used. Exact next action for an operator
  with legitimate access: deploy this SHA, then
  `docker compose -f deploy/docker-compose.yml exec api python -m student_execution_os llm-smoke`
  and record `result`/`route`/`model`/`latency_ms` here (expected route `RELAY`).
- **KNOWN LIMITATIONS:** request counters are charged for every attempt (abuse bound);
  a `NETWORK` failure is charged in full even when the connect never happened (httpx
  error classes are merged); relay/proxy endpoints are operator-trusted and not pinned.
- **NEXT DEPENDENCY:** R6 external capability API + MCP.

## R6 — External capability API + MCP (schema v25)

- **STATUS:** MERGED. PR #35 (`feature/r6-external-capabilities-mcp`), head `7f93ffc`,
  merge `4afd2b3`. Remote CI `verify` ×2 green on `7f93ffc` (`apk` not triggered: no
  mobile/static changes).
- **BASELINE (`dab7081`):** no external-agent surface. Session tokens carry the full
  authority of the app. Canonical owners: reads = `web/queries.py::UiService`;
  mutations = typed operations in `sync/commands.py::SyncService` (op_id log in
  `client_operations`, savepoint per op, field-level LWW + lifecycle-intent `CONFLICT`,
  account-scoped lookups). No per-op `expected_version` exists in the sync protocol;
  the conflict contract is the one documented at the top of `sync/commands.py`.
- **OWNER / IMPLEMENTATION (ADR 0030):** `capabilities.py` (grant store, scopes,
  deny-by-default operation→scope map, `destructive` for `*.delete`, SHA-256 token
  hash + constant-time compare, ≤20 active grants, expiry ≤366 d);
  `web/external.py` (`CapabilityGateway` → `UiService` reads with per-scope withholding,
  whole-batch authorization then `UiService.sync(actor=USER_VIA_LLM)`; MCP Streamable
  HTTP stateless JSON at `/mcp`, protocol 2025-06-18/2025-03-26/2024-11-05, tools
  filtered by scope, denials as `isError` tool results); `web/app.py` (session-only
  grant management `/api/v1/settings/capabilities`, grant-only `/api/v1/ext/*` and
  `/mcp`, `INVALID_GRANT` 401 + `WWW-Authenticate`, `CAPABILITY_DENIED` 403);
  migration `025_capability_grants.sql` + fail-closed `rollback/025_…_down.sql`;
  lifecycle: grants are account credentials (purged on deletion, never exported).
- **TESTS (`tests.web.test_capabilities_mcp`, 14, LOCAL INTEGRATION — real app + SQLite):**
  token shown once / only hash stored (full DB dump checked) / revocation immediate
  (REST + MCP); owner isolation (Bob cannot list/revoke Alice's grant), grant ≠ session
  both ways, grant cannot manage grants; scope/expiry validation and expiry at the
  boundary; allowed vs denied reads per scope, Today withholds `inbox_notes` without
  `notes:read`; reads never cross accounts; allowed mutation lands in the app's canonical
  state with `principal_id=grant:<id>` and audit actor `USER_VIA_LLM`; op replay (agent
  and app outbox share the op_id log), `OP_ID_REUSED`; mixed batch with one denied op
  applies nothing and records no op_id; unmapped types (projects, constraints, series
  create/holiday, execution, calibration, transcripts, unknown) denied; `*.delete` needs
  `destructive`; cross-account update/delete/create/`note.link` rejected without leaking;
  malformed envelopes / invalid payload / lifecycle `CONFLICT TASK_CANCELLED`; MCP
  initialize + version negotiation, notification 202, scope-filtered `tools/list`,
  unknown method/tool, batch 400, missing `jsonrpc`, GET 405; MCP `create_task`
  exactly-once by op_id, denied tool → `CAPABILITY_DENIED` tool error; account deletion
  purges only that account's grants (FK + integrity checks); v24→v25 upgrade
  (idempotent, integrity + FK) and fail-closed rollback until grants are revoked.
  Rollback chains in v14/v22/v23/v24 tests extended with the v25 step.
- **LOCAL VERIFICATION (Python 3.13, on `dab7081` + R6):** `make static` OK; `make test`
  473/473; `make api` 29/29; `make smoke` OK.
- **KNOWN LIMITATIONS:** tokens are pasted by the user (OAuth consent flow for ChatGPT
  comes with R7); no per-grant rate limit; a request already authenticated when a grant
  is revoked completes; the management UI comes with R7.
- **NEXT DEPENDENCY:** R7 ChatGPT/Codex integration on top of this surface.

## R7 — ChatGPT / Codex integration (schema v26)

- **STATUS:** MERGED (code/CI). PR #36 (`feature/r7-chatgpt-codex-integration`), head
  `2981c19`, merge `c2eb652`. Remote CI on `2981c19`: `verify` ×2 (incl. `connect_e2e`) and
  `apk` ×2 green.
  LIVE ChatGPT/Codex connection: **NOT VERIFIED** (blocker below).
- **BASELINE (`4afd2b3`):** R6 grants + `/mcp` exist; tokens could only be created via
  API; no UI; no OAuth. ChatGPT authenticates remote MCP servers with OAuth (API-key
  support not confirmable from official docs here); Codex takes `url` +
  `bearer_token_env_var` in `config.toml`, or OAuth.
- **OWNER / IMPLEMENTATION (ADR 0031):** `oauth.py` (RFC 9728/8414/7591 metadata and
  registration, code + PKCE S256, consent → single-use hashed code → token endpoint that
  returns an R6 capability grant token; replay revokes); `web/app.py` (well-known
  endpoints, `/oauth/register|authorize|token`, session-only consent API,
  `resource_metadata` in 401s, per-IP limits, `SEOS_PUBLIC_ORIGIN`); UI
  `views/connect.js` (consent, survives sign-in) and `connected-apps.js` (Settings list,
  disconnect, create token shown once with Codex snippet), EN/RU strings; migration
  `026_oauth_connect.sql` + lossless rollback; `docs/integrations/chatgpt-codex.md`.
  No vendor-specific business logic: ChatGPT/Codex reach the R6 gateway only.
- **TESTS:**
  - `tests.web.test_oauth_connect` 11 (LOCAL INTEGRATION): discovery metadata and 401
    `resource_metadata`; full connect → token → MCP `create_task` lands in the app's
    tasks → grant listed with the client's name → disconnect → 401; user narrows scopes,
    loopback redirect (Codex), JSON token body; code bound to verifier/client/redirect,
    single use, replay revokes the issued grant, 5-min expiry; authorize validation
    (unknown client / unregistered redirect never redirect; plain PKCE, short challenge,
    wrong response_type, unknown scope → redirected errors with state; default read-only
    scopes); consent needs a session, deny once, 10-min expiry; registration rules and
    rate limit; authorize rate limit, long state, expired-request purge; v25↔v26
    lossless rollback keeps OAuth-issued grants; account deletion cascades.
  - `tests.browser.connect_e2e` 1 (PRODUCTION-LIKE: real uvicorn server, real Chromium,
    390 px RU): client registers and sends the student to `/oauth/authorize` while signed
    out → sign in → consent resumes → student unticks a scope → Allow → redirect with
    code/state → HTTP client exchanges code → MCP `tools/call create_task` → task visible
    in the app → denied scope 403 → Settings lists "ChatGPT" + MCP URL → Disconnect → 401
    → create Codex token (shown once, `bearer_token_env_var` snippet) → it reads tasks;
    no page errors, no horizontal overflow.
  - R6 and v14/v22/v23/v24 rollback chains extended with the v26 step.
  - Fixed a flaky R6 test found here: it took the token secret as the text after the last
    `_`, but `token_urlsafe` secrets may contain `_` (a 1-char fragment then "leaked").
    Now parsed by the fixed prefix length; 15/15 repeated runs green. Product code was right.
- **LOCAL VERIFICATION (Python 3.13):** `make static` OK; `make test` 484/484; `make api`
  29/29; `make smoke` OK; `make browser` 36/36 (incl. `connect_e2e`); Compose configs
  (default, Tor overlay, nginx) validate.
- **LIVE VERIFICATION: NOT VERIFIED.** This environment cannot reach chatgpt.com /
  OpenAI docs (egress policy) and has no ChatGPT/Codex account session; the production
  server was not accessed. Next action for an operator: deploy, then in ChatGPT add a
  connector with `https://<server>/mcp` (OAuth) and in Codex add the `config.toml`
  snippet; record the result here.
- **KNOWN LIMITATIONS:** limiters are per process; client names are self-asserted
  (consent shows the return host); no refresh tokens (re-consent on expiry).
- **NEXT DEPENDENCY:** R8 collaborative groups.

## R8 — Collaborative academic groups (schema v27)

- **STATUS:** MERGED. PR #37 (`feature/r8-collaborative-groups`), head `07ceceb`, merge
  `a774be0`. Remote CI on `07ceceb`: `verify` ×2 (incl. `groups_e2e`) and `apk` ×2 green.
- **BASELINE:** `origin/feature/collaborative-groups` = one unmerged commit `b450e13`
  (schema v18 on a v17 base, 8.1k lines: own shared-event/overlay tables and agenda
  projection). `main` (`c2eb652`, v26) already owns "reality vs intent" via R3
  `SourceApplier` + USER layer. Not merged: its semantics were ported (ADR 0032).
- **PORTED SEMANTICS:** group owns shared facts; members decide personally; roles with a
  publishing role (old SCHEDULER → STAROSTA); proposals with moderation; invitation codes;
  membership lifecycle; personal state invisible to the group.
- **OWNER / IMPLEMENTATION:** `groups/service.py` (only owner of group tables; reaches
  member accounts only through `SourceApplier`); migration `027_groups.sql` + fail-closed
  rollback; `/api/v1/groups*` endpoints; UI `views/groups.js` (list, create, join by code,
  schedule add/edit/remove for staff, suggestions for members, approve/reject/withdraw,
  members/roles/remove, invite, leave), More entry, EN/RU strings.
- **ADVERSARIAL FINDING FIXED:** the revision check ran before the write transaction,
  so two starostas could both publish on the same revision; now a conditional bump inside
  the transaction (concurrency test: exactly one wins).
- **TESTS:**
  - `tests.web.test_groups` 9 (LOCAL INTEGRATION, 4 accounts): invitations (idempotent join,
    bad/used-up/revoked codes), non-members get 404 and cannot learn a group exists, member
    list exposes only login/role/joined; role matrix (member cannot publish/invite/moderate/
    manage; starosta publishes/invites/removes members only; last owner cannot demote or
    leave; ownership transfer); revision conflict + item validation; **personal overlay**:
    Bob moves one class, notes it, skips another, adds a private task and reminder → the
    starosta changes the room, moves a class for everyone and adds an exam → Bob's move,
    note and skip survive while untouched classes follow the group; Carol sees only group
    reality; none of Bob's data appears in Carol's views or in the group API; per-account
    template ids, no duplicates; proposals (pending not published, visibility, idempotent
    and final decisions, reject/withdraw, validation); leave/remove retract classes and keep
    personal data, rejoin restores the same identity and personal move, removed cannot
    rejoin; unpublish removes everywhere; owner account deletion → starosta inherits,
    integrity + FK checks; concurrent publishers; v26→v27 upgrade and fail-closed rollback.
  - `tests.browser.groups_e2e` 1 (PRODUCTION-LIKE: real server, two Chromium phones, RU):
    starosta creates group, adds a class, invites; student joins by code, sees the class in
    Calendar and no staff controls, suggests an exam; starosta approves; exam reaches the
    student's events; no page errors or horizontal overflow.
  - Rollback-chain tests now use `tests/rollback_chain.py` (every documented down-script
    from the current schema), so later stages need no per-test edits.
- **LOCAL VERIFICATION (Python 3.13):** `make static` OK; `make test` 493/493; `make api`
  29/29; `make smoke` OK; `make browser` 36/37 — the one failure is the known container-only
  `test_explicit_alarm_survives_model_omission_and_conflict_up_to_the_queued_create`
  (identical on `main` here, green in every CI run; root cause still to be found, R11).
- **KNOWN LIMITATIONS:** announcements, shared deadline tasks, per-member attendance
  preferences, diff notices and external-calendar binding from the old branch are not
  ported; group management is online-only; removed members cannot be re-admitted yet.
- **NEXT DEPENDENCY:** R9 smart reminders / notifications.

## R9 — Smart reminders / notifications

- **STATUS:** MERGED. PR #38 (`feature/r9-reminders-reconciliation`), head `6f618cb`,
  merge `5ed9f02`. Remote CI on `6f618cb`: `verify` ×2 green (no mobile/static change).
- **BASELINE (`a774be0`), existing and already tested:** adaptive prompts (intensity:
  unanswered cap, doubling backoff, daily cap), CRITICAL escalation ladder (48h…15m, each
  rung once, only the latest crossed), quiet hours, per-account spacing, grouping,
  acknowledgement/snooze/done from notifications, standalone reminders and wake alarms
  (multi-phone), dedupe, delivery retry with leases and a pre-send stale check, move/cancel
  *by the user* before delivery (`test_v16_reminders` 10, `test_pass8` 8, `test_pass9` 4).
  Travel transitions are planner input (`test_pass7`); there is no separate travel push.
- **DEFECT FOUND (reproduced):** an event's reminder moment was only recomputed by the
  user's own commands. When a source (academic feed, group starosta) moved an imported
  exam the student had asked to be reminded about, the reminder stayed at the old moment
  (exam 14:00→16:00, reminder still 13:00: hours early); a source cancellation left it
  armed; a restore did not re-arm it.
- **OWNER / FIX (ADR 0033):** `reminders/events.py` is the one owner of "start − lead";
  used by `sync/commands.py` (user, counts as interaction) and `recurrence/source.py`
  (update/cancel/restore/disappearance). `ReminderStore.retime` re-times without faking a
  user interaction and cancels undelivered messages about the old moment
  (`SOURCE_CHANGED`). The lead survives cancellation/disconnect and is re-applied on
  restore; nobody gets a reminder they did not ask for.
- **TESTS (`tests.integration.test_r9_reminder_reconciliation`, 8; LOCAL INTEGRATION
  with frozen clock, real engine + dispatcher + fake FCM):** source move → nothing at the
  old time, exactly one push at the new time; message queued for the old time withdrawn,
  new one sent; already-delivered then moved later → one new correct reminder; source
  cancel suppresses, restore and disconnect→reconnect bring it back with the same lead;
  identical refreshes + doubled ticks (restart) never duplicate or erase the exam reminder
  or a standalone reminder; user's own event move still retimes; group exam moved by the
  starosta retimes only the member who set a reminder; quiet hours across both 2026
  Europe/Berlin DST switches, incl. an end inside the skipped hour. 5 of 8 fail on
  `a774be0` (the 3 others guard behaviour that was already right).
- **FLAKY BROWSER TEST ROOT-CAUSED:** `test_explicit_alarm_survives_…_queued_create`
  (failing locally since R4, green in CI) cleared the request log but kept the sync-wait
  cursor, so once the new log grew past the stale cursor the wait skipped the new sync
  (`new_posts: []`). `_clear_posts()` resets both; 3/3 green locally where it failed
  before. Test-harness bug, product code unaffected.
- **LOCAL VERIFICATION (Python 3.13):** `make static` OK; `make test` 501/501; `make api`
  29/29; `make smoke` OK; `make browser` 37/37 (first fully green local browser run).
- **KNOWN LIMITATIONS:** no per-occurrence reminders for recurring classes; no separate
  travel push (travel is planner input); delivery is FCM-only (no web push).
- **NEXT DEPENDENCY:** R10 Android release/update/push.

## R10 — Android release / update / push in CI

- **STATUS:** MERGED — PR #39, head `a027fdd`, merge `0ce78e2` (all `apk`, `device`,
  `verify` checks green on the head).
- **GAP FOUND:** PR CI only assembled a debug APK. Native unit tests, lint, the
  instrumented device tests (notification buttons, wake alarm, update trust) and the
  install/upgrade path ran nowhere but the manual release workflow.
- **`apk` job (CI):** `testDebugUnitTest` + `lintDebug`; `assembleRelease` signed with a
  one-day throwaway key generated inside the job (no production signing credential exists
  in CI or the repo), `apksigner verify`, and both APKs scanned by
  `mobile/scripts/apk_secret_scan.py` for provider keys, private keys, capability tokens,
  keystores and the build password as a canary (`tests.unit.test_apk_secret_scan`).
- **`device` job (CI, API 34 x86_64 emulator from the runner's own SDK, KVM):**
  `native_e2e.py` renders a reminder produced by the real engine + push dispatcher, presses
  Snooze/Start/Done on the system notification against a real local server and asserts the
  task reached COMPLETED server-side; `connectedDebugAndroidTest` (wake alarm, update
  trust, 4 tests); `install_upgrade_check.py`: clean install, offline start, data kept on
  upgrade 100→101, downgrade refused (`VERSION_DOWNGRADE`), update signed by another key
  refused, clean reinstall sees no old data. Green on `3270a5c`, both runs, ~5 min each.
- **DEFECT FOUND ON THE DEVICE (fixed):** `AlarmStore.save` pruned finished alarms in place
  on the caller's list; an immutable list (`WakeAlarmDeviceTest`, logout/account switch)
  crashed with `UnsupportedOperationException`. It now prunes a copy.
- **CI HARDENING (root causes, not retries):** the first device runs hung to the job cap
  with no output. Job metadata showed the sequence: the emulator exited before boot
  (avdmanager and the emulator disagreed on the AVD directory; `ANDROID_AVD_HOME` is now
  pinned), then `adb wait-for-device` / `adb logcat -d` waited forever for a device that
  never came. Every wait is now bounded and watches the emulator process, each device step
  has its own timeout, diagnostics run on failure or cancel and are themselves bounded, the
  device test's HTTP polling has connect/read timeouts, and a device-side shell command in
  the upgrade check is now quoted as one string (adb joins arguments with spaces).
- **NOT COVERED (external):** a physical phone; real FCM delivery from Google (the
  dispatcher's exact FCM payload is exercised, the transport is not); Play/sideload update
  from a production-signed APK (needs the owner's release key, never in CI).

## R11 — Global data lifecycle, migrations, final student journey

- **STATUS:** MERGED — PR #40, head `55a7614`, merge `69fb504` (same tree `16f84bd0`; see
  "RC evidence closure" below).
- **LIFECYCLE (`tests.integration.test_r11_global_lifecycle`, 4; LOCAL INTEGRATION):**
  one database populated through the real APIs with the main user-facing features — tasks with progress
  and an active execution session, events with a reminder lead, notes, standalone
  reminders, projects, routines, a manual series with a personal move, an imported
  academic calendar with a personal note on one class (SOURCE + USER), a group with a
  shared class and a second member, a capability grant, an encrypted BYOK key — then:
  - backup (manifest: integrity ok, schema, 2 accounts) → restore into a clean path
    (sha256 verified, integrity ok, 0 FK violations) → a fresh server on the restored file
    returns byte-identical JSON for 12 read endpoints for both accounts; the grant token,
    session and encrypted BYOK still work; no private data crosses accounts. *Scope:* the
    fixture fills 34 of the 91 tables; the other 57 (among them `note_audio`,
    `attachment_blobs`, `oauth_clients`/`oauth_authorizations`, `mobile_devices`,
    `reminder_messages`, `group_proposals`, plan tables) are carried by the same SQLite
    online-backup copy of the whole file (voice audio is stored in the DB, not on disk) but
    their content is not asserted after restore;
  - export: contains the user's tasks, notes, reminders, projects, routines, series and the
    personal class note; excludes the other account, the five secret values planted in the
    fixture (BYOK key, grant secret, password, credential master key, feed key) and six
    credential tables (`llm_credentials`, `auth_sessions`, `auth_users`,
    `capability_grants`, `academic_schedule_connections`, `oauth_authorizations`). Backups,
    unlike exports, intentionally contain the encrypted/hashed credential rows and must be
    stored as secrets;
  - deletion: every table with `account_id` has 0 rows for the account (the tombstone keeps
    only id + deletion metadata by design); session and grant stop working; integrity and FKs
    ok; the other account's reads are unchanged and the shared group passes to them.
- **UPGRADE FROM REAL OLD DATA:** `tests/fixtures/upgrade/v22_populated_by_r2.sqlite.gz`
  (28 KB) was written by the actual R2 code (`d85ff2f`, schema v22; generator committed).
  Opening it with current code migrates v22→v27; login, tasks (incl. completed), events,
  notes, reminders read back through today's API; an offline op from before the upgrade
  replays exactly once; integrity + FK ok; row counts of 8 core tables (accounts,
  obligations, tasks, events, notes, reminders, projects, auth_users) are equal after the
  upgrade and again after rolling back to v22 (`client_operations` excluded from the
  comparison). *Scope:* this fixture holds only v22 data, so it proves no loss of pre-v23
  rows; data created in v23+ features is covered by the per-version rollback tests, several
  of which refuse (fail closed) rather than drop it. Row *contents* after rollback are not
  compared.
- **MIGRATIONS OVERALL:** fresh → v27 in every test DB; repeated `initialize()`; each of
  v23–v27 has an upgrade test from the previous version and a rollback test (lossless where
  documented: v26; fail-closed while data would be lost: v23 source state, v24 connections,
  v25 live grants, v27 groups); `tests/rollback_chain.py` drives chains from the current
  version.
- **FINAL STUDENT JOURNEY (`tests.browser.final_student_e2e`, PRODUCTION-LIKE: real uvicorn
  server + Chromium 390 px RU):** register → import the university .ics → classes in Today
  → capture task, note and reminder → personal note on one class → the feed changes
  (re-import with new SEQUENCE) → personal note survives, no duplicate series → offline
  capture → reconnect → exactly one row → study group shares an exam, classmate sees it and
  none of the student's data → MCP grant: read, write (visible in the app), refused outside
  scope → assistant answers via the local parser, labelled `LOCAL`/`NONE` → the real reminder
  engine delivers the explicit reminder exactly once → backup → clean restore → a second
  server returns identical tasks/notes/reminders/groups. 3/3 runs green.
- **DEFECTS FOUND BY THE JOURNEY (fixed, each with a failing-first test):**
  1. **Today could answer 500** ("plan id collision or non-deterministic projection"): the
     plan store compared blocks in `ORDER BY starts_at` *string* order; imported classes carry
     `+03:00` offsets next to `+00:00` ones, so the same plan read back looked different.
     Now compared/returned in instant order (`planning/store.py`). Also: a search cut by its
     wall-clock budget on a loaded server no longer raises — the complete result wins.
  2. **Capture lost the reminder time**: "Завтра в 10:00 взять зачётку" switched to
     *Reminder* (before or after the local parse) dropped the time the person wrote (it had
     been read as the task's target) and left Create disabled. Now the written time becomes
     the reminder time; a time set by hand is never overwritten by a later parse or AI answer.
  3. **Group list vs detail**: after the last owner's account was deleted, the list still
     showed the old role until the detail was opened; the list now applies the same
     ownership hand-over.

- **RELEASE CHECKLIST:** `docs/implementation/RELEASE_CHECKLIST.md` (code gate, secret
  rotation, backup + restore drill, deploy, post-deploy smoke incl. `llm-smoke` and a live
  ChatGPT/Codex connection, rollback plan).

## RC evidence closure (post-R11)

Gate definitions and the canonical RC criteria are in `RELEASE_CHECKLIST.md` (G1–G13);
this section records evidence against them. It supersedes the earlier "4 blockers" summary,
which grouped several of G5–G13 together.

- **SHA/TREE IDENTITY:** `main` = `69fb504` (merge of PR #40). `git rev-parse 69fb504^{tree}`
  = `git rev-parse 55a7614^{tree}` = `16f84bd040d3ca543ea47f5f07edbb2d965ae9af`: the merge
  commit's content is exactly the tested PR head.
- **G1 (CI, on the merge commit itself, push to `main`):** `ci` run 36471490460 `verify`
  success (static, unit/integration, API, Chromium incl. `final_student_e2e`, 12 smokes);
  `android` run 36471490520 `apk` + `device` success (emulator: notification buttons E2E,
  instrumented tests, install/upgrade/downgrade/foreign-key checks).
- **G2 (LOCAL, clean worktree of `69fb504`):** `make static` OK; `make test` 507/507;
  `make api` 29/29; `make smoke` OK; `make browser` 38/38 OK (Chromium 1194 via
  `CHROMIUM_PATH`; without it the container's Playwright looks for build 1200 and every
  class fails in setUp — environment, not code).
- **G3 (PRODUCTION-LIKE, LOCAL Docker):** `deploy/Dockerfile` built from the `69fb504`
  tree (image `sha256:b2f41161…`; base `python:3.13-slim` from `mirror.gcr.io`, with the
  sandbox's egress CA added only for this local build). API + reminder-worker containers
  with `SEOS_REVISION=69fb504…`: `deploy/smoke.py --expect-revision 69fb504… --expect-worker`
  → all checks ok (health, revision, schema 27, capture, lifecycle, exactly-once replay,
  conflict, reminders, worker heartbeat; `llm: degraded-local`, labelled as such). In the
  same container `backup` → manifest `integrity_check: ok`; `restore` → `sha256_verified:
  true`, `integrity_check: ok`, `foreign_key_violations: 0`. Negative control: the same
  image without `SEOS_REVISION` fails `--expect-revision` (`revision: "unknown"`).
- **DEFECTS FOUND (fixed on `feature/rc-evidence-closure`):**
  1. **Production APK would ship without push.** `android-release.yml` never supplied
     `google-services.json`; `app/build.gradle` then silently skips the Google Services
     plugin, so FCM registration is compiled out, and nothing checked it. The PACKAGE job now
     requires `SEOS_GOOGLE_SERVICES_JSON_B64`, restores the file (gitignored) and fails
     unless the built APK carries `string/google_app_id`. CI `apk` does the same with a
     synthetic Firebase config.
  2. **Deploy runbook produced no revision provenance.** `docker-compose.yml` defaults
     `SEOS_REVISION` to `unknown` and no documented `up` command set it, so the documented
     `smoke.py --expect-revision` step would fail (reproduced above). `deploy/README.md` and
     the checklist now export `SEOS_REVISION=<sha>` and record image IDs.
  3. **Production APK was never credential-scanned; no provenance record.** New
     `mobile/scripts/release_evidence.py` (used by CI `apk` and the release PACKAGE job):
     exactly one signer, expected versionName/Code, Firebase config present, `apk_secret_scan`
     with the signing passwords as canaries, clean tracked source tree; writes
     `provenance.json` (commit, tree, APK sha256/size, package/version, signer certificate
     SHA-256), attached to the immutable `v<version>` release
     (`tests.unit.test_release_evidence`, `tests.unit.test_update_release_contract`).
  4. **Script injection in the release workflow.** Free-text dispatch inputs
     (`summary_*`, `change_*`, `version`, …) were interpolated as shell text in the steps
     that hold `SEOS_UPDATE_SIGNING_KEY_B64` / create releases; they are now passed as
     environment data (contract test asserts none is interpolated into a `run:` script).
- **SECURITY INVARIANTS RE-CHECKED (no change needed):**
  - SSRF: calendar URL must be `https`, port 443, no userinfo; every resolved address must
    be public; the TCP connection goes only to the validated literal
    (`PinnedNetworkBackend` refuses other hosts/ports); redirects are not followed
    (`BLOCKED_REDIRECT`, tested through the pinned transport with `Location: 127.0.0.1`);
    `trust_env=False`. BYOK custom endpoints use the same pinned transport with
    `follow_redirects=False`; relay/proxy/platform URLs are operator configuration.
  - TLS: `PinnedTransport` refuses contexts without `CERT_REQUIRED` + `check_hostname`;
    real-TLS tests show SNI and certificate checked against the original hostname while
    connecting to the pinned IP, and a certificate for another name is rejected without
    leaking the URL.
  - OAuth: authorization code bound to `client_id` + exact registered `redirect_uri` + PKCE
    S256 (plain refused), single-use (a replayed code revokes the grant it produced), codes
    and tokens stored hashed. **Limitation:** dynamic client registration is open and the
    client *name* is self-asserted; the consent screen shows the redirect host, which is
    the only verified identity. Documented, not changed.
  - FCM transport: fixed `fcm.googleapis.com` / service-account `token_uri`; exercised
    against a fake HTTP server only — real delivery is G12.
- **CLAIM SCOPE (what the evidence does and does not show):**
  - *Backup/restore:* whole-file SQLite online backup; semantic equality asserted for the 34
    populated tables / 12 endpoints listed in R11, not for every table or feature.
  - *Rollback to v22:* no loss of v22-era rows (row counts, 8 tables) for the R2-written
    fixture; newer data is protected by fail-closed rollbacks, not by a lossless chain.
  - *No secrets in logs/exports/APK:* logs — the specific loggers captured in
    `test_v21_starter_llm`, `test_llm_credentials_api`, `test_r5_llm_egress_matrix`,
    `test_fcm_provider` for the values those tests plant; exports — five planted values and
    six credential tables; APK — the regex families in `apk_secret_scan.py` (Groq/OpenAI/
    Anthropic keys, PEM private keys, capability tokens, server secret variables, keystores)
    plus build-password canaries, on CI-built APKs (production APK from G11 onward). None
    of these is a proof that no secret can ever appear.
- **FIXES VERIFIED (CI, PR #41 head `edf3725`):** `verify`, `apk`, `device` green (×2).
  The `apk` job's `release_evidence.py` on the Gradle-built release APK: `fcm_config: true`,
  exactly one signer (`CN=botay CI throwaway`, certificate SHA-256 recorded),
  `tracked_changes: []` after `npm run sync` + Gradle, no credential material, versionCode/
  Name as expected. The first CI run on `44635c4` caught a parser mismatch with the runner's
  `apksigner` output (`V2 Signer: …`), fixed with the verbatim format as a test fixture.
  Local on the branch: `make static` OK, `make test` 513/513.
- **STATUS:** G1–G4 VERIFIED for `69fb504` (G4 in its pre-fix form: release-like build
  without Firebase config) and on PR #41's head for the fixed release path. After PR #41
  merges, the release SHA is its merge commit: G1 there = CI on the push to `main` (and
  tree identity with the tested head), G2/G3 carry over only through tree identity for
  the files they exercise (#41 changes workflows, a script, tests and docs, not `src/`
  or `deploy/Dockerfile`). G5–G13 NOT
  VERIFIED (owner-only; no production access was used). Verdict per the checklist
  definitions: **CLOSED-BETA READY** for `69fb504`; **not RELEASE-CANDIDATE READY**.
