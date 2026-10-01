// Which interpreter read the user's words (language model, local parser, offline),
// and whether the account has a live model at all.
import { peek, load } from './store.js';
import { t } from './i18n.js';

export async function capabilities() {
  const cached = peek('/api/v1/ask/capabilities');
  if (cached) return cached;
  try {
    return (await load('/api/v1/ask/capabilities')).data;
  } catch { return null; }
}

// Which interpreter read the text is always on screen: a language model, or the
// on-device parser — and if the model was meant to help but did not, why.
export function engineLine(state, { model = null, reason = null } = {}) {
  if (state === 'thinking') return { tone: 'muted', icon: 'spark', text: t('capture.engine.thinking') };
  if (state === 'ai') return { tone: 'accent', icon: 'spark', text: model ? t('capture.engine.aiModel', { model }) : t('capture.engine.ai') };
  if (state === 'fallback') {
    const key = `capture.engine.why.${reason}`;
    const why = t(key) === key ? t('capture.engine.why.other') : t(key);
    return { tone: 'warn', icon: 'alert', text: t('capture.engine.fallback', { why }) };
  }
  if (state === 'offline') return { tone: 'muted', icon: 'task', text: t('capture.engine.offline') };
  if (state === 'noai') return { tone: 'muted', icon: 'task', text: t('capture.engine.noAi') };
  return { tone: 'muted', icon: 'task', text: t('capture.engine.local') };
}
