// One capture flow for typed text, dictated speech and manual details:
//
//   "+" → "Что нужно сделать?" → text or voice → task card → Создать
//
// The on-device parser (nlparse.js) fills the card instantly and offline. When the
// server has a language model configured, its validated proposal refines the card
// (fields the user already set stay as the user set them). Creating always goes
// through the offline operation queue (task.create), so a simple task is saved
// without network or model and replayed exactly once after reconnect.
import { api, session } from './api.js';
import { CaptureSession, draftScope, readCaptureDraft, writeCaptureDraft, reconcileCaptureCandidates } from './capture-session.js';
import { completeTutorial } from './onboarding.js';
export { reconcileCaptureCandidates } from './capture-session.js';
import { t, fmtDuration, fmtDateTime, now, getLocale } from './i18n.js';
import { esc, icon, openSheet, chipGroup, localInputValue, isoFromLocalInput, toast, errorMessage, focusSoon } from './ui.js';
import { change, shell } from './actions.js';
import { newEntityId, settled } from './sync.js';
import { parseTask, correctedText, reminderTurn } from './nlparse.js';
import { startDictation, voiceSupported } from './native.js';
import { reachWarning } from './health.js';
import { parseCommand } from './commands.js';
import { parseRecurring, recurringActions, parseCheckinOutcome } from './recurring.js';
import { renderCommands, knownItems, isAssistantPlan } from './command-preview.js';
import { assistantSession, renderRead, validUntil } from './assistant-turn.js';
import { createReminder, deliveryChips, hasAlarm } from './reminders.js';
import { focusDurationOther } from './duration.js';
import { eventFieldsHtml, readEventFields, bindEventFields, eventWhen, conflictHtml, createEvent, DEFAULT_LEAD, leadPicker, readLead } from './events.js';
import { blankTaskDraft, KINDS, taskFromEvent, eventFromTask, mergeReminderDraft, localCandidate, modelCandidate } from './capture-candidates.js';
import { fieldsHtml, bindFields, FIELDS, setChip, factsHtml, questionsHtml, writeFields, answer, readFields, createPayload } from './task-draft.js';
import { engineLine, capabilities } from './capture-engine.js';

export const deviceTimeZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
const AI_ENRICH_DEBOUNCE_MS = 800;

// ---- the sheet ----------------------------------------------------------------------

export function openCapture({ text = '', listen: listenNow = false, sourceNoteId = null, initialKind = null, guided = false } = {}) {
  const scope = draftScope(session, window.location?.origin || '');
  const recovered = !text && !sourceNoteId && !initialKind ? readCaptureDraft(localStorage, scope) : null;
  const captureSession = new CaptureSession(recovered || {});
  if (recovered) text = recovered.raw || '';
  let draft = blankTaskDraft();
  let unresolved = [];
  const answered = new Set();
  let assistant = null; // { batch_id, action_id } when the model's proposal is on the card
  let commands = null;  // a proposal about existing items ("готово …"), local or from the server
  let serverSeq = 0;
  let serverTimer = null;
  let parseTimer = null;
  let kind = initialKind || recovered?.userKind || 'TASK'; // TASK | EVENT | NOTE | REMINDER
  let kindChosen = Boolean(initialKind || recovered?.userKind);
  captureSession.userKind = kindChosen ? kind : null;
  let eventDraft = null;
  let reminderDraft = null;   // { title, remind_at, delivery, wake_check, raise_volume, note }
  let reminderFloor = null;   // explicit alarm/wake semantics from the local parser
  const eventEdited = new Set(); // event fields the user set by hand
  let localSemantic = null;
  let modelSemantic = null;
  let fieldProvenance = {};
  let semanticConflicts = [];
  let dictation = null;
  let voiceGeneration = 0;
  let saveTimer = null;
  let closed = false;
  let saving = false;
  const persist = () => {
    if (scope !== draftScope(session, window.location?.origin || '')) return;
    writeCaptureDraft(localStorage, scope, captureSession.snapshot(input.value));
  };
  const scheduleSave = () => { clearTimeout(saveTimer); saveTimer = setTimeout(persist, 250); };

  const dialog = openSheet({
    title: t(guided ? 'tutorial.title' : 'capture.title'),
    full: true,
    body: `<div class="capture">
      ${guided ? `<div data-guidance><p>${esc(t('tutorial.captureCoach'))}</p><p class="help">${esc(t('tutorial.captureExample'))}</p><button type="button" class="link" data-tutorial-skip>${esc(t('tutorial.skip'))}</button></div>` : ''}
      <div class="capture-input">
        <textarea id="capture-text" rows="3" maxlength="4000" enterkeyhint="enter" aria-label="${esc(t('capture.title'))}" placeholder="${esc(t('capture.placeholder'))}">${esc(text)}</textarea>
        ${voiceSupported() ? `<button type="button" class="icon-button mic" data-mic aria-pressed="false" aria-label="${esc(t('capture.voice'))}">${icon('mic')}</button>` : ''}
      </div>
      <div class="voice-panel" data-voice role="status" aria-live="polite" hidden>
        <span class="voice-dot" aria-hidden="true"></span>
        <span class="voice-copy"><strong data-voice-state></strong><small data-voice-text></small></span>
        <button type="button" class="button small" data-voice-stop>${esc(t('voice.stop'))}</button>
        <button type="button" class="button small ghost" data-voice-cancel>${esc(t('common.cancel'))}</button>
      </div>
      <p class="help" data-capture-hint>${esc(t('capture.hint'))}</p>
      <p class="help follow-up" data-follow-up hidden>${esc(t('assistant.followingUp'))}
        <button type="button" class="link" data-follow-up-clear>${esc(t('assistant.newTopic'))}</button></p>
      <details class="kind-switch" data-kind-switch><summary data-kind-label>${esc(t(`capture.kind.${kind}`))} · ${esc(t('capture.changeKind'))}</summary>${chipGroup('capture-kind', KINDS.map((k) => [k, t(`capture.kind.${k}`)]), kind)}</details>
      <details class="help"><summary>${esc(t('capture.examples'))}</summary><p>${esc(t('capture.commandExamples'))}</p></details>
      <div data-capture-status class="capture-status" hidden></div>
      <div data-preview class="capture-preview" hidden></div>
      <div data-clarification aria-live="polite"></div>
      <div data-commands hidden></div>
      <details class="details" data-more>
        <summary>${icon('settings')} ${esc(t('capture.more'))}</summary>
        <p class="engine-line" data-engine hidden></p>
        <div data-task-details>${fieldsHtml(draft)}</div>
        <div data-event-details hidden></div>
      </details>
      <div class="capture-other">
        <button type="button" class="link" data-discard>${esc(t('capture.discard'))}</button>
        <button type="button" class="link" data-other="note-audio">${icon('mic')} ${esc(t('note.recordAudio'))}</button>
        <button type="button" class="link" data-other="recurring">${icon('repeat')} ${esc(t('capture.recurring'))}</button>
      </div>
    </div>`,
    actions: `<button value="cancel" class="button ghost">${esc(t('common.cancel'))}</button>
      <button type="button" class="button primary" data-create disabled>${esc(t('compose.create'))}</button>`,
  });
  const input = dialog.querySelector('#capture-text');
  const kindSwitch = dialog.querySelector('[data-kind-switch]');
  const preview = dialog.querySelector('[data-preview]');
  const details = dialog.querySelector('[data-more]');
  const createButton = dialog.querySelector('[data-create]');
  const status = dialog.querySelector('[data-capture-status]');
  const taskDetails = details.querySelector('[data-task-details]');
  const eventDetails = details.querySelector('[data-event-details]');
  bindFields(taskDetails);
  const growInput = () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 192)}px`;
    input.style.overflowY = input.scrollHeight > 192 ? 'auto' : 'hidden';
  };
  growInput();

  const showStatus = (message) => { status.hidden = !message; status.textContent = message || ''; };
  // «Нет, лучше в 10:30» follows up on the proposal the user applied or chose to refine.
  const followUp = dialog.querySelector('[data-follow-up]');
  const showFollowUp = () => { followUp.hidden = !assistantSession.active; };
  followUp.querySelector('[data-follow-up-clear]').addEventListener('click', () => { assistantSession.forget(); showFollowUp(); });
  showFollowUp();
  const commandBox = dialog.querySelector('[data-commands]');
  const refine = (state) => {
    // Keep this proposal as the context of the next sentence instead of applying it.
    assistantSession.commit(state.batchId, state.validUntil);
    commands = null;
    commandBox.hidden = true;
    input.value = '';
    input.dispatchEvent(new Event('input'));
    showFollowUp();
    focusSoon(input);
  };
  const engineEl = dialog.querySelector('[data-engine]');
  const showEngine = (state, extra) => {
    const line = engineLine(state, extra);
    engineEl.hidden = !String(input.value).trim();
    engineEl.dataset.engine = state;
    engineEl.className = `engine-line tone-${line.tone}`;
    engineEl.innerHTML = `${icon(line.icon)}<span>${esc(line.text)}</span>`;
  };

  function merge(fields, source) {
    for (const key of FIELDS) {
      if (answered.has(key) || !(key in fields)) continue;
      draft[key] = fields[key];
    }
    if (source === 'local' && !String(input.value).trim()) draft.title = '';
  }

  function resetParsedState() {
    const previousTask = draft;
    draft = blankTaskDraft();
    for (const key of answered) if (key in previousTask) draft[key] = previousTask[key];
    if (eventDraft) {
      const previousEvent = eventDraft;
      eventDraft = eventEdited.size ? { attendance_policy: 'REQUIRED', remind_before_minutes: DEFAULT_LEAD } : null;
      for (const key of eventEdited) if (key in previousEvent) eventDraft[key] = previousEvent[key];
    }
    if (reminderDraft) {
      const old = reminderDraft;
      const keep = old.whenChosen || old.deliveryChosen || old.wakeChosen;
      reminderDraft = keep ? {
        ...(old.whenChosen ? { remind_at: old.remind_at, whenChosen: true } : {}),
        ...(old.deliveryChosen ? { delivery: old.delivery, deliveryChosen: true } : {}),
        ...(old.wakeChosen ? { wake_check: old.wake_check, wakeChosen: true } : {}),
        title: '',
      } : null;
    }
  }

  // A parse (local or the model's) that found a fixed-time event.
  function adoptEvent(parsed) {
    const next = { ...(eventDraft || { attendance_policy: 'REQUIRED', remind_before_minutes: DEFAULT_LEAD }) };
    for (const key of ['title', 'description', 'category', 'importance', 'starts_at', 'ends_at',
      'attendance_policy', 'location_effect', 'arrival_requirement_minutes', 'remind_before_minutes']) {
      if (!eventEdited.has(key) && parsed[key] !== undefined) next[key] = parsed[key];
    }
    eventDraft = next;
    if (!kindChosen) kind = 'EVENT';
    merge(taskFromEvent(eventDraft), 'event');
  }

  function ensureEventDetails() {
    if (eventDetails.dataset.ready) return;
    eventDetails.dataset.ready = '1';
    eventDetails.innerHTML = eventFieldsHtml(eventDraft);
    bindEventFields(eventDetails, { onChange: fromEventDetails });
    eventDetails.querySelector('[data-e="title"]').addEventListener('input', fromEventDetails);
  }

  function writeEventFields() {
    if (!eventDetails.dataset.ready) return;
    const active = document.activeElement;
    const set = (name, value) => { const el = eventDetails.querySelector(`[data-e="${name}"]`); if (el && el !== active) el.value = value; };
    set('title', eventDraft.title || '');
    set('start', localInputValue(eventDraft.starts_at));
    set('end', localInputValue(eventDraft.ends_at));
    set('category', eventDraft.category || 'GENERAL');
    const lead = eventDraft.remind_before_minutes == null ? '' : String(eventDraft.remind_before_minutes);
    const preset = eventDetails.querySelector(`[data-chip-group="e-lead"] [data-value="${lead}"]`);
    setChip(eventDetails, 'e-lead', preset ? lead : 'other');
    const customLead = eventDetails.querySelector('[data-e-lead-custom]');
    if (customLead && !preset) customLead.value = lead;
    customLead?.closest('.event-lead-custom')?.classList.toggle('hidden', Boolean(preset));
  }

  function fromEventDetails() {
    let fields;
    try { fields = readEventFields(eventDetails); } catch { return; }
    for (const [key, value] of Object.entries(fields)) {
      if (JSON.stringify(value ?? null) !== JSON.stringify(eventDraft?.[key] ?? null)) {
        eventEdited.add(key);
        fieldProvenance[key] = 'USER_EDIT';
      }
    }
    eventDraft = { ...eventDraft, ...fields };
    captureSession.edit('EVENT', Object.fromEntries([...eventEdited].map((key) => [key, eventDraft[key]])));
    render();
  }

  function eventCardHtml() {
    const lead = eventDraft.remind_before_minutes == null ? '' : String(eventDraft.remind_before_minutes);
    return `<article class="capture-card event-card" data-kind="EVENT">
      <span class="eyebrow">${icon('event')} ${esc(t('capture.kindEvent'))}</span>
      <h3 class="capture-title"><button type="button" class="link" data-fact="title">${esc(eventDraft.title || '')} ${icon('edit')}</button></h3>
      <div class="capture-facts">
        <button type="button" class="fact" data-fact="event-time"><span class="fact-icon tone-accent">${icon('calendar')}</span>
          <span class="fact-copy"><small>${esc(t('card.when'))}</small><strong data-event-when>${esc(eventWhen(eventDraft))}</strong></span></button>
      </div>
      <details data-reminder-editor ${preview.querySelector('[data-reminder-editor]')?.open ? 'open' : ''}><summary>${icon('bell')} ${esc(eventDraft.remind_before_minutes == null ? t('event.lead.none') : t('capture.reminderBefore', { minutes: lead }))}</summary>
        <div class="field">${leadPicker('card-lead', lead, 'data-card-lead-custom')}</div></details>
      ${conflictHtml(eventDraft)}
      ${eventDraft.remind_before_minutes != null ? reachWarning() : ''}
      <button type="button" class="link" data-switch-kind="TASK">${esc(t('capture.asTask'))}</button>
    </article>`;
  }

  function reminderCardHtml() {
    const r = reminderDraft;
    return `<article class="capture-card reminder-card" data-kind="REMINDER">
      <span class="eyebrow">${icon('bell')} ${esc(t(hasAlarm(r.delivery) ? 'reminder.kindAlarm' : 'reminder.kind'))}</span>
      <h3 class="capture-title">${esc(r.title || '')}</h3>
      <label class="field"><span>${icon('clock')} ${esc(t('reminder.when'))}</span>
        <input type="datetime-local" data-card-remind value="${esc(r.remind_at ? localInputValue(r.remind_at) : '')}"></label>
      <div class="field"><span>${esc(t('reminder.how'))}</span>${deliveryChips('card-delivery', r.delivery)}</div>
      ${hasAlarm(r.delivery) ? `<div class="field"><span>${esc(t('reminder.wake'))}</span>${chipGroup('card-wake', [['false', t('reminder.wake.no')], ['true', t('reminder.wake.yes')]], String(Boolean(r.wake_check)))}</div>` : ''}
      ${reachWarning({ alarm: hasAlarm(r.delivery) })}
      <button type="button" class="link" data-switch-kind="TASK">${esc(t('capture.reminderAsTask'))}</button>
    </article>`;
  }

  function render() {
    scheduleSave();
    // A command about existing items replaces the creation form entirely.
    const commandMode = Boolean(commands);
    createButton.hidden = commandMode;
    kindSwitch.hidden = commandMode;
    setChip(kindSwitch, 'capture-kind', kind);
    kindSwitch.querySelector('[data-kind-label]').textContent = `${t(`capture.kind.${kind}`)} · ${t('capture.changeKind')}`;
    details.hidden = commandMode || kind === 'NOTE';
    dialog.querySelector('.capture-other').hidden = commandMode;
    if (kind === 'NOTE') {
      const noteText = String(input.value || '').trim();
      createButton.disabled = !noteText || Boolean(commands);
      preview.hidden = !noteText || Boolean(commands);
      dialog.querySelector('[data-capture-hint]').hidden = Boolean(noteText) || Boolean(commands);
      taskDetails.hidden = true;
      eventDetails.hidden = true;
      if (noteText && !commands) preview.innerHTML = `<article class="capture-card note-card" data-kind="NOTE">
        <span class="eyebrow">${icon('note')} ${esc(t('capture.kindNote'))}</span>
        <p class="capture-note-text">${esc(noteText)}</p>
        <div class="button-row"><button type="button" class="link" data-switch-kind="TASK">${esc(t('capture.asTask'))}</button>
        <button type="button" class="link" data-switch-kind="EVENT">${esc(t('capture.asEvent'))}</button></div>
      </article>`;
      return;
    }
    if (kind === 'REMINDER' && reminderDraft) {
      const hasTitle = Boolean(String(reminderDraft.title || '').trim());
      createButton.disabled = !hasTitle || !reminderDraft.remind_at || Boolean(commands);
      preview.hidden = !hasTitle || Boolean(commands);
      dialog.querySelector('[data-capture-hint]').hidden = hasTitle || Boolean(commands);
      taskDetails.hidden = true;
      eventDetails.hidden = true;
      if (hasTitle && !commands) preview.innerHTML = reminderCardHtml();
      return;
    }
    const isEvent = kind === 'EVENT' && eventDraft;
    const hasTitle = Boolean(String((isEvent ? eventDraft.title : draft.title) || '').trim());
    createButton.disabled = !hasTitle || Boolean(commands);
    preview.hidden = !hasTitle || Boolean(commands);
    dialog.querySelector('[data-capture-hint]').hidden = hasTitle || Boolean(commands);
    taskDetails.hidden = Boolean(isEvent);
    eventDetails.hidden = !isEvent;
    if (isEvent) {
      ensureEventDetails();
      writeEventFields();
      if (hasTitle && !commands) preview.innerHTML = eventCardHtml();
      return;
    }
    if (hasTitle && !commands) {
      const open = unresolved.filter((f) => !answered.has(f));
      preview.innerHTML = `<article class="capture-card">
        <h3 class="capture-title"><button type="button" class="link" data-fact="title">${esc(draft.title)} ${icon('edit')}</button></h3>
        ${factsHtml(draft)}
        ${draft.description ? `<p class="muted">${esc(draft.description)}</p>` : ''}
        ${questionsHtml(open.filter((field) => !['estimated_total_effort_minutes', 'actual_cutoff'].includes(field)))}
        ${draft.remind_at ? reachWarning() : ''}
        <button type="button" class="link" data-switch-kind="EVENT">${esc(t('capture.asEvent'))}</button>
        ${draft.remind_at ? `<button type="button" class="link" data-switch-kind="REMINDER">${esc(t('capture.asReminder'))}</button>` : ''}
      </article>`;
    }
    writeFields(taskDetails, draft);
  }

  // The time someone wrote ("завтра в 10:00 …") is read as a task's target, start or
  // deadline; once they say it is a reminder, that same time is when to remind.
  function reminderTimeOf(parsed) {
    return parsed.remind_at ?? parsed.target_at ?? parsed.actionable_from
      ?? (parsed.actual_cutoff?.state === 'KNOWN' ? parsed.actual_cutoff.at : null) ?? null;
  }

  // An explicit choice: from here on parses fill the chosen kind and never switch it.
  function switchKind(next) {
    const compatibleEdits = Object.fromEntries(Object.entries(captureSession.edits[kind] || {}).filter(([field]) => ['title', 'description', 'category', 'importance'].includes(field)));
    captureSession.edit(next, compatibleEdits);
    captureSession.userKind = next;
    kindChosen = true;
    kind = next;
    dialog.dataset.kindProvenance = 'USER_EDIT';
    if (kind === 'EVENT' && !eventDraft) eventDraft = eventFromTask(draft);
    if (kind === 'REMINDER' && !reminderDraft) {
      reminderDraft = { title: draft.title, remind_at: reminderTimeOf(draft), delivery: 'PUSH', wake_check: false, raise_volume: true };
    }
    if (kind === 'TASK' && eventDraft) { merge(taskFromEvent(eventDraft), 'event'); unresolved = draft.estimated_total_effort_minutes == null ? ['estimated_total_effort_minutes'] : []; }
    render();
  }

  function adoptReminder(parsed, source = 'assistant') {
    if (source === 'local') {
      reminderFloor = hasAlarm(parsed.delivery) ? { delivery: parsed.delivery, wake_check: parsed.wake_check === true } : null;
    }
    reminderDraft = mergeReminderDraft(reminderDraft, parsed, reminderFloor);
    if (!kindChosen) kind = 'REMINDER';
    // The same moment as a task's reminder, if the user says "это задача".
    merge({ title: parsed.title, remind_at: parsed.remind_at, actual_cutoff: { state: 'ABSENT' } }, 'reminder');
  }

  function applySemantic(result, source, selectedUnresolved = []) {
    fieldProvenance = { ...result.provenance };
    semanticConflicts = result.conflicts;
    const clarification = dialog.querySelector('[data-clarification]');
    clarification.innerHTML = semanticConflicts.length ? `<p>${esc(t('capture.clarify'))}</p><div class="button-row">${[localSemantic, modelSemantic].map((candidate, index) => {
      const value = candidate?.payload || {};
      const when = value.starts_at || value.remind_at || value.actual_cutoff?.at || value.actionable_from || value.target_at;
      const label = `${t(`capture.kind.${candidate.kind}`)} · ${when ? fmtDateTime(when) : value.title || value.content || ''}${value.duration_minutes ? ` · ${fmtDuration(value.duration_minutes)}` : ''}${value.remind_before_minutes != null ? ` · ${t('capture.reminderBefore', { minutes: value.remind_before_minutes })}` : ''}`;
      return `<button type="button" class="button" data-meaning="${index}">${esc(label)}</button>`;
    }).join('')}</div>` : '';
    dialog.dataset.fieldProvenance = JSON.stringify(fieldProvenance);
    if (!kindChosen) kind = result.kind;
    unresolved = selectedUnresolved;
    if (result.kind === 'EVENT') {
      unresolved = [];
      adoptEvent(result.payload);
    } else if (result.kind === 'REMINDER') {
      unresolved = [];
      adoptReminder(result.payload, source);
    } else if (result.kind === 'TASK') {
      merge(result.payload, source);
    }
    // A manual kind selection is provenance too. Convert only after the selected
    // semantic draft has been rebuilt, so stale fields from the previous text cannot leak.
    if (kindChosen && kind === 'EVENT' && !eventDraft) eventDraft = eventFromTask(draft);
    if (kindChosen && kind === 'REMINDER' && !reminderDraft) {
      reminderDraft = { title: draft.title, remind_at: reminderTimeOf(draft), delivery: 'PUSH', wake_check: false, raise_volume: true };
    }
    if (kindChosen && kind === 'REMINDER' && result.kind === 'TASK' && reminderDraft) {
      reminderDraft = mergeReminderDraft(reminderDraft, { title: draft.title,
        remind_at: reminderTimeOf(draft) }, reminderFloor);
    }
    render();
    if (semanticConflicts.length) createButton.disabled = true;
  }

  function showLocalCommand(action) { showLocalActions([action]); }

  function showLocalActions(actions) {
    commands = { source: 'local', actions };
    renderCommands(dialog.querySelector('[data-commands]'), commands, { onDone: () => dialog.close('applied') });
    showEngine('local');
    render();
  }

  function parseLocal() {
    const raw = input.value;
    commands = null;
    dialog.querySelector('[data-commands]').hidden = true;
    assistant = null;
    // "готово эссе", "перенеси созвон на 19:00": about something the user already has.
    const command = parseCommand(raw, now(), knownItems());
    if (command) { showLocalCommand(command); return; }
    // «Принял витамин»: an answer about a check-in the device knows.
    const outcome = parseCheckinOutcome(raw, knownItems().filter((x) => x.kind === 'CHECKIN'));
    if (outcome) { showLocalCommand(outcome); return; }
    // «Каждый день в 9 напоминай…»: something recurring is never a task card.
    const recurring = parseRecurring(raw, now());
    if (recurring) { showLocalActions(recurringActions(recurring, deviceTimeZone())); return; }
    const parsed = parseTask(raw, now());
    resetParsedState();
    showStatus('');
    reminderFloor = null;
    if (parsed.kind === 'REMINDER' && hasAlarm(parsed.delivery)) {
      reminderFloor = { delivery: parsed.delivery, wake_check: parsed.wake_check === true };
    }
    localSemantic = localCandidate(parsed, raw, captureSession.correctedKinds);
    modelSemantic = null;
    const reconciled = captureSession.interpret(localSemantic);
    applySemantic(reconciled, 'local', localSemantic.unresolved);
    showEngine('local');
  }

  async function enrich() {
    const raw = input.value.trim();
    const revision = captureSession.revision;
    const seq = ++serverSeq;
    const command = Boolean(parseCommand(raw, now(), knownItems()));
    // A recurring request or a check-in answer the device already read: a server answer
    // that is not a plan (e.g. a task card) must not replace that card.
    const localRecurring = Boolean(parseRecurring(raw, now())
      || parseCheckinOutcome(raw, knownItems().filter((x) => x.kind === 'CHECKIN')));
    if (!raw) return;
    const caps = await capabilities();
    if (closed || seq !== serverSeq || revision !== captureSession.revision || input.value.trim() !== raw) return;
    if (!command && !caps?.live_llm_provider) {
      // No model for this account (or no server answer): the device's parse stands.
      if (caps && caps.credential_status && caps.credential_status !== 'OK' && caps.credential_status !== 'UNTESTED') showEngine('fallback', { reason: caps.credential_status });
      else showEngine(caps ? 'noai' : 'offline');
      return;
    }
    showEngine('thinking');
    let result;
    const followUpContext = assistantSession.context();
    // Typed or dictated: the server records which, so a transcript slip is not taken as
    // something the user wrote (the latest correction still wins either way).
    const source = captureSession.turns.at(-1)?.source === 'voice' ? 'VOICE' : 'TEXT';
    try {
      result = await api('/api/v1/assistant/interpret', { method: 'POST', timeoutMs: 50000, body: {
        text: raw, context: { timezone: deviceTimeZone(), locale: getLocale(), source, ...followUpContext },
      } });
    } catch (err) {
      if (seq !== serverSeq) return;
      if (followUpContext.previous_batch_id && err.status === 403) {
        // The proposal it followed expired on the server: continue as a fresh request.
        assistantSession.forget();
        showFollowUp();
        enrich();
        return;
      }
      showEngine(err.code === 'NETWORK' ? 'offline' : 'fallback', { reason: err.code === 'NETWORK' ? null : 'SERVER' });
      if (command && err.code !== 'NETWORK') showStatus(errorMessage(err));
      return; // the local card stays; creating still works offline
    }
    if (closed || seq !== serverSeq || revision !== captureSession.revision || input.value.trim() !== raw || saving) return;
    showStatus('');
    if (result.engine === 'AI') showEngine('ai', { model: result.model });
    else if (result.fallback) showEngine('fallback', { reason: result.fallback_reason });
    else showEngine(caps?.live_llm_provider ? 'local' : 'noai');
    if (result.read) {
      // A question: show the server's facts; nothing is created or changed.
      commands = { source: 'server', read: result.read, actions: [] };
      renderRead(commandBox, result.read);
      render();
      return;
    }
    const actions = result.actions || [];
    if (isAssistantPlan(actions)) {
      commands = { source: 'server', batchId: result.batch_id, validUntil: validUntil(result), actions };
      // Only a language model can read «нет, лучше в 10:30» against the previous proposal.
      renderCommands(commandBox, commands, { onDone: () => dialog.close('applied'), onRefine: result.engine === 'AI' ? refine : null });
      render();
      return;
    }
    if (localRecurring) return;
    // The words asked for an alarm: a task or event reading of them is a downgrade
    // (an older server may still send one), so the local alarm card stays.
    if (reminderFloor && !kindChosen && actions[0]?.command !== 'CREATE_REMINDER') return;
    const create = actions.length === 1 ? actions[0] : null;
    modelSemantic = modelCandidate(create);
    if (!modelSemantic || !localSemantic) return;
    const reconciled = captureSession.interpret(modelSemantic, 'model', revision);
    if (!reconciled) return;
    const kindConflict = reconciled.conflicts.some((item) => item.field === 'kind');
    assistant = kindConflict ? null : { batch_id: result.batch_id, action_id: create.id };
    applySemantic(reconciled, 'assistant', kindConflict ? localSemantic.unresolved : modelSemantic.unresolved);
  }

  input.addEventListener('input', () => {
    const correctedKinds = new Set();
    correctedText(input.value, now(), correctedKinds);
    captureSession.input(input.value, 'keyboard', correctedKinds);
    scheduleSave();
    growInput();
    clearTimeout(parseTimer);
    clearTimeout(serverTimer);
    serverSeq += 1;
    parseTimer = setTimeout(() => { parseTimer = null; parseLocal(); }, 120);
    // The local parser already refreshes the card at 120 ms. Give keyboard input a
    // stable pause before spending a provider request; otherwise normal typing pauses
    // can send several LLM calls whose late results are discarded but still consume TPM.
    serverTimer = setTimeout(enrich, AI_ENRICH_DEBOUNCE_MS);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      if (!createButton.disabled) createButton.click();
    }
  });

  preview.addEventListener('change', (e) => {
    if (!e.target.matches('[data-card-remind]') || !reminderDraft) return;
    reminderDraft = { ...reminderDraft, remind_at: isoFromLocalInput(e.target.value), whenChosen: true };
    captureSession.edit('REMINDER', { remind_at: reminderDraft.remind_at });
    fieldProvenance.remind_at = 'USER_EDIT';
    render();
  });

  preview.addEventListener('input', (e) => {
    if (!e.target.matches('[data-card-lead-custom]') || !eventDraft) return;
    try {
      eventDraft.remind_before_minutes = readLead(preview, 'card-lead', '[data-card-lead-custom]');
      eventEdited.add('remind_before_minutes');
      fieldProvenance.remind_before_minutes = 'USER_EDIT';
      captureSession.edit('EVENT', { remind_before_minutes: eventDraft.remind_before_minutes });
      writeEventFields();
    } catch { /* keep editing until the value is valid */ }
  });

  preview.addEventListener('chipchange', (e) => {
    if (e.detail.name === 'card-delivery' && reminderDraft) {
      reminderDraft = { ...reminderDraft, delivery: e.detail.value, deliveryChosen: true };
      captureSession.edit('REMINDER', { delivery: reminderDraft.delivery });
      fieldProvenance.delivery = 'USER_EDIT';
      render();
      return;
    }
    if (e.detail.name === 'card-wake' && reminderDraft) {
      reminderDraft = { ...reminderDraft, wake_check: e.detail.value === 'true', wakeChosen: true };
      captureSession.edit('REMINDER', { wake_check: reminderDraft.wake_check });
      fieldProvenance.wake_check = 'USER_EDIT';
      return;
    }
    if (e.detail.name !== 'card-lead' || !eventDraft) return;
    preview.querySelector('[data-card-lead-custom]')?.closest('.event-lead-custom')?.classList.toggle('hidden', e.detail.value !== 'other');
    eventDraft.remind_before_minutes = e.detail.value === '' ? null
      : e.detail.value === 'other' ? Number(preview.querySelector('[data-card-lead-custom]')?.value || 50)
        : Number(e.detail.value);
    eventEdited.add('remind_before_minutes');
    captureSession.edit('EVENT', { remind_before_minutes: eventDraft.remind_before_minutes });
    fieldProvenance.remind_before_minutes = 'USER_EDIT';
    writeEventFields();
  });

  preview.addEventListener('click', (e) => {
    const switcher = e.target.closest('[data-switch-kind]');
    if (switcher) { switchKind(switcher.dataset.switchKind); return; }
    const chip = e.target.closest('[data-answer]');
    if (chip) {
      const values = answer(chip.dataset.answer, chip.dataset.value);
      if (!values && chip.dataset.answer === 'effort') { details.open = true; focusDurationOther(details, 'f-effort'); return; }
      if (!values) { details.open = true; setChip(details, 'f-deadline', 'KNOWN'); details.querySelector('[data-f="cutoff"]').classList.remove('hidden'); details.querySelector('[data-f="cutoff"]').focus(); return; }
      Object.assign(draft, values);
      Object.keys(values).forEach((k) => answered.add(k));
      captureSession.edit('TASK', values);
      render();
      return;
    }
    const fact = e.target.closest('[data-fact]');
    if (fact) {
      if (fact.dataset.fact === 'title' || fact.dataset.fact === 'event-time') {
        const current = kind === 'EVENT' ? eventDraft : draft;
        const time = fact.dataset.fact === 'event-time';
        const editor = openSheet({ title: t(time ? 'reminder.when' : 'form.title'), body: time
          ? `<label class="field"><span>${esc(t('reminder.when'))}</span><input type="datetime-local" data-inline-start value="${esc(localInputValue(current.starts_at))}"></label><label class="field"><span>${esc(t('form.effort'))} · ${esc(t('duration.minutes'))}</span><input type="number" min="5" max="1440" data-inline-duration value="${Math.round((Date.parse(current.ends_at) - Date.parse(current.starts_at)) / 60000)}"></label>`
          : `<label class="field"><span>${esc(t('form.title'))}</span><input data-inline-title maxlength="300" value="${esc(current.title)}"></label>`,
          actions: `<button type="button" class="button primary" data-inline-save>${esc(t('common.save'))}</button>` });
        editor.dataset.inlineEditor = 'true';
        editor.querySelector('[data-inline-save]').addEventListener('click', () => {
          let fields;
          if (time) {
            const start = isoFromLocalInput(editor.querySelector('[data-inline-start]').value);
            const duration = Number(editor.querySelector('[data-inline-duration]').value);
            if (!start || !Number.isFinite(duration) || duration < 5 || duration > 1440) return;
            fields = { starts_at: start, ends_at: new Date(Date.parse(start) + duration * 60000).toISOString() };
          } else {
            const title = editor.querySelector('[data-inline-title]').value.trim();
            if (!title) return;
            fields = { title };
          }
          Object.assign(current, fields);
          captureSession.edit(kind, fields);
          for (const field of Object.keys(fields)) { fieldProvenance[field] = 'USER_EDIT'; (kind === 'EVENT' ? eventEdited : answered).add(field); }
          render();
          editor.close('saved');
        });
        focusSoon(editor.querySelector('input'));
        return;
      }
      details.open = true;
      const target = fact.dataset.fact === 'title' && kind === 'EVENT' ? eventDetails.querySelector('[data-e="title"]')?.closest('.field')
        : fact.dataset.fact === 'event-time' ? eventDetails.querySelector('[data-e="start"]')?.closest('.field')
        : details.querySelector(`[data-field="${fact.dataset.fact}"]`) || details.querySelector('[data-field="title"]');
      target?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      target?.querySelector('input,select,textarea,button')?.focus({ preventScroll: true });
    }
  });

  // Any manual edit wins over later parses of the text.
  const fromDetails = () => {
    let fields;
    try { fields = readFields(taskDetails); } catch { return; }
    for (const key of FIELDS) {
      if (JSON.stringify(fields[key] ?? null) !== JSON.stringify(draft[key] ?? null)) {
        answered.add(key);
        fieldProvenance[key] = 'USER_EDIT';
      }
    }
    Object.assign(draft, fields);
    captureSession.edit('TASK', Object.fromEntries([...answered].map((key) => [key, draft[key]])));
    render();
  };
  taskDetails.addEventListener('change', fromDetails);
  taskDetails.addEventListener('chipchange', () => setTimeout(fromDetails));
  taskDetails.querySelector('[data-f="title"]').addEventListener('input', fromDetails);

  const mic = dialog.querySelector('[data-mic]');
  const onPageHide = () => persist();
  window.addEventListener('pagehide', onPageHide);
  const voicePanel = dialog.querySelector('[data-voice]');
  mic?.addEventListener('click', () => (dictation ? dictation.stop() : listen()));
  dialog.querySelector('[data-voice-stop]').addEventListener('click', () => dictation?.stop());
  dialog.querySelector('[data-voice-cancel]').addEventListener('click', () => {
    voiceGeneration += 1;
    dictation?.cancel?.();
    dictation = null;
    voiceState('idle');
  });
  dialog.addEventListener('close', () => {
    closed = true;
    window.removeEventListener('pagehide', onPageHide);
    serverSeq += 1;
    clearTimeout(serverTimer); clearTimeout(parseTimer); clearTimeout(saveTimer);
    voiceGeneration += 1;
    dictation?.cancel?.(); dictation = null;
    if (['saved', 'discarded', 'applied'].includes(dialog.returnValue)) writeCaptureDraft(localStorage, scope, null);
    else persist();
    // A follow-up survives only a proposal that was carried out, not a dismissed dialog.
    if (dialog.returnValue !== 'applied') assistantSession.forget();
  });
  dialog.querySelector('[data-discard]').addEventListener('click', () => dialog.close('discarded'));
  dialog.querySelector('[data-tutorial-skip]')?.addEventListener('click', () => {
    completeTutorial();
    dialog.querySelector('[data-guidance]').hidden = true;
  });
  if (guided) dialog.addEventListener('close', () => {
    if (dialog.returnValue === 'saved') { completeTutorial(); toast(t('tutorial.executionCoach')); }
  });
  dialog.querySelector('[data-clarification]').addEventListener('click', (event) => {
    const choice = event.target.closest('[data-meaning]');
    if (!choice) return;
    const selectedIndex = Number(choice.dataset.meaning);
    const original = selectedIndex === 0 ? localSemantic : modelSemantic;
    const candidate = semanticConflicts.some((conflict) => conflict.field === 'kind') ? original
      : { ...original, payload: { ...captureSession.intent.payload, ...Object.fromEntries(semanticConflicts.map((conflict) => [conflict.field, selectedIndex === 0 ? conflict.local : conflict.model])) } };
    if (candidate.kind === 'EVENT' && semanticConflicts.some((conflict) => ['starts_at', 'ends_at', 'duration_minutes'].includes(conflict.field))) {
      for (const field of ['starts_at', 'ends_at', 'duration_minutes']) if (field in original.payload) candidate.payload[field] = original.payload[field];
    }
    kind = candidate.kind; kindChosen = true;
    applySemantic(captureSession.choose(candidate), 'user', candidate.unresolved);
  });

  function voiceState(state, text = '') {
    voicePanel.hidden = state === 'idle';
    voicePanel.dataset.state = state;
    voicePanel.querySelector('[data-voice-state]').textContent = state === 'idle' ? '' : t(`voice.${state}`);
    voicePanel.querySelector('[data-voice-text]').textContent = text;
    voicePanel.querySelector('[data-voice-stop]').hidden = state !== 'recording';
    if (mic) {
      mic.classList.toggle('recording', state === 'recording');
      mic.setAttribute('aria-pressed', String(state === 'recording'));
      mic.setAttribute('aria-label', t(state === 'recording' ? 'voice.stop' : 'capture.voice'));
    }
  }

  // Dictation: tap to start, tap again (or "Стоп") to finish. The transcript goes
  // into the text field and through the same parse as typing; nothing is created
  // until the user presses "Создать".
  async function listen() {
    const generation = ++voiceGeneration;
    voiceState('starting');
    dictation = startDictation({
      onState: (state) => voiceState(state),
      onPartial: (text) => voiceState('recording', text),
    });
    try {
      const heard = await dictation.result;
      if (generation !== voiceGeneration) return;
      dictation = null;
      if (closed) return;
      if (!heard) { voiceState('error', t('ask.voiceEmpty')); setTimeout(() => { if (!dictation) voiceState('idle'); }, 2500); return; }
      voiceState('processing', heard);
      const before = input.value.trim();
      const correctedKinds = new Set();
      if (reminderTurn(heard, now())) correctedKinds.add('reminder');
      const typeCorrection = /(?:сделай\s+(?:это\s+)?|это\s+|make\s+(?:it\s+)?(?:an?\s+)?)(событием|событие|задача|задачей|заметка|заметкой|event|task|note)/iu.exec(heard);
      if (typeCorrection) {
        const requested = typeCorrection[1].toLowerCase();
        switchKind(/событ|event/u.test(requested) ? 'EVENT' : /замет|note/u.test(requested) ? 'NOTE' : 'TASK');
        input.value = before;
      } else input.value = correctedText(before ? `${before}. ${heard}` : heard, now(), correctedKinds);
      captureSession.input(heard, 'voice', correctedKinds);
      growInput();
      parseLocal();
      voiceState('idle');
      enrich();
    } catch (error) {
      if (generation !== voiceGeneration || closed) return;
      dictation = null;
      const key = { VOICE_UNAVAILABLE: 'ask.voiceUnavailable', VOICE_DENIED: 'ask.voiceDenied' }[error.code] || 'ask.voiceFailed';
      voiceState('error', t(key));
    }
  }

  kindSwitch.addEventListener('chipchange', (e) => { if (e.detail.name === 'capture-kind') switchKind(e.detail.value); });
  dialog.querySelectorAll('[data-other]').forEach((button) => button.addEventListener('click', async () => {
    dialog.close('other');
    const { composers } = await import('./compose.js');
    composers[button.dataset.other]?.();
  }));

  createButton.addEventListener('click', async (e) => {
    if (parseTimer) { clearTimeout(parseTimer); parseTimer = null; parseLocal(); }
    if (saving || semanticConflicts.length) return;
    saving = true;
    createButton.disabled = true;
    try {
    if (kind === 'NOTE') {
      const content = String(input.value || '').trim();
      if (!content) return;
      const noteId = newEntityId('note');
      const created = await change('note.create', noteId, { content, source_kind: 'CAPTURE' });
      if (!created) return;
      dialog.close('saved');
      const state = await settled(created.op_id);
      toast(state === 'PENDING' ? t('capture.savedOffline') : t('note.created'), {
        action: { label: t('common.open'), run: () => shell.go('note', { params: [noteId] }) },
      });
      return;
    }
    if (kind === 'REMINDER' && reminderDraft) {
      const fields = { ...reminderDraft, title: String(reminderDraft.title || '').trim() };
      if (assistant) fields.assistant_batch_id = assistant.batch_id;
      if (await createReminder(fields)) dialog.close('saved');
      return;
    }
    if (kind === 'EVENT' && eventDraft) {
      try {
        if (preview.querySelector('[data-chip-group="card-lead"]')) eventDraft.remind_before_minutes = readLead(preview, 'card-lead', '[data-card-lead-custom]');
        if (details.open) { readEventFields(eventDetails); fromEventDetails(); }
      } catch (err) { toast(err.message, { error: true }); return; }
      const fields = { ...eventDraft, title: String(eventDraft.title || '').trim() };
      if (assistant) fields.assistant_batch_id = assistant.batch_id;
      if (!fields.title) { toast(t('form.titleRequired'), { error: true }); return; }
      if (!(new Date(fields.ends_at) > new Date(fields.starts_at))) { toast(t('event.endBeforeStart'), { error: true }); return; }
      const id = await createEvent(fields, { toastText: t('capture.eventSaved', { when: eventWhen(fields) }) });
      if (id && sourceNoteId) await change('note.link', sourceNoteId, { target_kind: 'EVENT', target_id: id });
      if (id) dialog.close('saved');
      return;
    }
    if (details.open) {
      try { readFields(taskDetails); } catch (err) { toast(err.message, { error: true }); return; }
      fromDetails();
    }
    const payload = createPayload(draft);
    if (!payload.title) { toast(t('form.titleRequired'), { error: true }); return; }
    if (assistant) { payload.assistant_batch_id = assistant.batch_id; }
    const taskId = newEntityId('task');
    const created = await change('task.create', taskId, payload);
    if (!created) return;
    if (sourceNoteId) await change('note.link', sourceNoteId, { target_kind: 'TASK', target_id: taskId });
    dialog.close('saved');
    const state = await settled(created.op_id);
    toast(state === 'PENDING' ? t('capture.savedOffline') : t('compose.taskCreated'), {
      action: { label: t('common.open'), run: () => shell.go('task', { params: [taskId] }) },
    });
    } finally { saving = false; if (!closed) render(); }
  });

  render();
  if (recovered?.intent) {
    localSemantic = captureSession.local || { kind: recovered.intent.kind, payload: recovered.intent.payload, unresolved: [] };
    modelSemantic = captureSession.model;
    const restored = captureSession.interpret(localSemantic);
    applySemantic(modelSemantic ? captureSession.interpret(modelSemantic, 'model') : restored, 'recovery', localSemantic.unresolved);
  } else if (text) { parseLocal(); if (initialKind === 'EVENT' && !eventDraft) { eventDraft = eventFromTask(draft); kind = 'EVENT'; render(); } enrich(); }
  focusSoon(input);
  if (listenNow && voiceSupported()) listen();
  return dialog;
}
