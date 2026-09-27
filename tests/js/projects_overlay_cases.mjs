import assert from 'node:assert/strict';
import { project } from '../../src/student_execution_os/web/static/js/overlay.js';

const op = (type, entity_id, payload, at = '2026-09-29T10:00:00Z') => ({
  state: 'PENDING', queued_at: at,
  operation: { op_id: 'op-' + type + '-' + entity_id, type, entity_id, payload: payload || {} },
});

const create = op('project.create', 'project-1', { title: 'Compiler release' });
let projects = project('/api/v1/projects', [], [create]);
assert.equal(projects.length, 1);
assert.equal(projects[0].title, 'Compiler release');

const task = op('project.task.create', 'project-1', {
  task_id: 'task-project-1', title: 'Release notes', estimated_total_effort_minutes: 60,
  actual_cutoff: { state: 'ABSENT' },
});
projects = project('/api/v1/projects', [], [create, task]);
assert.equal(projects[0].members.length, 1);
assert.equal(projects[0].progress.tasks_total, 1);
assert.equal(projects[0].progress.percent, 0);

const done = op('task.complete', 'task-project-1', { occurred_at: '2026-09-29T10:30:00Z' });
projects = project('/api/v1/projects', [], [create, task, done]);
assert.equal(projects[0].members[0].status, 'COMPLETED');
assert.equal(projects[0].progress.percent, 100);

const milestone = op('milestone.create', 'milestone-1', {
  project_id: 'project-1', title: 'RC', marker_at: '2026-10-01T10:00:00Z',
});
projects = project('/api/v1/projects', [], [create, task, milestone]);
assert.equal(projects[0].milestones.length, 1);

const close = op('project.complete', 'project-1', {});
projects = project('/api/v1/projects', [], [create, task, close]);
assert.equal(projects[0].status, 'COMPLETED');

console.log('projects overlay: ok');
