// Prints the device parser's readings for tests/fixtures/nl_recurring_cases.json
// (run with TZ set to the fixture's zone; compared in tests/unit/test_recurring_parse.py).
import { readFileSync } from 'node:fs';
import { parseRecurring } from '../../src/student_execution_os/web/static/js/recurring.js';

const fixture = JSON.parse(readFileSync(new URL('../fixtures/nl_recurring_cases.json', import.meta.url), 'utf8'));
const now = new Date(fixture.now);
console.log(JSON.stringify(fixture.cases.map(({ text }) => parseRecurring(text, now))));
