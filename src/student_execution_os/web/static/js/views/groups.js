import { api } from '../api.js';
import { invalidate } from '../store.js';
import { t, fmtDateTime } from '../i18n.js';
import { esc, chip, toast, errorMessage, setBusy, confirmSheet, openSheet, chipGroup, chipValue, localInputValue, isoFromLocalInput, focusSoon } from '../ui.js';
import { durationPicker, takeDuration } from '../duration.js';
import { newEntityId } from '../sync.js';
import { shell } from '../actions.js';

// Collaborative groups. The group owns shared academic facts (its schedule); what each
// student does about them (moving a class, skipping it, notes, reminders) stays in their
// own calendar and is never shown here. Members suggest changes; the owner/starosta
// publishes and decides.

const STAFF = new Set(['OWNER', 'STAROSTA']);
const ROLE_TONE = { OWNER: 'ok', STAROSTA: 'accent', MEMBER: 'muted' };

function roleChip(role) { return chip(t(`groups.role.${role}`), ROLE_TONE[role] || 'muted'); }

function when(item) {
  const i = item.item;
  if (item.kind === 'EVENT') return fmtDateTime(i.starts_at);
  const weekly = /INTERVAL=2/.test(i.recurrence_rule) ? t('form.repeat.biweekly') : /DAILY/.test(i.recurrence_rule) ? t('form.repeat.daily') : t('form.repeat.weekly');
  return `${fmtDateTime(`${i.dtstart_local}`)} · ${weekly}`;
}

export const groups = {
  id: 'groups',
  tab: 'more',
  title: () => t('nav.groups'),
  async load() { return { data: await api('/api/v1/groups') }; },
  render(list) {
    return `<section class="section" data-groups>
      <div class="card form">
        <p class="muted">${esc(t('groups.help'))}</p>
        ${list.length ? list.map((g) => `<button class="menu-row" data-action="group-open" data-id="${esc(g.id)}">
            <span class="menu-copy"><strong>${esc(g.name)}</strong><small>${esc(t('groups.members', { n: g.member_count }))}</small></span>
            ${roleChip(g.role)}</button>`).join('') : `<p class="muted">${esc(t('groups.none'))}</p>`}
        <div class="button-row">
          <button class="button" data-action="group-join">${esc(t('groups.join'))}</button>
          <button class="button primary" data-action="group-create">${esc(t('groups.create'))}</button>
        </div>
      </div>
    </section>`;
  },
  actions: {
    'group-open': (el) => shell.go('group', { params: [el.dataset.id] }),
    'group-create': () => {
      const dialog = openSheet({
        title: t('groups.create'),
        body: `<div class="form"><label class="field"><span>${esc(t('groups.name'))}</span><input data-name maxlength="120" placeholder="БИ-24-1"></label></div>`,
        actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
          <button type="button" class="button primary" data-save>${esc(t('groups.create'))}</button>`,
      });
      focusSoon(dialog.querySelector('[data-name]'));
      dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
        const name = dialog.querySelector('[data-name]').value.trim();
        if (!name) return;
        setBusy(e.currentTarget, true);
        try {
          const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Europe/Moscow';
          const group = await api('/api/v1/groups', { method: 'POST', body: { id: newEntityId('group'), name, timezone_name: zone } });
          dialog.close('saved');
          shell.go('group', { params: [group.id] });
        } catch (err) { setBusy(e.currentTarget, false); toast(errorMessage(err), { error: true }); }
      });
    },
    'group-join': () => {
      const dialog = openSheet({
        title: t('groups.join'),
        body: `<div class="form"><label class="field"><span>${esc(t('groups.code'))}</span><input data-code autocomplete="off" placeholder="BOTAY-…"></label></div>`,
        actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
          <button type="button" class="button primary" data-save>${esc(t('groups.join'))}</button>`,
      });
      focusSoon(dialog.querySelector('[data-code]'));
      dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
        setBusy(e.currentTarget, true);
        try {
          const group = await api('/api/v1/groups/join', { method: 'POST', body: { code: dialog.querySelector('[data-code]').value.trim() } });
          dialog.close('saved');
          invalidate();
          shell.go('group', { params: [group.id] });
        } catch (err) { setBusy(e.currentTarget, false); toast(errorMessage(err), { error: true }); }
      });
    },
  },
};

function itemSheet(group, ctx, existing = null, { propose = false } = {}) {
  const kind = existing?.kind || 'SERIES';
  const i = existing?.item || {};
  const dialog = openSheet({
    title: propose ? t('groups.suggest') : existing ? t('groups.editItem') : t('groups.addItem'),
    full: true,
    body: `<div class="form">
      ${existing ? '' : `<div class="field"><span>${esc(t('groups.kind'))}</span>${chipGroup('group-kind', [['SERIES', t('groups.kind.SERIES')], ['EVENT', t('groups.kind.EVENT')]], kind)}</div>`}
      <label class="field"><span>${esc(t('form.title'))}</span><input data-g="title" maxlength="180" value="${esc(i.title || '')}"></label>
      <label class="field"><span>${esc(t('form.firstStart'))}</span><input type="datetime-local" data-g="start" value="${esc(i.dtstart_local ? i.dtstart_local.slice(0, 16) : i.starts_at ? localInputValue(new Date(i.starts_at)) : '')}"></label>
      <div class="field"><span>${esc(t('form.duration'))}</span>${durationPicker('g-duration', i.duration_minutes || (i.starts_at ? Math.round((new Date(i.ends_at) - new Date(i.starts_at)) / 60000) : 90))}</div>
      <div class="field" data-series-only><span>${esc(t('form.repeatCount'))}</span>${chipGroup('g-count', [['8', '8'], ['16', '16'], ['30', '30']], (/COUNT=(\d+)/.exec(i.recurrence_rule || '') || [])[1] || '16')}</div>
      <label class="field"><span>${esc(t('series.location'))}</span><input data-g="location" maxlength="200" value="${esc(i.location_text || '')}"></label>
      <label class="field"><span>${esc(t('series.teacher'))}</span><input data-g="teacher" maxlength="200" value="${esc(i.teacher || '')}"></label>
      ${propose ? `<label class="field"><span>${esc(t('groups.note'))}</span><input data-g="note" maxlength="500"></label>` : ''}
    </div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-save>${esc(propose ? t('groups.send') : t('common.save'))}</button>`,
  });
  const $ = (n) => dialog.querySelector(`[data-g="${n}"]`);
  const currentKind = () => existing?.kind || chipValue(dialog, 'group-kind');
  const toggle = () => { dialog.querySelector('[data-series-only]').hidden = currentKind() !== 'SERIES'; };
  dialog.addEventListener('chipchange', toggle);
  toggle();
  focusSoon($('title'));
  dialog.querySelector('[data-save]').addEventListener('click', async (e) => {
    const title = $('title').value.trim();
    const start = $('start').value;
    const duration = takeDuration(dialog, 'g-duration');
    if (!title || !start || !duration) { toast(t('form.titleAndTime'), { error: true }); return; }
    const k = currentKind();
    const base = { title, location_text: $('location').value.trim() || null, teacher: $('teacher').value.trim() || null };
    const item = k === 'SERIES'
      ? { ...base, dtstart_local: `${start}:00`, duration_minutes: duration, recurrence_rule: `FREQ=WEEKLY;COUNT=${chipValue(dialog, 'g-count')}`, timezone_name: group.timezone_name, category: 'LESSON', changes: i.changes || [], exdates_local: i.exdates_local || [] }
      : { ...base, starts_at: isoFromLocalInput(start), ends_at: new Date(new Date(isoFromLocalInput(start)).getTime() + duration * 60000).toISOString(), category: 'EXAM' };
    const uid = existing?.uid || newEntityId(k === 'SERIES' ? 'gseries' : 'gevent');
    setBusy(e.currentTarget, true);
    try {
      if (propose) {
        await api(`/api/v1/groups/${encodeURIComponent(group.id)}/proposals`, { method: 'POST', body: { id: newEntityId('proposal'), action: 'UPSERT', uid, kind: k, item, note: $('note').value.trim() || null } });
        toast(t('groups.sent'));
      } else {
        await api(`/api/v1/groups/${encodeURIComponent(group.id)}/schedule/${encodeURIComponent(uid)}`, { method: 'PUT', body: { kind: k, item, expected_revision: group.schedule_revision } });
        invalidate();
      }
      dialog.close('saved');
      ctx.refresh();
    } catch (err) { setBusy(e.currentTarget, false); toast(errorMessage(err), { error: true }); }
  });
}

export const group = {
  id: 'group',
  tab: 'more',
  detail: true,
  title: () => t('nav.groups'),
  async load({ params }) { return { data: await api(`/api/v1/groups/${encodeURIComponent(params[0] || '')}`) }; },
  render(g) {
    const staff = STAFF.has(g.my_role);
    const owner = g.my_role === 'OWNER';
    const pending = g.proposals.filter((p) => p.status === 'PENDING');
    return `<section class="section" data-group="${esc(g.id)}">
      <div class="section-head"><h2>${esc(g.name)}</h2>${roleChip(g.my_role)}</div>
      <div class="card form" data-group-schedule>
        <h3>${esc(t('groups.schedule'))}</h3>
        <p class="help">${esc(t('groups.personalHelp'))}</p>
        ${g.schedule.length ? g.schedule.map((item) => `<div class="row static" data-item="${esc(item.uid)}">
            <span class="row-main"><strong>${esc(item.item.title)}</strong>
              <small>${esc(when(item))}${item.item.location_text ? ` · ${esc(item.item.location_text)}` : ''}${item.item.teacher ? ` · ${esc(item.item.teacher)}` : ''}</small></span>
            ${staff ? `<button class="button ghost" data-action="group-item-edit" data-uid="${esc(item.uid)}">${esc(t('groups.edit'))}</button>
              <button class="button danger ghost" data-action="group-item-remove" data-uid="${esc(item.uid)}">${esc(t('groups.remove'))}</button>`
              : `<button class="button ghost" data-action="group-item-suggest" data-uid="${esc(item.uid)}">${esc(t('groups.suggest'))}</button>`}
          </div>`).join('') : `<p class="muted">${esc(t('groups.emptySchedule'))}</p>`}
        <button class="button wide" data-action="${staff ? 'group-item-add' : 'group-item-suggest'}">${esc(staff ? t('groups.addItem') : t('groups.suggest'))}</button>
      </div>
      ${g.proposals.length ? `<div class="card form" data-group-proposals><h3>${esc(t('groups.proposals'))}</h3>
        ${g.proposals.map((p) => `<div class="row static" data-proposal="${esc(p.id)}">
          <span class="row-main"><strong>${esc(p.action === 'REMOVE' ? t('groups.proposeRemove', { uid: (g.schedule.find((s) => s.uid === p.uid)?.item.title) || p.uid }) : p.item?.title || p.uid)}</strong>
            <small>${esc(t(`groups.status.${p.status}`))}${p.note ? ` · ${esc(p.note)}` : ''}</small></span>
          ${p.status === 'PENDING' && staff ? `<button class="button ghost" data-action="group-reject" data-id="${esc(p.id)}">${esc(t('groups.reject'))}</button>
            <button class="button primary" data-action="group-approve" data-id="${esc(p.id)}">${esc(t('groups.approve'))}</button>` : ''}
          ${p.status === 'PENDING' && !staff ? `<button class="button ghost" data-action="group-withdraw" data-id="${esc(p.id)}">${esc(t('groups.withdraw'))}</button>` : ''}
        </div>`).join('')}</div>` : ''}
      <div class="card form" data-group-members>
        <h3>${esc(t('groups.membersTitle'))}${pending.length && staff ? ` ${chip(String(pending.length), 'warn')}` : ''}</h3>
        ${g.members.map((m) => `<div class="row static" data-member="${esc(m.member_id)}">
          <span class="row-main"><strong>${esc(m.login || '—')}${m.me ? ` (${esc(t('groups.you'))})` : ''}</strong></span>
          ${roleChip(m.role)}
          ${owner && !m.me ? `<button class="button ghost" data-action="group-role" data-member="${esc(m.member_id)}" data-role="${esc(m.role)}">${esc(t('groups.changeRole'))}</button>` : ''}
          ${staff && !m.me && (owner || m.role === 'MEMBER') ? `<button class="button danger ghost" data-action="group-kick" data-member="${esc(m.member_id)}" data-login="${esc(m.login || '')}">${esc(t('groups.remove'))}</button>` : ''}
        </div>`).join('')}
        <div class="button-row">
          ${staff ? `<button class="button" data-action="group-invite">${esc(t('groups.invite'))}</button>` : ''}
          <button class="button danger ghost" data-action="group-leave">${esc(t('groups.leave'))}</button>
        </div>
      </div>
    </section>`;
  },
  actions: {
    'group-item-add': (_el, ctx) => itemSheet(ctx.data, ctx),
    'group-item-edit': (el, ctx) => itemSheet(ctx.data, ctx, ctx.data.schedule.find((s) => s.uid === el.dataset.uid)),
    'group-item-suggest': (el, ctx) => itemSheet(ctx.data, ctx, ctx.data.schedule.find((s) => s.uid === el.dataset.uid) || null, { propose: true }),
    'group-item-remove': async (el, ctx) => {
      const g = ctx.data;
      const ok = await confirmSheet({ title: t('groups.removeItemTitle'), body: `<p>${esc(t('groups.removeItemBody'))}</p>`, confirmLabel: t('groups.remove'), danger: true });
      if (!ok) return;
      await api(`/api/v1/groups/${encodeURIComponent(g.id)}/schedule/${encodeURIComponent(el.dataset.uid)}/remove`, { method: 'POST', body: { expected_revision: g.schedule_revision } });
      invalidate();
      ctx.refresh();
    },
    'group-approve': async (el, ctx) => { await api(`/api/v1/groups/${encodeURIComponent(ctx.data.id)}/proposals/${encodeURIComponent(el.dataset.id)}/decide`, { method: 'POST', body: { decision: 'APPROVE' } }); invalidate(); ctx.refresh(); },
    'group-reject': async (el, ctx) => { await api(`/api/v1/groups/${encodeURIComponent(ctx.data.id)}/proposals/${encodeURIComponent(el.dataset.id)}/decide`, { method: 'POST', body: { decision: 'REJECT' } }); ctx.refresh(); },
    'group-withdraw': async (el, ctx) => { await api(`/api/v1/groups/${encodeURIComponent(ctx.data.id)}/proposals/${encodeURIComponent(el.dataset.id)}/withdraw`, { method: 'POST' }); ctx.refresh(); },
    'group-invite': async (el, ctx) => {
      setBusy(el, true);
      try {
        const invitation = await api(`/api/v1/groups/${encodeURIComponent(ctx.data.id)}/invitations`, { method: 'POST', body: { expires_in_days: 7, max_uses: 100 } });
        openSheet({ title: t('groups.invite'), body: `<div class="form"><p>${esc(t('groups.inviteHelp', { when: fmtDateTime(invitation.expires_at) }))}</p>
          <label class="field"><span>${esc(t('groups.code'))}</span><input readonly data-invite-code value="${esc(invitation.code)}"></label></div>`,
        actions: `<button value="done" class="button primary">${esc(t('common.close'))}</button>` });
      } finally { setBusy(el, false); }
    },
    'group-role': (el, ctx) => {
      const dialog = openSheet({ title: t('groups.changeRole'), body: `<div class="form">${chipGroup('g-role', ['OWNER', 'STAROSTA', 'MEMBER'].map((r) => [r, t(`groups.role.${r}`)]), el.dataset.role)}</div>`,
        actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button><button type="button" class="button primary" data-save>${esc(t('common.save'))}</button>` });
      dialog.querySelector('[data-save]').addEventListener('click', async () => {
        try {
          await api(`/api/v1/groups/${encodeURIComponent(ctx.data.id)}/members/${encodeURIComponent(el.dataset.member)}/role`, { method: 'POST', body: { role: chipValue(dialog, 'g-role') } });
          dialog.close('saved'); ctx.refresh();
        } catch (err) { toast(errorMessage(err), { error: true }); }
      });
    },
    'group-kick': async (el, ctx) => {
      const ok = await confirmSheet({ title: t('groups.kickTitle', { login: el.dataset.login }), body: `<p>${esc(t('groups.kickBody'))}</p>`, confirmLabel: t('groups.remove'), danger: true });
      if (!ok) return;
      await api(`/api/v1/groups/${encodeURIComponent(ctx.data.id)}/members/${encodeURIComponent(el.dataset.member)}`, { method: 'DELETE' });
      ctx.refresh();
    },
    'group-leave': async (_el, ctx) => {
      const ok = await confirmSheet({ title: t('groups.leaveTitle'), body: `<p>${esc(t('groups.leaveBody'))}</p>`, confirmLabel: t('groups.leave'), danger: true });
      if (!ok) return;
      await api(`/api/v1/groups/${encodeURIComponent(ctx.data.id)}/leave`, { method: 'POST' });
      invalidate();
      shell.go('groups');
    },
  },
};

export default groups;
