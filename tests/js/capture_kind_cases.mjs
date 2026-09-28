// Capture's Task/Note decision for parses that are neither an Event nor a Reminder.
import assert from 'node:assert/strict';
import { parseTask, captureKind } from '../../src/student_execution_os/web/static/js/nlparse.js';

const now = new Date('2026-09-23T10:00:00+03:00');
const kindOf = (text) => {
  const parsed = parseTask(text, now);
  return parsed.kind === 'EVENT' || parsed.kind === 'REMINDER' ? parsed.kind : captureKind(parsed, text);
};
const cases = [
  ['Идея: сделать тёмную тему', 'NOTE'],
  ['идея — dependency graph', 'NOTE'],
  ['Заметка: купить молоко', 'NOTE'],
  ['Note: finish the essay', 'NOTE'],
  ['Идеальный план на неделю', 'NOTE'],
  ['Интересная мысль про графы', 'NOTE'],
  ['Сделать лабораторную', 'TASK'],
  ['Доделать лабораторную, примерно 1 час 35 минут', 'TASK'],
  ['сдать эссе, срочно', 'TASK'],
  ['Лаба по физике 2 часа', 'TASK'],
  ['Сегодня в 18:00 созвон с Ариадной', 'EVENT'],
];
for (const [text, expected] of cases) assert.equal(kindOf(text), expected, text);
assert.equal(captureKind({}, 'Идеальный'), 'NOTE');
assert.equal(captureKind({ estimated_total_effort_minutes: 30 }, 'Курсовая'), 'TASK');
console.log(`capture kind: ${cases.length} cases ok`);
