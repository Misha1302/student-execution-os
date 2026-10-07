import { load, peek } from '../store.js';
import { t } from '../i18n.js';
import { esc, icon, empty, sectionHead } from '../ui.js';
import { checkinRow, ruleLabel, seriesRow, seriesMenu, createCheckinSheet, isMedication } from '../checkins.js';

// «Регулярное»: today's check-ins with their outcome, every check-in with its last
// days, and recurring reminders (attention only, no outcome to record).
export default {
  id: 'checkins',
  tab: 'more',
  detail: true,
  title: () => t('nav.checkins'),
  load: ({ fresh }) => load('/api/v1/checkins', { fresh }),
  render(data) {
    const checkins = data?.checkins || [];
    const series = data?.reminder_series || [];
    const active = checkins.filter((c) => c.status === 'ACTIVE');
    const ended = checkins.filter((c) => c.status !== 'ACTIVE');
    const today = active.flatMap((c) => c.today || []).sort((a, b) => String(a.scheduled_at).localeCompare(String(b.scheduled_at)));
    const add = `<button class="button small" data-action="checkin-new">${icon('plus')}${esc(t('checkin.new'))}</button>`;
    if (!checkins.length && !series.length) {
      return `<section class="section">${empty(t('checkin.empty'), t('checkin.emptyHint'), 'repeat')}
        <div class="button-row pad">${add}</div></section>`;
    }
    const card = (c) => {
      const days = (c.recent_days || []).slice(0, 3);
      const history = days.map((d) => `${d.local_date.slice(8, 10)}.${d.local_date.slice(5, 7)}: ${d.done}/${d.total}`).join(' · ');
      return `<button class="row" data-action="open-checkin" data-id="${esc(c.id)}">
        <span class="row-icon tone-accent">${isMedication(c) ? '💊' : icon(c.checkin_kind === 'QUOTA' ? 'tasks' : 'repeat')}</span>
        <span class="row-main"><strong>${esc(c.title)}</strong><small>${esc(ruleLabel(c.recurrence_rule))} · ${esc(String(c.dtstart_local || '').slice(11, 16))}${c.dose_text ? ` · ${esc(c.dose_text)}` : ''}${history ? ` · ${esc(history)}` : ''}${c._pending ? ` · ${esc(t('checkin.pendingSync'))}` : ''}</small></span>
        ${icon('chevron')}
      </button>`;
    };
    return `
      <section class="section">${sectionHead(t('checkin.today'), add)}
        ${today.length ? `<div class="list">${today.map((o) => checkinRow(o)).join('')}</div>` : `<p class="muted pad">${esc(t('checkin.nothingToday'))}</p>`}
      </section>
      ${active.length ? `<section class="section">${sectionHead(t('checkin.tracked'))}<div class="list">${active.map(card).join('')}</div></section>` : ''}
      ${series.length ? `<section class="section">${sectionHead(t('checkin.reminders'))}<p class="help pad">${esc(t('checkin.remindersHelp'))}</p>
        <div class="list">${series.map(seriesRow).join('')}</div></section>` : ''}
      ${ended.length ? `<details class="section"><summary>${esc(t('checkin.endedList', { n: ended.length }))}</summary><div class="list">${ended.map(card).join('')}</div></details>` : ''}`;
  },
  actions: {
    'series-menu'(el) {
      const series = (peek('/api/v1/checkins')?.reminder_series || []).find((s) => s.id === el.dataset.id);
      if (series) seriesMenu(series);
    },
    'checkin-new'() { createCheckinSheet(); },
  },
};
