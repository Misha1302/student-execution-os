# IMPLEMENTATION MASTER PROMPT

## Student Execution OS — First Architecture-Valid Vertical Slice

You are a senior software engineer and software architect acting as the implementation owner for **Student Execution OS**.

Your task is to **implement the first architecture-valid vertical slice**, not to redesign the product and not to modify another repository.

---

# 0. HARD TARGET REPOSITORY BOUNDARY

The ONLY target repository for this task is:

```text
Misha1302/student-execution-os
```

Before any write:

1. inspect the actual repository;
2. verify the repository identity;
3. read the actual current `main`;
4. record the current `main` HEAD SHA;
5. verify that the writable target is exactly `Misha1302/student-execution-os`.

If the writable target is any other repository, **do not mutate it**.

Explicitly forbidden target:

```text
Misha1302/chatgpt-knowledge-base
```

That repository is a separate personal knowledge/provenance system. Do NOT place Student Execution OS code, schemas, migrations, implementation docs, or architecture there.

Do not transplant the previous ChatGPT Knowledge Base v0.3 implementation. Its `SearchBackend`, `SourceImporter`, Markdown/YAML canonical model, KB provenance and migration architecture are not Student Execution OS requirements.

---

# 1. SOURCE OF TRUTH

Use this order:

```text
1. actual current main of Misha1302/student-execution-os
2. docs/SPECIFICATION.md
3. CONTRIBUTING.md
4. SECURITY.md
5. accepted ADRs/current tests
6. older review/research material
7. your architectural preferences
```

Read at minimum:

```text
README.md
docs/SPECIFICATION.md
docs/LICENSING.md
CONTRIBUTING.md
SECURITY.md
LICENSE
.gitignore
```

Also inspect the complete current repository tree and recent history.

The known baseline when this prompt was written was:

```text
e55aa3f5fbb85bfa9ca560f2681dc3723e16b991
```

Do not trust that SHA as current. Re-read `main` and use the actual current revision.

---

# 2. TASK

Implement the **first vertical slice** defined by the current normative specification.

The target user-visible flow is:

```text
manually create Tasks / fixed Events
        ↓
define effort, remaining effort, timing and dependencies
        ↓
build immutable PlanningSnapshot
        ↓
evaluate tri-state feasibility
        ↓
derive deterministic risk
        ↓
build a legal PlanSnapshot with WORK blocks
        ↓
return 1–5 explainable next actions
        ↓
edit one material input
        ↓
invalidate/recompute safely
```

The final result must be runnable and testable from a clean checkout.

Do not stop at an architecture essay, TODO list, interfaces-only scaffold, pseudocode, or mock-only proof of concept.

---

# 3. IMPLEMENTATION FREEDOM WITHOUT ACCIDENTALLY FREEZING PRODUCT SEMANTICS

The specification intentionally does not mandate a programming language, framework, ORM, exact database layout, solver, frontend toolkit, or deployment platform.

Because the repository is currently specification-first:

1. choose the **smallest boring implementation stack** that can implement and test the required vertical slice;
2. record the choice in an ADR such as `docs/adr/0001-implementation-stack.md`;
3. explain why the choice is reversible enough for the current phase;
4. keep domain semantics independent from framework-specific details;
5. do not ask the user to choose between equivalent technologies unless a real blocker remains after repository/environment inspection.

Do NOT introduce distributed systems, plugin frameworks, queues, vector databases, event sourcing, microservices, or optimizer infrastructure merely for future extensibility.

---

# 4. REQUIRED DOMAIN SCOPE

Implement the minimum model required by the vertical slice.

At minimum this includes the current-spec semantics for:

- account scoping;
- `Obligation`;
- `Task`;
- fixed `Event`;
- `Project` only if needed as a container, never as a schedulable task;
- minimal `Dependency` / `MUST_COMPLETE_BEFORE`;
- `actionable_from`;
- `target_at`;
- effective hard `actual_cutoff`;
- expected/remaining effort;
- splittable/non-splittable work;
- chunk constraints;
- `UserTimeConstraint`;
- immutable/revision-bound `PlanningSnapshot`;
- derived `PlanSnapshot` and WORK blocks;
- deterministic next-action explanation;
- optimistic/revision-safe invalidation where required by the current slice.

Keep distinct:

```text
actual cutoff
!= user target
!= actionable-from
!= fixed Event occupancy
!= planned work
!= completion
```

The UI wording `hard/soft deadline` may exist, but it must not collapse these concepts into one authoritative field.

---

# 5. FEASIBILITY CONTRACT

Feasibility is tri-state:

```text
FEASIBLE
INFEASIBLE
UNKNOWN
```

Rules:

- `FEASIBLE` requires a legal witness schedule for the considered hard constraints.
- `INFEASIBLE` requires sound evidence that no legal schedule exists within the considered model/horizon.
- heuristic failure, timeout, unsupported semantics, or incomplete analysis MUST produce `UNKNOWN`, not false impossibility.
- raw free-minute capacity alone is not proof of feasibility.
- non-splittable work requires a legal contiguous interval.
- splittable work must respect chunk rules, including the final residual-chunk rule.
- analysis horizon must extend far enough to judge the relevant cutoff; a shorter display horizon is not an infeasibility proof.
- overlapping REQUIRED fixed Events must be surfaced as a hard conflict/infeasibility.

Start with the simplest exact/sound method sufficient for the slice. Do not add a heavyweight optimizer unless evidence proves it is required.

---

# 6. RISK / PRIORITY / COLOUR

Preserve the current specification's separation:

```text
importance = user/domain consequence
risk       = computed execution/feasibility state
colour     = derived display projection
manual display floor = presentation override
```

Do not silently mutate importance as a deadline approaches.

If you expose the five-level visual scheme:

```text
TRANSPARENT
GREEN
YELLOW
RED
BURNING
```

make its mapping explicit and derived. A manual red/burning floor must not falsify the computed risk value.

Automatic ordering must be deterministic for the same state/policy and expose enough explanation to answer why one task is currently above another.

---

# 7. MANUAL CAPTURE AND FIRST USER FLOW

The first slice does not require connectors or LLM ingestion.

Provide a minimal real interaction path capable of:

1. creating a Task;
2. creating a fixed Event;
3. setting/changing target/cutoff/actionable-from;
4. setting remaining effort and chunking;
5. defining a dependency;
6. adding user availability/time constraints;
7. requesting feasibility;
8. requesting the current plan;
9. requesting 1–5 next actions with explanations;
10. changing a material input and observing a new revision/plan.

The interaction may be a CLI, HTTP API, or another minimal testable interface appropriate to the selected stack. It must be real, not test-only internal calls.

A polished GUI is NOT required for this milestone.

---

# 8. OUT OF SCOPE FOR THIS IMPLEMENTATION

Do not implement merely because the full specification discusses them:

- SmartLMS/Canvas/Moodle/Calendar/email connectors;
- LLM extraction or LLM action tools;
- travel routing;
- recurrence engine;
- reminder delivery infrastructure;
- completion follow-up notification delivery;
- offline multi-device replication;
- plugin marketplace/public plugin ABI;
- generic event bus;
- vector/semantic search;
- complex fatigue model;
- calibrated probabilistic confidence;
- globally optimal planner objective;
- broad public API for every future aggregate.

Preserve extension boundaries through clean ownership, not framework machinery.

---

# 9. PROJECT STRUCTURE AND OWNERSHIP

Create explicit owners for at least:

```text
domain model / commands
planning snapshot construction
feasibility
risk
planner
persistence or repository adapter
application/API/CLI boundary
tests/fixtures
```

The planner must not redefine external truth or mutate canonical input state.

Feasibility must not depend on display colour.

Derived plan state must not become independently writable canonical truth.

Framework code must not own domain invariants.

---

# 10. TESTING

Use the current specification acceptance tests as normative behavior.

For the first vertical slice, implement and pass the applicable cases from:

```text
AT-11 through AT-32
AT-75
AT-78
AT-79
AT-80
AT-81
```

If one listed case is genuinely outside the exact first-slice capability, document why it is not enabled rather than silently omitting it.

Also add tests for:

- adjacent half-open intervals;
- no-cutoff versus unknown-cutoff;
- non-splittable fragmented capacity;
- final residual chunk;
- deterministic tie-breaking;
- snapshot invalidation after material input change;
- scheduled work not implying completion;
- stable result on identical input revision.

Prefer executable fixtures over prose assertions.

---

# 11. CI AND CLEAN-CHECKOUT CONTRACT

Add or update CI so a fresh checkout can perform the relevant equivalent of:

```text
restore/install
build/static checks
unit tests
acceptance/integration tests
real smoke invocation
```

Do not weaken tests to make the implementation pass.

Before final delivery, verify from the final revision:

- build/compile;
- complete relevant test suite;
- acceptance tests;
- smoke flow;
- no secret/private local files;
- repository diff contains no unrelated KB content;
- documentation describes only architecture that actually exists.

---

# 12. GIT WORKFLOW

Work from the actual current `main`.

Required:

1. create a feature branch;
2. make reviewable commits;
3. keep unrelated repository changes out;
4. push the branch;
5. open/update a pull request targeting `main`;
6. verify the exact remote head SHA and CI status when available.

Do NOT merge into `main` without separate explicit user permission.

Do NOT modify `Misha1302/chatgpt-knowledge-base`.

If you discover useful code in the mistaken KB branch/PR, do not cherry-pick it wholesale. Reuse only a small piece that independently fits Student Execution OS, with an explicit rationale and tests.

---

# 13. DEFINITION OF DONE

The task is complete only when the final branch contains a runnable first vertical slice that demonstrates:

```text
Task/Event manual capture
+
timing/effort/dependency semantics
+
UserTimeConstraints
+
immutable/revision-bound PlanningSnapshot
+
sound tri-state feasibility
+
deterministic risk
+
legal derived WORK plan
+
1–5 explainable next actions
+
safe invalidation/replan after an input change
```

and the final revision has passing relevant tests and a real smoke path.

Do not claim production readiness. The expected state is:

```text
FIRST_VERTICAL_SLICE_IMPLEMENTED_AND_VERIFIED
READY_FOR_REVIEW
```

not a finished product.

---

# 14. FINAL RESPONSE

Return a compact engineering delivery report containing:

```text
repository
base SHA
final branch
final HEAD SHA
selected implementation stack + ADR
implemented scope
changed files/modules
tests and exact results
acceptance-test coverage
smoke command/result
known deferred capabilities
PR URL
merge status
```

State explicitly that:

- the implementation lives in `Misha1302/student-execution-os`;
- `Misha1302/chatgpt-knowledge-base` was not used as the target;
- the PR was not merged unless the user separately authorized merge.
