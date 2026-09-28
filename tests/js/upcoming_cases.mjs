// Today "Soon": tasks, events and reminders in one time-ordered list.
import assert from 'node:assert/strict';
import { upcomingItems, currentEvent } from '../../src/student_execution_os/web/static/js/upcoming.js';

const cur = new Date('2026-09-28T22:00:00Z');
const at = (h, m = 0, day = 28) => new Date(Date.UTC(2026, 8, day, h, m)).toISOString();
const ev = (id, start, end) => ({ id, title: id, starts_at: start, ends_at: end });

const events = [
  ev('finished', at(20), at(21)),
  ev('running', at(21, 30), at(22, 30)),
  ev('in-an-hour', at(23), at(23, 45)),
  ev('crosses-midnight', at(23, 30), at(0, 30, 29)),
  ev('after-midnight', at(0, 30, 29), at(1, 30, 29)),
  ev('too-far', at(12, 0, 29), at(13, 0, 29)),
];
const reminders = [
  { id: 'r-soon', status: 'SCHEDULED', remind_at: at(22, 15), title: 'Bread' },
  { id: 'r-fired', status: 'FIRED', remind_at: at(21), title: 'Rang already' },
  { id: 'r-task', status: 'SCHEDULED', remind_at: at(22, 20), title: 'Task reminder', obligation_id: 't1' },
  { id: 'r-far', status: 'SCHEDULED', remind_at: at(15, 0, 29), title: 'Far' },
];
const tasks = [
  { task: { id: 'untimed', title: 'Anytime' }, ready: true, why: 'anytime' },
  { task: { id: 'due', title: 'Due', actual_cutoff: { state: 'KNOWN', at: at(23, 50) } }, ready: true, why: 'due' },
  { task: { id: 'deferred', title: 'Later', actionable_from: at(22, 40) }, ready: false, why: 'deferred' },
];

// "Now" shows the running event: Soon leaves it out.
assert.equal(currentEvent(events, cur).id, 'running');
let items = upcomingItems({ events, reminders, tasks }, cur, { nowEventId: 'running' });
assert.deepEqual(items.map((x) => x.id),
  ['r-soon', 'deferred', 'in-an-hour', 'crosses-midnight', 'due', 'after-midnight', 'untimed']);
assert.deepEqual(items.map((x) => x.kind),
  ['REMINDER', 'TASK', 'EVENT', 'EVENT', 'TASK', 'EVENT', 'TASK']);

// "Now" is taken by a work session: the running event leads Soon, marked running.
items = upcomingItems({ events, reminders, tasks: [] }, cur);
assert.equal(items[0].id, 'running');
assert.equal(items[0].running, true);

// Past midnight the crossing event is the running one.
const later = new Date('2026-09-29T00:10:00Z');
assert.equal(currentEvent(events, later).id, 'crosses-midnight');
assert.deepEqual(upcomingItems({ events }, later, { nowEventId: 'crosses-midnight' }).map((x) => x.id), ['after-midnight', 'too-far']); // 12:00 is within 12 h of 00:10

// Duplicated feeds (day list + upcoming window) never show one event twice.
assert.deepEqual(upcomingItems({ events: [...events, ...events] }, cur, { nowEventId: 'running' }).filter((x) => x.id === 'in-an-hour').length, 1);

console.log('upcoming: ok');
