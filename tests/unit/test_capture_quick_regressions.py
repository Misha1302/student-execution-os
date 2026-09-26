from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "src/student_execution_os/web/static/js"


def run_node(body: str) -> dict:
    prelude = """
globalThis.window = { Capacitor: null, addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
globalThis.document = { documentElement: {}, querySelector() { return null; }, addEventListener() {} };
globalThis.localStorage = { getItem() { return null; }, setItem() {}, removeItem() {} };
globalThis.CustomEvent = class { constructor(name, init) { this.name = name; this.detail = init?.detail; } };
const assert = (condition, message) => { if (!condition) throw new Error(message); };
"""
    result = subprocess.run(["node", "--input-type=module", "-e", prelude + body], cwd=ROOT,
                            text=True, capture_output=True, timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return json.loads(result.stdout.strip().splitlines()[-1])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class CaptureAndQuickActionRegressions(unittest.TestCase):
    def test_reminder_enrichment_is_presence_aware_and_respects_authority(self):
        out = run_node(f"""
const {{ mergeReminderDraft }} = await import('file://{JS}/capture.js');
const floor = {{ delivery: 'ALARM', wake_check: true, raise_volume: true }};
const local = {{ title: 'Подъём', remind_at: '2026-09-27T04:00:00Z', delivery: 'ALARM',
  wake_check: true, raise_volume: true, note: 'known' }};
const omitted = mergeReminderDraft(local, {{ title: 'Wake' }}, floor);
const conflicting = mergeReminderDraft(local, {{ delivery: 'PUSH', wake_check: false }}, floor);
const manual = mergeReminderDraft({{ ...local, delivery: 'PUSH', deliveryChosen: true,
  wake_check: false, wakeChosen: true }}, {{ delivery: 'ALARM', wake_check: true }}, floor);
assert(omitted.delivery === 'ALARM' && omitted.wake_check && omitted.raise_volume, 'omission weakened alarm');
assert(omitted.note === 'known' && omitted.remind_at === local.remind_at, 'omission cleared known optional fields');
assert(conflicting.delivery === 'ALARM' && conflicting.wake_check, 'AI conflict crossed semantic floor');
assert(manual.delivery === 'PUSH' && !manual.wake_check, 'manual edit lost authority');
console.log(JSON.stringify({{ omitted, conflicting, manual }}));
""")
        self.assertEqual(out["omitted"]["delivery"], "ALARM")
        self.assertEqual(out["manual"]["delivery"], "PUSH")

    def test_android_like_long_press_sequence_opens_once_and_eats_followups(self):
        out = run_node(f"""
const {{ installQuickActions, singleFlightQuickAction }} = await import('file://{JS}/quick.js');
const listeners = new Map();
const root = {{
  addEventListener(name, fn) {{ if (!listeners.has(name)) listeners.set(name, []); listeners.get(name).push(fn); }}
}};
const node = {{ dataset: {{ kind: 'TASK', id: 'task-1' }}, contains(target) {{ return target === targetNode; }},
  style: {{}}, classList: {{ toggle() {{}}, remove() {{}} }}, offsetWidth: 300 }};
const targetNode = {{ closest(selector) {{
  if (selector === 'input,textarea,select' || selector === '[data-swipe="archive"]') return null;
  return selector.includes('[data-kind]') ? node : null;
}} }};
const event = (extra = {{}}) => ({{ target: targetNode, defaultPrevented: false, stopped: 0,
  preventDefault() {{ this.defaultPrevented = true; }}, stopPropagation() {{ this.stopped += 1; }},
  stopImmediatePropagation() {{ this.stopped += 1; }}, ...extra }});
const dispatch = (name, value) => (listeners.get(name) || []).forEach((fn) => fn(value));
let opens = 0;
installQuickActions(root, async () => {{ opens += 1; }});
dispatch('touchstart', event({{ touches: [{{ clientX: 10, clientY: 10 }}] }}));
await new Promise((resolve) => setTimeout(resolve, 570));
const menu = event(); dispatch('contextmenu', menu);
dispatch('touchend', event());
const click = event(); dispatch('click', click);
assert(opens === 1, 'long press opened ' + opens + ' times');
assert(menu.defaultPrevented && menu.stopped && click.defaultPrevented, 'follow-up events leaked');

let shortOpens = 0;
const root2 = {{ addEventListener(name, fn) {{ if (!listeners.has('b:' + name)) listeners.set('b:' + name, []); listeners.get('b:' + name).push(fn); }} }};
installQuickActions(root2, async () => {{ shortOpens += 1; }});
const send2 = (name, value) => (listeners.get('b:' + name) || []).forEach((fn) => fn(value));
send2('touchstart', event({{ touches: [{{ clientX: 0, clientY: 0 }}] }}));
send2('touchend', event());
await new Promise((resolve) => setTimeout(resolve, 20));
assert(shortOpens === 0, 'short tap became long press');

let moved = 0;
send2('touchstart', event({{ touches: [{{ clientX: 0, clientY: 0 }}] }}));
send2('touchmove', event({{ touches: [{{ clientX: 30, clientY: 0 }}] }}));
await new Promise((resolve) => setTimeout(resolve, 570));
assert(moved === 0 && shortOpens === 0, 'moved touch opened sheet');

const desktop = event(); send2('contextmenu', desktop);
assert(shortOpens === 1, 'desktop right click did not open once');

let actions = 0; let release;
const first = singleFlightQuickAction('TASK', 'same', async () => {{ actions += 1; await new Promise((r) => {{ release = r; }}); }});
const duplicate = await singleFlightQuickAction('TASK', 'same', async () => {{ actions += 1; }});
release(); await first;
assert(actions === 1 && duplicate === false, 'single flight allowed duplicate action');
console.log(JSON.stringify({{ opens, menuPrevented: menu.defaultPrevented, clickPrevented: click.defaultPrevented,
  shortOpens, actions }}));
""")
        self.assertEqual(out["opens"], 1)
        self.assertTrue(out["menuPrevented"])
        self.assertTrue(out["clickPrevented"])
        self.assertEqual(out["actions"], 1)


if __name__ == "__main__":
    unittest.main()
