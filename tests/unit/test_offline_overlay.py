"""Offline-first client rules (overlay.js + sync.js), run in Node without a browser."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "src/student_execution_os/web/static/js"

PRELUDE = """
globalThis.window = { Capacitor: null, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
globalThis.CustomEvent = class { constructor(name, init) { this.name = name; this.detail = init?.detail; } };
const values = new Map(JSON.parse(process.env.SEOS_STORAGE || '[]'));
globalThis.localStorage = { getItem: (k) => values.get(k) ?? null, setItem: (k, v) => values.set(k, String(v)),
  removeItem: (k) => values.delete(k), key: (i) => [...values.keys()][i], get length() { return values.size; } };
const dump = () => JSON.stringify([...values.entries()]);
const assert = (ok, message) => { if (!ok) { throw new Error(message); } };
"""


def run_node(body: str, storage: str = "[]") -> dict:
    script = PRELUDE + body
    result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True,
                            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "SEOS_STORAGE": storage, "TZ": "Europe/Moscow"},
                            timeout=60)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return json.loads(result.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class OfflineOverlayTests(unittest.TestCase):
    def test_today_projection_hides_done_and_deferred_work_and_lists_new_tasks(self):
        out = run_node(f"""
const {{ project }} = await import('file://{JS}/overlay.js');
const now = new Date('2026-09-23T10:00:00Z');
const task = (id, extra = {{}}) => ({{ id, title: id, status: 'ACTIVE', estimated_total_effort_minutes: 60, remaining_effort_minutes: 60, actual_cutoff: {{ state: 'ABSENT' }}, ...extra }});
const today = {{
  tasks: [task('a'), task('b'), task('c')], needs_refinement: [],
  next_actions: [{{ task_id: 'a' }}, {{ task_id: 'b' }}, {{ task_id: 'c' }}],
  plan: {{ feasibility_status: 'FEASIBLE', blocks: [{{ type: 'WORK', obligation_id: 'a' }}, {{ type: 'WORK', obligation_id: 'b' }}], canonical_events: [] }},
}};
const frozen = JSON.stringify(today);
const op = (type, entity_id, payload = {{}}, extra = {{}}) => ({{ operation: {{ op_id: type + entity_id, type, entity_id, payload }}, state: 'PENDING', queued_at: '2026-09-23T10:00:00Z', ...extra }});
const items = [
  op('task.complete', 'a'),
  op('task.defer', 'b', {{ until: '2026-09-23T11:00:00Z' }}),
  op('task.delete', 'c'),
  op('task.create', 'n', {{ title: 'New', estimated_total_effort_minutes: 30, actual_cutoff: {{ state: 'ABSENT' }} }}),
  op('task.create', 'd', {{ title: 'Draft', actual_cutoff: {{ state: 'UNKNOWN' }} }}),
  op('event.create', 'e1', {{ title: 'Занятие', starts_at: '2026-09-23T18:00:00Z', ends_at: '2026-09-23T19:00:00Z', remind_before_minutes: 15 }}),
  op('task.complete', 'x', {{}}, {{ state: 'REJECTED' }}),
];
const view = project('/api/v1/today', today, items, {{ now }});
assert(JSON.stringify(today) === frozen, 'the cached response was modified');
console.log(JSON.stringify({{
  tasks: view.tasks.map((t) => t.id).sort(), next: view.next_actions.map((a) => a.task_id),
  work: view.plan.blocks.map((b) => b.obligation_id), drafts: view.needs_refinement.map((t) => t.id),
  unplanned: view.unplanned_pending, events: view.plan.canonical_events.map((e) => [e.id, e.duration_minutes, e.remind_before_minutes]),
  pending: view.pending_changes,
}}));
""")
        self.assertEqual(out["tasks"], ["b", "n"])            # b stays (deferred), a done, c deleted
        self.assertEqual(out["next"], [])                     # nothing the user already handled is suggested
        self.assertEqual(out["work"], [])
        self.assertEqual(out["drafts"], ["d"])
        self.assertEqual(out["unplanned"], ["n"])
        self.assertEqual(out["events"], [["e1", 60, 15]])
        self.assertEqual(out["pending"], 6)

    def test_acknowledged_changes_show_until_a_newer_response_and_progress_is_not_counted_twice(self):
        out = run_node(f"""
const {{ project }} = await import('file://{JS}/overlay.js');
const base = [{{ id: 't', title: 't', status: 'ACTIVE', estimated_total_effort_minutes: 60, remaining_effort_minutes: 60, last_progress_at: null }}];
const acked = {{ operation: {{ op_id: 'p1', type: 'task.progress', entity_id: 't', payload: {{ minutes: 20 }} }}, state: 'ACKED', acked_at: 2000, queued_at: '2026-09-23T10:00:00Z' }};
const older = project('/api/v1/tasks', base, [acked], {{ fetchedAt: 1000 }});
const newer = project('/api/v1/tasks', base, [acked], {{ fetchedAt: 3000 }});
// The server response already contains the progress (last_progress_at after the op).
const applied = [{{ ...base[0], remaining_effort_minutes: 40, last_progress_at: '2026-09-23T10:00:05Z' }}];
const pending = {{ ...acked, state: 'PENDING' }};
const twice = project('/api/v1/tasks', applied, [pending], {{ fetchedAt: 3000 }});
console.log(JSON.stringify({{ older: older[0].remaining_effort_minutes, newer: newer[0].remaining_effort_minutes, twice: twice[0].remaining_effort_minutes }}));
""")
        self.assertEqual(out, {"older": 40, "newer": 60, "twice": 40})

    def test_counted_progress_and_reopen_and_archive_lifecycle(self):
        out = run_node(f"""
const {{ applyTaskOp }} = await import('file://{JS}/overlay.js');
const op = (type, payload = {{}}) => ({{ operation: {{ op_id: type, type, entity_id: 't', payload }}, queued_at: '2026-09-23T10:00:00Z' }});
let t = {{ id: 't', status: 'ACTIVE', estimated_total_effort_minutes: 100, remaining_effort_minutes: 100, count_progress: {{ total: 10, done: 0, unit: 'задач' }} }};
t = applyTaskOp(t, op('task.progress', {{ count: 3 }}));
const afterCount = [t.count_progress.done, t.remaining_effort_minutes];
t = applyTaskOp(t, op('task.complete'));
t = applyTaskOp(t, op('task.archive'));
const archived = t.status;
t = applyTaskOp(t, op('task.unarchive'));
const restored = t.status;
t = applyTaskOp(t, op('task.reopen'));
console.log(JSON.stringify({{ afterCount, archived, restored, reopened: t.status, gone: applyTaskOp(t, op('task.delete')) }}));
""")
        self.assertEqual(out, {"afterCount": [3, 70], "archived": "ARCHIVED", "restored": "COMPLETED",
                               "reopened": "ACTIVE", "gone": None})

    def test_queue_survives_restart_collapses_repeated_taps_and_replays_in_order(self):
        first = run_node(f"""
globalThis.fetch = async () => {{ throw new TypeError('offline'); }};
const api = await import('file://{JS}/api.js');
api.session.authMode = 'bound';
const sync = await import('file://{JS}/sync.js');
sync.queueOperation('task.create', 't1', {{ title: 'Read', estimated_total_effort_minutes: 30, actual_cutoff: {{ state: 'ABSENT' }} }});
sync.queueOperation('task.start', 't1');
for (let i = 0; i < 5; i += 1) sync.queueOperation('task.complete', 't1');   // taps on a bad network
await sync.flushSync();                                                     // fails: stays queued
const state = sync.syncState();
assert(state.pending === 3, 'expected 3 pending, got ' + state.pending);
console.log(dump());
""")
        # "Restart": a new process with the same storage.
        second = run_node(f"""
const sent = [];
globalThis.fetch = async (_url, opts) => {{
  const body = JSON.parse(opts.body); sent.push(...body.operations);
  return {{ ok: true, headers: {{ get: () => 'application/json' }}, json: async () => ({{ results: body.operations.map((o) => ({{ op_id: o.op_id, status: 'APPLIED', entity: null }})), server_revision: 1 }}) }};
}};
const api = await import('file://{JS}/api.js');
api.session.authMode = 'bound';
const sync = await import('file://{JS}/sync.js');
const store = await import('file://{JS}/store.js');
assert(sync.syncState().pending === 3, 'queue lost on restart');
localStorage.setItem('seos.cache./api/v1/tasks', JSON.stringify({{ scope: 'same-origin|bound', data: [], fetchedAt: 1 }}));
const cached = await store.load('/api/v1/tasks', {{ cached: true }});
assert(cached.data.length === 1 && cached.data[0].status === 'COMPLETED' && cached.data[0].started_at, 'overlay after restart');
await sync.flushSync();
console.log(JSON.stringify({{ types: sent.map((o) => o.type), ids: sent.map((o) => o.op_id), pending: sync.syncState().pending }}));
""", storage=json.dumps(first) if isinstance(first, list) else "[]")
        self.assertEqual(second["types"], ["task.create", "task.start", "task.complete"])
        self.assertEqual(len(set(second["ids"])), 3)
        self.assertEqual(second["pending"], 0)

    def test_change_queued_during_a_flush_is_sent_right_after_it(self):
        out = run_node(f"""
const batches = [];
let release;
globalThis.fetch = async (_url, opts) => {{
  const body = JSON.parse(opts.body); batches.push(body.operations.map((o) => o.type));
  if (batches.length === 1) await new Promise((r) => {{ release = r; }});
  return {{ ok: true, headers: {{ get: () => 'application/json' }}, json: async () => ({{ results: body.operations.map((o) => ({{ op_id: o.op_id, status: 'APPLIED' }})) }}) }};
}};
const api = await import('file://{JS}/api.js');
api.session.authMode = 'bound';
const sync = await import('file://{JS}/sync.js');
sync.queueOperation('task.start', 't1');
const run = sync.flushSync();
await new Promise((r) => setTimeout(r, 20));
sync.queueOperation('task.complete', 't1');
sync.flushSync();
release();
await run;
await new Promise((r) => setTimeout(r, 50));
console.log(JSON.stringify({{ batches, pending: sync.syncState().pending }}));
""")
        self.assertEqual(out["batches"], [["task.start"], ["task.complete"]])
        self.assertEqual(out["pending"], 0)


if __name__ == "__main__":
    unittest.main()
