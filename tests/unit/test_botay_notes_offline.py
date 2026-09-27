from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "src/student_execution_os/web/static/js"


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BotayNotesOfflineTests(unittest.TestCase):
    def test_note_create_is_durable_projected_and_replayed_with_same_operation_id(self):
        script = f"""
globalThis.window = {{ Capacitor: null, addEventListener() {{}}, removeEventListener() {{}}, dispatchEvent() {{}} }};
globalThis.CustomEvent = class {{ constructor(name, init) {{ this.name=name; this.detail=init?.detail; }} }};
const values = new Map();
globalThis.localStorage = {{ getItem:k=>values.get(k)??null, setItem:(k,v)=>values.set(k,String(v)), removeItem:k=>values.delete(k), key:i=>[...values.keys()][i], get length(){{return values.size;}} }};
let online = false; const sent = [];
globalThis.fetch = async (_url, opts) => {{
  if (!online) throw new TypeError('offline');
  const body = JSON.parse(opts.body); sent.push(...body.operations);
  return {{ ok:true, headers:{{get:()=> 'application/json'}}, json:async()=>({{results:body.operations.map(o=>({{op_id:o.op_id,type:o.type,entity_id:o.entity_id,status:'APPLIED',entity:null,replayed:false}})),server_revision:1}}) }};
}};
const api = await import('file://{JS}/api.js'); api.session.authMode='bound';
const sync = await import('file://{JS}/sync.js');
const overlay = await import('file://{JS}/overlay.js');
const queued = sync.queueOperation('note.create','note-offline',{{content:'idea',source_kind:'CAPTURE'}});
const stored = sync.readQueue();
if (stored.length !== 1 || stored[0].operation.op_id !== queued.op_id) throw new Error('note operation not durable');
const projected = overlay.project('/api/v1/notes', [], stored);
if (projected.length !== 1 || projected[0].content !== 'idea' || !projected[0]._pending) throw new Error('offline note not projected');
online = true; await sync.flushSync();
if (sent.length !== 1 || sent[0].op_id !== queued.op_id || sent[0].type !== 'note.create') throw new Error('replay identity changed');
console.log(JSON.stringify({{ok:true,pending:sync.syncState().pending}}));
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["pending"], 0)


if __name__ == "__main__":
    unittest.main()
