import { t } from '../i18n.js';
import { esc, icon } from '../ui.js';
import { peek } from '../store.js';

// Sources/evidence diagnostics live under Settings → Advanced.
export const MORE_ITEMS = [
  ['calendar', 'calendar', 'more.calendarHint'],
  ['notifications', 'bell', 'more.notificationsHint'],
  ['places', 'place', 'more.placesHint'],
  ['settings', 'settings', 'more.settingsHint'],
];

export default {
  id: 'more',
  tab: 'more',
  title: () => t('nav.more'),
  load: async () => ({ data: null, stale: false }),
  render() {
    const pending = (peek('/api/v1/notifications') || []).filter((n) => !n.seen_at && !n.acted_at).length;
    return `<div class="menu-list">${MORE_ITEMS.map(([id, ic, hint]) => `
      <button class="menu-row" data-nav="${id}">
        <span class="menu-icon tone-accent">${icon(ic)}</span>
        <span class="menu-copy"><strong>${esc(t(`nav.${id}`))}</strong><small>${esc(t(hint))}</small></span>
        ${id === 'notifications' && pending ? `<span class="count">${pending}</span>` : ''}
        ${icon('chevron', 'menu-chevron')}
      </button>`).join('')}</div>`;
  },
};
