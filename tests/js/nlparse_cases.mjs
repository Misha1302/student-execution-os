// Prints the JS parser's results for tests/fixtures/nl_capture_cases.json.
// Run with TZ set to the fixture's time zone (see tests/unit/test_nl_capture.py).
import { readFileSync } from 'node:fs';
import { parseTask } from '../../src/student_execution_os/web/static/js/nlparse.js';

const fixture = JSON.parse(readFileSync(new URL('../fixtures/nl_capture_cases.json', import.meta.url), 'utf8'));
const now = new Date(fixture.now);
const pad = (n) => String(n).padStart(2, '0');
const local = (value) => {
  if (!value) return null;
  const d = new Date(value);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const results = fixture.cases.map(({ text }) => {
  const r = parseTask(text, now);
  if (r.kind === 'EVENT') {
    return {
      kind: 'EVENT', title: r.title, category: r.category, starts_at: local(r.starts_at), ends_at: local(r.ends_at),
      duration_minutes: r.duration_minutes, unresolved: r.unresolved,
    };
  }
  const cutoff = r.actual_cutoff;
  return {
    title: r.title, estimated_total_effort_minutes: r.estimated_total_effort_minutes,
    importance: r.importance, category: r.category,
    cutoff: cutoff.state + (cutoff.at ? ` ${local(cutoff.at)}` : ''),
    target_at: local(r.target_at), actionable_from: local(r.actionable_from), remind_at: local(r.remind_at),
    splittable: r.splittable, unresolved: r.unresolved,
  };
});
process.stdout.write(JSON.stringify(results));
