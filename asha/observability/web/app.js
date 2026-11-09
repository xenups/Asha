'use strict';
/* Asha observability dashboard — vanilla ES2020, no dependencies.
   Every value rendered here came from /api/*; nothing is estimated. */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const esc = (v) => (v === null || v === undefined ? '' : String(v));

const state = {
  runId: null,
  meta: {},
  snap: null,
  events: [],
  seen: new Set(),
  cursor: 0,
  es: null,
  conn: 'DISCONNECTED',
  follow: true,
  tab: 'live',
  selectedWorker: null,
  selectedStage: null,
  selectedGraph: null,
  refetchTimer: null,
  gFilter: '',
  gRel: '',
  view: { x: 0, y: 0, k: 1 },
  gview: { x: 0, y: 0, k: 1 },
};

/* ---------- connection ---------- */
function setConn(mode) {
  state.conn = mode;
  const pill = $('conn');
  pill.textContent = mode;
  pill.className = 'conn ' + ({ LIVE: 'live', RECONNECTING: 'reconnecting', ENDED: 'ended' }[mode] || 'off');
}

async function api(path) {
  const res = await fetch(path, { headers: { 'Accept': 'application/json' } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

/* ---------- formatting ---------- */
const fmtTime = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleTimeString([], { hour12: false }) + '.' +
    String(d.getMilliseconds()).padStart(3, '0');
};
const fmtClock = (iso) => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
};
const fmtDur = (ms) => {
  if (ms === null || ms === undefined || isNaN(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s - m * 60)}s`;
};
const fmtAgo = (iso) => {
  if (!iso) return '—';
  const ms = Date.now() - new Date(iso).getTime();
  if (isNaN(ms) || ms < 0) return '—';
  return fmtDur(ms);
};
const shortId = (s) => (s && s.length > 12 ? s.slice(0, 8) + '…' : esc(s));
const list = (v) => (Array.isArray(v) && v.length ? v.map((x) => esc(typeof x === 'string' ? x : JSON.stringify(x))) : []);
const asList = (v) => (Array.isArray(v) ? v : (v ? [String(v)] : []));

function pillFor(status) {
  const s = String(status || '').toUpperCase();
  if (/COMPLETED_CLEAN|COMPLETED|APPROVED|DONE|SEALED|NO_FAILURES|PASSED|OK/.test(s)) return 'ok';
  if (/FAILED|REJECTED|INVALID|VIOLATION|REFUSED|BLOCKED|NEW_FAILURES/.test(s)) return 'bad';
  if (/WARN|UNKNOWN|INCOMPLETE|PRE_EXISTING|NOT_COMPARED|NOT_SEALED|STALE|PARTIAL/.test(s)) return 'warn';
  if (/RUNNING|READY|QUEUED|PENDING|STARTED/.test(s)) return 'run';
  return 'unknown';
}

/* ---------- run loading ---------- */
async function loadRuns() {
  try {
    const data = await api('/api/runs');
    const select = $('runSelect');
    const prev = state.runId;
    select.replaceChildren();
    if (!data.runs.length) {
      select.append(new Option('no runs recorded', ''));
      $('dagMeta').textContent = 'No runs recorded yet. Start a run with ASHA_TELEMETRY=1.';
      return [];
    }
    for (const run of data.runs) {
      const label = `${run.run_id.slice(-17)} · ${run.status} · ${run.workers || 0}w`;
      select.append(new Option(label, run.run_id));
    }
    const wanted = prev || new URLSearchParams(location.search).get('run_id') || data.runs[0].run_id;
    select.value = data.runs.includes(data.runs.find((r) => r.run_id === wanted))
      ? wanted : data.runs[0].run_id;
    return data.runs;
  } catch (err) {
    console.error(err);
    return [];
  }
}

async function openRun(runId, { reset = true } = {}) {
  if (!runId) return;
  if (state.es) { state.es.close(); state.es = null; }
  if (reset) {
    state.events = []; state.seen = new Set(); state.cursor = 0;
    state.selectedWorker = null; state.selectedStage = null; state.selectedGraph = null;
  }
  state.runId = runId;
  state.streamDone = false;
  try {
    state.snap = await api(`/api/runs/${encodeURIComponent(runId)}`);
    try {
      const g = await api(`/api/runs/${encodeURIComponent(runId)}/graph`);
      if (g && g.codegraph && g.codegraph.available) state.snap.codegraph = g.codegraph;
      if (g && g.dag && g.dag.available) state.snap.dag = g.dag;
    } catch { /* graph is optional */ }
  } catch (err) {
    setConn('DISCONNECTED');
    $('liveTail').replaceChildren(el('div', 'lvl-error', `Cannot read run: ${err.message}`));
    // the dashboard server may simply be down: keep trying, never a dead page
    setTimeout(() => openRun(runId, { reset: false }), 3000);
    return;
  }
  state.meta = state.snap.meta || {};
  renderAll();
  startStream();
}

function startStream() {
  if (!state.runId) return;
  setConn('RECONNECTING');
  const url = `/api/stream?run_id=${encodeURIComponent(state.runId)}&after=${state.cursor}`;
  const es = new EventSource(url);
  state.es = es;
  es.onopen = () => setConn('LIVE');
  // every frame is a plain message; the payload carries event_type
  es.onmessage = (ev) => ingest(ev);
  es.addEventListener('end', () => {
    state.streamDone = true;
    setConn('ENDED');
    if (state.es) { state.es.close(); state.es = null; }
    scheduleRefetch();
  });
  es.onerror = () => {
    if (state.streamDone) return;  // a finished run must not reconnect in a loop
    if (es.readyState === 2) { setConn('DISCONNECTED'); setTimeout(() => startStream(), 3000); }
    else setConn('RECONNECTING');
  };
}

function ingest(ev) {
  let payload;
  try { payload = JSON.parse(ev.data); } catch { return; }
  const seq = Number(payload.sequence);
  // duplicate-event protection is per (run_id, sequence): two runs both
  // start at sequence 1, so a bare sequence key would drop real events.
  if (!seq || payload.run_id !== state.runId) return;
  const key = payload.run_id + ':' + seq;
  if (state.seen.has(key)) return;
  state.seen.add(key);
  state.events.push(payload);
  state.cursor = Math.max(state.cursor, seq);
  if (state.events.length > 5000) state.events = state.events.slice(-4000);
  appendLogRow(payload);
  scheduleRefetch();
}

function scheduleRefetch() {
  if (state.refetchTimer) return;
  state.refetchTimer = setTimeout(async () => {
    state.refetchTimer = null;
    try {
      const prev = state.snap;
      state.snap = await api(`/api/runs/${encodeURIComponent(state.runId)}`);
      if (prev) {
        if (prev.codegraph && prev.codegraph.available) state.snap.codegraph = prev.codegraph;
        if (prev.dag && prev.dag.available && !(state.snap.dag || {}).available) state.snap.dag = prev.dag;
      }
      renderAll();
    } catch { /* keep last good snapshot */ }
  }, 700);
}

/* ---------- render: header + stats ---------- */
function renderAll() {
  renderHeader();
  renderStats();
  renderPipeline();
  renderCurrent();
  renderWorkers();
  renderTail();
  renderDag();
  renderCodeGraph();
  renderEvidence();
  renderLog();
  if (state.tab === 'history') renderHistory();
}

function renderHeader() {
  const s = state.snap || {};
  $('runId').textContent = state.runId || '—';
  $('rootPath').textContent = state.meta.root || '';
  $('runId').title = state.runId || '';
  $('rootPath').title = state.meta.root || '';
  const status = s.status || 'UNKNOWN';
  const pill = $('statusPill');
  pill.textContent = status;
  pill.className = 'pill ' + pillFor(status);
  const phases = s.phases || [];
  const running = phases.filter((p) => p.status === 'running');
  const last = phases[phases.length - 1];
  $('phaseName').textContent = running.length ? running[running.length - 1].id : (last ? last.id : '—');
  const start = s.started_at || state.meta.started_at;
  const end = s.ended_at;
  $('elapsed').textContent = end ? fmtDur(s.duration_ms) : (start ? fmtAgo(start) : '—');
}

function renderStats() {
  const s = state.snap || {};
  const workers = Object.values(s.workers || {});
  const count = (st) => workers.filter((w) => w.state === st).length;
  const total = workers.length;
  const done = count('DONE');
  const running = count('RUNNING');
  const failed = count('FAILED') + count('INVALID_EVIDENCE');
  const blocked = count('BLOCKED');
  const pending = count('PENDING') + count('READY') + count('DEFERRED');
  const box = $('stats');
  box.replaceChildren();
  const add = (label, value, cls = '') => {
    const d = el('div', 'stat ' + cls);
    d.append(el('b', '', String(value)), el('span', '', label));
    box.append(d);
  };
  add('workers', total);
  add('completed', done, done ? 'ok' : '');
  add('running', running, running ? 'run' : '');
  add('pending', pending, '');
  add('failed', failed, failed ? 'bad' : '');
  add('blocked', blocked, blocked ? 'warn' : '');
  if (total) {
    const pct = Math.round((done / total) * 100);
    add('progress', `${pct}%`, pct === 100 ? 'ok' : 'run');
    const bar = el('div', 'bar');
    const fill = el('i');
    fill.style.width = `${pct}%`;
    bar.append(fill);
    box.append(bar);
  } else {
    const d = el('div', 'stat');
    d.append(el('b', '', '—'), el('span', '', 'progress indeterminate'));
    box.append(d);
  }
}

/* ---------- render: pipeline ---------- */
const STAGE_DESC = {
  spec: 'Specification loading and validation', planning: 'Planning and dependency analysis',
  dag: 'DAG construction', scheduling: 'Worker scheduling', worktree: 'Worktree preparation',
  baseline: 'Baseline verification', worker: 'Worker execution', scope: 'Scope validation',
  candidate: 'Candidate verification', delta: 'Differential failure analysis',
  evidence: 'Evidence generation and sealing', result: 'Final result',
  codegraph: 'CodeGraph build', context: 'Context slicing',
};

function renderPipeline() {
  const s = state.snap || {};
  const phases = (s.phases || []).slice();
  const dag = s.dag || {};
  if (dag.available && !phases.some((p) => p.id === 'dag')) {
    phases.unshift({ id: 'dag', status: 'done', started_at: s.started_at, workers: [] });
  }
  const box = $('pipeline');
  box.replaceChildren();
  if (!phases.length) {
    box.append(el('div', 'detail empty', 'No phase events yet. Phases are created by the engine, not by this page.'));
    return;
  }
  for (const p of phases) {
    const cls = p.status === 'completed' || p.status === 'done' ? 'done'
      : p.status === 'failed' ? 'bad'
        : p.status === 'running' ? 'run' : 'warn';
    const node = el('div', 'stage ' + cls + (state.selectedStage === p.id ? ' sel' : ''));
    const dot = el('span', 'dot');
    const main = el('div');
    main.append(el('div', 'name', p.id), el('div', 'desc', STAGE_DESC[p.id] || 'engine-reported phase'));
    const meta = el('div', 'meta', `${p.status || '?'} · ${fmtDur(p.duration_ms)}`);
    node.append(dot, main, meta);
    node.onclick = () => { state.selectedStage = state.selectedStage === p.id ? null : p.id; renderPipeline(); renderPhaseDetail(); };
    box.append(node);
  }
  renderPhaseDetail();
}

function renderPhaseDetail() {
  const box = $('phaseDetail');
  const s = state.snap || {};
  const phase = (s.phases || []).find((p) => p.id === state.selectedStage);
  box.replaceChildren();
  if (!phase) { box.append(el('div', 'empty', 'Select a phase for its records.')); return; }
  const dl = el('dl');
  const row = (k, v) => { dl.append(el('dt', '', k), el('dd', '', v)); };
  row('status', phase.status || '—');
  row('started', fmtClock(phase.started_at));
  row('duration', fmtDur(phase.duration_ms));
  row('workers', (phase.workers || []).join(', ') || '—');
  row('last message', phase.last_message || '—');
  box.append(dl);
}

/* ---------- render: current worker ---------- */
function renderCurrent() {
  const s = state.snap || {};
  const workers = Object.values(s.workers || {});
  const active = workers.filter((w) => w.state === 'RUNNING');
  const box = $('currentWorker');
  box.replaceChildren();
  if (!active.length) {
    box.append(el('div', 'empty', workers.length ? 'No worker is running.' : 'No worker has been reported yet.'));
    return;
  }
  for (const w of active) {
    const head = el('h3', '', w.id);
    box.append(head);
    const dl = el('dl');
    const row = (k, v) => { dl.append(el('dt', '', k), el('dd', '', v)); };
    row('task', w.task || '—');
    row('current operation', w.last_message || w.last_event || '—');
    row('files', list(w.writes).join(', ') || list(w.reads).join(', ') || '—');
    row('dependencies', (w.deps || []).join(', ') || '—');
    row('elapsed', w.started_at ? fmtAgo(w.started_at) : '—');
    box.append(dl);
    box.append(el('div', 'indet', 'Only observable engine actions are shown; worker-internal reasoning is not visible.'));
  }
}

/* ---------- render: worker table ---------- */
function renderWorkers() {
  const s = state.snap || {};
  const workers = Object.values(s.workers || {}).sort((a, b) => a.id.localeCompare(b.id));
  const wrap = $('workerTable');
  wrap.replaceChildren();
  if (!workers.length) { wrap.append(el('div', 'detail empty', 'No workers yet.')); return; }
  const table = el('table');
  const head = el('tr');
  for (const h of ['Worker', 'State', 'Task', 'Duration', 'Reason']) head.append(el('th', '', h));
  table.append(head);
  for (const w of workers) {
    const tr = el('tr', 'clickable');
    tr.append(el('td', 'mono', w.id));
    const td = el('td');
    const pill = el('span', 'pill ' + pillFor(w.state), w.state);
    td.append(pill);
    tr.append(td);
    tr.append(el('td', '', w.task || '—'));
    tr.append(el('td', 'mono', fmtDur(w.duration_ms)));
    tr.append(el('td', 'dim', w.reason || '—'));
    tr.onclick = () => { state.selectedWorker = w.id; selectTab('dag'); renderDag(); };
    table.append(tr);
  }
  wrap.append(table);
}

/* ---------- render: live tail ---------- */
function renderTail() {
  const box = $('liveTail');
  box.replaceChildren();
  for (const ev of state.events.slice(-40)) box.append(tailRow(ev));
  box.scrollTop = box.scrollHeight;
}

function tailRow(ev) {
  const row = el('div', 'lvl-' + severityOf(ev));
  row.append(el('span', 'ts', fmtTime(ev.timestamp)));
  row.append(el('span', 'et', ev.event_type));
  row.append(document.createTextNode(' ' + (ev.worker_id ? `[${ev.worker_id}] ` : '') + (ev.message || '')));
  return row;
}

const severityOf = (ev) => {
  const t = ev.event_type || '';
  if (t === 'log.error' || t === 'execution.error' || t === 'evidence.rejected' || t.endsWith('.failed')) return 'error';
  if (t === 'log.warning' || t === 'execution.warning' || t === 'context_slice.incomplete') return 'warning';
  return 'info';
};

/* ---------- DAG ---------- */
function dagLayout(dag) {
  const nodes = (dag.nodes || []).filter((n) => n && n.id);
  const edges = dag.edges || {};
  const ids = new Set(nodes.map((n) => n.id));
  const preds = new Map();
  for (const n of nodes) {
    const deps = (edges[n.id] || n.deps || []).filter((d) => ids.has(d));
    preds.set(n.id, deps);
  }
  const depth = new Map();
  const visit = (id, seen) => {
    if (depth.has(id)) return depth.get(id);
    if (seen.has(id)) return 0; // cycle: never inflate depth
    seen.add(id);
    const d = preds.get(id) || [];
    const value = d.length ? 1 + Math.max(...d.map((p) => visit(p, seen))) : 0;
    depth.set(id, value);
    return value;
  };
  const gens = new Map();
  for (const n of nodes) {
    const d = visit(n.id, new Set());
    if (!gens.has(d)) gens.set(d, []);
    gens.get(d).push(n.id);
  }
  const pos = new Map();
  const NW = 168, NH = 44, GX = 120, GY = 30;
  for (const [gen, idsInGen] of [...gens.entries()].sort((a, b) => a[0] - b[0])) {
    idsInGen.sort();
    idsInGen.forEach((id, i) => {
      pos.set(id, { x: gen * (NW + GX), y: i * (NH + GY), gen });
    });
  }
  return { nodes, pos, preds, NW, NH, gens: gens.size, edges: dag.edges || {} };
}

function svgInit(svg) {
  svg.replaceChildren();
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  defs.innerHTML = '<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#4b5b86"/></marker>';
  svg.append(defs);
  return defs;
}

function makePanZoom(svg, view, after) {
  let drag = null;
  svg.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.node')) return;
    drag = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
    svg.classList.add('drag'); svg.setPointerCapture(e.pointerId);
  });
  svg.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const rect = svg.getBoundingClientRect();
    view.x = drag.vx + ((e.clientX - drag.x) * rect.width) / (view.w || rect.width);
    view.y = drag.vy + ((e.clientY - drag.y) * rect.height) / (view.h || rect.height);
    after();
  });
  const stop = () => { drag = null; svg.classList.remove('drag'); };
  svg.addEventListener('pointerup', stop);
  svg.addEventListener('pointercancel', stop);
  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = svg.getBoundingClientRect();
    const px = view.x + ((e.clientX - rect.left) * (view.w || rect.width)) / rect.width;
    const py = view.y + ((e.clientY - rect.top) * (view.h || rect.height)) / rect.height;
    const k = Math.exp(-e.deltaY * 0.0016);
    view.w = (view.w || rect.width) / k;
    view.h = (view.h || rect.height) / k;
    view.x = px - (px - view.x) / k;
    view.y = py - (py - view.y) / k;
    after();
  }, { passive: false });
}

function renderDag() {
  const s = state.snap || {};
  const dag = s.dag || {};
  const svg = $('dagSvg');
  svgInit(svg);
  if (!dag.available || !(dag.nodes || []).length) {
    $('dagMeta').textContent = 'No DAG event in this run. The graph is only shown when the orchestrator emitted dag.created.';
    return;
  }
  const layout = dagLayout(dag);
  const states = s.workers || {};
  const rect = svg.getBoundingClientRect();
  if (!state.view.w) { state.view = { x: 0, y: 0, w: rect.width || 1200, h: rect.height || 520 }; }
  const view = state.view;
  svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`);
  $('dagMeta').textContent = `${layout.nodes.length} workers · ${layout.gens} generations · ${Object.keys(layout.edges).length} nodes with dependencies · generation ${dag.generation ?? '—'}`;
  const gEdges = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  const gNodes = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  svg.append(gEdges, gNodes);

  const pad = 80;
  const visible = (p) => p.x + layout.NW > view.x - pad && p.x < view.x + view.w + pad &&
    p.y + layout.NH > view.y - pad && p.y < view.y + view.h + pad;
  const sel = state.selectedWorker;

  for (const [id, p] of layout.pos.entries()) {
    const deps = layout.preds.get(id) || [];
    for (const dep of deps) {
      const from = layout.pos.get(dep);
      if (!from) continue;
      if (!visible(p) && !visible(from)) continue;
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      const x1 = from.x + layout.NW, y1 = from.y + layout.NH / 2;
      const x2 = p.x, y2 = p.y + layout.NH / 2;
      const mid = (x1 + x2) / 2;
      line.setAttribute('d', `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`);
      line.setAttribute('class', 'edge' + (sel && (id === sel || dep === sel) ? ' hl' : ''));
      if (sel && id !== sel && dep !== sel) line.style.opacity = '.25';
      gEdges.append(line);
    }
  }

  for (const node of layout.nodes) {
    const p = layout.pos.get(node.id);
    if (!p || !visible(p)) continue;
    const w = states[node.id] || {};
    const st = String(w.state || node.state || 'PENDING').toUpperCase();
    const cls = /^(PENDING|READY|RUNNING|DONE|FAILED|BLOCKED|CANCELLED)$/.test(st)
      ? st.toLowerCase() : 'unknown';
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    g.setAttribute('class', `node ${cls}` + (sel === node.id ? ' sel' : '') + (sel && sel !== node.id ? ' dimmed' : ''));
    g.setAttribute('transform', `translate(${p.x},${p.y})`);
    const rectEl = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
    rectEl.setAttribute('width', layout.NW);
    rectEl.setAttribute('height', layout.NH);
    rectEl.setAttribute('rx', '8');
    const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    label.setAttribute('x', '10'); label.setAttribute('y', '18');
    label.textContent = node.id.length > 24 ? node.id.slice(0, 23) + '…' : node.id;
    const sub = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    sub.setAttribute('x', '10'); sub.setAttribute('y', '33');
    sub.setAttribute('fill', '#8b93ad'); sub.setAttribute('font-size', '10');
    sub.textContent = `${st}${w.duration_ms ? ' · ' + fmtDur(w.duration_ms) : ''}`;
    g.append(rectEl, label, sub);
    g.addEventListener('click', (e) => {
      e.stopPropagation();
      state.selectedWorker = node.id;
      renderDag(); renderDagDetail();
    });
    gNodes.append(g);
  }
  makePanZoom(svg, view, renderDag);
}

function renderDagDetail() {
  const box = $('dagDetail');
  const s = state.snap || {};
  const dag = s.dag || {};
  const node = (dag.nodes || []).find((n) => n.id === state.selectedWorker);
  const w = (s.workers || {})[state.selectedWorker] || {};
  box.replaceChildren();
  if (!node && !w.id) { box.append(el('div', 'empty', 'Select a node.')); return; }
  const dl = el('dl');
  const row = (k, v) => { dl.append(el('dt', '', k), el('dd', '', v)); };
  row('worker', state.selectedWorker || '—');
  row('state', w.state || 'UNKNOWN');
  row('task', node ? node.task || w.task || '—' : w.task || '—');
  const edges = dag.edges || {};
  row('dependencies', ((edges[state.selectedWorker] || (node && node.deps) || [])).join(', ') || 'none');
  row('reads', list(node && node.reads).join(', ') || list(w.reads).join(', ') || '—');
  row('writes', list(node && node.writes).join(', ') || list(w.writes).join(', ') || '—');
  row('declared scope', list(node && node.declared_scope).join(', ') || list(w.declared_scope).join(', ') || '—');
  row('started', fmtClock(w.started_at));
  row('duration', fmtDur(w.duration_ms));
  row('last action', w.last_message || '—');
  row('reason', w.reason || '—');
  box.append(dl);
}

/* ---------- CodeGraph ---------- */
function graphLayout(cg) {
  const nodes = (cg.nodes || []).filter((n) => n && n.id);
  const rels = (cg.relations || []).filter((r) => r && r.from && r.to);
  const byModule = new Map();
  nodes.forEach((n, i) => {
    const mod = n.module || '—';
    if (!byModule.has(mod)) byModule.set(mod, []);
    byModule.get(mod).push({ ...n, i });
  });
  const pos = new Map();
  const mods = [...byModule.keys()].sort();
  const R = Math.max(260, mods.length * 70);
  mods.forEach((mod, mi) => {
    const angle = (mi / Math.max(1, mods.length)) * Math.PI * 2;
    const cx = 600 + Math.cos(angle) * R;
    const cy = 360 + Math.sin(angle) * R * 0.55;
    byModule.get(mod).forEach((n, ni) => {
      const a = (ni / Math.max(1, byModule.get(mod).length)) * Math.PI * 2;
      const r = 30 + (ni % 3) * 26;
      pos.set(n.id, { x: cx + Math.cos(a) * r, y: cy + Math.sin(a) * r * 0.7, module: mod });
    });
  });
  return { nodes, rels, pos, mods };
}

function renderCodeGraph() {
  const s = state.snap || {};
  const cg = s.codegraph || {};
  const svg = $('gSvg');
  svgInit(svg);
  const meta = $('gMeta');
  if (!cg.available) {
    meta.textContent = `CodeGraph unavailable — ${cg.reason || 'no CodeGraph event in this run'} (state ${cg.state || 'UNKNOWN'}). Nothing is substituted.`;
    return;
  }
  const layout = graphLayout(cg);
  const rect = svg.getBoundingClientRect();
  if (!state.gview.w) state.gview = { x: 0, y: 0, w: rect.width || 1200, h: rect.height || 520 };
  const view = state.gview;
  svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`);
  const counts = layout.nodes.length;
  meta.textContent = `state ${cg.state || 'UNKNOWN'} · ${counts} nodes · ${layout.rels.length} relations · ${layout.mods.length} modules · source ${cg.source || '—'}` +
    (cg.state === 'COMPLETE' ? '' : ' — incompleteness is not safety: UNKNOWN is never rendered as SAFE');
  const gEdges = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  const gNodes = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  svg.append(gEdges, gNodes);
  const needle = state.gFilter.toLowerCase();
  const relFilter = state.gRel;
  const matches = (n) => !needle || `${n.id} ${n.module || ''} ${n.kind || ''}`.toLowerCase().includes(needle);
  const sel = state.selectedGraph;

  if (relFilter && $('gRel').options.length < 2) {
    for (const r of new Set(layout.rels.map((x) => x.rel))) $('gRel').append(new Option(r, r));
  }
  const visible = (p) => p.x + 200 > view.x && p.x - 40 < view.x + view.w && p.y + 100 > view.y && p.y - 40 < view.y + view.h;

  for (const r of layout.rels) {
    if (relFilter && r.rel !== relFilter) continue;
    const a = layout.pos.get(r.from); const b = layout.pos.get(r.to);
    if (!a || !b) continue;
    if (!visible(a) && !visible(b)) continue;
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', a.x); line.setAttribute('y1', a.y);
    line.setAttribute('x2', b.x); line.setAttribute('y2', b.y);
    const rel = String(r.rel || 'relation').toLowerCase();
    line.setAttribute('class', `edge rel-${rel}` + (sel && (r.from === sel || r.to === sel) ? ' hl' : ''));
    if (sel && r.from !== sel && r.to !== sel) line.style.opacity = '.15';
    gEdges.append(line);
  }
  for (const n of layout.nodes) {
    const p = layout.pos.get(n.id);
    if (!p || !visible(p)) continue;
    const ok = matches(n);
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    const conf = String(n.confidence || 'UNKNOWN').toUpperCase();
    const cls = conf === 'COMPLETE' ? 'done' : conf === 'INCOMPLETE' ? 'blocked' : 'unknown';
    g.setAttribute('class', `node ${cls}` + (sel === n.id ? ' sel' : '') + (ok ? '' : ' dimmed'));
    g.setAttribute('transform', `translate(${p.x},${p.y})`);
    const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    c.setAttribute('r', '7');
    const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    label.setAttribute('x', '12'); label.setAttribute('y', '4');
    label.textContent = n.id.length > 26 ? n.id.slice(0, 25) + '…' : n.id;
    g.append(c, label);
    g.addEventListener('click', (e) => {
      e.stopPropagation();
      state.selectedGraph = n.id;
      renderCodeGraph(); renderGraphDetail();
    });
    gNodes.append(g);
  }
  makePanZoom(svg, view, renderCodeGraph);
  $('gFit').onclick = () => {
    state.gview = { x: -100, y: -100, w: 1500, h: 700 };
    renderCodeGraph();
  };
}

function renderGraphDetail() {
  const s = state.snap || {};
  const cg = s.codegraph || {};
  const id = state.selectedGraph;
  const box = $('gDetail');
  box.replaceChildren();
  const node = (cg.nodes || []).find((n) => n.id === id);
  if (!node) { box.append(el('div', 'empty', 'Select a node.')); return; }
  const rels = (cg.relations || []).filter((r) => r.from === id || r.to === id);
  const dl = el('dl');
  const row = (k, v) => { dl.append(el('dt', '', k), el('dd', '', v)); };
  row('node', node.id);
  row('kind', node.kind || '—');
  row('module', node.module || '—');
  row('confidence', node.confidence || 'UNKNOWN');
  row('incoming', rels.filter((r) => r.to === id).map((r) => `${r.from} (${r.rel})`).join(', ') || 'none');
  row('outgoing', rels.filter((r) => r.from === id).map((r) => `${r.to} (${r.rel})`).join(', ') || 'none');
  box.append(dl);
}

/* ---------- evidence ---------- */
function renderEvidence() {
  const s = state.snap || {};
  const v = s.verdict;
  const vbox = $('verdictBox');
  vbox.replaceChildren();
  if (!v) {
    vbox.append(el('div', 'empty', 'No verdict event recorded in this run.'));
  } else {
    const dl = el('dl');
    const row = (k, val) => { dl.append(el('dt', '', k), el('dd', '', val)); };
    row('verdict', v.status || 'UNKNOWN');
    row('reason', v.reason || '—');
    row('new failures', asList(v.new_failures).join(', ') || 'none');
    row('pre-existing', asList(v.pre_existing).join(', ') || 'none');
    row('unparsed', asList(v.unparsed).join(', ') || 'none');
    vbox.append(dl);
    const note = el('div', 'notice', 'A verdict describes Asha\'s comparison outcome. A sealed evidence document is not proof that the code is correct, and this dashboard never authorizes shipping.');
    vbox.append(note);
  }

  const seal = s.evidence;
  const sbox = $('sealBox');
  sbox.replaceChildren();
  if (!seal) {
    sbox.append(el('div', 'empty', 'No evidence event in this run.'));
  } else {
    const dl = el('dl');
    const row = (k, val) => { dl.append(el('dt', '', k), el('dd', '', val)); };
    row('state', seal.event === 'evidence.sealed' ? 'SEALED' : (seal.status || 'NOT_SEALED'));
    row('sha256', seal.sha256 || '—');
    row('document', seal.path ? String(seal.path).split('/').slice(-2).join('/') : '—');
    row('reason', seal.reason || '—');
    sbox.append(dl);
  }

  const scope = s.scope;
  const scbox = $('scopeBox');
  scbox.replaceChildren();
  if (!scope) scbox.append(el('div', 'empty', 'No scope validation event.'));
  else {
    const dl = el('dl');
    const row = (k, val) => { dl.append(el('dt', '', k), el('dd', '', val)); };
    row('status', scope.status || '—');
    row('violations', asList(scope.violations).join(', ') || 'none');
    row('files', String((scope.files || []).length));
    scbox.append(dl);
  }

  const cl = $('checkList');
  cl.replaceChildren();
  const checks = (s.checks || []).slice(-200).reverse();
  if (!checks.length) cl.append(el('div', 'detail empty', 'No check events.'));
  else {
    const t = el('table');
    const head = el('tr');
    for (const h of ['Check', 'Status', 'Exit', 'Worker']) head.append(el('th', '', h));
    t.append(head);
    for (const c of checks) {
      const tr = el('tr');
      tr.append(el('td', 'mono', c.name || '—'));
      const td = el('td');
      td.append(el('span', 'pill ' + pillFor(c.status), c.status || '—'));
      tr.append(td);
      tr.append(el('td', 'mono', c.exit_code ?? '—'));
      tr.append(el('td', 'mono', c.worker_id || '—'));
      t.append(tr);
    }
    cl.append(t);
  }

  const wv = $('workerVerdicts');
  wv.replaceChildren();
  const rows = Object.values(s.workers || {}).filter((w) => w.verdict || w.evidence || w.state);
  if (!rows.length) wv.append(el('div', 'detail empty', 'No worker verdicts yet.'));
  else {
    const t = el('table');
    const head = el('tr');
    for (const h of ['Worker', 'State', 'Verdict', 'Evidence', 'Reason']) head.append(el('th', '', h));
    t.append(head);
    for (const w of rows) {
      const tr = el('tr');
      tr.append(el('td', 'mono', w.id));
      tr.append(el('td', '', w.state || '—'));
      tr.append(el('td', '', w.verdict || 'NOT_REPORTED'));
      tr.append(el('td', 'mono', w.evidence || '—'));
      tr.append(el('td', 'dim', w.reason || '—'));
      t.append(tr);
    }
    wv.append(t);
  }
}

/* ---------- log ---------- */
function renderLog() {
  const box = $('logList');
  const sev = $('logSeverity').value;
  const worker = $('logWorker').value;
  const q = $('logSearch').value.toLowerCase();
  box.replaceChildren();
  for (const ev of state.events) {
    if (sev && severityOf(ev) !== sev) continue;
    if (worker && ev.worker_id !== worker) continue;
    if (q && !JSON.stringify(ev).toLowerCase().includes(q)) continue;
    box.append(logRow(ev));
  }
  if (!box.children.length) box.append(el('div', 'ev', 'No events match the filters.'));
  if (state.follow) box.scrollTop = box.scrollHeight;
  const workers = new Set(state.events.map((e) => e.worker_id).filter(Boolean));
  const sel = $('logWorker');
  const current = sel.value;
  sel.replaceChildren(new Option('all workers', ''));
  for (const w of [...workers].sort()) sel.append(new Option(w, w));
  sel.value = current;
}

function logRow(ev) {
  const row = el('div', 'ev');
  const head = el('div', 'head');
  head.append(el('span', 'ts', fmtTime(ev.timestamp)));
  const lvl = severityOf(ev);
  head.append(el('span', 'tag ' + (lvl === 'error' ? 'red' : lvl === 'warning' ? 'yellow' : 'purple'), lvl));
  head.append(el('span', 'type', ev.event_type));
  if (ev.worker_id) head.append(el('span', 'tag', ev.worker_id));
  if (ev.status) head.append(el('span', 'tag green', ev.status));
  head.append(el('span', 'dim', ev.message || ''));
  const body = el('div', 'body', JSON.stringify(ev, null, 1));
  row.append(head, body);
  row.onclick = () => row.classList.toggle('open');
  return row;
}

function appendLogRow(ev) {
  if (state.tab !== 'log') return;
  const box = $('logList');
  const sev = $('logSeverity').value;
  const worker = $('logWorker').value;
  const q = $('logSearch').value.toLowerCase();
  if (sev && severityOf(ev) !== sev) return;
  if (worker && ev.worker_id !== worker) return;
  if (q && !JSON.stringify(ev).toLowerCase().includes(q)) return;
  box.append(logRow(ev));
  if (state.follow) box.scrollTop = box.scrollHeight;
}

/* ---------- history ---------- */
async function renderHistory() {
  const wrap = $('historyTable');
  let data;
  try { data = await api('/api/runs'); } catch { return; }
  wrap.replaceChildren();
  $('histCount').textContent = `${data.runs.length} run(s) kept`;
  if (!data.runs.length) { wrap.append(el('div', 'detail empty', 'No runs recorded.')); return; }
  const t = el('table');
  const head = el('tr');
  for (const h of ['Run', 'State', 'Verdict', 'Workers', 'Started', 'Duration', 'Repo']) head.append(el('th', '', h));
  t.append(head);
  for (const r of data.runs) {
    const tr = el('tr', 'clickable');
    tr.append(el('td', 'mono', r.run_id));
    const td = el('td'); td.append(el('span', 'pill ' + pillFor(r.status), r.status || '—'));
    tr.append(td);
    tr.append(el('td', '', r.verdict || 'NOT_REPORTED'));
    tr.append(el('td', 'mono', String(r.workers || 0)));
    tr.append(el('td', 'mono', fmtClock(r.started_at)));
    tr.append(el('td', 'mono', r.ended_at ? fmtDur(new Date(r.ended_at) - new Date(r.started_at)) : '—'));
    tr.append(el('td', 'dim trunc', r.root || '—'));
    tr.onclick = async () => {
      selectTab('live');
      await openRun(r.run_id);
      $('runSelect').value = r.run_id;
    };
    t.append(tr);
  }
  wrap.append(t);
}

/* ---------- tabs + wiring ---------- */
function selectTab(name) {
  state.tab = name;
  for (const b of document.querySelectorAll('.tabs button')) b.classList.toggle('active', b.dataset.tab === name);
  for (const pane of document.querySelectorAll('.tabpane')) pane.classList.toggle('active', pane.id === `tab-${name}`);
  if (name === 'dag') { state.view = {}; renderDag(); renderDagDetail(); }
  if (name === 'graph') { state.gview = {}; renderCodeGraph(); renderGraphDetail(); }
  if (name === 'evidence') renderEvidence();
  if (name === 'log') renderLog();
  if (name === 'history') renderHistory();
}
for (const b of document.querySelectorAll('.tabs button')) b.onclick = () => selectTab(b.dataset.tab);

$('runSelect').onchange = (e) => openRun(e.target.value);
$('logSearch').oninput = renderLog;
$('logSeverity').onchange = renderLog;
$('logWorker').onchange = renderLog;
$('logFollow').onclick = () => {
  state.follow = !state.follow;
  $('logFollow').classList.toggle('on', state.follow);
  $('logFollow').textContent = state.follow ? 'Auto-scroll' : 'Paused';
};
$('logCopy').onclick = async () => {
  const text = state.events.map((e) => JSON.stringify(e)).join('\n');
  try { await navigator.clipboard.writeText(text); $('logCopy').textContent = 'Copied'; }
  catch { $('logCopy').textContent = 'Copy failed'; }
  setTimeout(() => { $('logCopy').textContent = 'Copy'; }, 1500);
};
$('gSearch').oninput = (e) => { state.gFilter = e.target.value; renderCodeGraph(); };
$('gRel').onchange = (e) => { state.gRel = e.target.value; renderCodeGraph(); };
$('gFit').onclick = () => { state.gview = {}; renderCodeGraph(); };
$('dagFit').onclick = () => { state.view = {}; renderDag(); };
$('dagZoomIn').onclick = () => { const v = state.view; v.w /= 1.25; v.h /= 1.25; renderDag(); };
$('dagZoomOut').onclick = () => { const v = state.view; v.w *= 1.25; v.h *= 1.25; renderDag(); };

/* follow a newly started run: the dashboard can be open first */
async function followLiveRun() {
  if (!state.snap || !state.runId) return;
  if (!['RUNNING', 'INTERRUPTED', 'UNKNOWN'].includes(String(state.snap.status))) return;
  try {
    const data = await api('/api/runs');
    const live = data.runs.find((r) => r.run_id !== state.runId && r.status === 'RUNNING');
    const stillListed = data.runs.some((r) => r.run_id === state.runId);
    if (live) {
      $('runSelect').value = live.run_id;
      await openRun(live.run_id);
    } else if (!stillListed) {
      await loadRuns();
    }
  } catch { /* server gone: keep the current view */ }
}
setInterval(followLiveRun, 3000);

/* elapsed ticks while a run is live */
setInterval(() => {
  if (!state.snap) return;
  const s = state.snap;
  if (s.ended_at) return;
  const start = s.started_at || state.meta.started_at;
  if (start) $('elapsed').textContent = fmtAgo(start);
}, 1000);

(async function boot() {
  const runs = await loadRuns();
  const wanted = new URLSearchParams(location.search).get('run_id') ||
    (runs.length ? $('runSelect').value : null);
  $('footStats').textContent = runs.length ? `${runs.length} run(s) on disk` : 'no runs on disk';
  if (wanted) await openRun(wanted);
  else {
    $('liveTail').replaceChildren(el('div', 'lvl-warning',
      'No run recorded yet. Run: ASHA_TELEMETRY=1 asha run --spec <file> (or any asha command) and reload.'));
  }
})();
