import { load } from '../store.js';
import { t, fmtDay } from '../i18n.js';
import { esc, icon, chip, kv, empty, sectionHead } from '../ui.js';
import { checkinRow, ruleLabel, isMedication, isQuota, editCheckinSheet, endCheckin, deleteCheckin, changeTimeSheet, fmtPace } from '../checkins.js';

// One check-in: what it is, and an honest day-by-day history of what happened.
export default {
  id: 'checkin',
  tab: 'more',
  detail: true,
  title: () => t('checkin.detailTitle'),
  load: ({ params, fresh }) => load(`/api/v1/checkins/${encodeURIComponent(params[0])}`, { fresh }),
  render(data) {
    if (!data || data.deleted) return empty(t('checkin.gone'), '', 'x');
    const history = data.history || [];
    const days = history.map((day) => `<div class="history-day">
      <div class="section-head"><h3>${esc(fmtDay(`${day.local_date}T12:00:00`))}</h3>${chip(`${day.done} / ${day.total}`, day.total && day.done === day.total ? 'ok' : day.open ? 'accent' : 'muted')}</div>
      <div class="list">${day.occurrences.map((o) => checkinRow(o)).join('')}</div></div>`).join('');
    const nextOpen = history.flatMap((d) => d.occurrences).filter((o) => o.status === 'PENDING')
      .sort((a, b) => String(a.original_recurrence_id).localeCompare(String(b.original_recurrence_id)))[0];
    return `
      <section class="section card">
        <h2>${isMedication(data) ? '💊 ' : ''}${esc(data.title)}</h2>
        <dl class="kv-list">
          ${kv(t('checkin.what'), t(`checkin.kind.${data.checkin_kind}`))}
          ${data.dose_text ? kv(t('checkin.dose'), data.dose_text) : ''}
          ${isQuota(data) ? kv(t('checkin.target'), `${data.target_quantity} ${data.unit || ''}`) : ''}
          ${isQuota(data) ? kv(t('checkin.pace'), data.unit_effort_seconds ? t('checkin.pacePerUnit', { d: fmtPace(data.unit_effort_seconds) }) : t('checkin.paceUnknown')) : ''}
          ${kv(t('routine.repeat'), `${ruleLabel(data.recurrence_rule)} · ${String(data.dtstart_local || '').slice(11, 16)}`)}
          ${kv(t('checkin.remindLabel'), data.remind ? t(data.delivery === 'PUSH' ? 'checkin.remind.push' : 'checkin.remind.alarm') : t('checkin.remind.none'))}
          ${data.status !== 'ACTIVE' ? kv(t('checkin.state'), t('checkin.ended')) : ''}
        </dl>
        ${isMedication(data) ? `<p class="help">${esc(t('checkin.medicationNotice'))}</p>` : ''}
        <div class="button-row">
          ${data.status === 'ACTIVE' ? `<button class="button small" data-action="checkin-edit">${esc(t('checkin.edit'))}</button>` : ''}
          ${data.status === 'ACTIVE' && nextOpen ? `<button class="button small ghost" data-action="checkin-change-time" data-rid="${esc(nextOpen.original_recurrence_id)}">${esc(t('checkin.changeTime'))}</button>` : ''}
          ${data.status === 'ACTIVE' ? `<button class="button small ghost" data-action="checkin-end">${esc(t('checkin.end'))}</button>` : ''}
          <button class="button small ghost danger" data-action="checkin-delete">${icon('x')}${esc(t('lifecycle.delete'))}</button>
        </div>
      </section>
      <section class="section" data-history>${sectionHead(t('checkin.history'), `<span class="muted">${esc(t('checkin.historyWindow', { n: data.history_days || 30 }))}</span>`)}
        ${days || `<p class="muted pad">${esc(t('checkin.noHistory'))}</p>`}
      </section>`;
  },
  actions: {
    'checkin-edit'(_el, ctx) { editCheckinSheet(ctx.data); },
    'checkin-end'(_el, ctx) { return endCheckin(ctx.data); },
    'checkin-delete'(_el, ctx) { return deleteCheckin(ctx.data); },
    'checkin-change-time'(el, ctx) { changeTimeSheet(ctx.data, el.dataset.rid); },
  },
};
