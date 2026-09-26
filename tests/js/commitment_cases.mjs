// Prints the device agenda projection for tests/fixtures/commitments_cases.json.
import { readFileSync } from 'node:fs';
import { commitments } from '../../src/student_execution_os/web/static/js/agenda.js';

const f = JSON.parse(readFileSync(new URL('../fixtures/commitments_cases.json', import.meta.url), 'utf8'));
const now = new Date(f.now);
process.stdout.write(JSON.stringify(f.queries.map(({ place, query }) =>
  commitments({ tasks: f.tasks, events: f.events, reminders: f.reminders, shared: f.shared }, { now, place, query })
    .map(({ entity, ...rest }) => rest))));
