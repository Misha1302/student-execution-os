// One duration picker for every "how long" question: task effort, remaining time,
// chunk sizes, time spent, routine and class length. Presets, "Other…" with hours
// and minutes (1 h 35 min, not "95 minutes"), and "Don't know" where unknown is a
// real answer. Values are whole minutes; null means unknown or not chosen.
import { t, fmtDuration } from './i18n.js';
import { esc, chipGroup, chipValue, toast } from './ui.js';

export const DURATION_PRESETS = [15, 30, 45, 60, 90, 120, 180];

const OTHER = 'other';
const UNKNOWN = 'unknown';

function optionValues(root, name) {
  return [...(root.querySelector(`[data-chip-group="${name}"]`)?.querySelectorAll('.chip-toggle') || [])].map((b) => b.dataset.value);
}

// presets: minute values offered as chips; labels: per-value label overrides
// ({0: 'Done'}); extra: [value, label] pairs shown first (e.g. ['', 'No time']).
export function durationPicker(name, minutes, { presets = DURATION_PRESETS, labels = {}, extra = [], unknown = false } = {}) {
  const options = [...extra, ...presets.map((m) => [String(m), labels[m] ?? fmtDuration(m)]), [OTHER, t('duration.other')]];
  if (unknown) options.push([UNKNOWN, t('duration.unknown')]);
  const values = options.map(([v]) => String(v));
  const selected = minutes == null || minutes === '' ? (unknown ? UNKNOWN : '') : values.includes(String(minutes)) ? String(minutes) : OTHER;
  const custom = selected === OTHER ? Number(minutes) : null;
  return `<div class="duration-picker" data-duration="${esc(name)}">
    ${chipGroup(name, options, selected)}
    <div class="duration-custom" data-duration-custom ${selected === OTHER ? '' : 'hidden'}>
      <label><input type="number" inputmode="numeric" min="0" max="999" step="1" data-duration-h value="${custom != null ? Math.floor(custom / 60) : ''}" aria-label="${esc(t('duration.hours'))}"><span>${esc(t('duration.h'))}</span></label>
      <label><input type="number" inputmode="numeric" min="0" max="59" step="5" data-duration-m value="${custom != null ? custom % 60 : ''}" aria-label="${esc(t('duration.minutes'))}"><span>${esc(t('duration.m'))}</span></label>
    </div>
  </div>`;
}

// Minutes, or null for unknown / nothing chosen / an extra with an empty value.
// "Other" with nothing typed throws a user-facing error.
export function readDuration(root, name) {
  const choice = chipValue(root, name);
  if (choice == null || choice === '' || choice === UNKNOWN) return null;
  if (choice !== OTHER) return Number(choice);
  const box = root.querySelector(`[data-duration="${name}"] [data-duration-custom]`);
  const hours = Math.max(0, Math.floor(Number(box.querySelector('[data-duration-h]').value) || 0));
  const mins = Math.max(0, Math.floor(Number(box.querySelector('[data-duration-m]').value) || 0));
  const total = hours * 60 + mins;
  if (!(total > 0)) throw new Error(t('duration.required'));
  return total;
}

// For a save button: minutes, or null after telling the user what is missing.
export function takeDuration(root, name) {
  try { return readDuration(root, name); } catch (err) { toast(err.message, { error: true }); return null; }
}

export function writeDuration(root, name, minutes, { unknown = false } = {}) {
  const picker = root.querySelector(`[data-duration="${name}"]`);
  if (!picker) return;
  // The user's own "1 h 0 min" stays as typed rather than snapping to the 1 h chip.
  if (chipValue(picker, name) === OTHER && minutes != null) {
    try { if (readDuration(picker, name) === Number(minutes)) return; } catch { /* empty: overwrite below */ }
  }
  const values = optionValues(root, name);
  const selected = minutes == null ? (unknown ? UNKNOWN : '') : values.includes(String(minutes)) ? String(minutes) : OTHER;
  picker.querySelectorAll('.chip-toggle').forEach((b) => {
    const on = b.dataset.value === selected;
    b.classList.toggle('on', on);
    b.setAttribute('aria-checked', String(on));
  });
  const box = picker.querySelector('[data-duration-custom]');
  box.hidden = selected !== OTHER;
  if (selected === OTHER && !box.contains(document.activeElement)) {
    box.querySelector('[data-duration-h]').value = Math.floor(minutes / 60);
    box.querySelector('[data-duration-m]').value = minutes % 60;
  }
}

// Opens "Other…" and puts the cursor in the hours field.
export function focusDurationOther(root, name) {
  const picker = root.querySelector(`[data-duration="${name}"]`);
  const other = picker?.querySelector(`.chip-toggle[data-value="${OTHER}"]`);
  if (!other) return;
  if (!other.classList.contains('on')) other.click();
  picker.querySelector('[data-duration-h]')?.focus();
}

document.addEventListener('chipchange', (e) => {
  const picker = e.target.closest?.('[data-duration]');
  if (!picker || picker.dataset.duration !== e.detail.name) return;
  const box = picker.querySelector('[data-duration-custom]');
  box.hidden = e.detail.value !== OTHER;
  if (!box.hidden) box.querySelector('[data-duration-h]').focus();
});
