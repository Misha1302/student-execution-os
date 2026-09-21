const state = {
  view: 'today',
  data: new Map(),
  loading: false,
  currentIntent: null,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function fmtDate(value, opts = {}) {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const base = { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' };
  return new Intl.DateTimeFormat(undefined, { ...base, ...opts }).format(d);
}

function fmtTime(value) {
  if (!value) return '—';
  const d = new Date(value);
  return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' }).format(d);
}

function fmtDuration(minutes) {
  if (minutes == null) return '—';
  const m = Number(minutes);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const r = m % 60;
  return r ? `${h}h ${r}m` : `${h}h`;
}

function titleCase(value) {
  return String(value ?? '').toLowerCase().replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
}

function badge(text, kind = '') {
  return `<span class="badge ${esc(kind)}">${esc(text)}</span>`;
}

function riskBadge(risk) {
  if (!risk) return badge('Risk unavailable', 'unknown');
  const map = {
    SAFE: 'safe', START_SOON: 'stale', AT_RISK: 'conflict', CRITICAL: 'conflict',
    IMPOSSIBLE: 'conflict', OVERDUE: 'conflict', UNKNOWN: 'unknown', NOT_APPLICABLE: ''
  };
  return badge(risk.state, map[risk.state] || '');
}

function statusClass(status) {
  return status === 'FEASIBLE' ? 'status-feasible' : status === 'INFEASIBLE' ? 'status-infeasible' : 'status-unknown';
}

function statusSymbol(status) {
  return status === 'FEASIBLE' ? '✓' : status === 'INFEASIBLE' ? '×' : '?';
}

function statusCopy(status, reasons = []) {
  if (status === 'FEASIBLE') return 'A concrete legal witness exists for the current supported hard constraints.';
  if (status === 'INFEASIBLE') return reasons[0] ? `Hard contradiction: ${titleCase(reasons[0])}.` : 'No legal schedule exists under the current supported hard constraints.';
  return reasons[0] ? `The system cannot prove feasibility yet: ${titleCase(reasons[0])}.` : 'The system cannot prove feasibility from the currently admissible inputs.';
}

async function api(path, options = {}) {
  const headers = { Accept: 'application/json', ...(options.headers || {}) };
  if (options.body && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('json') ? await response.json() : await response.text();
  if (!response.ok) {
    const err = new Error(payload?.error?.message || `HTTP ${response.status}`);
    err.code = payload?.error?.code || `HTTP_${response.status}`;
    err.status = response.status;
    throw err;
  }
  return payload;
}

function toast(message, error = false) {
  const region = $('#toast-region');
  const el = document.createElement('div');
  el.className = `toast${error ? ' error' : ''}`;
  el.textContent = message;
  region.append(el);
  setTimeout(() => el.remove(), 4200);
}

function setLoading(view) {
  const workspace = $('#workspace');
  workspace.dataset.view = view;
  workspace.dataset.viewState = 'loading';
  workspace.setAttribute('aria-busy', 'true');
  workspace.innerHTML = `<div class="grid grid-2"><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div></div>`;
}

function setViewReady(view) {
  const workspace = $('#workspace');
  workspace.dataset.view = view;
  workspace.dataset.viewState = 'ready';
  workspace.setAttribute('aria-busy', 'false');
}

function setPageMeta(view) {
  const meta = {
    today: ['Today', 'Execution cockpit'],
    plan: ['Plan', 'Facts vs projections'],
    tasks: ['Tasks', 'Canonical workload'],
    calendar: ['Calendar', 'Canonical commitments'],
    evidence: ['Evidence', 'Sources, conflicts, provenance'],
    places: ['Places', 'Location and travel evidence'],
    ask: ['Ask', 'Safe assistant boundary'],
    settings: ['Settings', 'Health, policy, diagnostics'],
    more: ['More', 'Product areas'],
  }[view] || ['Student Execution OS', ''];
  $('#page-title').textContent = meta[0];
  $('#eyebrow').textContent = meta[1];
  document.title = `${meta[0]} · Student Execution OS`;
  $$('[data-nav]').forEach(b => b.classList.toggle('active', b.dataset.nav === view || (view === 'more' && b.dataset.nav === 'more')));
}

async function navigate(view, { refresh = false } = {}) {
  if (view === 'more') {
    state.view = 'more';
    setPageMeta('more');
    renderMore();
    setViewReady('more');
    return;
  }
  state.view = view;
  setPageMeta(view);
  history.replaceState(null, '', `#${view}`);
  closeInspector();
  setLoading(view);
  try {
    const data = await loadView(view, refresh);
    await renderView(view, data);
    setViewReady(view);
  } catch (err) {
    renderError(err, view);
  }
}

async function loadView(view, refresh) {
  if (!refresh && state.data.has(view)) return state.data.get(view);
  if (view === 'settings') {
    const [diagnostics, notifications, deletion] = await Promise.all([
      api('/api/v1/settings/diagnostics'),
      api('/api/v1/notifications'),
      api('/api/v1/account/deletion-policy'),
    ]);
    const data = { ...diagnostics, notifications, deletion };
    state.data.set(view, data);
    return data;
  }
  const routes = {
    today: '/api/v1/today',
    plan: '/api/v1/plan/current',
    tasks: '/api/v1/tasks',
    calendar: '/api/v1/calendar',
    evidence: '/api/v1/evidence',
    places: '/api/v1/places',
    ask: '/api/v1/ask/capabilities',
  };
  const data = await api(routes[view]);
  state.data.set(view, data);
  return data;
}

function invalidate(...views) {
  if (!views.length) state.data.clear();
  else views.forEach(v => state.data.delete(v));
}

async function renderView(view, data) {
  const renderers = { today: renderToday, plan: renderPlan, tasks: renderTasks, calendar: renderCalendar, evidence: renderEvidence, places: renderPlaces, ask: renderAsk, settings: renderSettings };
  await renderers[view](data);
}

function renderError(err, view = state.view) {
  const workspace = $('#workspace');
  workspace.dataset.view = view;
  workspace.dataset.viewState = 'error';
  workspace.setAttribute('aria-busy', 'false');
  workspace.innerHTML = `<div class="empty"><div><strong>${esc(err.code || 'ERROR')}</strong>${esc(err.message)}<div class="inline-actions" style="justify-content:center;margin-top:14px"><button class="button" data-action="retry">Retry</button></div></div></div>`;
}

function sourceHealthSummary(rows = []) {
  if (!rows.length) return 'No connectors';
  const bad = rows.filter(r => r.health_status !== 'CURRENT');
  return bad.length ? `${bad.length} connector${bad.length === 1 ? '' : 's'} need attention` : 'Sources current';
}

function renderToday(data) {
  const plan = data.plan;
  const status = plan.feasibility_status;
  $('#revision-chip').textContent = `rev ${data.server_revision}\n${plan.input_hash.slice(0, 10)}`;
  $('#sync-indicator').textContent = sourceHealthSummary(data.source_health);
  const next = data.next_actions || [];
  const travel = data.travel?.transitions || [];
  const boundaries = [];
  for (const task of data.tasks || []) {
    if (task.risk?.latest_safe_start) boundaries.push({ at: task.risk.latest_safe_start, label: `Latest safe start · ${task.title}`, kind: 'task' });
    if (task.actual_cutoff?.at) boundaries.push({ at: task.actual_cutoff.at, label: `Hard cutoff · ${task.title}`, kind: 'cutoff' });
    if (task.target_at) boundaries.push({ at: task.target_at, label: `Target · ${task.title}`, kind: 'target' });
  }
  for (const t of travel) boundaries.push({ at: t.latest_safe_departure, label: `Leave ${t.origin} → ${t.destination}`, kind: 'travel' });
  boundaries.sort((a,b) => new Date(a.at) - new Date(b.at));

  const actionsHtml = next.length ? next.map((a, i) => {
    const task = data.tasks.find(t => t.id === a.task_id);
    const risk = task?.risk?.state || 'UNKNOWN';
    const start = plan.blocks.find(b => b.type === 'WORK' && b.obligation_id === a.task_id)?.starts_at;
    return `<div class="action-row" data-action="inspect-task" data-id="${esc(a.task_id)}">
      <div class="action-time">${i === 0 ? 'NOW' : esc(fmtTime(start))}<br>${esc(fmtDuration(a.recommended_duration_minutes))}</div>
      <div class="action-copy"><strong>${esc(a.what)}</strong><small>${esc(titleCase(a.why_now))}</small></div>
      <div>${riskBadge({state:risk})}</div>
    </div>`;
  }).join('') : `<div class="empty"><div><strong>No executable next action</strong>${status === 'FEASIBLE' ? 'No remaining hard work is scheduled in the current view.' : 'Resolve the feasibility state before the system can recommend work.'}</div></div>`;

  const boundariesHtml = boundaries.slice(0, 7).map(b => `<div class="key-value"><dt>${esc(fmtDate(b.at))}</dt><dd>${esc(b.label)}</dd></div>`).join('') || `<p class="muted">No upcoming hard boundaries in the current horizon.</p>`;

  $('#workspace').innerHTML = `
    <section class="section">
      <div class="hero-status">
        <div>
          <p class="eyebrow">Current proof state</p>
          <h2 class="${statusClass(status)}">${esc(status)}</h2>
          <p>${esc(statusCopy(status, plan.explanations))}</p>
          <div class="legend" style="margin-top:14px">${badge(`rev ${plan.input_server_revision}`, '')}${badge(`plan ${plan.plan_revision}`, 'derived')}${badge(sourceHealthSummary(data.source_health), data.source_health.some(s=>s.health_status!=='CURRENT')?'stale':'safe')}</div>
        </div>
        <div class="status-symbol ${statusClass(status)}" aria-hidden="true">${statusSymbol(status)}</div>
      </div>
    </section>
    <section class="section">
      <div class="section-head"><div><h2>Next actions</h2><p>Server-generated from the current revision-bound plan.</p></div><button class="button small" data-nav="plan">Open plan</button></div>
      <div class="action-list">${actionsHtml}</div>
    </section>
    <section class="section">
      <div class="section-head"><div><h2>Today’s occupancy</h2><p>Canonical facts and derived work/travel remain visually separate.</p></div></div>
      ${renderTimeline(plan, data.now, true)}
    </section>
    <section class="section grid grid-2">
      <div class="panel"><div class="section-head"><div><h2>Safe boundaries</h2><p>Latest-safe moments and hard cutoffs.</p></div></div>${boundariesHtml}</div>
      <div class="panel"><div class="section-head"><div><h2>Travel</h2><p>Travel is hard occupancy, not decorative commute metadata.</p></div></div>${renderTravelSummary(travel, data.travel)}</div>
    </section>`;
}

function renderTravelSummary(transitions, travel) {
  if (travel?.unknown_reasons?.length) return `<div class="warning-strip"><strong>UNKNOWN</strong><br>${esc(travel.unknown_reasons.map(titleCase).join(' · '))}</div>`;
  if (travel?.infeasible_reasons?.length) return `<div class="conflict-strip"><strong>INFEASIBLE</strong><br>${esc(travel.infeasible_reasons.map(titleCase).join(' · '))}</div>`;
  if (!transitions?.length) return `<p class="muted">No required derived travel in the current horizon.</p>`;
  return transitions.map(t => `<div class="panel-subtle" style="margin-bottom:8px"><div class="route"><strong>${esc(t.origin)}</strong><span class="route-arrow">→</span><strong>${esc(t.destination)}</strong>${badge(`${t.safe_duration_minutes ?? '—'}m safe`, 'derived')}</div><div class="key-value"><dt>Leave by</dt><dd>${esc(fmtDate(t.latest_safe_departure))}</dd></div><div class="key-value"><dt>Arrival buffer</dt><dd>${esc(fmtDuration(t.arrival_requirement_minutes))}</dd></div></div>`).join('');
}

function renderPlan(plan) {
  $('#revision-chip').textContent = `plan ${plan.plan_revision}\n${plan.input_hash.slice(0,10)}`;
  $('#workspace').innerHTML = `
    <section class="section">
      <div class="hero-status">
        <div><p class="eyebrow">Feasibility proof</p><h2 class="${statusClass(plan.feasibility_status)}">${esc(plan.feasibility_status)}</h2><p>${esc(statusCopy(plan.feasibility_status, plan.explanations))}</p></div>
        <div class="status-symbol ${statusClass(plan.feasibility_status)}" aria-hidden="true">${statusSymbol(plan.feasibility_status)}</div>
      </div>
    </section>
    <section class="section">
      <div class="section-head"><div><h2>Timeline</h2><p>Facts on the left. Planner projections on the right.</p></div><div>${badge('Canonical', 'canonical')} ${badge('Derived', 'derived')}</div></div>
      ${renderTimeline(plan, plan.horizon_start, false)}
      <div class="legend"><span class="legend-item"><span class="legend-mark canonical"></span>Canonical Event</span><span class="legend-item"><span class="legend-mark constraint"></span>User constraint</span><span class="legend-item"><span class="legend-mark derived"></span>WORK / Event projection / Travel / Buffer</span></div>
    </section>
    <section class="section grid grid-2">
      <div class="panel"><h2>Plan identity</h2><dl>${kv('Revision', plan.plan_revision)}${kv('Input server revision', plan.input_server_revision)}${kv('Input hash', `<span class="mono">${esc(plan.input_hash)}</span>`, true)}${kv('Generated', fmtDate(plan.generated_at))}</dl></div>
      <div class="panel"><h2>Planner explanations</h2>${plan.explanations?.length ? `<div class="provenance">${plan.explanations.map(x=>`<div class="provenance-node">${esc(titleCase(x))}</div>`).join('')}</div>` : '<p class="muted">No extra planner explanation codes.</p>'}</div>
    </section>`;
}

function renderTimeline(plan, nowValue, compact) {
  const start = new Date(plan.horizon_start);
  const hardEnd = new Date(plan.horizon_end);
  const end = new Date(Math.min(hardEnd.getTime(), start.getTime() + (compact ? 16 : 24) * 3600000));
  const totalMin = Math.max(60, (end - start) / 60000);
  const height = Math.max(compact ? 480 : 680, totalMin * (compact ? .72 : .86));
  const y = (value) => ((new Date(value) - start) / 60000) / totalMin * height;
  const h = (a,b) => Math.max(4, ((new Date(b) - new Date(a))/60000) / totalMin * height);
  const densityClass = (a,b) => { const px=h(a,b); return px < 18 ? ' ultra-short' : px < 34 ? ' short' : ''; };
  const inRange = (a,b) => new Date(a) < end && start < new Date(b);
  const canonical = [];
  for (const e of plan.canonical_events || []) if (inRange(e.starts_at,e.ends_at)) canonical.push({ type:'event', starts_at:e.starts_at, ends_at:e.ends_at, label:e.title, detail:`${e.attendance_policy} · ${e.location_effect.kind}`, data:e });
  for (const c of plan.constraints || []) if (inRange(c.starts_at,c.ends_at)) canonical.push({ type:'constraint', starts_at:c.starts_at, ends_at:c.ends_at, label:titleCase(c.type), detail:c.reason || 'User-owned constraint', data:c });
  const derived = (plan.blocks || []).filter(b=>inRange(b.starts_at,b.ends_at));
  const hours = [];
  const step = compact ? 120 : 120;
  for (let m = 0; m <= totalMin; m += step) {
    const t = new Date(start.getTime() + m*60000);
    hours.push(`<div class="hour-label" style="top:${(m/totalMin)*height}px">${esc(fmtTime(t))}</div><div class="hour-line" style="top:${(m/totalMin)*height}px"></div>`);
  }
  const canHtml = canonical.map(item => `<button class="timeline-block ${item.type === 'constraint' ? 'constraint' : 'canonical'}${densityClass(item.starts_at,item.ends_at)}" style="top:${Math.max(0,y(item.starts_at))}px;height:${h(item.starts_at,item.ends_at)}px" data-action="inspect-timeline" data-payload="${esc(encodeURIComponent(JSON.stringify(item)))}"><strong>${esc(item.label)}</strong><small>${esc(fmtTime(item.starts_at))}–${esc(fmtTime(item.ends_at))} · ${esc(item.detail)}</small></button>`).join('');
  const derHtml = derived.map(b => {
    const cls = b.type === 'WORK' ? 'work' : b.type === 'EVENT_PROJECTION' ? 'event-projection' : b.type === 'TRAVEL_TRANSITION' ? 'travel' : 'buffer';
    return `<button class="timeline-block ${cls}${densityClass(b.starts_at,b.ends_at)}" style="top:${Math.max(0,y(b.starts_at))}px;height:${h(b.starts_at,b.ends_at)}px" data-action="inspect-timeline" data-payload="${esc(encodeURIComponent(JSON.stringify(b)))}"><strong>${esc(b.label || titleCase(b.type))}</strong><small>${esc(fmtTime(b.starts_at))}–${esc(fmtTime(b.ends_at))} · ${esc(titleCase(b.type))}</small></button>`;
  }).join('');
  const now = new Date(nowValue || Date.now());
  const nowHtml = now >= start && now <= end ? `<div class="timeline-now" style="top:${y(now)}px"></div>` : '';
  const agendaItems = [
    ...canonical.map(i=>({...i, ownership:'CANONICAL', kind:i.type === 'constraint' ? 'Constraint' : 'Event'})),
    ...derived.map(b=>({starts_at:b.starts_at, ends_at:b.ends_at, label:b.label || titleCase(b.type), detail:titleCase(b.type), ownership:'DERIVED', kind:b.type, data:b}))
  ].sort((a,b)=>new Date(a.starts_at)-new Date(b.starts_at));
  const agenda = agendaItems.map(i => `<button class="agenda-item" data-action="inspect-timeline" data-payload="${esc(encodeURIComponent(JSON.stringify(i.data || i)))}"><span class="agenda-time">${esc(fmtTime(i.starts_at))}<br>${esc(fmtTime(i.ends_at))}</span><span><strong>${esc(i.label)}</strong><br><small class="muted">${esc(i.ownership)} · ${esc(i.detail)}</small></span></button>`).join('') || '<div class="empty">No occupancy in this display horizon.</div>';
  return `<div class="timeline-shell"><div class="timeline-head"><div>Time</div><div>Facts</div><div>Derived</div></div><div class="timeline" style="height:${height}px"><div class="timeline-time">${hours.join('')}</div><div class="timeline-lane">${canHtml}</div><div class="timeline-lane">${derHtml}</div>${nowHtml}</div></div><div class="mobile-agenda">${agenda}</div>`;
}

function renderTasks(tasks) {
  $('#sync-indicator').textContent = `${tasks.length} tasks`;
  const rows = tasks.map(t => `<tr data-action="inspect-task" data-id="${esc(t.id)}">
      <td><div class="value-stack"><strong>${esc(t.title)}</strong><small>${badge(t.status, t.status==='ACTIVE'?'safe':'')}</small></div></td>
      <td>${esc(fmtDuration(t.remaining_effort_minutes))} <span class="muted">/ ${esc(fmtDuration(t.estimated_total_effort_minutes))}</span></td>
      <td>${esc(fmtDate(t.target_at))}</td>
      <td><div class="value-stack"><strong>${esc(t.actual_cutoff.state)}</strong><small>${esc(fmtDate(t.actual_cutoff.at))}</small></div></td>
      <td>${riskBadge(t.risk)}</td>
      <td>${t.splittable ? `split ${esc(t.min_chunk_minutes ?? '—')}–${esc(t.max_chunk_minutes ?? '—')}m` : 'contiguous'}</td>
    </tr>`).join('');
  $('#workspace').innerHTML = `
    <section class="section">
      <div class="section-head"><div><h2>Canonical tasks</h2><p>Target, hard cutoff, actionable-from and remaining effort stay distinct.</p></div><button class="button primary" data-action="new-task">New task</button></div>
      ${tasks.length ? `<div class="table-wrap"><table><thead><tr><th>Task</th><th>Remaining / total</th><th>Target</th><th>Hard cutoff</th><th>Risk</th><th>Chunks</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="empty"><div><strong>No obligations yet.</strong>Add a task, add an event, or ingest a source.</div></div>`}
    </section>`;
}

function renderCalendar(data) {
  const events = data.events || [];
  const templates = data.recurring_templates || [];
  const occurrences = (data.occurrences || []).filter(item => !item.cancelled);
  const cards = events.map(e => `<div class="panel source-card" data-action="inspect-event" data-id="${esc(e.id)}">
    <div class="source-head"><div><p class="eyebrow">${e.location_effect.kind === 'MOVE' ? 'Booked journey' : 'Fixed event'}</p><h3>${esc(e.title)}</h3></div>${badge('CANONICAL','canonical')}</div>
    <div class="key-value"><dt>When</dt><dd>${esc(fmtDate(e.starts_at))} → ${esc(fmtDate(e.ends_at))}</dd></div>
    <div class="key-value"><dt>Attendance</dt><dd>${esc(e.attendance_policy)}</dd></div>
    <div class="key-value"><dt>Location effect</dt><dd>${esc(e.location_effect.kind)}${e.location_effect.origin_place_id ? ` · ${esc(e.location_effect.origin_place_id)} → ${esc(e.location_effect.destination_place_id)}` : e.location_effect.destination_place_id ? ` · ${esc(e.location_effect.destination_place_id)}` : ''}</dd></div>
    <div class="key-value"><dt>Arrival requirement</dt><dd>${esc(fmtDuration(e.arrival_requirement_minutes))}</dd></div>
  </div>`).join('');
  const series = templates.map(t => `<div class="panel source-card">
    <div class="source-head"><div><p class="eyebrow">Recurring rule · v${esc(t.version)}</p><h3>${esc(t.title)}</h3></div>${badge('CANONICAL RULE','canonical')}</div>
    <div class="key-value"><dt>DTSTART (local)</dt><dd>${esc(t.dtstart_local)}</dd></div>
    <div class="key-value"><dt>RRULE</dt><dd><span class="mono">${esc(t.recurrence_rule)}</span></dd></div>
    <div class="key-value"><dt>Timezone</dt><dd>${esc(t.timezone_name)}</dd></div>
    <div class="key-value"><dt>Duration</dt><dd>${esc(fmtDuration(t.duration_minutes))}</dd></div>
    <p class="muted">The rule is canonical; expanded occurrences are derived and keep their original recurrence identity when moved.</p>
  </div>`).join('');
  const occurrenceRows = occurrences.slice(0, 24).map(o => {
    const template = templates.find(t => t.id === o.template_id);
    return `<tr><td><strong>${esc(template?.title || o.template_id)}</strong><small class="table-sub">${o.override_id ? 'moved/overridden' : 'generated'}</small></td><td>${esc(fmtDate(o.starts_at))}</td><td><span class="mono">${esc(o.original_recurrence_id)}</span></td><td>${badge('DERIVED OCCURRENCE','derived')}</td></tr>`;
  }).join('');
  $('#workspace').innerHTML = `
    <section class="section"><div class="section-head"><div><h2>Calendar facts</h2><p>Canonical fixed commitments. A booked MOVE journey is not a derived commute.</p></div><div class="inline-actions"><button class="button" data-action="new-recurring-event">New recurring event</button><button class="button primary" data-action="new-event">New fixed event</button></div></div>${events.length ? `<div class="grid grid-2">${cards}</div>` : `<div class="empty"><div><strong>No fixed events.</strong>Add a canonical event. Flexible-window events remain unsupported and are not silently coerced.</div></div>`}</section>
    <section class="section"><div class="section-head"><div><h2>Recurring series</h2><p>Local civil DTSTART + IANA timezone remain canonical. Occurrence identity is separate from its moved start time.</p></div></div>${templates.length ? `<div class="grid grid-2">${series}</div>` : '<div class="empty">No recurring templates yet.</div>'}</section>
    <section class="section"><div class="section-head"><div><h2>Expanded occurrences</h2><p>Derived planning instances for the current 30-day horizon; original recurrence id remains inspectable.</p></div></div>${occurrenceRows ? `<div class="table-wrap"><table><thead><tr><th>Series</th><th>Effective start</th><th>Original recurrence id</th><th>Ownership</th></tr></thead><tbody>${occurrenceRows}</tbody></table></div>` : '<div class="empty">No occurrences in the current horizon.</div>'}</section>`;
}

function renderEvidence(data) {
  const sources = data.sources || [];
  const sourceCards = sources.map(s => {
    const health = s.connector?.health_status || s.availability || 'ACTIVE';
    const kind = ['STALE','UNAVAILABLE'].includes(health) ? 'stale' : 'safe';
    return `<div class="panel source-card"><div class="source-head"><div><p class="eyebrow">${esc(s.kind)}</p><h3>${esc(s.id)}</h3></div>${badge(health,kind)}</div>${s.connector ? `<div class="key-value"><dt>Provider</dt><dd>${esc(s.connector.provider)}</dd></div><div class="key-value"><dt>Last complete sync</dt><dd>${esc(fmtDate(s.connector.last_successful_complete_sync_at))}</dd></div>${s.connector.latest_failure_reason ? `<div class="warning-strip">${esc(s.connector.latest_failure_reason)}</div>`:''}` : '<p class="muted">No live connector state for this source.</p>'}</div>`;
  }).join('');
  const conflicts = (data.conflicts || []).filter(c => c.status === 'OPEN');
  const conflictCards = conflicts.map(c => `<div class="panel"><div class="source-head"><div><p class="eyebrow">Open conflict</p><h3>${esc(c.entity_ref)} · ${esc(c.field_path)}</h3></div>${badge('CONFLICT','conflict')}</div><div class="key-value"><dt>Evidence</dt><dd>${esc(c.evidence_ids.join(', '))}</dd></div><div class="key-value"><dt>Policy</dt><dd>${esc(c.policy_version)}</dd></div><p class="muted">The conflict remains truth. A conservative planning projection, when present, is a separate derived bound.</p></div>`).join('');
  const effective = (data.effective_fields || []).map(e => `<div class="panel-subtle"><div class="source-head"><strong>${esc(e.entity_ref)} · ${esc(e.field_path)}</strong>${badge(e.state,e.state==='CONFLICT'?'conflict':e.state==='OVERRIDDEN'?'stale':'')}</div><div class="key-value"><dt>Evidence</dt><dd>${esc((e.evidence_ids||[]).join(', ') || '—')}</dd></div><div class="key-value"><dt>Planning projection</dt><dd>${esc(e.planning_projection?.at ? fmtDate(e.planning_projection.at) : '—')}</dd></div><div class="key-value"><dt>Reason</dt><dd>${esc(e.reason || '—')}</dd></div></div>`).join('');
  const observations = (data.observations || []).slice(0, 20).map(o => `<tr><td>${esc(fmtDate(o.observed_at))}</td><td>${esc(o.source_system_id)}</td><td>${esc(o.field_path)}</td><td>${esc(o.value_type)}</td><td>${esc(o.extraction_certainty)}</td></tr>`).join('');
  $('#workspace').innerHTML = `
    <section class="section"><div class="section-head"><div><h2>Sources</h2><p>Health and staleness are workflow/evidence state, not canonical truth.</p></div></div>${sources.length ? `<div class="grid grid-3">${sourceCards}</div>` : '<div class="empty">No evidence sources have been ingested yet.</div>'}</section>
    <section class="section"><div class="section-head"><div><h2>Conflicts</h2><p>Conflicting evidence remains inspectable even when planning uses a conservative projection.</p></div></div>${conflicts.length ? `<div class="grid grid-2">${conflictCards}</div>` : '<div class="empty">No open reconciliation conflicts.</div>'}</section>
    <section class="section"><div class="section-head"><div><h2>Effective interpretations</h2><p>Local interpretation with provenance — evidence is never rewritten to match it.</p></div></div><div class="grid grid-2">${effective || '<div class="empty">No reconciled fields yet.</div>'}</div></section>
    <section class="section"><div class="section-head"><div><h2>Recent observations</h2><p>Imported content is data, never authorization.</p></div></div>${observations ? `<div class="table-wrap"><table><thead><tr><th>Observed</th><th>Source</th><th>Field</th><th>Type</th><th>Certainty</th></tr></thead><tbody>${observations}</tbody></table></div>` : '<div class="empty">No observations.</div>'}</section>`;
}

function renderPlaces(data) {
  const c = data.current_location;
  const placeCards = (data.places || []).map(p => `<div class="panel-subtle"><div class="source-head"><strong>${esc(p.alias || p.display_name)}</strong>${badge('PRIVATE ALIAS','canonical')}</div><p class="muted">${esc(p.display_name)}</p><div class="key-value"><dt>Visibility</dt><dd>${esc(p.visibility_policy)}</dd></div><div class="key-value"><dt>Version</dt><dd>${esc(p.version)}</dd></div></div>`).join('');
  const routes = (data.route_estimates || []).map(r => `<div class="panel"><div class="source-head"><div class="route"><strong>${esc(r.origin)}</strong><span class="route-arrow">→</span><strong>${esc(r.destination)}</strong></div>${badge(r.fresh ? 'FRESH' : 'STALE', r.fresh?'safe':'stale')}</div><div class="key-value"><dt>Expected / safe</dt><dd>${esc(fmtDuration(r.expected_duration_minutes))} / <strong>${esc(fmtDuration(r.safe_duration_minutes))}</strong></dd></div><div class="key-value"><dt>Calculated</dt><dd>${esc(fmtDate(r.calculated_at))}</dd></div><div class="key-value"><dt>Expires</dt><dd>${esc(fmtDate(r.expires_at))}</dd></div><div class="key-value"><dt>Source</dt><dd>${esc(r.source)}</dd></div></div>`).join('');
  $('#workspace').innerHTML = `
    <section class="section"><div class="hero-status"><div><p class="eyebrow">Current location</p><h2>${esc(c.state)}${c.place ? ` · ${esc(c.place)}` : ''}</h2><p>Recorded ${esc(fmtDate(c.recorded_at))} · source ${esc(c.source)}. Feasibility-material UNKNOWN origins are never fabricated.</p></div><div class="status-symbol ${c.state==='UNKNOWN'?'status-unknown':'status-feasible'}">${c.state==='UNKNOWN'?'?':'⌖'}</div></div></section>
    <section class="section"><div class="section-head"><div><h2>Places</h2><p>Ordinary UI payloads expose aliases/display names, not exact private coordinates or addresses.</p></div></div>${placeCards ? `<div class="grid grid-3">${placeCards}</div>` : '<div class="empty">No saved places.</div>'}</section>
    <section class="section"><div class="section-head"><div><h2>Route evidence</h2><p>Safe duration and freshness are planning inputs.</p></div></div>${routes ? `<div class="grid grid-2">${routes}</div>` : '<div class="empty">No route estimates.</div>'}</section>
    <section class="section"><div class="warning-strip">${esc(data.privacy.note)}</div></section>`;
}

async function renderAsk(capabilities) {
  const tasks = await api('/api/v1/tasks');
  const events = await api('/api/v1/events');
  const active = [...tasks.map(x=>({...x,kind:'TASK'})), ...events.map(x=>({...x,kind:'EVENT'}))].filter(x=>x.status==='ACTIVE');
  const options = active.map(o=>`<option value="${esc(o.id)}" data-version="${esc(o.version)}">${esc(o.title)} · ${esc(o.kind)}</option>`).join('');
  $('#workspace').innerHTML = `
    <section class="section"><div class="hero-status"><div><p class="eyebrow">Assistant boundary</p><h2>Explain safely. Act only through server intent.</h2><p>${esc(capabilities.message)}</p></div><div class="status-symbol status-unknown">✦</div></div></section>
    <section class="section grid grid-2">
      <div class="panel"><h2>Ask / explain</h2><p class="muted">The live LLM provider is intentionally not faked. Existing server-owned explanations remain available throughout Today, Plan and Evidence.</p><div class="provenance"><div class="provenance-node"><strong>Why is the plan UNKNOWN?</strong><br><span class="muted">Inspect the current feasibility reasons.</span></div><div class="provenance-node"><strong>Where did this cutoff come from?</strong><br><span class="muted">Open Evidence → effective interpretation → source observations.</span></div><div class="provenance-node"><strong>Why now?</strong><br><span class="muted">Next action carries plan revision and current risk.</span></div></div></div>
      <div class="panel"><h2>Destructive action preview</h2><p class="muted">Imported/source text cannot authorize this. The server binds principal, account, target, command and expected version.</p>${active.length ? `<div class="field"><label for="agent-target">Target obligation</label><select id="agent-target">${options}</select></div><div class="inline-actions" style="margin-top:12px"><button class="button danger" data-action="agent-preview">Preview cancellation</button></div>` : '<div class="empty">No active obligation to target.</div>'}</div>
    </section>`;
}

function renderSettings(data) {
  $('#revision-chip').textContent = `schema v${data.schema_version}\nrev ${data.server_revision}`;
  const connectors = (data.connector_health || []).map(c => `<div class="panel-subtle"><div class="source-head"><strong>${esc(c.id)}</strong>${badge(c.health_status,c.health_status==='CURRENT'?'safe':'stale')}</div><div class="key-value"><dt>Provider</dt><dd>${esc(c.provider)}</dd></div><div class="key-value"><dt>Last success</dt><dd>${esc(fmtDate(c.last_successful_complete_sync_at))}</dd></div></div>`).join('');
  const notifications = (data.notifications || []).map(n => {
    const effective = n.snoozed_until || n.scheduled_for;
    const canSnooze = !['DELIVERED','SUPPRESSED'].includes(n.state);
    return `<div class="panel-subtle"><div class="source-head"><div><strong>${esc(titleCase(n.kind))}</strong><small>${esc(n.entity_ref || 'system')}</small></div>${badge(n.state,n.state==='DELIVERED'?'safe':n.state==='SUPPRESSED'?'stale':'')}</div><div class="key-value"><dt>Delivery</dt><dd>${esc(fmtDate(effective))}</dd></div><div class="key-value"><dt>Revision binding</dt><dd>domain ${esc(n.domain_revision)} · workflow v${esc(n.version)}</dd></div>${n.last_error ? `<div class="warning-strip">${esc(n.last_error)}</div>` : ''}${canSnooze ? `<div class="inline-actions" style="margin-top:10px"><button class="button ghost" data-action="snooze-notification" data-id="${esc(n.id)}" data-version="${esc(n.version)}">Snooze</button></div>` : ''}</div>`;
  }).join('');
  $('#workspace').innerHTML = `
    <section class="section grid grid-2">
      <div class="panel"><h2>Runtime</h2><dl>${kv('Version',data.version)}${kv('Schema',`v${data.schema_version}`)}${kv('Server revision',data.server_revision)}${kv('Recurring templates',data.recurring_template_count)}${kv('Account scope',data.account_binding)}${kv('Principal scope',data.principal_binding)}</dl></div>
      <div class="panel"><h2>Current plan</h2>${data.latest_plan ? `<dl>${kv('Plan',data.latest_plan.id)}${kv('Revision',data.latest_plan.plan_revision)}${kv('Feasibility',data.latest_plan.feasibility_status)}${kv('Input hash',`<span class="mono">${esc(data.latest_plan.input_hash)}</span>`,true)}</dl>` : '<p class="muted">No derived plan persisted yet.</p>'}</div>
    </section>
    <section class="section"><div class="section-head"><div><h2>Notifications</h2><p>Workflow state is revision-bound and separate from Task/Event truth. Snooze changes delivery time only.</p></div></div>${notifications ? `<div class="grid grid-2">${notifications}</div>` : '<div class="empty">No notification workflow state.</div>'}</section>
    <section class="section"><div class="section-head"><div><h2>Connections</h2><p>Health only; no fake OAuth setup flow is exposed.</p></div></div>${connectors ? `<div class="grid grid-2">${connectors}</div>` : '<div class="empty">No connector workflow state.</div>'}</section>
    <section class="section"><div class="panel"><h2>Data lifecycle</h2><p class="muted">Account export is server-scoped to the authenticated account. It intentionally includes private account data, including exact saved locations, and must be handled as a sensitive artifact.</p><div class="inline-actions" style="margin-top:12px"><a class="button ghost" href="/api/v1/account/export" download="student-execution-os-export.json">Download account export</a></div><hr class="panel-rule"><div class="source-head"><div><strong>Delete local account</strong><small>${esc(data.deletion.policy_version)} · server revision ${esc(data.deletion.server_revision)}</small></div>${badge('DESTRUCTIVE','stale')}</div><p class="muted">Deletion immediately purges account-scoped operational/private SQLite state. For ${esc(data.deletion.tombstone_retention_days)} days only a minimal tombstone is retained to reject stale connector/client replay and account-id reuse. Audit/provenance is not retained by this local policy; this release has no OAuth/secret store to revoke.</p><div class="inline-actions" style="margin-top:12px"><button class="button danger" data-action="account-delete-preview">Review account deletion</button></div></div></section>
    <section class="section"><div class="panel"><h2>Privacy boundary</h2><p class="muted">Exact private locations are not serialized by the normal Places endpoint. Imported content is untrusted evidence. Browser requests cannot self-assert account/principal identity.</p></div></section>`;
}

function renderMore() {
  $('#workspace').innerHTML = `<section class="section"><div class="more-grid"><button class="nav-item panel" data-nav="calendar">Calendar</button><button class="nav-item panel" data-nav="evidence">Evidence</button><button class="nav-item panel" data-nav="places">Places</button><button class="nav-item panel" data-nav="ask">Ask</button><button class="nav-item panel" data-nav="settings">Settings</button></div></section>`;
}

function kv(key, value, raw=false) {
  return `<div class="key-value"><dt>${esc(key)}</dt><dd>${raw ? value : esc(value ?? '—')}</dd></div>`;
}

function inspect(title, html) {
  $('#inspector-title').textContent = title;
  $('#inspector-body').innerHTML = html;
  $('#inspector').setAttribute('aria-hidden','false');
  $('#app-shell').classList.add('inspector-open');
}

function closeInspector() {
  $('#inspector').setAttribute('aria-hidden','true');
  $('#app-shell').classList.remove('inspector-open');
}

async function inspectTask(id) {
  const tasks = state.data.get('tasks') || await loadView('tasks', true);
  const task = tasks.find(t=>t.id===id);
  if (!task) return;
  const truth = task.cutoff_truth;
  const actions = task.status === 'ACTIVE' ? `<div class="inline-actions"><button class="button small" data-action="edit-task" data-id="${esc(id)}">Edit</button><button class="button small" data-action="lifecycle" data-id="${esc(id)}" data-version="${task.version}" data-lifecycle="complete">Complete</button><button class="button small danger" data-action="lifecycle" data-id="${esc(id)}" data-version="${task.version}" data-lifecycle="cancel">Cancel</button></div>` : task.status === 'COMPLETED' || task.status === 'CANCELLED' ? `<button class="button small" data-action="lifecycle" data-id="${esc(id)}" data-version="${task.version}" data-lifecycle="reopen">Reopen</button>` : '';
  inspect(task.title, `
    <p>${badge('CANONICAL','canonical')} ${riskBadge(task.risk)}</p>
    <dl>${kv('Lifecycle',task.status)}${kv('Version',task.version)}${kv('Importance',task.importance)}${kv('Remaining',fmtDuration(task.remaining_effort_minutes))}${kv('Estimated total',fmtDuration(task.estimated_total_effort_minutes))}${kv('Actionable from',fmtDate(task.actionable_from))}${kv('Target',fmtDate(task.target_at))}${kv('Actual cutoff state',task.actual_cutoff.state)}${kv('Actual cutoff',fmtDate(task.actual_cutoff.at))}</dl>
    ${truth ? `<div class="section"><h3>Cutoff interpretation</h3><div class="${truth.state==='CONFLICT'?'conflict-strip':'panel-subtle'}"><strong>${esc(truth.state)}</strong><br>${esc(truth.reason || '')}</div><dl>${kv('Evidence',truth.evidence_ids.join(', ')||'—')}${kv('Policy',truth.policy_version)}${kv('Planning projection',fmtDate(truth.planning_projection?.at))}</dl></div>`:''}
    <div class="section"><h3>Chunk semantics</h3><dl>${kv('Splittable',task.splittable?'yes':'no')}${kv('Minimum chunk',fmtDuration(task.min_chunk_minutes))}${kv('Maximum chunk',fmtDuration(task.max_chunk_minutes))}</dl></div>
    ${actions}`);
}

async function inspectEvent(id) {
  const events = state.data.get('calendar') || await loadView('calendar', true);
  const e = events.find(x=>x.id===id);
  if (!e) return;
  inspect(e.title, `<p>${badge('CANONICAL','canonical')} ${e.location_effect.kind==='MOVE'?badge('BOOKED MOVE','canonical'):''}</p><dl>${kv('Lifecycle',e.status)}${kv('Version',e.version)}${kv('Starts',fmtDate(e.starts_at))}${kv('Ends',fmtDate(e.ends_at))}${kv('Attendance',e.attendance_policy)}${kv('Location effect',e.location_effect.kind)}${kv('Origin',e.location_effect.origin_place_id)}${kv('Destination',e.location_effect.destination_place_id)}${kv('Arrival requirement',fmtDuration(e.arrival_requirement_minutes))}</dl>${e.status==='ACTIVE'?`<div class="inline-actions"><button class="button small danger" data-action="lifecycle" data-id="${esc(e.id)}" data-version="${e.version}" data-lifecycle="cancel">Cancel</button></div>`:''}`);
}

function inspectTimeline(payload) {
  const item = JSON.parse(decodeURIComponent(payload));
  const ownership = item.ownership || (item.canonical ? 'CANONICAL' : 'DERIVED');
  inspect(item.label || item.title || titleCase(item.type || 'Timeline block'), `<p>${badge(ownership, ownership==='CANONICAL'?'canonical':'derived')}</p><dl>${kv('Type',item.type || item.kind)}${kv('Starts',fmtDate(item.starts_at))}${kv('Ends',fmtDate(item.ends_at))}${kv('Explanation',titleCase(item.explanation))}${kv('Source event',item.source_event_id)}${kv('Travel estimate',item.travel_estimate_id)}${kv('Constraint version',item.version)}</dl>${ownership==='DERIVED' && item.type==='WORK' ? '<div class="warning-strip">This PlanBlock is not writable canonical state. Pinning/dragging must create or update a UserTimeConstraint.</div>' : ''}`);
}

function openModal({eyebrow='', title, body, actions=''}) {
  $('#modal-eyebrow').textContent = eyebrow;
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = body;
  $('#modal-actions').innerHTML = actions;
  $('#modal').showModal();
}

function newTaskModal() {
  openModal({ eyebrow:'Canonical state', title:'New task', body:`<div class="form-grid">
    <div class="field full"><label>Title</label><input id="f-title" required maxlength="180"></div>
    <div class="field"><label>Estimated total effort (min)</label><input id="f-effort" type="number" min="1" value="60"></div>
    <div class="field"><label>Importance</label><select id="f-importance"><option>NORMAL</option><option>HIGH</option><option>CRITICAL</option><option>LOW</option></select></div>
    <div class="field"><label>Target (optional)</label><input id="f-target" type="datetime-local"></div>
    <div class="field"><label>Actionable from (optional)</label><input id="f-actionable" type="datetime-local"></div>
    <div class="field"><label>Actual cutoff state</label><select id="f-cutoff-state"><option>UNKNOWN</option><option>KNOWN</option><option>ABSENT</option></select></div>
    <div class="field"><label>Known cutoff time</label><input id="f-cutoff" type="datetime-local"></div>
    <div class="field"><label>Splittable</label><select id="f-splittable"><option value="true">Yes</option><option value="false">No</option></select></div>
    <div class="field"><label>Min chunk (min)</label><input id="f-min" type="number" min="1" value="30"></div>
    <div class="field"><label>Max chunk (min)</label><input id="f-max" type="number" min="1" value="60"></div>
    <div class="field full"><p class="help">Target is user-owned planning intent. Actual cutoff is separate and becomes reconciliation-owned if evidence is attached.</p></div>
  </div>`, actions:`<button value="cancel" class="button ghost">Cancel</button><button type="button" class="button primary" data-action="save-task">Create task</button>` });
}

function toIsoLocal(input) {
  const value = input?.value;
  if (!value) return null;
  return new Date(value).toISOString();
}

async function saveTask() {
  const cutoffState = $('#f-cutoff-state').value;
  const payload = {
    title: $('#f-title').value.trim(),
    importance: $('#f-importance').value,
    estimated_total_effort_minutes: Number($('#f-effort').value),
    remaining_effort_minutes: Number($('#f-effort').value),
    target_at: toIsoLocal($('#f-target')),
    actionable_from: toIsoLocal($('#f-actionable')),
    splittable: $('#f-splittable').value === 'true',
    min_chunk_minutes: $('#f-min').value ? Number($('#f-min').value) : null,
    max_chunk_minutes: $('#f-max').value ? Number($('#f-max').value) : null,
    actual_cutoff: { state: cutoffState, at: cutoffState === 'KNOWN' ? toIsoLocal($('#f-cutoff')) : null },
  };
  try {
    await api('/api/v1/tasks',{method:'POST',body:payload});
    $('#modal').close(); invalidate(); toast('Task created'); navigate('tasks',{refresh:true});
  } catch (err) { toast(`${err.code}: ${err.message}`,true); }
}

async function editTaskModal(id) {
  const tasks = state.data.get('tasks') || await loadView('tasks', true);
  const t = tasks.find(x=>x.id===id); if (!t) return;
  openModal({eyebrow:`v${t.version} · optimistic concurrency`, title:`Edit · ${t.title}`, body:`<div class="form-grid"><div class="field"><label>Remaining effort (min)</label><input id="e-remaining" type="number" min="0" value="${esc(t.remaining_effort_minutes)}"></div><div class="field"><label>Target</label><input id="e-target" type="datetime-local"></div><div class="field"><label>Actionable from</label><input id="e-actionable" type="datetime-local"></div><div class="field full"><p class="help">Hard cutoff is intentionally not edited here when reconciliation owns it. This mutation is bound to expected version ${esc(t.version)}.</p></div></div>`,actions:`<button value="cancel" class="button ghost">Cancel</button><button type="button" class="button primary" data-action="save-task-edit" data-id="${esc(id)}" data-version="${t.version}">Save</button>`});
}

async function saveTaskEdit(id, version) {
  const payload = { expected_version:Number(version), remaining_effort_minutes:Number($('#e-remaining').value) };
  if ($('#e-target').value) payload.target_at = toIsoLocal($('#e-target'));
  if ($('#e-actionable').value) payload.actionable_from = toIsoLocal($('#e-actionable'));
  try { await api(`/api/v1/tasks/${encodeURIComponent(id)}`,{method:'PATCH',body:payload}); $('#modal').close(); closeInspector(); invalidate(); toast('Task updated'); navigate('tasks',{refresh:true}); }
  catch(err){ if(err.code==='VERSION_CONFLICT') toast('Task changed on the server. Reloaded current version.',true); else toast(`${err.code}: ${err.message}`,true); invalidate('tasks'); navigate('tasks',{refresh:true}); }
}

function newEventModal() {
  openModal({eyebrow:'Canonical state',title:'New fixed event',body:`<div class="form-grid"><div class="field full"><label>Title</label><input id="ev-title"></div><div class="field"><label>Starts</label><input id="ev-start" type="datetime-local"></div><div class="field"><label>Ends</label><input id="ev-end" type="datetime-local"></div><div class="field"><label>Attendance</label><select id="ev-att"><option>REQUIRED</option><option>OPTIONAL</option><option>PREFERRED</option></select></div><div class="field"><label>Location effect</label><select id="ev-location"><option>NONE</option><option>REMOTE</option></select></div><div class="field"><label>Arrival requirement (min)</label><input id="ev-arrival" type="number" min="0" value="0"></div><div class="field full"><p class="help">The current UI intentionally does not offer FLEXIBLE_WINDOW because the current planner rejects it. STAY/MOVE require existing Place IDs and are inspected from imported/canonical data until a dedicated place picker lands.</p></div></div>`,actions:`<button value="cancel" class="button ghost">Cancel</button><button type="button" class="button primary" data-action="save-event">Create event</button>`});
}

function newRecurringEventModal() {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  openModal({eyebrow:'Canonical recurrence rule',title:'New recurring event',body:`<div class="form-grid"><div class="field full"><label>Title</label><input id="rec-title"></div><div class="field"><label>DTSTART · local civil time</label><input id="rec-start" type="datetime-local"></div><div class="field"><label>Duration (min)</label><input id="rec-duration" type="number" min="1" value="60"></div><div class="field"><label>IANA timezone</label><input id="rec-zone" value="${esc(zone)}"></div><div class="field"><label>RRULE</label><input id="rec-rule" value="FREQ=WEEKLY"></div><div class="field"><label>Attendance</label><select id="rec-att"><option>REQUIRED</option><option>OPTIONAL</option><option>PREFERRED</option></select></div><div class="field"><label>Location effect</label><select id="rec-location"><option>NONE</option><option>REMOTE</option></select></div><div class="field full"><p class="help">Supported RRULE subset: DAILY/WEEKLY with INTERVAL, COUNT or UNTIL. DTSTART stays a local civil time plus IANA timezone; generated occurrences remain derived.</p></div></div>`,actions:`<button value="cancel" class="button ghost">Cancel</button><button type="button" class="button primary" data-action="save-recurring-event">Create series</button>`});
}

async function saveRecurringEvent() {
  const payload={title:$('#rec-title').value.trim(),dtstart_local:$('#rec-start').value ? `${$('#rec-start').value}:00` : null,duration_minutes:Number($('#rec-duration').value),recurrence_rule:$('#rec-rule').value.trim(),timezone_name:$('#rec-zone').value.trim(),attendance_policy:$('#rec-att').value,location_effect:{kind:$('#rec-location').value}};
  try{await api('/api/v1/recurrence/templates',{method:'POST',body:payload});$('#modal').close();invalidate();toast('Recurring series created');navigate('calendar',{refresh:true});}catch(err){toast(`${err.code}: ${err.message}`,true);}
}

function snoozeNotificationModal(id, version) {
  openModal({eyebrow:`Notification workflow · v${version}`,title:'Snooze notification',body:`<div class="form-grid"><div class="field full"><label>Deliver instead at</label><input id="notif-snooze" type="datetime-local"></div><div class="field full"><p class="help">Snooze mutates notification workflow state only. It cannot change a Task cutoff/target or fixed Event time.</p></div></div>`,actions:`<button value="cancel" class="button ghost">Cancel</button><button type="button" class="button primary" data-action="save-notification-snooze" data-id="${esc(id)}" data-version="${esc(version)}">Snooze</button>`});
}

async function saveNotificationSnooze(id, version) {
  try{await api(`/api/v1/notifications/${encodeURIComponent(id)}/snooze`,{method:'POST',body:{until:toIsoLocal($('#notif-snooze')),expected_version:Number(version)}});$('#modal').close();invalidate('settings');toast('Notification snoozed; canonical task/event state unchanged');navigate('settings',{refresh:true});}catch(err){toast(`${err.code}: ${err.message}`,true);}
}

async function saveEvent() {
  const payload={title:$('#ev-title').value.trim(),starts_at:toIsoLocal($('#ev-start')),ends_at:toIsoLocal($('#ev-end')),attendance_policy:$('#ev-att').value,arrival_requirement_minutes:Number($('#ev-arrival').value||0),location_effect:{kind:$('#ev-location').value}};
  try{await api('/api/v1/events',{method:'POST',body:payload});$('#modal').close();invalidate();toast('Event created');navigate('calendar',{refresh:true});}catch(err){toast(`${err.code}: ${err.message}`,true);}
}

async function lifecycle(id, version, action) {
  try { await api(`/api/v1/obligations/${encodeURIComponent(id)}/${encodeURIComponent(action)}`,{method:'POST',body:{expected_version:Number(version)}}); closeInspector(); invalidate(); toast(`${titleCase(action)} applied`); navigate(state.view === 'today' ? 'today' : state.view,{refresh:true}); }
  catch(err){ if(err.code==='VERSION_CONFLICT') toast('Entity changed on the server. Current state was reloaded.',true); else toast(`${err.code}: ${err.message}`,true); invalidate(); navigate(state.view,{refresh:true}); }
}

function accountDeletePreview() {
  const settings = state.data.get('settings');
  const deletion = settings?.deletion;
  if (!deletion) return;
  openModal({
    eyebrow:`Account deletion · ${deletion.policy_version}`,
    title:'Confirm local account deletion',
    body:`<div class="conflict-strip"><strong>This removes the local account immediately.</strong><br>Tasks, events, evidence/provenance, plans, connector state, places, recurrence and notification workflow are purged for this account.</div><dl>${kv('Account id',deletion.account_id)}${kv('Expected server revision',deletion.server_revision)}${kv('Tombstone retention',`${deletion.tombstone_retention_days} days`)}${kv('Retained audit/provenance',deletion.retained_audit_or_provenance?'yes':'no')}${kv('Secret revocation',deletion.secret_revocation)}</dl><div class="field"><label for="account-delete-confirm">Type the exact account id to confirm</label><input id="account-delete-confirm" autocomplete="off" spellcheck="false" placeholder="${esc(deletion.account_id)}"></div><p class="help">The retained tombstone contains only account id, deletion receipt/timestamps, policy version and retention reason. It is not a recoverable copy of your account.</p>`,
    actions:`<button value="cancel" class="button ghost">Keep account</button><button type="button" class="button danger" data-action="account-delete-confirm">Delete local account</button>`,
  });
}

async function accountDeleteConfirm() {
  const settings = state.data.get('settings');
  const deletion = settings?.deletion;
  if (!deletion) return;
  const typed = $('#account-delete-confirm')?.value || '';
  try {
    const result = await api('/api/v1/account/delete',{method:'POST',body:{expected_server_revision:Number(deletion.server_revision),confirm_account_id:typed}});
    $('#modal').close();
    state.data.clear();
    $('#revision-chip').textContent = 'account deleted';
    $('#page-title').textContent = 'Account deleted';
    $('#eyebrow').textContent = 'Data lifecycle';
    $('#workspace').dataset.view = 'settings';
    $('#workspace').dataset.viewState = 'ready';
    $('#workspace').innerHTML = `<section class="section"><div class="panel"><h2>Local account deleted</h2><p>Account-scoped operational/private state was purged.</p><dl>${kv('Deletion id',result.deletion_id)}${kv('Deleted at',fmtDate(result.deleted_at))}${kv('Tombstone until',fmtDate(result.purge_after))}${kv('Policy',result.policy_version)}${kv('Secret revocation',result.secret_revocation_status)}</dl><div class="warning-strip">This UI session is no longer attached to a live account. Restart/reprovision explicitly after the tombstone retention window if you intend to reuse this account id.</div></div></section>`;
  } catch(err) {
    if (err.code === 'VERSION_CONFLICT') toast('Account changed on the server. Reload Settings before deleting.',true);
    else toast(`${err.code}: ${err.message}`,true);
  }
}

async function agentPreview() {
  const select=$('#agent-target'); if(!select) return;
  const option=select.options[select.selectedIndex];
  try{
    const preview=await api('/api/v1/agent/cancel/preview',{method:'POST',body:{obligation_id:select.value,expected_version:Number(option.dataset.version)}});
    state.currentIntent=preview;
    openModal({eyebrow:'Authenticated action intent',title:'Confirm destructive action',body:`<div class="conflict-strip"><strong>${esc(preview.command)}</strong><br>Target: ${esc(preview.target_title)}<br>Scope: ${esc(preview.scope)}</div><dl>${kv('Target id',preview.target_entity_id)}${kv('Expected version',preview.expected_version)}${kv('Effect',preview.effect)}</dl><p class="help">Source/imported text is not authorization. This confirmation is a new authenticated UI action bound to the server-minted intent.</p>`,actions:`<button value="cancel" class="button ghost">Keep obligation</button><button type="button" class="button danger" data-action="agent-confirm">Confirm cancellation</button>`});
  }catch(err){toast(`${err.code}: ${err.message}`,true);}
}

async function agentConfirm() {
  if(!state.currentIntent) return;
  try{await api('/api/v1/agent/cancel/confirm-execute',{method:'POST',body:{intent_id:state.currentIntent.intent_id,idempotency_key:`ui-${state.currentIntent.intent_id}`}});state.currentIntent=null;$('#modal').close();invalidate();toast('Cancellation executed through authenticated action boundary');navigate('ask',{refresh:true});}
  catch(err){toast(`${err.code}: ${err.message}`,true);}
}

async function refreshCurrent() { invalidate(state.view); await navigate(state.view,{refresh:true}); }

document.addEventListener('click', async (event) => {
  const el = event.target.closest('[data-nav],[data-action]');
  if (!el) return;
  if (el.dataset.nav) { event.preventDefault(); navigate(el.dataset.nav); return; }
  const action = el.dataset.action;
  if (action === 'retry') navigate(state.view,{refresh:true});
  else if (action === 'inspect-task') inspectTask(el.dataset.id);
  else if (action === 'inspect-event') inspectEvent(el.dataset.id);
  else if (action === 'inspect-timeline') inspectTimeline(el.dataset.payload);
  else if (action === 'new-task') newTaskModal();
  else if (action === 'save-task') saveTask();
  else if (action === 'edit-task') editTaskModal(el.dataset.id);
  else if (action === 'save-task-edit') saveTaskEdit(el.dataset.id,el.dataset.version);
  else if (action === 'new-event') newEventModal();
  else if (action === 'save-event') saveEvent();
  else if (action === 'new-recurring-event') newRecurringEventModal();
  else if (action === 'save-recurring-event') saveRecurringEvent();
  else if (action === 'snooze-notification') snoozeNotificationModal(el.dataset.id,el.dataset.version);
  else if (action === 'save-notification-snooze') saveNotificationSnooze(el.dataset.id,el.dataset.version);
  else if (action === 'account-delete-preview') accountDeletePreview();
  else if (action === 'account-delete-confirm') accountDeleteConfirm();
  else if (action === 'lifecycle') lifecycle(el.dataset.id,el.dataset.version,el.dataset.lifecycle);
  else if (action === 'agent-preview') agentPreview();
  else if (action === 'agent-confirm') agentConfirm();
});

$('#inspector-close').addEventListener('click', closeInspector);
$('#refresh-button').addEventListener('click', refreshCurrent);
window.addEventListener('hashchange', () => navigate(location.hash.slice(1) || 'today'));

const initial = location.hash.slice(1);
navigate(['today','plan','tasks','calendar','evidence','places','ask','settings'].includes(initial) ? initial : 'today');
