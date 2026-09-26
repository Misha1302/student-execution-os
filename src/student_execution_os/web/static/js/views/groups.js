// «Группы»: the groups the user belongs to, and one group's screen (updates, events,
// deadlines, suggestions, members). Group facts come from the member's personal
// projection (/api/v1/me/shared), so the same cards and actions as in «Дела» apply
// and they work from the cache offline; group-wide changes need the network.
import { api } from '../api.js';
import { load } from '../store.js';
import { t, code, fmtDateTime } from '../i18n.js';
import { esc, icon, chip, empty, sectionHead } from '../ui.js';
import {
  SHARED_PATH, afterJoin, sharedRow, sharedSheet, findShared, createGroupSheet, joinSheet, subscriptionSheet, inviteSheet,
  memberActions, entityForm, proposalSheet, groupSettingsSheet, leaveGroup, openSharedActions,
} from '../groups.js';

const has = (group, capability) => (group?.capabilities || []).includes(capability);

function groupCard(g) {
  const pending = g.my_membership?.status === 'PENDING';
  return `<button class="menu-row" data-nav-group="${esc(g.id)}">
    <span class="menu-icon tone-accent">${icon('tasks')}</span>
    <span class="menu-copy"><strong>${esc(g.name)}</strong>
      <small>${esc(code('role', g.my_membership?.role))} · ${esc(t('groups.members', { n: g.member_count || 0 }))}${g.status === 'ARCHIVED' ? ` · ${esc(t('groups.archived'))}` : ''}</small></span>
    ${pending ? chip(t('groups.pendingApproval'), 'warn') : ''}${g.pending_proposals ? `<span class="count">${g.pending_proposals}</span>` : ''}
    ${icon('chevron', 'menu-chevron')}
  </button>`;
}

export const groupsView = {
  id: 'groups',
  tab: 'more',
  title: () => t('nav.groups'),
  async load({ fresh }) {
    const result = await load(SHARED_PATH, { fresh });
    return { data: result.data, stale: result.stale, fetchedAt: result.fetchedAt };
  },
  render(data) {
    const groups = data?.groups || [];
    return `<section class="section">
        <div class="field-row"><button class="button primary" data-action="group-create">${icon('plus')}${esc(t('groups.create'))}</button>
          <button class="button" data-action="group-join">${icon('route')}${esc(t('groups.join'))}</button></div>
      </section>
      <section class="section">${groups.length ? `<div class="menu-list">${groups.map(groupCard).join('')}</div>`
        : empty(t('groups.emptyTitle'), t('groups.emptyHint'), 'tasks')}</section>
      <p class="help pad">${icon('alert')} ${esc(t('groups.privacyNote'))}</p>`;
  },
  mount(root, _data, ctx) {
    root.addEventListener('click', (event) => {
      const row = event.target.closest('[data-nav-group]');
      if (row) location.hash = `#/group/${encodeURIComponent(row.dataset.navGroup)}`;
    });
  },
  actions: {
    'group-create': () => createGroupSheet(),
    'group-join': () => joinSheet(),
  },
};

// #/join/<token>: an invite link opened in the app.
export const joinView = {
  id: 'join',
  detail: true,
  tab: 'more',
  title: () => t('groups.joinTitle'),
  load: async () => ({ data: null, stale: false }),
  render: () => `<div class="empty">${icon('route', 'empty-icon')}<strong>${esc(t('groups.joinTitle'))}</strong></div>`,
  mount(_root, _data, ctx) {
    const token = ctx.params?.[0];
    if (token) joinSheet(`/join/${token}`);
  },
};

let tab = 'updates';

export const groupView = {
  id: 'group',
  detail: true,
  tab: 'more',
  title: () => t('nav.group'),
  async load({ fresh, params }) {
    const id = params[0];
    const [shared, group] = await Promise.all([load(SHARED_PATH, { fresh }), load(`/api/v1/groups/${encodeURIComponent(id)}`, { fresh })]);
    const g = group.data;
    const extra = {};
    if (!group.stale) {
      const base = `/api/v1/groups/${encodeURIComponent(id)}`;
      const reads = [api(`${base}/proposals`).then((r) => { extra.proposals = r.items; }).catch(() => {}),
        api(`${base}/members`).then((r) => { extra.members = r.items; }).catch(() => {})];
      if (has(g, 'MANAGE_MEMBERS')) reads.push(api(`${base}/members?include=other`).then((r) => { extra.others = r.items; }).catch(() => {}));
      await Promise.all(reads);
    }
    return { data: { group: g, items: (shared.data?.items || []).filter((x) => x.group_id === id), ...extra },
      stale: shared.stale || group.stale, fetchedAt: shared.fetchedAt };
  },
  render(data) {
    const g = data.group;
    this._data = data;
    const publish = has(g, 'PUBLISH_SHARED');
    const items = data.items || [];
    // «Новое»: what this member has not opened yet (a new fact or a change of one), newest first.
    const unseen = (x) => x.change || Number(x.personal?.last_seen_version || 0) < Number(x.version || 0);
    const updates = items.filter((x) => x.status === 'PUBLISHED' ? unseen(x) : Boolean(x.change))
      .sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
    const events = items.filter((x) => x.kind === 'SHARED_EVENT');
    const deadlines = items.filter((x) => x.kind === 'SHARED_OBLIGATION');
    const proposals = data.proposals || [];
    const tabs = [['updates', t('groups.tab.updates')], ['events', t('groups.tab.events')], ['deadlines', t('groups.tab.deadlines')],
      ['proposals', `${t('groups.tab.proposals')}${proposals.length ? ` ${proposals.length}` : ''}`], ['members', t('groups.tab.members')]];
    const list = (rows, emptyKey) => (rows.length ? `<div class="task-list">${rows.map(sharedRow).join('')}</div>` : empty(t(emptyKey), '', 'check'));
    let body = '';
    if (tab === 'updates') body = list(updates, 'groups.noUpdates');
    if (tab === 'events') body = list(events, 'groups.noEvents');
    if (tab === 'deadlines') body = list(deadlines, 'groups.noDeadlines');
    if (tab === 'proposals') {
      body = proposals.length ? `<div class="list">${proposals.map((p) => `<button class="row" data-proposal="${esc(p.id)}">
        <span class="row-main"><strong>${esc(p.payload.title)}</strong><small>${esc(t('groups.proposalFrom', { name: p.author }))} · ${esc(fmtDateTime(p.created_at))}</small></span>
        ${chip(code('proposalStatus', p.status), p.status === 'PENDING' ? 'warn' : 'muted')}</button>`).join('')}</div>`
        : empty(t('groups.noProposals'), has(g, 'MODERATE_PROPOSALS') ? '' : t('groups.proposeHint'), 'check');
    }
    if (tab === 'members') {
      const all = [...(data.members || []), ...(data.others || [])];
      body = `<div class="list">${all.map((m) => `<button class="row" data-member="${esc(m.account_id)}">
        <span class="row-main"><strong>${esc(m.display_name)}</strong><small>${esc(code('role', m.role))}</small></span>
        ${m.status !== 'ACTIVE' ? chip(code('membershipStatus', m.status), m.status === 'PENDING' ? 'warn' : 'muted') : ''}</button>`).join('')}</div>
        <p class="help">${esc(t('groups.membersPrivacy'))}</p>`;
    }
    const create = (kind, label) => `<button class="button" data-create="${kind}">${icon('plus')}${esc(t(label))}</button>`;
    return `<section class="section group-head">
        <h2>${esc(g.name)}</h2>
        <p class="muted">${esc(code('groupType', g.type))} · ${esc(code('role', g.my_membership?.role))}${g.status === 'ARCHIVED' ? ` · ${esc(t('groups.archived'))}` : ''}</p>
        ${g.description ? `<p>${esc(g.description)}</p>` : ''}
        ${g.my_membership?.status === 'PENDING' ? `<div class="banner warn">${icon('clock')}<div><p>${esc(t('groups.pendingApprovalHelp'))}</p></div></div>` : ''}
        <div class="field-row wrap">
          ${g.status === 'ACTIVE' ? `${create('SHARED_EVENT', publish ? 'groups.newEvent' : 'groups.proposeEvent')}
          ${create('SHARED_OBLIGATION', publish ? 'groups.newDeadline' : 'groups.proposeDeadline')}
          ${create('ANNOUNCEMENT', publish ? 'groups.newAnnouncement' : 'groups.proposeAnnouncement')}` : ''}
          <button class="button ghost" data-action="group-preferences">${icon('bell')}${esc(t('groups.myPreferences'))}</button>
          ${has(g, 'MANAGE_INVITES') ? `<button class="button ghost" data-action="group-invites">${icon('route')}${esc(t('groups.invite'))}</button>` : ''}
          ${has(g, 'MANAGE_SETTINGS') ? `<button class="button ghost" data-action="group-settings">${icon('settings')}${esc(t('groups.settings'))}</button>` : ''}
          ${g.my_membership?.role !== 'OWNER' ? `<button class="button ghost" data-action="group-leave">${icon('logout')}${esc(t('groups.leave'))}</button>` : ''}
        </div></section>
      <div class="toolbar"><div class="segmented">${tabs.map(([id, label]) => `<button type="button" class="chip-toggle${id === tab ? ' on' : ''}" data-group-tab="${id}">${esc(label)}</button>`).join('')}</div></div>
      <section class="section">${body}</section>`;
  },
  mount(root, data, ctx) {
    if (afterJoin.delete(data.group.id)) subscriptionSheet(data.group, { afterJoin: true });
    root.addEventListener('click', async (event) => {
      const tabButton = event.target.closest('[data-group-tab]');
      if (tabButton) { tab = tabButton.dataset.groupTab; ctx.rerender(); return; }
      const create = event.target.closest('[data-create]');
      if (create) { entityForm(data.group, create.dataset.create); return; }
      const proposal = event.target.closest('[data-proposal]');
      if (proposal) { proposalSheet(data.group, (data.proposals || []).find((p) => p.id === proposal.dataset.proposal)); return; }
      const member = event.target.closest('[data-member]');
      if (member) {
        const all = [...(data.members || []), ...(data.others || [])];
        await memberActions(data.group, all.find((m) => m.account_id === member.dataset.member));
      }
    });
  },
  actions: {
    'open-shared'(el) {
      const item = (groupView._data?.items || []).find((x) => x.id === el.dataset.id) || findShared(el.dataset.id);
      if (item) sharedSheet(item);
    },
    'group-preferences': () => subscriptionSheet(groupView._data.group),
    'group-invites': () => inviteSheet(groupView._data.group),
    'group-settings': () => groupSettingsSheet(groupView._data.group),
    'group-leave': () => leaveGroup(groupView._data.group),
  },
};

export { openSharedActions };
