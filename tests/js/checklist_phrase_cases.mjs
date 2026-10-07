// The device recognizes a checklist edit as a command (so the server's typed Assistant reads
// it, no model needed) and never mistakes an ordinary new task for one.
import assert from 'node:assert/strict';
import { isChecklistStepPhrase } from '../../src/student_execution_os/web/static/js/commands.js';

for (const text of [
  'добавь к задаче "лаба" шаг "написать тесты"',
  'Добавь в задачу «Курсовая» пункт «введение»',
  'добавь шаг «парсер» к задаче «лаба»',
  'отметь в лабе шаг "парсер" выполненным',
  'верни шаг «парсер» в лабе',
  'удали шаг «парсер» из лабы',
  'add step "write tests" to task "lab"',
  'mark step "parser" in "lab" as done',
]) assert.equal(isChecklistStepPhrase(text), true, text);

for (const text of [
  'Купить хлеб',
  'добавь задачу купить хлеб',
  'отметь зарядку',
  'шаг за шагом разобрать конспект',
  'Готово эссе',
]) assert.equal(isChecklistStepPhrase(text), false, text);

console.log('checklist phrases: ok');
