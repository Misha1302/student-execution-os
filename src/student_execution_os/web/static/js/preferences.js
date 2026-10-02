// Soft planning preferences (canonical, schema v30): how they read in the UI.
// The planner reports each one as APPLIED / RELAXED / UNSATISFIABLE in plan.preference_status.
import { t, fmtDuration, fmtDay } from './i18n.js';

function days(p) {
  if (!p.date_until) return p.date_from ? t('pref.days.from', { from: fmtDay(`${p.date_from}T12:00:00`) }) : '';
  if (p.date_until === p.date_from) return fmtDay(`${p.date_from}T12:00:00`);
  return t('pref.days.range', { from: fmtDay(`${p.date_from}T12:00:00`), until: fmtDay(`${p.date_until}T12:00:00`) });
}

export function describePreference(p) {
  const d = fmtDuration(p.minutes || 0);
  let text;
  switch (p.kind) {
    case 'KEEP_FREE': text = t('pref.keepFree', { d, from: p.window_start, to: p.window_end }); break;
    case 'WORK_LIMIT': text = t('pref.workLimit', { d }); break;
    case 'REST_AFTER_EVENTS': text = t(`pref.rest.${p.target || 'CLASSES'}`, { d }); break;
    case 'AVOID_WORK': {
      const what = t(`pref.target.${p.target || 'ALL'}`);
      text = p.anchor === 'WAKE' ? t('pref.avoidWake', { what, d })
        : p.window_end ? t('pref.avoidRange', { what, from: p.window_start, to: p.window_end })
          : t('pref.avoidAfter', { what, from: p.window_start });
      break;
    }
    default: text = p.kind;
  }
  const when = days(p);
  return when ? `${text} · ${when}` : text;
}

export function preferenceStatus(plan, id) {
  const status = plan?.preference_status?.[id];
  return status ? t(`pref.status.${status}`) : '';
}

export function deletePreferenceOperation(preference) {
  return { type: 'preference.delete', entity_id: preference.id, payload: { expected_version: preference.version } };
}
