# botay! 0.7.0 product hardening

Implementation baseline: `387e60490bee81a9b82eb10f09639bbe83a6cdb5` (v0.6.4).
The research worktree and its untracked artifacts are not part of this change.

## Causal changes

- Correction turns replace earlier temporal propositions in the shared RU/EN parser,
  preserving unrelated title, duration and reminder meaning. Semantic fragments are
  independent of harmless line breaks; Notes retain their original multiline content.
- A CaptureSession owns candidates, turns, revision, per-kind user edits and conflicts.
  An explicit edit wins; weaker local classification and inferred defaults may improve
  through AI. Incompatible critical meanings require concrete user choice. Event
  intervals are atomic, and stale asynchronous replies cannot update newer input.
- Voice updates this same draft: date/time correction, additional reminder information,
  removal and type correction. Recording supports stop/cancel and retry. No automatic
  creation occurs. Browser speech is tested with controlled recognition events; native
  hardware recognition is not claimed.
- Local drafts are debounced, scoped by server/account, expire after seven days and
  clear on successful creation/discard or logout/server change. Restored instants do
  not move when relative words are reopened on another day.
- Type choice, reminder presets and diagnostics are disclosed only when needed. Title
  and event time/duration have compact editors; advanced fields remain available.
- Quick tasks use existing canonical low/high estimate columns: provisional 15–60 min,
  nominal 30 min for planning. Creation provenance uses the existing audit record.
  Explicit `estimated_total_effort_minutes: null` retains the unknown/DRAFT contract;
  omission requests a useful default. No schema migration is introduced (schema 27).
  If creation audit retention removes provenance, a surviving estimate remains RANGE,
  never silently becomes an explicit user estimate. Refinement clears provisional bounds.
- Today leads with the next action. Start/Done are primary; other actions remain under
  More, including long-press quick actions. Focus defaults to planner suggestions.
  Planner/source diagnostics are secondary; transient source failures offer no fake
  human fix action. Existing retry, offline queue and exactly-once semantics remain.
- Onboarding is a real guided Capture. Closing does not mark it complete; explicit Skip
  or first creation does. Help includes intent examples, not a syntax manual.

## First-value decision

Session-mode APIs require authenticated account ownership. Native cached operations,
encrypted credentials, sync principal and SQLite entities are bound to that identity.
A guest-create path would require a new identity/migration/security subsystem. This
pass intentionally retains authentication and makes the first post-registration action
a real guided Capture instead. Registration and account isolation are regression tested.

## QA and latency method

`make verify` runs static checks, Node cases, relay Worker tests, Python discovery,
web API tests, real FastAPI/SQLite/Chromium browser suites and all documented smokes.
The product browser suite covers six sizes (320x700, 360x800, 390x844, 412x915,
768x1024, 1280x800), RU/EN and light/dark, including no horizontal overflow.
Screenshots are written outside the repository using `UI_QA_SCREENSHOT_DIR`.

Measured baseline preview/request scheduling was approximately 290/1204 ms including
opening Capture. With the 300 ms quiet-period AI debounce, repeated real-endpoint browser
measurements were approximately 256–288/381–393 ms. These are automation measurements,
not human timings or provider network completion guarantees. Local parsing remains
120 ms debounced, storage 250 ms; network AI never blocks local creation.

The four quick-intent checks (milk, half-hour call, call with 50-minute reminder,
oven reminder) use open → express → verify → create, with no mandatory extra fields.
No claim of measured human ten-second or thirty-second performance is made.

Old tests asserting title-only tasks must remain unusable DRAFTs are replaced by the
new plannable/provisional invariant. Unknown-effort lifecycle tests explicitly supply
null and retain their original assertions. Cache-refresh UI assertions await the actual
visible result rather than reading an old ready cache or increasing sleeps.

Physical Android device QA was not performed in this pass.
Production signing, immutable release provenance and deployment are handled through
the existing release workflow and documented exact-revision nginx deployment, not a
debug APK. The final delivery report records observed CI/release/deploy results.
