// Prints the device command grammar's results for tests/fixtures/nl_command_cases.json.
import { readFileSync } from 'node:fs';
import { parseCommand } from '../../src/student_execution_os/web/static/js/commands.js';

const fixture = JSON.parse(readFileSync(new URL('../fixtures/nl_command_cases.json', import.meta.url), 'utf8'));
const now = new Date(fixture.now);
process.stdout.write(JSON.stringify(fixture.cases.map(({ text }) => parseCommand(text, now, fixture.items))));
