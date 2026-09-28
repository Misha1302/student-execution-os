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

## R3 — Class series exceptions and stable external identity (schema v23)

- **STATUS:** DONE on `feature/r3-recurrence-exceptions`, PR open.
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
