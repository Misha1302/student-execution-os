from __future__ import annotations

import json
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]


class MobileOfflineContractTests(unittest.TestCase):
    def test_offline_queue_survives_reload_and_flushes_same_operation_after_reconnect(self):
        script = f"""
globalThis.window = {{ Capacitor: null, addEventListener() {{}}, dispatchEvent() {{}} }};
globalThis.CustomEvent = class {{ constructor(name, init) {{ this.name=name; this.detail=init?.detail; }} }};
const values = new Map();
globalThis.localStorage = {{ getItem:k=>values.get(k)??null, setItem:(k,v)=>values.set(k,String(v)), removeItem:k=>values.delete(k), key:i=>[...values.keys()][i], get length(){{return values.size;}} }};
let online = false; let sent = [];
globalThis.fetch = async (_url, opts) => {{
  if (!online) throw new TypeError('offline');
  const body = JSON.parse(opts.body); sent.push(...body.operations);
  return {{ ok:true, headers:{{get:()=> 'application/json'}}, json:async()=>({{results:body.operations.map(o=>({{...o,status:'APPLIED',entity:null,replayed:false}})),server_revision:1}}) }};
}};
const api = await import('file://{ROOT}/src/student_execution_os/web/static/js/api.js');
api.session.authMode='bound';
const sync = await import('file://{ROOT}/src/student_execution_os/web/static/js/sync.js');
const pending = await sync.queueOperation('task.start','task-0001',{{}});
if (pending.status !== 'PENDING' || sync.syncState().pending !== 1) throw new Error('operation was not persisted');
const before = sync.syncState().items[0].operation.op_id;
online = true; await sync.flushSync();
if (sync.syncState().pending !== 0 || sent.length !== 1 || sent[0].op_id !== before) throw new Error('reconnect replay changed identity');
console.log(JSON.stringify({{ok:true,op_id:before}}));
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])

    def test_backend_switch_clears_old_server_identity_only_after_change(self):
        script = f"""
globalThis.window = {{ Capacitor: null }};
const values = new Map();
globalThis.localStorage = {{ getItem:k=>values.get(k)??null, setItem:(k,v)=>values.set(k,String(v)), removeItem:k=>values.delete(k) }};
const api = await import('file://{ROOT}/src/student_execution_os/web/static/js/api.js');
api.session.server='https://old.example'; api.session.token='secret-token'; api.session.user={{account_id:'old'}};
await api.setServer('https://new.example/');
if (api.session.server !== 'https://new.example' || api.session.token !== null || api.session.user !== null) throw new Error('server identity mixed');
console.log('{{"ok":true}}');
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_backend_switch_cannot_reuse_another_servers_memory_cache(self):
        script = f"""
globalThis.window = {{ Capacitor: null }};
const values = new Map();
globalThis.localStorage = {{ getItem:k=>values.get(k)??null, setItem:(k,v)=>values.set(k,String(v)), removeItem:k=>values.delete(k), key:i=>[...values.keys()][i], get length(){{return values.size;}} }};
let responseTitle = 'old task';
globalThis.fetch = async () => ({{ ok:true, headers:{{get:()=> 'application/json'}}, json:async()=>[{{id:'task-1',title:responseTitle}}] }});
const api = await import('file://{ROOT}/src/student_execution_os/web/static/js/api.js');
const store = await import('file://{ROOT}/src/student_execution_os/web/static/js/store.js');
api.session.server='https://old.example'; api.session.user={{account_id:'old'}};
const oldData = await store.load('/api/v1/tasks');
responseTitle = 'new task';
api.session.server='https://new.example'; api.session.user={{account_id:'new'}};
const newData = await store.load('/api/v1/tasks');
if (oldData.data[0].title !== 'old task' || newData.data[0].title !== 'new task') throw new Error('memory cache crossed server scope');
console.log('{{"ok":true}}');
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_response_cut_off_mid_body_is_a_network_failure_not_empty_data(self):
        # Seen as a stuck «Места» screen: a reload cut the old page's response after its
        # 200 headers, the unreadable body became `null` data in the saved cache, and the
        # next cold start rendered `null`. A body that cannot be read is a network failure:
        # the last good copy stays, and a copy without data is never shown.
        script = f"""
globalThis.window = {{ Capacitor: null }};
const values = new Map();
globalThis.localStorage = {{ getItem:k=>values.get(k)??null, setItem:(k,v)=>values.set(k,String(v)), removeItem:k=>values.delete(k), key:i=>[...values.keys()][i], get length(){{return values.size;}} }};
let cut = false;
globalThis.fetch = async () => ({{ ok:true, status:200, headers:{{get:()=> 'application/json'}},
  json: async () => {{ if (cut) throw new DOMException('The user aborted a request.', 'AbortError'); return {{ places:[{{id:'place-1'}}] }}; }} }});
const api = await import('file://{ROOT}/src/student_execution_os/web/static/js/api.js');
const store = await import('file://{ROOT}/src/student_execution_os/web/static/js/store.js');
api.session.authMode='bound';
const good = await store.load('/api/v1/places');
cut = true;
let code = null;
try {{ await api.api('/api/v1/places'); }} catch (err) {{ code = err.code; }}
if (code !== 'NETWORK') throw new Error('an unreadable 200 body was returned as data: ' + code);
const again = await store.load('/api/v1/places', {{ fresh:true }});
if (!again.stale || again.data?.places?.[0]?.id !== 'place-1') throw new Error('the last good copy was not kept: ' + JSON.stringify(again));
const saved = JSON.parse(localStorage.getItem('seos.cache./api/v1/places'));
if (saved.data == null) throw new Error('null data was saved for the next start');
// A copy saved by an older version with no data is not a copy: the next start asks the server.
localStorage.setItem('seos.cache./api/v1/notes', JSON.stringify({{ ...saved, data:null }}));
store.invalidate(); store.setCacheFirst(true); cut = false;
const cold = await store.load('/api/v1/notes');
if (cold.data == null || cold.stale) throw new Error('a saved copy without data was shown: ' + JSON.stringify(cold));
console.log(JSON.stringify({{ok:true, first:good.stale}}));
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_refresh_in_flight_keeps_what_the_screen_shows_for_its_buttons(self):
        # «Маршрут» right after a sync answer said «add two places» on a screen showing
        # two: the answer emptied the loaded responses before the new ones arrived.
        script = f"""
globalThis.window = {{ Capacitor: null }};
const values = new Map();
globalThis.localStorage = {{ getItem:k=>values.get(k)??null, setItem:(k,v)=>values.set(k,String(v)), removeItem:k=>values.delete(k), key:i=>[...values.keys()][i], get length(){{return values.size;}} }};
let calls = 0; let release;
globalThis.fetch = async () => {{
  calls += 1;
  if (calls > 1) await new Promise((resolve) => {{ release = resolve; }});
  return {{ ok:true, headers:{{get:()=> 'application/json'}}, json:async()=>({{ places:[{{id:'home'}},{{id:'hse'}}].slice(0, calls > 1 ? 1 : 2) }}) }};
}};
const api = await import('file://{ROOT}/src/student_execution_os/web/static/js/api.js');
const store = await import('file://{ROOT}/src/student_execution_os/web/static/js/store.js');
api.session.authMode='bound';
await store.load('/api/v1/places');
store.invalidate();
const refreshing = store.load('/api/v1/places');
if (calls !== 2) throw new Error('an outdated response was served instead of asking the server');
if (store.peek('/api/v1/places')?.places?.length !== 2) throw new Error('the screen lost its places while refreshing');
release();
await refreshing;
if (store.peek('/api/v1/places')?.places?.length !== 1) throw new Error('the new response did not replace the old one');
store.clearAll();
if (store.peek('/api/v1/places') !== undefined) throw new Error('clearAll kept a response');
console.log('{{"ok":true}}');
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_backend_probe_rejects_incompatible_server_without_switching(self):
        script = f"""
globalThis.window = {{ Capacitor: null }};
const values = new Map();
globalThis.localStorage = {{ getItem:k=>values.get(k)??null, setItem:(k,v)=>values.set(k,String(v)), removeItem:k=>values.delete(k) }};
globalThis.fetch = async () => ({{ ok:true, headers:{{get:()=> 'application/json'}}, json:async()=>({{service:'student-execution-os',api_version:0}}) }});
const api = await import('file://{ROOT}/src/student_execution_os/web/static/js/api.js');
api.session.server='https://old.example';
let rejected = false;
try {{ await api.probeServer('https://new.example'); }} catch (error) {{ rejected = error.code === 'INCOMPATIBLE_SERVER'; }}
if (!rejected || api.session.server !== 'https://old.example') throw new Error('incompatible server changed active configuration');
console.log('{{"ok":true}}');
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_push_and_voice_dependencies_and_safe_voice_flow_are_wired(self):
        package = json.loads((ROOT / "mobile/package.json").read_text())
        self.assertIn("@capacitor/push-notifications", package["dependencies"])
        self.assertIn("@capacitor-community/speech-recognition", package["dependencies"])
        manifest = (ROOT / "mobile/android/app/src/main/AndroidManifest.xml").read_text()
        self.assertIn("android.permission.POST_NOTIFICATIONS", manifest)
        self.assertIn("android.permission.RECORD_AUDIO", manifest)
        native = (ROOT / "src/student_execution_os/web/static/js/native.js").read_text()
        self.assertIn("pushNotificationReceived", native)
        self.assertIn("pushNotificationActionPerformed", native)
        # Dictation feeds the same parse and card as typing; nothing is created until Create.
        capture = (ROOT / "src/student_execution_os/web/static/js/capture.js").read_text()
        voice_block = capture.split("async function listen(", 1)[1].split("\n  }\n", 1)[0]
        self.assertIn("startDictation", voice_block)
        self.assertIn("parseLocal()", voice_block)
        self.assertNotIn("change(", voice_block)
        self.assertNotIn("/assistant/apply", voice_block)
        # Recording is explicit: a stop control and visible recording/processing/error states.
        self.assertIn("data-voice-stop", capture)
        for state in ("recording", "processing", "error"):
            self.assertIn(f"'{state}'", capture + native)
        # Reminder notifications are rendered natively with background action buttons.
        self.assertIn(".reminders.ReminderMessagingService", manifest)
        self.assertIn(".reminders.ReminderActionReceiver", manifest)
        self.assertIn('android:name="com.capacitorjs.plugins.pushnotifications.MessagingService"', manifest)
        self.assertIn("reminder-actions-v1", (ROOT / "src/student_execution_os/web/static/app.js").read_text())
        # Push registration is gated on a Firebase-enabled build: register() without
        # google-services.json crashes the native bridge.
        self.assertIn("pushEnabled()", native.split("export async function setupPush", 1)[1])
        sync_web = (ROOT / "mobile/scripts/sync-web.mjs").read_text()
        self.assertIn("google-services.json", sync_web)

    def test_voice_note_has_visible_record_stop_cancel_and_original_audio_path(self):
        source = (ROOT / "src/student_execution_os/web/static/js/notes-audio.js").read_text()
        for contract in ("navigator.mediaDevices.getUserMedia", "new MediaRecorder", "data-record-state",
                         "data-record-stop", "data-record-cancel", "apiUpload"):
            self.assertIn(contract, source)
        self.assertNotIn("content_base64", source)

    def test_token_is_never_sent_to_a_candidate_server_and_failed_login_keeps_old_identity(self):
        script = f"""
globalThis.window = {{ Capacitor: null }};
const values = new Map();
globalThis.localStorage = {{ getItem:k=>values.get(k)??null, setItem:(k,v)=>values.set(k,String(v)), removeItem:k=>values.delete(k) }};
const seen = [];
globalThis.fetch = async (url, opts) => {{
  seen.push({{ url, auth: opts.headers.Authorization || null }});
  return {{ ok:false, status:401, headers:{{get:()=> 'application/json'}}, json:async()=>({{error:{{code:'UNAUTHENTICATED',message:'bad'}}}}) }};
}};
const api = await import('file://{ROOT}/src/student_execution_os/web/static/js/api.js');
api.session.server='https://old.example'; api.session.token='old-secret'; api.session.user={{account_id:'old'}};
try {{ await api.api('/api/v1/auth/login', {{ method:'POST', server:'https://new.example', body:{{}} }}); }} catch {{}}
try {{ await api.api('/api/v1/tasks', {{ server:'https://new.example' }}); }} catch {{}}
if (seen.some((x) => x.auth)) throw new Error('old token leaked to candidate server');
if (api.session.token !== 'old-secret' || api.session.server !== 'https://old.example') throw new Error('failed candidate request dropped the current identity');
console.log('{{"ok":true}}');
"""
        result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__": unittest.main()
