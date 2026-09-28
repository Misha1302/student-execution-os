# botay! Release Candidate — implementation ledger

One entry per stage. Each claim links to executed evidence (tests, CI runs, commands);
"not run" is written as such.

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

- **STATUS:** IMPLEMENTED on `feature/r5-production-groq`; PR/CI/merge recorded below.
  LIVE GROQ: **NOT VERIFIED** (exact blocker below).
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

