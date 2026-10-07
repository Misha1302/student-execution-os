import { t } from '../i18n.js';
import { esc, icon } from '../ui.js';
import { peek } from '../store.js';

// «Ещё» is grouped by what the user wants to do, not by the kind of record behind it:
// work that goes into the plan, things to mark as done, things that organize the day,
// and the app itself. Sources/evidence diagnostics live under Settings → Advanced.
export const MORE_GROUPS = [
  ['more.group.do', [
    ['projects', 'plan', 'more.projectsHint'],
    ['routines', 'repeat', 'more.routinesHint'],
  ]],
  ['more.group.track', [
    ['checkins', 'check', 'more.checkinsHint'],
    ['reflection', 'clock', 'more.reflectionHint'],
  ]],
  ['more.group.organize', [
    ['calendar', 'calendar', 'more.calendarHint'],
    ['places', 'place', 'more.placesHint'],
    ['notes', 'note', 'more.notesHint'],
    ['groups', 'calendar', 'more.groupsHint'],
  ]],
  ['more.group.system', [
    ['notifications', 'bell', 'more.notificationsHint'],
    ['settings', 'settings', 'more.settingsHint'],
  ]],
];

export const MORE_ITEMS = MORE_GROUPS.flatMap(([, items]) => items);

export default {
  id: 'more',
  tab: 'more',
  title: () => t('nav.more'),
  load: async () => ({ data: null, stale: false }),
  render() {
    const pending = (peek('/api/v1/notifications') || []).filter((n) => !n.seen_at && !n.acted_at).length;
    return MORE_GROUPS.map(([group, items]) => `<section class="section" data-more-group="${esc(group.slice(11))}">
      <h2 class="group-title">${esc(t(group))}</h2>
      <div class="menu-list">${items.map(([id, ic, hint]) => `
      <button class="menu-row" data-nav="${id}">
        <span class="menu-icon tone-accent">${icon(ic)}</span>
        <span class="menu-copy"><strong>${esc(t(`nav.${id}`))}</strong><small>${esc(t(hint))}</small></span>
        ${id === 'notifications' && pending ? `<span class="count">${pending}</span>` : ''}
        ${icon('chevron', 'menu-chevron')}
      </button>`).join('')}</div></section>`).join('');
  },
};
