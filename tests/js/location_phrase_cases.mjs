// Prints the device reading of tests/fixtures/nl_location_trigger_cases.json (see test_location_phrases.py).
import { readFileSync } from 'node:fs';
import { parseLocationTrigger, parsePlaceCreate } from '../../src/student_execution_os/web/static/js/location-phrases.js';

const fixture = JSON.parse(readFileSync(new URL('../fixtures/nl_location_trigger_cases.json', import.meta.url), 'utf8'));
console.log(JSON.stringify({ triggers: fixture.cases.map(({ text }) => parseLocationTrigger(text, fixture.places)),
  places: fixture.place_cases.map(({ text }) => parsePlaceCreate(text)) }));
