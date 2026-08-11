// Ollie Mission Control — the persistent SHELL.
//
// Owns: token mgmt, the bearer `api()` fetch, toast/esc, the STATUS STRIP +
// 7-pill HEALTH RAIL (polled via /api/health), the ACTIVITY FEED (SSE), the
// OVERVIEW tile grid, and the PANEL REGISTRY that drives nav + body + per-
// panel refresh. Panels never edit this file — they call registerPanel() /
// registerTile() at import time (see PANELS.md).

// ── Token management ──────────────────────────────────────────────────────
export let TOKEN = localStorage.getItem('ce_token') || '';
const modal = document.getElementById('token-modal');

export function saveToken() {
  const t = document.getElementById('token-input').value.trim();
  if (!t) { document.getElementById('token-err').textContent = 'Token required'; return; }
  TOKEN = t;
  localStorage.setItem('ce_token', t);
  modal.style.display = 'none';
  init();
}

export function logout() {
  localStorage.removeItem('ce_token');
  TOKEN = '';
  modal.style.display = 'flex';
  document.getElementById('token-input').value = '';
  _stopActivityStream();
}

// ── API helpers ───────────────────────────────────────────────────────────
export async function api(method, path, body) {
  const opts = {
    method,
    headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' }
  };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  if (r.status === 401) { logout(); throw new Error('401'); }
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error((data && data.error) || r.statusText);
  return data;
}

export function toast(msg, ok = true) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.borderColor = ok ? 'var(--ok)' : 'var(--danger)';
  el.style.display = 'block';
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.display = 'none'; }, 3000);
}

// ── Util ──────────────────────────────────────────────────────────────────
export function esc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
                  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ════════════════════════════════════════════════════════════════════════
//  PANEL + TILE REGISTRY  (the drop-in contract)
// ════════════════════════════════════════════════════════════════════════
//
// registerPanel({ id, title, group, render(el, api), refreshMs, onActivate })
//   id        unique slug (also the body container id: panel-<id>)
//   title     nav label
//   group     nav group heading (e.g. 'Curiosity', 'System'); optional
//   render    (el, api) => void   build the panel into `el` (called once)
//   refreshMs poll cadence in ms while ACTIVE (omit/0 = no auto-refresh)
//   onActivate() optional hook fired each time the panel is shown
//
// registerTile({ id, render(el, api), refreshMs })
//   Overview grid card. Same lifecycle, rendered into the overview grid.

const PANELS = [];
const TILES = [];
const _panelById = {};

export function registerPanel(def) {
  if (!def || !def.id) { console.warn('registerPanel: missing id', def); return; }
  if (_panelById[def.id]) { console.warn('registerPanel: duplicate id', def.id); return; }
  const panel = {
    id: def.id,
    title: def.title || def.id,
    group: def.group || '',
    render: def.render || (() => {}),
    refreshMs: def.refreshMs || 0,
    onActivate: def.onActivate || null,
    _rendered: false,
  };
  PANELS.push(panel);
  _panelById[def.id] = panel;
}

export function registerTile(def) {
  if (!def || !def.id) { console.warn('registerTile: missing id', def); return; }
  TILES.push({
    id: def.id,
    render: def.render || (() => {}),
    refreshMs: def.refreshMs || 0,
    _rendered: false,
  });
}

// ── Active-panel refresh driver (only the visible panel polls) ──────────────
let _activePanel = 'overview';
let _refreshTimer = null;

function _stopRefresh() {
  if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
}

function _startRefresh(panel, el) {
  _stopRefresh();
  if (panel && panel.refreshMs > 0) {
    _refreshTimer = setInterval(() => {
      try { panel.render(el, api); } catch (e) { /* panel guards its own errors */ }
    }, panel.refreshMs);
  }
}

// ── Build nav + body containers from the registry ───────────────────────────
function buildShell() {
  const nav = document.getElementById('mc-nav');
  const body = document.getElementById('mc-body');
  if (!nav || !body) return;

  // Overview is always first.
  nav.innerHTML = '';
  body.innerHTML = '';

  const addNav = (id, label) => {
    const d = document.createElement('div');
    d.className = 'mc-tab';
    d.dataset.panel = id;
    d.textContent = label;
    d.onclick = () => showPanel(id);
    nav.appendChild(d);
    return d;
  };

  addNav('overview', 'Overview');

  // Overview body container (the tile grid).
  const ov = document.createElement('div');
  ov.id = 'panel-overview';
  ov.className = 'mc-panel';
  ov.innerHTML = '<div class="mc-tile-grid" id="mc-tile-grid"></div>';
  body.appendChild(ov);

  // Group panels by their `group` heading (stable insertion order).
  const groups = {};
  const groupOrder = [];
  PANELS.forEach(p => {
    const g = p.group || '·';
    if (!(g in groups)) { groups[g] = []; groupOrder.push(g); }
    groups[g].push(p);
  });
  groupOrder.forEach(g => {
    if (g && g !== '·') {
      const h = document.createElement('div');
      h.className = 'mc-nav-group';
      h.textContent = g;
      nav.appendChild(h);
    }
    groups[g].forEach(p => {
      addNav(p.id, p.title);
      const el = document.createElement('div');
      el.id = 'panel-' + p.id;
      el.className = 'mc-panel';
      body.appendChild(el);
    });
  });
}

// ── Overview tiles ──────────────────────────────────────────────────────────
function renderTiles() {
  const grid = document.getElementById('mc-tile-grid');
  if (!grid) return;
  TILES.forEach(t => {
    let cell = document.getElementById('tile-' + t.id);
    if (!cell) {
      cell = document.createElement('div');
      cell.id = 'tile-' + t.id;
      cell.className = 'mc-tile';
      grid.appendChild(cell);
    }
    try { t.render(cell, api); } catch (e) { /* tile guards its own errors */ }
  });
}

let _tileTimer = null;
function startTileRefresh() {
  if (_tileTimer) clearInterval(_tileTimer);
  // Single shared cadence for the overview (tiles idle when not shown).
  _tileTimer = setInterval(() => { if (_activePanel === 'overview') renderTiles(); }, 15000);
}
function stopTileRefresh() { if (_tileTimer) { clearInterval(_tileTimer); _tileTimer = null; } }

// ── Panel switching (nav / pill / tile click target) ────────────────────────
export function showPanel(id) {
  _activePanel = id;
  document.querySelectorAll('.mc-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.panel === id));
  document.querySelectorAll('.mc-panel').forEach(p => p.classList.remove('active'));
  const el = document.getElementById('panel-' + id);
  if (el) el.classList.add('active');

  _stopRefresh();
  if (id === 'overview') {
    renderTiles();
    return;
  }
  const panel = _panelById[id];
  if (panel && el) {
    if (!panel._rendered) { try { panel.render(el, api); } catch (e) {} panel._rendered = true; }
    else { try { panel.render(el, api); } catch (e) {} }
    if (panel.onActivate) { try { panel.onActivate(el, api); } catch (e) {} }
    _startRefresh(panel, el);
  }
}

// ════════════════════════════════════════════════════════════════════════
//  STATUS STRIP + 7-PILL HEALTH RAIL  (polled /api/health)
// ════════════════════════════════════════════════════════════════════════
import { LADDER, ladder } from './components.js';

const PILL_ORDER = ['gateway', 'hands', 'factcheck', 'jobs', 'watchdog', 'curiosity', 'lab'];

function buildRail() {
  const rail = document.getElementById('mc-rail');
  if (!rail) return;
  rail.innerHTML = '';
  PILL_ORDER.forEach(name => {
    const pill = document.createElement('button');
    pill.className = 'mc-pill';
    pill.dataset.pill = name;
    pill.innerHTML = `<span class="mc-pill-glyph">◌</span><span class="mc-pill-name">${esc(name)}</span>`;
    // Clicking a pill opens that drill-down panel if one is registered.
    pill.onclick = () => { if (_panelById[name]) showPanel(name); };
    rail.appendChild(pill);
  });
}

let _pollTimer = null;
let _pulse = false;

function _ageStr(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (isNaN(t)) return '—';
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (sec < 60) return sec + 's';
  if (sec < 3600) return Math.round(sec / 60) + 'm';
  return Math.round(sec / 3600) + 'h';
}

const VERDICT_STATE = {
  NOMINAL: 'ok', DEGRADED: 'stale', ATTENTION: 'warn',
  CRITICAL: 'critical', MAINTENANCE: 'maintenance',
};

function renderHealth(data) {
  // Strip verdict (largest element).
  const verdict = (data && data.verdict) || 'DEGRADED';
  const vEl = document.getElementById('mc-verdict');
  if (vEl) {
    const st = VERDICT_STATE[verdict] || 'stale';
    vEl.textContent = verdict;
    vEl.style.color = ladder(st).color;
    vEl.dataset.glyph = ladder(st).glyph;
    const g = document.getElementById('mc-verdict-glyph');
    if (g) { g.textContent = ladder(st).glyph; g.style.color = ladder(st).color; }
  }
  // Last-beat-age.
  const ageEl = document.getElementById('mc-beat-age');
  if (ageEl) ageEl.textContent = data ? _ageStr(data.last_beat || data.checked_at) : '—';

  // Freshness chip: pulse once on success.
  const chip = document.getElementById('mc-fresh');
  if (chip) {
    chip.classList.remove('reconnecting');
    chip.textContent = 'LIVE';
    _pulse = !_pulse;
    chip.classList.remove('pulse');
    // force reflow to retrigger the one-shot pulse animation
    void chip.offsetWidth;
    chip.classList.add('pulse');
  }

  // Pills.
  const pills = (data && data.pills) || {};
  PILL_ORDER.forEach(name => {
    const el = document.querySelector(`.mc-pill[data-pill="${name}"]`);
    if (!el) return;
    const st = pills[name] || 'stale';
    const meta = ladder(st);
    el.style.borderColor = meta.color;
    el.style.color = meta.color;
    el.title = `${name}: ${meta.word}`;
    const g = el.querySelector('.mc-pill-glyph');
    if (g) g.textContent = meta.glyph;
  });
}

function renderHealthError() {
  const chip = document.getElementById('mc-fresh');
  if (chip) {
    chip.classList.remove('pulse');
    chip.classList.add('reconnecting');
    chip.textContent = 'RECONNECTING';
  }
}

async function pollHealth() {
  try {
    const data = await api('GET', '/api/health');
    renderHealth(data);
  } catch (e) {
    renderHealthError();
  }
}

function startHealthPoll() {
  if (_pollTimer) clearInterval(_pollTimer);
  pollHealth();
  _pollTimer = setInterval(pollHealth, 10000);
}

// ════════════════════════════════════════════════════════════════════════
//  ACTIVITY / AUDIT FEED  (SSE, newest-first, collapsible)
// ════════════════════════════════════════════════════════════════════════
let _es = null;

const _SEV_RE = {
  critical: /\b(crit|critical|fatal|error|fail)/i,
  warn:     /\b(warn|degraded|retry|timeout)/i,
  ok:       /\b(ok|done|nominal|recovered|started)/i,
};
function _severity(line) {
  if (_SEV_RE.critical.test(line)) return 'critical';
  if (_SEV_RE.warn.test(line)) return 'warn';
  if (_SEV_RE.ok.test(line)) return 'ok';
  return 'stale';
}

function _appendActivity(line) {
  const list = document.getElementById('mc-activity-list');
  if (!list || !line) return;
  const row = document.createElement('div');
  row.className = 'mc-activity-row sev-' + _severity(line);
  row.textContent = line;
  list.insertBefore(row, list.firstChild);   // newest-first
  // cap at 300 rows
  while (list.childNodes.length > 300) list.removeChild(list.lastChild);
}

function _startActivityStream() {
  _stopActivityStream();
  // EventSource can't send Authorization headers, so pass the token as a
  // query param (the server accepts bearer; for the SSE route the shell uses
  // the same token — but EventSource has no header support, so we fall back to
  // a manual fetch-reader to keep auth in the header).
  _startActivityFetchReader();
}

let _activityAbort = null;
async function _startActivityFetchReader() {
  _activityAbort = new AbortController();
  try {
    const r = await fetch('/api/activity/stream', {
      headers: { 'Authorization': 'Bearer ' + TOKEN },
      signal: _activityAbort.signal,
    });
    if (!r.ok || !r.body) return;
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        chunk.split('\n').forEach(l => {
          if (l.startsWith('data:')) _appendActivity(l.slice(5).trim());
        });
      }
    }
  } catch (e) {
    // disconnected / aborted — the health poll surfaces RECONNECTING.
  }
}

function _stopActivityStream() {
  if (_activityAbort) { try { _activityAbort.abort(); } catch (_) {} _activityAbort = null; }
  if (_es) { try { _es.close(); } catch (_) {} _es = null; }
}

export function toggleActivity() {
  const feed = document.getElementById('mc-activity');
  if (feed) feed.classList.toggle('collapsed');
}

// ── Init / boot ─────────────────────────────────────────────────────────────
export function init() {
  buildShell();
  buildRail();
  renderTiles();
  showPanel('overview');
  startHealthPoll();
  startTileRefresh();
  _startActivityStream();
}

// ── Expose for inline onclick= handlers still in index.html (curiosity) ─────
// Panels bind their own handlers onto window at import time. The shell binds
// only the shell-level entry points here.
Object.assign(window, {
  saveToken, logout, showPanel, toggleActivity,
});

// ── Panel manifest (the ONLY shared append-point) ───────────────────────────
import './panels/index.js';

// ── Boot ────────────────────────────────────────────────────────────────────
document.getElementById('token-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') saveToken();
});

if (TOKEN) { modal.style.display = 'none'; init(); }
else        { modal.style.display = 'flex'; }
