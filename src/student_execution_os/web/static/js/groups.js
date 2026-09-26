// Collaborative groups (schema v18, ADR 0021).
//
//   GROUP tells the user WHAT is happening.
//   PERSONAL OS decides WHAT THE USER should do about it.
//
// Two kinds of changes, never mixed in one control:
// * «Для меня» — the member's own overlay (attendance, criticality, reminders, alarm,
//   hide, preparation). Queued like any task change (sync.js): works offline.
// * «Для группы» — publication, edits, cancellation, moderation, roles, invites.
//   Sent directly and shown only after the server confirmed it; one mutation id per
//   user intent, so a retry of the same form never publishes twice.
import { api, session } from './api.js';
import { load, peek, invalidate } from './store.js';
import { t, code, fmtDay, fmtTime, fmtDateTime, fmtRelative, fmtDuration } from './i18n.js';
import { esc, icon, chip, openSheet, confirmSheet, actionSheet, chipGroup, chipValue, localInputValue, isoFromLocalInput,
  toast, errorMessage, setBusy } from './ui.js';
import { change, shell } from './actions.js';
import { newEntityId } from './sync.js';
import { haptic } from './native.js';

export const SHARED_PATH = '/api/v1/me/shared';
// Groups just joined whose screen should open the subscription choice.
export const afterJoin = new Set();
export const EVENT_KINDS = ['LECTURE', 'SEMINAR', 'PRACTICE', 'LAB', 'CLASS', 'CONSULTATION', 'QUIZ', 'TEST', 'CONTROL_WORK',
  'COLLOQUIUM', 'EXAM', 'GROUP_MEETING', 'OTHER'];
export const ASSESSMENTS = new Set(['QUIZ', 'TEST', 'CONTROL_WORK', 'COLLOQUIUM', 'EXAM']);
const CLASSES = new Set(['CLASS', 'LECTURE', 'SEMINAR', 'PRACTICE', 'LAB', 'CONSULTATION']);
const OBLIGATION_KINDS = ['ASSIGNMENT', 'LAB_REPORT', 'HOMEWORK', 'REGISTRATION', 'SUBMISSION', 'OTHER'];
const ATTENDANCE = ['REQUIRED', 'PREFERRED', 'OPTIONAL', 'SKIP'];
const CRITICALITY = ['NORMAL', 'IMPORTANT', 'CRITICAL'];
const LEADS = [15, 30, 60, 120, 180, 1440];

export const sharedData = () => peek(SHARED_PATH) || { groups: [], items: [] };
export const findShared = (id) => (sharedData().items || []).find((x) => x.id === id) || null;
export const groupOf = (id) => (sharedData().groups || []).find((g) => g.id === id) || null;
const can = (group, capability) => (group?.capabilities || []).includes(capability);

// ---- group-wide requests ------------------------------------------------------------

// Online, confirmed by the server. Returns the entity, or null after showing why not.
export async function groupRequest(path, { method = 'POST', body = {}, mutationId = newEntityId('mut'), success,
  onDuplicate } = {}) {
  try {
    const result = await api(path, { method, body: { ...body, mutation_id: mutationId } });
    invalidate();
    haptic('LIGHT');
    if (success) toast(success);
    return result?.entity ?? result;
  } catch (err) {
    if (err.code === 'POSSIBLE_DUPLICATE' && onDuplicate) return onDuplicate(err);
    if (err.code === 'PROPOSAL_REQUIRED') { toast(t('groups.err.proposalRequired'), { error: true }); return null; }
    toast(err.code === 'NETWORK' ? t('groups.err.online') : errorMessage(err), { error: true });
    if (err.code === 'VERSION_CONFLICT') { invalidate(); await shell.rerender(true); }
    return null;
  }
}

async function refresh() { invalidate(); await shell.rerender(true); }

// ---- labels -------------------------------------------------------------------------

export function whenOf(item) {
  if (item.kind === 'SHARED_EVENT') return `${fmtDay(item.starts_at)} · ${fmtTime(item.starts_at)}–${fmtTime(item.ends_at)}`;
  if (item.kind === 'SHARED_OBLIGATION') return `${t('groups.dueBy')} ${fmtDateTime(item.deadline)} · ${fmtRelative(item.deadline)}`;
  return fmtDateTime(item.published_at);
}

const CRIT_TONE = { CRITICAL: 'danger', IMPORTANT: 'warn', NORMAL: 'muted' };

export function criticalityChip(value, { mine = false } = {}) {
  return chip(`${code('criticality', value)}${mine ? ` · ${t('groups.mine')}` : ''}`, CRIT_TONE[value] || 'muted');
}

// A change the member has not seen yet, as a diff: "Чт 10:30 → Пт 12:10", "R201 → R301".
export function changeLine(item) {
  const c = item.change;
  if (!c) return '';
  if (c.action === 'CANCEL') return t('groups.change.cancelled');
  if (c.action === 'RETRACT') return t('groups.change.retracted');
  const parts = [];
  const ch = c.changes || {};
  if (ch.starts_at) parts.push(`${fmtDay(ch.starts_at[0])} ${fmtTime(ch.starts_at[0])} → ${fmtDay(ch.starts_at[1])} ${fmtTime(ch.starts_at[1])}`);
  else if (ch.deadline) parts.push(`${fmtDateTime(ch.deadline[0])} → ${fmtDateTime(ch.deadline[1])}`);
  if (ch.location) parts.push(`${ch.location[0] || '—'} → ${ch.location[1] || '—'}`);
  if (!parts.length) return t('groups.change.updated');
  return `${t(ch.starts_at || ch.deadline ? 'groups.change.moved' : 'groups.change.room')}: ${parts.join(' · ')}`;
}

// Card row for «Дела» and the group screen.
export function sharedRow(item) {
  const cancelled = item.status !== 'PUBLISHED';
  const typeLabel = item.kind === 'SHARED_EVENT' ? code('eventKind', item.event_kind)
    : item.kind === 'SHARED_OBLIGATION' ? code('obligationKind', item.obligation_kind) : t('groups.announcement');
  const ic = item.kind === 'SHARED_EVENT' ? 'event' : item.kind === 'SHARED_OBLIGATION' ? 'flag' : 'bell';
  const chips = [];
  if (cancelled) chips.push(chip(t(item.kind === 'ANNOUNCEMENT' ? 'groups.retracted' : 'groups.cancelled'), 'muted'));
  else if (item.criticality) chips.push(criticalityChip(item.criticality.effective, { mine: item.criticality.overridden }));
  else if (item.importance && item.importance !== 'NORMAL') chips.push(chip(code('announcementImportance', item.importance), 'warn'));
  const diff = changeLine(item);
  const personal = item.personal || {};
  const prep = personal.preparation_task || personal.personal_task;
  return `<button class="task-card agenda-row shared-row${cancelled ? ' is-cancelled' : ''}" data-action="open-shared" data-kind="${esc(item.kind)}" data-id="${esc(item.id)}">
    <span class="task-top"><span class="kind-icon kind-shared">${icon(ic)}</span><strong class="task-title">${esc(item.title)}</strong>${chips.join('')}</span>
    <span class="task-meta"><span>${esc(typeLabel)} · ${esc(item.group_name)}</span>
      ${item.kind !== 'ANNOUNCEMENT' ? `<span>${icon(item.kind === 'SHARED_EVENT' ? 'calendar' : 'flag')}${esc(whenOf(item))}</span>` : `<span class="muted">${esc(item.body || '')}</span>`}
      ${item.location ? `<span>${icon('place')}${esc(item.location)}</span>` : ''}
      ${item.attendance ? `<span>${esc(code('attendance', item.attendance.effective))}${item.attendance.overridden ? ` · ${esc(t('groups.mine'))}` : ''}</span>` : ''}
      ${diff ? `<span class="text-warn">${icon('alert')}${esc(diff)}</span>` : ''}
      ${prep ? `<span>${icon('task')}${esc(t('groups.prepLinked', { status: code('status', prep.status) }))}</span>` : ''}
      ${item._pending ? `<small class="muted">${icon('clock')}${esc(t('sync.pendingShort'))}</small>` : ''}</span>
  </button>`;
}

// ---- personal actions («Для меня») ------------------------------------------------------

const stateOp = (item) => (item.kind === 'SHARED_EVENT' ? 'shared_event_state.update'
  : item.kind === 'SHARED_OBLIGATION' ? 'shared_obligation_state.update' : 'announcement_state.update');

export function setMine(item, payload, success = t('groups.savedForMe')) {
  return change(stateOp(item), item.id, payload, { success });
}

async function chooseValue(title, values, labelOf, current, groupValue) {
  const items = [{ id: '__group', label: t('groups.likeGroup', { value: labelOf(groupValue) }), icon: 'repeat', tone: 'muted' },
    ...values.map((v) => ({ id: v, label: labelOf(v), icon: v === current ? 'check' : 'flag' }))];
  const chosen = await actionSheet({ title, items });
  if (!chosen) return undefined;
  return chosen === '__group' ? null : chosen;
}

async function leadSheet(item, field, title) {
  const current = item.personal?.[field];
  const items = [...LEADS.map((m) => ({ id: String(m), label: t('groups.leadBefore', { d: fmtDuration(m) }), icon: 'clock' })),
    ...(current != null ? [{ id: 'off', label: t('groups.leadOff'), icon: 'x', tone: 'muted' }] : [])];
  const chosen = await actionSheet({ title, items });
  if (!chosen) return null;
  return setMine(item, { [field]: chosen === 'off' ? null : Number(chosen) });
}

// "Создать подготовку": an ordinary private task linked to the assessment; the planner
// spreads it over the member's free time. The group never sees it.
export function prepareSheet(item) {
  const obligation = item.kind === 'SHARED_OBLIGATION';
  const hint = item.estimated_effort_hint_minutes;
  const options = [60, 120, 180, 240, 360, 480].map((m) => [String(m), fmtDuration(m)]);
  const dialog = openSheet({
    eyebrow: item.title,
    title: t(obligation ? 'groups.takeOnTitle' : 'groups.prepareTitle'),
    body: `<label class="field"><span>${esc(t('form.title'))}</span>
        <input data-p="title" maxlength="300" value="${esc(obligation ? item.title : t('groups.prepareDefault', { title: item.title }))}"></label>
      <div class="field"><span>${esc(t('groups.prepareEffort'))}</span>${chipGroup('p-effort', options, String(hint || (obligation ? 120 : 240)))}</div>
      <p class="help">${esc(t(obligation ? 'groups.takeOnHelp' : 'groups.prepareHelp', { when: whenOf(item) }))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('groups.prepareCreate'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const minutes = Number(chipValue(dialog, 'p-effort') || 120);
    const due = obligation ? item.deadline : item.starts_at;
    await change('task.create', newEntityId('task'), {
      title: dialog.querySelector('[data-p="title"]').value.trim() || item.title,
      estimated_total_effort_minutes: minutes, splittable: minutes > 60, min_chunk_minutes: minutes > 60 ? 30 : null,
      max_chunk_minutes: minutes > 60 ? 120 : null,
      importance: { CRITICAL: 'CRITICAL', IMPORTANT: 'HIGH', NORMAL: 'NORMAL' }[item.criticality?.effective] || 'NORMAL',
      actual_cutoff: { state: 'KNOWN', at: due, boundary: 'EXCLUSIVE' },
      prepares: { kind: item.kind, id: item.id },
    }, { success: t('groups.prepareCreated') });
    dialog.close('saved');
  });
}

// ---- the action sheet: «Для меня» and «Для группы» are separate sections --------------

export function sharedActions(item) {
  const me = [];
  const group = [];
  const actions = new Set(item.available_actions || []);
  const addMe = (id, ic, run, tone = 'accent') => me.push({ id, label: t(`groups.act.${id}`), icon: ic, tone, run, section: t('groups.forMe') });
  const addGroup = (id, ic, run, tone = 'accent') => group.push({ id: `g-${id}`, label: t(`groups.act.${id}`), icon: ic, tone, run,
    section: t('groups.forGroup') });
  if (actions.has('set_attendance')) addMe('set_attendance', 'check', async () => {
    const value = await chooseValue(t('groups.act.set_attendance'), ATTENDANCE, (v) => code('attendance', v),
      item.attendance.effective, item.attendance.group);
    if (value !== undefined) await setMine(item, { attendance_override: value });
  });
  if (actions.has('set_criticality')) addMe('set_criticality', 'alert', async () => {
    const value = await chooseValue(t('groups.act.set_criticality'), CRITICALITY, (v) => code('criticality', v),
      item.criticality.effective, item.criticality.group);
    if (value !== undefined) await setMine(item, { criticality_override: value });
  });
  if (actions.has('remind')) addMe('remind', 'bell', () => leadSheet(item, 'remind_before_minutes', t('groups.act.remind')));
  if (actions.has('alarm')) addMe('alarm', 'clock', () => leadSheet(item, 'alarm_before_minutes', t('groups.act.alarm')));
  if (actions.has('prepare')) addMe('prepare', 'task', () => prepareSheet(item));
  if (actions.has('take_on')) addMe('take_on', 'task', () => prepareSheet(item));
  if (actions.has('accept')) addMe('accept', 'check', () => setMine(item, { acceptance_state: 'ACCEPTED' }), 'ok');
  if (actions.has('decline')) addMe('decline', 'x', () => setMine(item, { acceptance_state: 'DECLINED' }), 'muted');
  if (actions.has('hide')) addMe('hide', 'x', () => setMine(item, { muted: true }, t('groups.hidden')), 'muted');
  if (actions.has('unhide')) addMe('unhide', 'repeat', () => setMine(item, { muted: false }));
  if (actions.has('dismiss')) addMe('dismiss', 'check', () => setMine(item, { dismissed: true }, t('groups.dismissed')));
  if (actions.has('undismiss')) addMe('undismiss', 'repeat', () => setMine(item, { dismissed: false }));
  const g = groupOf(item.group_id) || { id: item.group_id, name: item.group_name, capabilities: [] };
  if (actions.has('edit_for_group')) addGroup('edit_for_group', 'settings', () => entityForm(g, item.kind, item));
  if (actions.has('reschedule_for_group')) addGroup('reschedule_for_group', 'calendar', () => entityForm(g, item.kind, item, { focusTime: true }));
  if (actions.has('cancel_for_group')) addGroup('cancel_for_group', 'x', () => closeForGroup(item), 'danger');
  if (actions.has('retract_for_group')) addGroup('retract_for_group', 'x', () => closeForGroup(item), 'danger');
  if (actions.has('detach_external')) addGroup('detach_external', 'route', () => detachExternal(item), 'muted');
  return [...me, ...group];
}

export async function openSharedActions(item) {
  const items = sharedActions(item);
  haptic('MEDIUM');
  const chosen = await actionSheet({ title: item.title, items });
  const hit = items.find((x) => x.id === chosen);
  if (hit) await hit.run();
}

const ENTITY_PATH = { SHARED_EVENT: 'events', SHARED_OBLIGATION: 'obligations', ANNOUNCEMENT: 'announcements' };

async function closeForGroup(item) {
  const retract = item.kind === 'ANNOUNCEMENT';
  const ok = await confirmSheet({
    title: t(retract ? 'groups.retractTitle' : 'groups.cancelTitle'),
    body: `<p>${esc(t(retract ? 'groups.retractBody' : 'groups.cancelBody', { title: item.title, group: item.group_name }))}</p>`,
    confirmLabel: t(retract ? 'groups.act.retract_for_group' : 'groups.act.cancel_for_group'),
    cancelLabel: t('common.keep'), danger: true,
  });
  if (!ok) return;
  const done = await groupRequest(`/api/v1/groups/${encodeURIComponent(item.group_id)}/${ENTITY_PATH[item.kind]}/${encodeURIComponent(item.id)}/${retract ? 'retract' : 'cancel'}`,
    { body: { expected_version: item.version }, success: t(retract ? 'groups.retracted' : 'groups.cancelledToast') });
  if (done) await refresh();
}

async function detachExternal(item) {
  const ok = await confirmSheet({ title: t('groups.act.detach_external'), body: `<p>${esc(t('groups.detachBody'))}</p>`,
    confirmLabel: t('groups.act.detach_external'), cancelLabel: t('common.keep') });
  if (!ok) return;
  if (await groupRequest(`/api/v1/groups/${encodeURIComponent(item.group_id)}/events/${encodeURIComponent(item.id)}/external-binding?expected_version=${item.version}`,
    { method: 'DELETE' })) await refresh();
}

// ---- detail sheet -------------------------------------------------------------------------

export function sharedSheet(item) {
  const personal = item.personal || {};
  const rows = [];
  const kv = (label, value) => `<div class="kv"><dt>${esc(label)}</dt><dd>${value}</dd></div>`;
  rows.push(kv(t('groups.group'), esc(item.group_name)));
  if (item.kind !== 'ANNOUNCEMENT') rows.push(kv(t(item.kind === 'SHARED_EVENT' ? 'groups.when' : 'groups.dueBy'), esc(whenOf(item))));
  if (item.location) rows.push(kv(t('groups.location'), esc(item.location)));
  if (item.external?.bound) rows.push(kv(t('groups.external'), esc(t(item.external.local_event_id ? 'groups.externalMine' : 'groups.externalOfficial'))));
  if (item.attendance) rows.push(kv(t('groups.attendance'), `${esc(code('attendance', item.attendance.effective))}${item.attendance.overridden
    ? ` <small class="muted">${esc(t('groups.groupSays', { value: code('attendance', item.attendance.group) }))}</small>` : ''}`));
  if (item.criticality) rows.push(kv(t('groups.criticality'), `${criticalityChip(item.criticality.effective)}${item.criticality.overridden
    ? ` <small class="muted">${esc(t('groups.groupSays', { value: code('criticality', item.criticality.group) }))}</small>` : ''}`));
  if (personal.remind_before_minutes != null) rows.push(kv(t('groups.act.remind'), esc(t('groups.leadBefore', { d: fmtDuration(personal.remind_before_minutes) }))));
  if (personal.alarm_before_minutes != null) rows.push(kv(t('groups.act.alarm'), esc(t('groups.leadBefore', { d: fmtDuration(personal.alarm_before_minutes) }))));
  const prep = personal.preparation_task || personal.personal_task;
  if (prep) rows.push(kv(t('groups.preparation'), `<a href="#/task/${encodeURIComponent(prep.id)}">${esc(prep.title)}</a> · ${esc(code('status', prep.status))}`));
  rows.push(kv(t('groups.author'), esc(item.author || '—')));
  const diff = changeLine(item);
  const banners = [];
  if (diff) banners.push(`<div class="banner warn">${icon('alert')}<div><strong>${esc(diff)}</strong></div></div>`);
  if (personal.preparation_deadline_stale || personal.deadline_stale) banners.push(`<div class="banner warn">${icon('flag')}<div>
    <p>${esc(t('groups.staleDeadline'))}</p><button type="button" class="button ghost" data-fix-deadline>${esc(t('groups.fixDeadline'))}</button></div></div>`);
  if (personal.suggest_remove_preparation || personal.suggest_remove_task) banners.push(`<div class="banner">${icon('question')}<div>
    <p>${esc(t('groups.cancelledPrep'))}</p><button type="button" class="button ghost" data-drop-prep>${esc(t('groups.dropPrep'))}</button></div></div>`);
  const dialog = openSheet({
    eyebrow: item.kind === 'SHARED_EVENT' ? code('eventKind', item.event_kind) : item.kind === 'SHARED_OBLIGATION'
      ? code('obligationKind', item.obligation_kind) : t('groups.announcement'),
    title: item.title,
    body: `${banners.join('')}${item.body ? `<p class="announcement-body">${esc(item.body)}</p>` : ''}
      ${item.description ? `<p class="muted">${esc(item.description)}</p>` : ''}<dl class="kv-list">${rows.join('')}</dl>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.close'))}</button>
      <button type="button" class="button primary" data-more>${esc(t('groups.actions'))}</button>`,
  });
  dialog.querySelector('[data-more]').addEventListener('click', () => { dialog.close('more'); openSharedActions(item); });
  dialog.querySelector('[data-fix-deadline]')?.addEventListener('click', async () => {
    const due = item.kind === 'SHARED_EVENT' ? item.starts_at : item.deadline;
    await change('task.update', prep.id, { actual_cutoff: { state: 'KNOWN', at: due, boundary: 'EXCLUSIVE' } }, { success: t('groups.deadlineFixed') });
    await setMine(item, item.kind === 'SHARED_EVENT' ? { dismiss_stale_preparation: true } : { dismiss_stale_deadline: true }, null);
    dialog.close('saved');
  });
  dialog.querySelector('[data-drop-prep]')?.addEventListener('click', async () => {
    await change('task.cancel', prep.id, {}, { success: t('lifecycle.done.cancel'), undo: () => change('task.reopen', prep.id, {}) });
    dialog.close('saved');
  });
  // Opening the card is seeing it (and its latest change).
  const seenVersion = Math.max(item.change?.new_version || 0, item.version || 0);
  if (seenVersion > (personal.last_seen_version || 0)) setMine(item, { last_seen_version: seenVersion }, null);
  return dialog;
}

// ---- forms («Для группы») ---------------------------------------------------------------------

const field = (label, control, hint = '') => `<label class="field"><span>${esc(label)}</span>${control}${hint ? `<small class="help">${esc(hint)}</small>` : ''}</label>`;
const select = (name, values, current, labelOf) => `<select data-g="${name}">${values.map((v) => `<option value="${esc(v)}" ${v === current ? 'selected' : ''}>${esc(labelOf(v))}</option>`).join('')}</select>`;

// Create/edit a shared event, deadline or announcement for the group — or, for a member
// without the right to publish, suggest it (a proposal the scheduler reviews).
export function entityForm(group, kind, existing = null, { proposalEdits = null } = {}) {
  const publishing = can(group, 'PUBLISH_SHARED');
  const proposing = !existing && !proposalEdits && !publishing;
  const bound = existing?.external?.bound;
  const e = existing || proposalEdits?.payload || {};
  const assessment = (k) => ASSESSMENTS.has(k);
  let body;
  if (kind === 'SHARED_EVENT') {
    const kindNow = e.event_kind || 'CONTROL_WORK';
    body = `${field(t('form.title'), `<input data-g="title" maxlength="300" value="${esc(e.title || '')}" placeholder="${esc(t('groups.eventPlaceholder'))}">`)}
      ${field(t('groups.eventKind'), select('event_kind', EVENT_KINDS, kindNow, (v) => code('eventKind', v)))}
      ${bound ? `<p class="help">${icon('route')} ${esc(t('groups.externalOwned'))}</p>` : `<div class="field-row">
        ${field(t('form.starts'), `<input type="datetime-local" data-g="start" value="${esc(localInputValue(e.starts_at))}">`)}
        ${field(t('form.ends'), `<input type="datetime-local" data-g="end" value="${esc(localInputValue(e.ends_at))}">`)}</div>
        ${field(t('groups.location'), `<input data-g="location" maxlength="200" value="${esc(e.location || '')}" placeholder="R201">`)}`}
      <div class="field"><span>${esc(t('groups.attendanceDefault'))}</span>${chipGroup('g-attendance', ATTENDANCE.map((v) => [v, code('attendance', v)]),
        e.attendance?.group || e.attendance_default || (assessment(kindNow) ? 'REQUIRED' : 'PREFERRED'))}</div>
      <div class="field"><span>${esc(t('groups.criticalityGroup'))}</span>${chipGroup('g-criticality', CRITICALITY.map((v) => [v, code('criticality', v)]),
        e.criticality?.group || e.group_criticality || (assessment(kindNow) ? 'CRITICAL' : 'NORMAL'))}
        <small class="help">${esc(t('groups.criticalityHelp'))}</small></div>
      ${field(t('form.description'), `<textarea data-g="description" rows="2" maxlength="5000">${esc(e.description || '')}</textarea>`)}`;
  } else if (kind === 'SHARED_OBLIGATION') {
    body = `${field(t('form.title'), `<input data-g="title" maxlength="300" value="${esc(e.title || '')}">`)}
      ${field(t('groups.obligationKind'), select('obligation_kind', OBLIGATION_KINDS, e.obligation_kind || 'ASSIGNMENT', (v) => code('obligationKind', v)))}
      ${field(t('groups.deadline'), `<input type="datetime-local" data-g="deadline" value="${esc(localInputValue(e.deadline))}">`)}
      <div class="field"><span>${esc(t('groups.criticalityGroup'))}</span>${chipGroup('g-criticality', CRITICALITY.map((v) => [v, code('criticality', v)]),
        e.criticality?.group || e.group_criticality || 'NORMAL')}</div>
      ${field(t('groups.effortHint'), `<input type="number" min="1" max="100000" data-g="effort" value="${esc(e.estimated_effort_hint_minutes || '')}">`, t('groups.effortHintHelp'))}
      ${field(t('form.description'), `<textarea data-g="description" rows="2" maxlength="5000">${esc(e.description || '')}</textarea>`)}`;
  } else {
    body = `${field(t('form.title'), `<input data-g="title" maxlength="300" value="${esc(e.title || '')}">`)}
      ${field(t('groups.announcementBody'), `<textarea data-g="body" rows="4" maxlength="5000">${esc(e.body || '')}</textarea>`)}
      <div class="field"><span>${esc(t('groups.importance'))}</span>${chipGroup('g-importance', ['NORMAL', 'IMPORTANT', 'URGENT'].map((v) => [v, code('announcementImportance', v)]), e.importance || 'NORMAL')}</div>`;
  }
  const verb = proposalEdits ? 'groups.approveEdited' : existing ? 'groups.saveForGroup' : proposing ? 'groups.propose' : 'groups.publish';
  const dialog = openSheet({
    eyebrow: group.name,
    title: t(existing ? 'groups.editForGroupTitle' : proposing ? 'groups.proposeTitle' : `groups.newTitle.${kind}`),
    body: `<div class="banner ${proposing ? '' : 'warn'}">${icon(proposing ? 'question' : 'alert')}<div><p>${esc(t(proposing ? 'groups.proposeHelp' : 'groups.forGroupHelp', { group: group.name }))}</p></div></div>
      <div class="form">${body}</div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t(verb))}</button>`,
  });
  // One mutation id for this form: a retry after a network error cannot publish twice.
  const mutationId = newEntityId('mut');
  const entityId = newEntityId(kind === 'SHARED_EVENT' ? 'sev' : kind === 'SHARED_OBLIGATION' ? 'sob' : 'san');
  const $g = (n) => dialog.querySelector(`[data-g="${n}"]`);
  dialog.querySelector('[data-save]').addEventListener('click', async (event) => {
    let payload;
    try {
      payload = readForm(kind, $g, dialog, bound);
    } catch (err) { toast(err.message, { error: true }); return; }
    const button = event.currentTarget;
    setBusy(button, true);
    const base = `/api/v1/groups/${encodeURIComponent(group.id)}`;
    let result;
    if (proposalEdits) {
      result = await groupRequest(`${base}/proposals/${encodeURIComponent(proposalEdits.id)}/approve`,
        { body: { expected_version: proposalEdits.version, edits: changedFields(proposalEdits.payload, payload) }, mutationId,
          success: t('groups.approved') });
    } else if (existing) {
      const patch = changedFields(editable(existing), payload);
      if (!Object.keys(patch).length) { dialog.close('unchanged'); setBusy(button, false); return; }
      result = await groupRequest(`${base}/${ENTITY_PATH[kind]}/${encodeURIComponent(existing.id)}`,
        { method: 'PATCH', body: { ...patch, expected_version: existing.version }, mutationId, success: t('groups.savedForGroup') });
    } else if (proposing) {
      result = await groupRequest(`${base}/proposals`, { body: { id: newEntityId('prp'), payload: { kind: kind === 'ANNOUNCEMENT' ? 'SHARED_ANNOUNCEMENT' : kind, ...payload } },
        mutationId, success: t('groups.proposed') });
    } else {
      const send = (extra = {}) => groupRequest(`${base}/${ENTITY_PATH[kind]}`, { body: { id: entityId, ...payload, ...extra },
        mutationId: extra.allow_duplicate ? newEntityId('mut') : mutationId, success: t('groups.published'),
        onDuplicate: async () => (await confirmSheet({ title: t('groups.duplicateTitle'), body: `<p>${esc(t('groups.duplicateBody'))}</p>`,
          confirmLabel: t('groups.publishAnyway'), cancelLabel: t('common.cancel') }) ? send({ allow_duplicate: true }) : null) });
      result = await send();
    }
    setBusy(button, false);
    if (result) { dialog.close('saved'); await refresh(); }
  });
  return dialog;
}

function editable(item) {
  if (item.kind === 'SHARED_EVENT') return { title: item.title, event_kind: item.event_kind, starts_at: item.starts_at, ends_at: item.ends_at,
    location: item.location, attendance_default: item.attendance.group, group_criticality: item.criticality.group, description: item.description };
  if (item.kind === 'SHARED_OBLIGATION') return { title: item.title, obligation_kind: item.obligation_kind, deadline: item.deadline,
    group_criticality: item.criticality.group, estimated_effort_hint_minutes: item.estimated_effort_hint_minutes, description: item.description };
  return { title: item.title, body: item.body, importance: item.importance };
}

const sameInstant = (a, b) => a && b && !Number.isNaN(new Date(a).getTime()) && new Date(a).getTime() === new Date(b).getTime();

function changedFields(before, after) {
  const out = {};
  for (const [k, v] of Object.entries(after)) {
    const old = before?.[k] ?? null;
    if ((v ?? null) === old || sameInstant(v, old)) continue;
    out[k] = v;
  }
  return out;
}

function readForm(kind, $g, dialog, bound) {
  const title = $g('title').value.trim();
  if (!title) throw new Error(t('form.titleRequired'));
  const description = $g('description')?.value.trim() || null;
  if (kind === 'SHARED_EVENT') {
    const out = { title, event_kind: $g('event_kind').value, attendance_default: chipValue(dialog, 'g-attendance'),
      group_criticality: chipValue(dialog, 'g-criticality'), description };
    if (!bound) {
      const startsAt = isoFromLocalInput($g('start').value);
      const endsAt = isoFromLocalInput($g('end').value);
      if (!startsAt || !endsAt) throw new Error(t('form.titleAndTime'));
      if (new Date(endsAt) <= new Date(startsAt)) throw new Error(t('event.endBeforeStart'));
      Object.assign(out, { starts_at: startsAt, ends_at: endsAt, location: $g('location').value.trim() || null });
    }
    return out;
  }
  if (kind === 'SHARED_OBLIGATION') {
    const deadline = isoFromLocalInput($g('deadline').value);
    if (!deadline) throw new Error(t('groups.deadlineRequired'));
    const effort = Number($g('effort').value || 0);
    return { title, obligation_kind: $g('obligation_kind').value, deadline, group_criticality: chipValue(dialog, 'g-criticality'),
      estimated_effort_hint_minutes: effort > 0 ? effort : null, description };
  }
  const body = $g('body').value.trim();
  if (!body) throw new Error(t('groups.bodyRequired'));
  return { title, body, importance: chipValue(dialog, 'g-importance') };
}

// «Отметить для группы»: an imported class (Google/ICS/…) gets a group annotation, e.g.
// "на этой паре — контрольная". The time and room stay owned by the external calendar.
export async function annotateExternalSheet(localEvent) {
  let data = sharedData();
  if (!data.groups?.length) data = (await load(SHARED_PATH, { fresh: true }).catch(() => ({ data: { groups: [] } }))).data;
  const groups = (data.groups || []).filter((g) => can(g, 'BIND_EXTERNAL') && can(g, 'PUBLISH_SHARED') && g.status === 'ACTIVE');
  if (!groups.length) { toast(t('groups.noAnnotateRight'), { error: true }); return; }
  const dialog = openSheet({
    eyebrow: localEvent.title,
    title: t('groups.annotateTitle'),
    body: `<div class="banner">${icon('route')}<div><p>${esc(t('groups.annotateHelp'))}</p></div></div>
      <div class="form">${field(t('groups.group'), `<select data-g="group">${groups.map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('')}</select>`)}
      ${field(t('form.title'), `<input data-g="title" maxlength="300" value="" placeholder="${esc(t('groups.eventPlaceholder'))}">`)}
      ${field(t('groups.eventKind'), select('event_kind', EVENT_KINDS, 'CONTROL_WORK', (v) => code('eventKind', v)))}
      <div class="field"><span>${esc(t('groups.criticalityGroup'))}</span>${chipGroup('g-criticality', CRITICALITY.map((v) => [v, code('criticality', v)]), 'CRITICAL')}</div></div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('groups.publish'))}</button>`,
  });
  const mutationId = newEntityId('mut');
  const entityId = newEntityId('sev');
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const $g = (n) => dialog.querySelector(`[data-g="${n}"]`);
    const kind = $g('event_kind').value;
    const result = await groupRequest(`/api/v1/groups/${encodeURIComponent($g('group').value)}/events`, {
      body: { id: entityId, title: $g('title').value.trim() || code('eventKind', kind), event_kind: kind,
        group_criticality: chipValue(dialog, 'g-criticality'), external_local_event_id: localEvent.id },
      mutationId, success: t('groups.published') });
    if (result) { dialog.close('saved'); await refresh(); }
  });
}

// ---- groups: create, join, subscription, invites, members -----------------------------------

export function createGroupSheet() {
  const dialog = openSheet({
    title: t('groups.createTitle'),
    body: `<div class="form">${field(t('groups.name'), `<input data-g="name" maxlength="120" placeholder="ПИ-261">`)}
      <div class="field"><span>${esc(t('groups.type'))}</span>${chipGroup('g-type', ['ACADEMIC', 'PROJECT', 'OTHER'].map((v) => [v, code('groupType', v)]), 'ACADEMIC')}</div>
      ${field(t('form.description'), `<textarea data-g="description" rows="2" maxlength="2000"></textarea>`)}
      <label class="check"><input type="checkbox" data-g="approval"> ${esc(t('groups.approvalRequired'))}</label></div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('groups.create'))}</button>`,
  });
  const mutationId = newEntityId('mut');
  const groupId = newEntityId('grp');
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const name = dialog.querySelector('[data-g="name"]').value.trim();
    if (!name) { toast(t('groups.nameRequired'), { error: true }); return; }
    const group = await groupRequest('/api/v1/groups', { mutationId, success: t('groups.created'), body: {
      id: groupId, name, type: chipValue(dialog, 'g-type'), description: dialog.querySelector('[data-g="description"]').value.trim() || null,
      settings: { default_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' },
      join_policy: { approval_required: dialog.querySelector('[data-g="approval"]').checked } } });
    if (group) { dialog.close('saved'); shell.go('group', { params: [group.id] }); }
  });
}

// A link "https://host/join/<token>" or a code "PI261-X4T9-K2MZ".
export function parseInvite(text) {
  const value = String(text || '').trim();
  const link = value.match(/\/join\/([A-Za-z0-9_-]{16,})/);
  if (link) return { token: link[1] };
  if (/^[A-Za-z0-9_-]{24,}$/.test(value) && !value.includes('-')) return { token: value };
  return value ? { code: value } : null;
}

export function joinSheet(prefill = '') {
  const dialog = openSheet({
    title: t('groups.joinTitle'),
    body: `<div class="form">${field(t('groups.inviteInput'), `<input data-g="invite" autocomplete="off" value="${esc(prefill)}" placeholder="PI261-X4T9-K2MZ">`, t('groups.inviteInputHelp'))}</div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('groups.join'))}</button>`,
  });
  const mutationId = newEntityId('mut');
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const invite = parseInvite(dialog.querySelector('[data-g="invite"]').value);
    if (!invite) return;
    const group = await groupRequest('/api/v1/groups/join', { body: invite, mutationId });
    if (!group) return;
    dialog.close('joined');
    const pending = group.my_membership?.status === 'PENDING';
    toast(t(pending ? 'groups.joinPending' : 'groups.joined', { name: group.name }));
    // Navigating closes open sheets: the group screen asks about the subscription once shown.
    if (!pending) afterJoin.add(group.id);
    shell.go('group', { params: [group.id] });
  });
}

// After joining: what to show and how loud. Aggressive (escalating) reminders are
// never switched on silently — the member ticks them here.
export function subscriptionSheet(group, { afterJoin = false } = {}) {
  const p = group.preferences || group.settings?.default_subscription_preferences || {};
  const box = (name, on, label) => `<label class="check"><input type="checkbox" data-s="${name}" ${on !== false ? 'checked' : ''}> ${esc(label)}</label>`;
  const dialog = openSheet({
    eyebrow: group.name,
    title: t(afterJoin ? 'groups.subscribeTitle' : 'groups.preferencesTitle'),
    body: `<div class="form"><p class="muted">${esc(t('groups.subscribeHelp'))}</p>
      ${box('show_regular_classes', p.show_regular_classes, t('groups.sub.classes'))}
      ${box('show_assessments', p.show_assessments, t('groups.sub.assessments'))}
      ${box('show_deadlines', p.show_deadlines, t('groups.sub.deadlines'))}
      ${box('show_announcements', p.show_announcements, t('groups.sub.announcements'))}
      ${box('announcements_in_agenda', p.announcements_in_agenda === true, t('groups.sub.announcementsInAgenda'))}
      <div class="field"><span>${esc(t('groups.notifications'))}</span>${chipGroup('s-notify', ['SILENT', 'CHANGES', 'CHANGES_AND_CRITICAL'].map((v) => [v, code('notificationBehavior', v)]),
        p.notification_behavior || 'CHANGES')}<small class="help">${esc(t('groups.notificationsHelp'))}</small></div>
      ${!afterJoin ? box('muted', p.muted === true ? false : true, t('groups.notMuted')) : ''}</div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t(afterJoin ? 'groups.later' : 'common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const payload = { notification_behavior: chipValue(dialog, 's-notify') };
    for (const el of dialog.querySelectorAll('[data-s]')) {
      payload[el.dataset.s] = el.dataset.s === 'muted' ? !el.checked : el.checked;
    }
    await change('group_preferences.update', group.id, payload, { success: t('groups.savedForMe') });
    dialog.close('saved');
  });
}

export async function inviteSheet(group) {
  const base = `/api/v1/groups/${encodeURIComponent(group.id)}/invites`;
  let invites = [];
  try { invites = (await api(base)).items || []; } catch (err) { toast(errorMessage(err), { error: true }); return; }
  const origin = session.server || location.origin;
  const active = invites.filter((i) => i.status === 'ACTIVE');
  const dialog = openSheet({
    eyebrow: group.name,
    title: t('groups.invitesTitle'),
    body: `<div data-new-invite></div>
      <div class="field-row"><button type="button" class="button" data-kind="LINK">${icon('route')}${esc(t('groups.newLink'))}</button>
      <button type="button" class="button" data-kind="CODE">${icon('plus')}${esc(t('groups.newCode'))}</button></div>
      <h3>${esc(t('groups.activeInvites'))}</h3>
      <div class="list">${active.length ? active.map((i) => `<article class="row"><span class="row-main">
        <strong>${esc(i.kind === 'CODE' ? i.code : t('groups.linkEnding', { hint: i.token_hint }))}</strong>
        <small>${esc(t('groups.inviteUses', { n: i.use_count, max: i.max_uses ?? '∞' }))} · ${esc(t('groups.until', { when: fmtDateTime(i.expires_at) }))}</small></span>
        <button type="button" class="button ghost" data-revoke="${esc(i.id)}" data-version="${esc(i.version)}">${esc(t('groups.revoke'))}</button></article>`).join('')
      : `<p class="muted">${esc(t('groups.noInvites'))}</p>`}</div>
      <p class="help">${esc(t('groups.inviteSecurity'))}</p>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.close'))}</button>`,
  });
  dialog.querySelectorAll('[data-kind]').forEach((button) => button.addEventListener('click', async () => {
    const invite = await groupRequest(base, { body: { kind: button.dataset.kind } });
    if (!invite) return;
    const text = invite.kind === 'CODE' ? invite.code : `${origin}${invite.path}`;
    dialog.querySelector('[data-new-invite]').innerHTML = `<div class="banner">${icon('check')}<div>
      <p>${esc(t(invite.kind === 'CODE' ? 'groups.codeReady' : 'groups.linkReady'))}</p><input readonly class="invite-value" value="${esc(text)}">
      <button type="button" class="button primary" data-copy>${esc(t('groups.copy'))}</button></div></div>`;
    dialog.querySelector('[data-copy]').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(text); toast(t('groups.copied')); } catch { dialog.querySelector('.invite-value').select(); }
    });
  }));
  dialog.querySelectorAll('[data-revoke]').forEach((button) => button.addEventListener('click', async () => {
    const ok = await confirmSheet({ title: t('groups.revokeTitle'), body: `<p>${esc(t('groups.revokeBody'))}</p>`, confirmLabel: t('groups.revoke'),
      cancelLabel: t('common.keep'), danger: true });
    if (!ok) return;
    if (await groupRequest(`${base}/${encodeURIComponent(button.dataset.revoke)}/revoke`, { body: { expected_version: Number(button.dataset.version) },
      success: t('groups.revoked') })) button.closest('.row')?.remove();
  }));
}

// Member actions depend on the caller's capabilities; the server checks them again.
export async function memberActions(group, member) {
  const items = [];
  const add = (id, ic, body, tone = 'accent') => items.push({ id, label: t(`groups.member.${id}`), icon: ic, tone, body });
  const path = `/api/v1/groups/${encodeURIComponent(group.id)}/members/${encodeURIComponent(member.account_id)}`;
  if (member.status === 'PENDING' && can(group, 'MANAGE_MEMBERS')) {
    add('approve', 'check', { status: 'ACTIVE' }, 'ok');
    add('reject', 'x', { status: 'REMOVED' }, 'muted');
  }
  if (member.status === 'ACTIVE' && can(group, 'MANAGE_ROLES') && member.role !== 'OWNER') {
    for (const role of ['ADMIN', 'SCHEDULER', 'MEMBER']) if (role !== member.role) add(`role_${role}`, 'settings', { role });
    add('role_OWNER', 'flag', { role: 'OWNER' }, 'warn');
  }
  if (member.status === 'ACTIVE' && can(group, 'MANAGE_MEMBERS') && member.role !== 'OWNER'
    && (member.role !== 'ADMIN' || can(group, 'MANAGE_ROLES'))) {
    add('remove', 'x', { status: 'REMOVED' }, 'danger');
    add('block', 'alert', { status: 'BLOCKED' }, 'danger');
  }
  if (member.status === 'BLOCKED' && can(group, 'MANAGE_MEMBERS')) add('unblock', 'repeat', { status: 'ACTIVE' });
  if (!items.length) return null;
  const chosen = await actionSheet({ title: member.display_name, items });
  const hit = items.find((x) => x.id === chosen);
  if (!hit) return null;
  if (['remove', 'block', 'role_OWNER'].includes(hit.id)) {
    const ok = await confirmSheet({ title: hit.label, body: `<p>${esc(t(`groups.member.confirm_${hit.id}`, { name: member.display_name }))}</p>`,
      confirmLabel: hit.label, cancelLabel: t('common.keep'), danger: hit.id !== 'role_OWNER' });
    if (!ok) return null;
  }
  const done = await groupRequest(path, { method: 'PATCH', body: { ...hit.body, expected_version: member.version }, success: t('groups.savedForGroup') });
  if (done) await refresh();
  return done;
}

export function proposalSheet(group, proposal) {
  const p = proposal.payload;
  const when = p.kind === 'SHARED_EVENT' ? `${fmtDay(p.starts_at)} · ${fmtTime(p.starts_at)}–${fmtTime(p.ends_at)}`
    : p.kind === 'SHARED_OBLIGATION' ? fmtDateTime(p.deadline) : '';
  const moderator = can(group, 'MODERATE_PROPOSALS') && proposal.status === 'PENDING';
  const mine = proposal.author_account_id === session.user?.account_id;
  const dialog = openSheet({
    eyebrow: t('groups.proposalFrom', { name: proposal.author || '—' }),
    title: p.title,
    body: `<dl class="kv-list"><div class="kv"><dt>${esc(t('groups.kind'))}</dt><dd>${esc(p.kind === 'SHARED_EVENT' ? code('eventKind', p.event_kind)
        : p.kind === 'SHARED_OBLIGATION' ? code('obligationKind', p.obligation_kind) : t('groups.announcement'))}</dd></div>
      ${when ? `<div class="kv"><dt>${esc(t('groups.when'))}</dt><dd>${esc(when)}</dd></div>` : ''}
      ${p.location ? `<div class="kv"><dt>${esc(t('groups.location'))}</dt><dd>${esc(p.location)}</dd></div>` : ''}
      ${p.group_criticality ? `<div class="kv"><dt>${esc(t('groups.criticality'))}</dt><dd>${criticalityChip(p.group_criticality)}</dd></div>` : ''}
      <div class="kv"><dt>${esc(t('groups.status'))}</dt><dd>${esc(code('proposalStatus', proposal.status))}</dd></div></dl>
      ${p.body ? `<p>${esc(p.body)}</p>` : ''}${p.description ? `<p class="muted">${esc(p.description)}</p>` : ''}
      ${moderator ? `<label class="field"><span>${esc(t('groups.reviewComment'))}</span><input data-g="comment" maxlength="1000"></label>` : ''}`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.close'))}</button>
      ${moderator ? `<button type="button" class="button" data-reject>${esc(t('groups.reject'))}</button>
        <button type="button" class="button" data-edit>${esc(t('groups.editApprove'))}</button>
        <button type="button" class="button primary" data-approve>${esc(t('groups.approve'))}</button>` : ''}
      ${!moderator && mine && proposal.status === 'PENDING' ? `<button type="button" class="button" data-withdraw>${esc(t('groups.withdraw'))}</button>` : ''}`,
  });
  const base = `/api/v1/groups/${encodeURIComponent(group.id)}/proposals/${encodeURIComponent(proposal.id)}`;
  const mutationId = newEntityId('mut');
  const comment = () => dialog.querySelector('[data-g="comment"]')?.value.trim() || null;
  dialog.querySelector('[data-approve]')?.addEventListener('click', async () => {
    if (await groupRequest(`${base}/approve`, { body: { expected_version: proposal.version, comment: comment() }, mutationId, success: t('groups.approved') })) {
      dialog.close('done'); await refresh();
    }
  });
  dialog.querySelector('[data-edit]')?.addEventListener('click', () => {
    dialog.close('edit');
    entityForm(group, p.kind === 'SHARED_ANNOUNCEMENT' ? 'ANNOUNCEMENT' : p.kind, null, { proposalEdits: proposal });
  });
  dialog.querySelector('[data-reject]')?.addEventListener('click', async () => {
    if (await groupRequest(`${base}/reject`, { body: { expected_version: proposal.version, comment: comment() }, mutationId, success: t('groups.rejected') })) {
      dialog.close('done'); await refresh();
    }
  });
  dialog.querySelector('[data-withdraw]')?.addEventListener('click', async () => {
    if (await groupRequest(`${base}/withdraw`, { body: { expected_version: proposal.version }, mutationId, success: t('groups.withdrawn') })) {
      dialog.close('done'); await refresh();
    }
  });
}

export async function groupSettingsSheet(group) {
  const s = group.settings;
  const p = group.join_policy;
  const box = (name, on, label) => `<label class="check"><input type="checkbox" data-s="${name}" ${on ? 'checked' : ''}> ${esc(label)}</label>`;
  const dialog = openSheet({
    eyebrow: group.name,
    title: t('groups.settingsTitle'),
    body: `<div class="form">${field(t('groups.name'), `<input data-g="name" maxlength="120" value="${esc(group.name)}">`)}
      ${field(t('form.description'), `<textarea data-g="description" rows="2" maxlength="2000">${esc(group.description || '')}</textarea>`)}
      ${box('allow_members_to_publish', s.allow_members_to_publish, t('groups.allowMembersPublish'))}
      ${box('approval_required', p.approval_required, t('groups.approvalRequired'))}
      ${box('invite_links_enabled', p.invite_links_enabled, t('groups.linksEnabled'))}
      ${box('join_codes_enabled', p.join_codes_enabled, t('groups.codesEnabled'))}
      ${box('allow_rejoin_after_removal', p.allow_rejoin_after_removal, t('groups.rejoinAfterRemoval'))}</div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      ${can(group, 'ARCHIVE_GROUP') ? `<button type="button" class="button" data-archive>${esc(t(group.status === 'ARCHIVED' ? 'groups.unarchive' : 'groups.archive'))}</button>` : ''}
      ${can(group, 'DELETE_GROUP') ? `<button type="button" class="button danger" data-delete>${esc(t('groups.delete'))}</button>` : ''}
      <button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>`,
  });
  const base = `/api/v1/groups/${encodeURIComponent(group.id)}`;
  const flag = (n) => dialog.querySelector(`[data-s="${n}"]`).checked;
  dialog.querySelector('[data-save]').addEventListener('click', async () => {
    const body = { expected_version: group.version, name: dialog.querySelector('[data-g="name"]').value.trim(),
      description: dialog.querySelector('[data-g="description"]').value.trim() || null,
      settings: { allow_members_to_publish: flag('allow_members_to_publish') },
      join_policy: { approval_required: flag('approval_required'), invite_links_enabled: flag('invite_links_enabled'),
        join_codes_enabled: flag('join_codes_enabled'), allow_rejoin_after_removal: flag('allow_rejoin_after_removal') } };
    if (await groupRequest(base, { method: 'PATCH', body, success: t('groups.savedForGroup') })) { dialog.close('saved'); await refresh(); }
  });
  dialog.querySelector('[data-archive]')?.addEventListener('click', async () => {
    if (await groupRequest(`${base}/${group.status === 'ARCHIVED' ? 'unarchive' : 'archive'}`, { body: { expected_version: group.version } })) {
      dialog.close('saved'); await refresh();
    }
  });
  dialog.querySelector('[data-delete]')?.addEventListener('click', async () => {
    const ok = await confirmSheet({ title: t('groups.deleteTitle'), body: `<p>${esc(t('groups.deleteBody', { name: group.name }))}</p>`,
      confirmLabel: t('groups.delete'), cancelLabel: t('common.keep'), danger: true });
    if (ok && await groupRequest(`${base}/delete`, { body: { expected_version: group.version }, success: t('groups.deleted') })) {
      dialog.close('saved'); shell.go('groups');
    }
  });
}

export async function leaveGroup(group) {
  const ok = await confirmSheet({ title: t('groups.leaveTitle'), body: `<p>${esc(t('groups.leaveBody', { name: group.name }))}</p>`,
    confirmLabel: t('groups.leave'), cancelLabel: t('common.keep'), danger: true });
  if (ok && await groupRequest(`/api/v1/groups/${encodeURIComponent(group.id)}/leave`, { success: t('groups.left') })) shell.go('groups');
}

export const isAssessment = (kind) => ASSESSMENTS.has(kind);
export const isClass = (kind) => CLASSES.has(kind);
