// Watchdog & Budget panel — active alerts + recent history, lane budget gauges,
// and token cost aggregated from trajectory usage events.
// Self-registers via side-effect import (per PANELS.md). No shared-file edits here.
// Uses components: ladder (glyph/word/color), barGauge (ceilings vs counts), esc.
import { api, ctrl, registerPanel, registerTile } from '../app.js';
import { barGauge, ladder, esc } from '../components.js';

let _data = null;

function _fmtPct(used, ceil) {
  if (ceil == null || ceil <= 0) return '—';
  const p = Math.max(0, Math.min(100, Math.round((used / ceil) * 100)));
  return `${p}%`;
}

function _renderAlerts(root, alerts, history, mutes, acks) {
  const box = root.querySelector('#wd-alerts');
  if (!box) return;
  if (!alerts || !alerts.length) {
    box.innerHTML = `<div class="muted">No active alerts. <span class="mc-na">—</span></div>`;
  } else {
    const rows = alerts.map(a => {
      const m = ladder(a.severity || 'warn');
      const key = a.subsystem;
      const muted = mutes && mutes[key] && (mutes[key] > (Date.now()/1000|0));
      const acked = acks && acks[key];
      const chips = []
        + (muted ? ` <span class="tag" style="background:#2a1e1e;color:#a44">muted</span>` : '')
        + (acked ? ` <span class="tag" style="background:#1e2a1e;color:#4fa">acked</span>` : '');
      const actions = `
        <button class="btn btn-sm" data-ack="${esc(key)}" style="margin-left:6px">Ack</button>
        <select class="btn btn-sm" data-mute="${esc(key)}" style="margin-left:4px">
          <option value="">Mute ▾</option>
          <option value="60">1h</option>
          <option value="240">4h</option>
          <option value="1440">24h</option>
        </select>`;
      return `<div class="mc-activity-row sev-${esc(a.severity || 'warn')}" style="border-left-color:${m.color}">
        <span style="color:${m.color}">${m.glyph}</span>
        <b>${esc(a.subsystem)}</b>: ${esc(a.message)}
        ${chips}
        ${actions}
      </div>`;
    }).join('');
    box.innerHTML = rows;

    // Wire Ack/Mute controls (delegated; one-time per render)
    box.querySelectorAll('button[data-ack]').forEach(b => {
      if (b._wired) return; b._wired = true;
      b.onclick = async () => {
        const k = b.getAttribute('data-ack');
        try { await ctrl('POST', '/api/ctrl/watchdog/ack', { key: k, confirm: true }); } catch (_) {}
        // Refresh the panel to pick up the ack in the feed.
        const el = document.getElementById('panel-watchdog');
        if (el) { try { await refresh(el); } catch (_) {} }
      };
    });
    box.querySelectorAll('select[data-mute]').forEach(sel => {
      if (sel._wired) return; sel._wired = true;
      sel.onchange = async () => {
        const k = sel.getAttribute('data-mute');
        const mins = parseInt(sel.value || '0', 10);
        if (!mins) { sel.value = ''; return; }
        try { await ctrl('POST', '/api/ctrl/watchdog/mute', { key: k, minutes: mins, confirm: true }); } catch (_) {}
        sel.value = '';
        const el = document.getElementById('panel-watchdog');
        if (el) { try { await refresh(el); } catch (_) {} }
      };
    });
  }

  const hist = root.querySelector('#wd-history');
  if (hist) {
    if (!history || !history.length) {
      hist.textContent = '(no recent events)';
    } else {
      hist.textContent = history.slice(-30).join('\n');
    }
  }
}

function _renderBudget(root, budget) {
  const box = root.querySelector('#wd-budget');
  if (!box) return;
  const cfg = (budget && budget.config) || {};
  const st  = (budget && budget.state)  || {};
  const counts = (st && st.counts) || {};
  const ceilings = (cfg && cfg.ceilings) || {};
  const lanes = Object.keys({ ...ceilings, ...counts });

  if (!lanes.length) {
    box.innerHTML = `<div class="muted">No budget config/state. <span class="mc-na">—</span></div>`;
    return;
  }
  const html = lanes.sort().map(lane => {
    const ceil = ceilings[lane];
    const used = counts[lane] || 0;
    const state = (ceil != null && used >= ceil) ? 'warn' : 'ok';
    const label = `${lane} ${used}/${ceil != null ? ceil : '∞'}`;
    return barGauge(ceil != null ? used : 0, ceil != null ? ceil : Math.max(1, used || 1), { state, label });
  }).join('');
  const gcap = cfg.global_self_directed;
  const gused = Object.keys(ceilings).reduce((s, k) => s + (counts[k] || 0), 0);
  const gline = gcap != null
    ? `<div class="muted" style="margin-top:6px">global self-directed: ${gused}/${gcap} ${gcap ? '(' + _fmtPct(gused, gcap) + ')' : ''}</div>`
    : '';
  box.innerHTML = html + gline;
}

function _renderTokens(root, tokens) {
  const box = root.querySelector('#wd-tokens');
  if (!box) return;
  if (!tokens || !tokens.totals) {
    box.innerHTML = `<div class="muted">No token data. <span class="mc-na">—</span></div>`;
    return;
  }
  const t = tokens.totals;
  const lanes = tokens.by_lane || {};
  const rows = Object.keys(lanes).sort().map(l => {
    const c = lanes[l];
    return `<tr>
      <td><span class="tag">${esc(l)}</span></td>
      <td style="text-align:right">${c.calls || 0}</td>
      <td style="text-align:right">${(c.input||0).toLocaleString()}</td>
      <td style="text-align:right">${(c.output||0).toLocaleString()}</td>
      <td style="text-align:right">${(c.total||0).toLocaleString()}</td>
    </tr>`;
  }).join('');
  box.innerHTML = `
    <div style="margin:6px 0 4px;color:var(--muted);font-size:11px;letter-spacing:.05em">TOKENS (from trajectory usage)</div>
    <div style="font-size:12px;margin-bottom:4px">
      calls ${t.calls || 0}
      · input ${ (t.input||0).toLocaleString() }
      · output ${ (t.output||0).toLocaleString() }
      · total ${ (t.total||0).toLocaleString() }
    </div>
    <table style="font-size:12px"><thead>
      <tr><th>lane</th><th style="text-align:right">calls</th><th style="text-align:right">input</th><th style="text-align:right">output</th><th style="text-align:right">total</th></tr>
    </thead><tbody>${rows || `<tr><td colspan="5" class="muted">—</td></tr>`}</tbody></table>
    <div class="muted" style="margin-top:4px">as of ${esc(tokens.as_of || '—')}</div>
  `;
}

async function refresh(root) {
  try {
    const d = await api('GET', '/api/watchdog');
    _data = d;
    _renderAlerts(root, d && d.alerts, d && d.history, d && d.mc_mutes, d && d.mc_acks);
    _renderBudget(root, d && d.budget);
    // tokens are separate (expensive, cached server-side)
    try {
      const tk = await api('GET', '/api/budget/tokens');
      _renderTokens(root, tk);
    } catch (e) {
      const tb = root.querySelector('#wd-tokens');
      if (tb) tb.innerHTML = `<div class="muted">Token fetch failed. <span class="mc-na">—</span></div>`;
    }
  } catch (e) {
    const box = root.querySelector('#wd-alerts');
    if (box) box.innerHTML = `<div class="muted">Watchdog unavailable. <span class="mc-na">—</span></div>`;
  }
}

const MARKUP = `
  <div class="panel-hdr"><h2>WATCHDOG &amp; BUDGET</h2></div>
  <div class="panel-body">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div>
        <div style="color:var(--muted);font-size:11px;letter-spacing:.05em;margin-bottom:6px">ACTIVE ALERTS</div>
        <div id="wd-alerts" style="background:var(--input);border:1px solid var(--border);border-radius:4px;padding:8px;min-height:120px"></div>
        <div style="color:var(--muted);font-size:11px;letter-spacing:.05em;margin:10px 0 4px">RECENT HISTORY (tail)</div>
        <pre id="wd-history" style="background:var(--input);border:1px solid var(--border);border-radius:4px;padding:8px;min-height:120px;overflow:auto;white-space:pre-wrap"></pre>
      </div>
      <div>
        <div style="color:var(--muted);font-size:11px;letter-spacing:.05em;margin-bottom:6px">LANE BUDGET (ceilings vs counts)</div>
        <div id="wd-budget" style="background:var(--input);border:1px solid var(--border);border-radius:4px;padding:8px;min-height:120px"></div>
        <div style="color:var(--muted);font-size:11px;letter-spacing:.05em;margin:10px 0 4px">TOKEN USAGE (aggregated)</div>
        <div id="wd-tokens" style="background:var(--input);border:1px solid var(--border);border-radius:4px;padding:8px;min-height:160px"></div>
      </div>
    </div>
    <div class="form-actions mt8">
      <button class="btn btn-sm" onclick="refreshWatchdog()">Refresh</button>
    </div>
  </div>
`;

function mount(el) {
  if (!el.dataset.built) { el.innerHTML = MARKUP; el.dataset.built = '1'; }
  refresh(el);
}

registerPanel({
  id: 'watchdog',
  title: 'Watchdog & Budget',
  group: 'System',
  render: mount,
  refreshMs: 30000,
});

registerTile({
  id: 'watchdog',
  refreshMs: 30000,
  async render(el) {
    let summary = null;
    let state = 'stale';
    try {
      const d = await api('GET', '/api/watchdog');
      const alerts = (d && d.alerts) || [];
      const crit = alerts.filter(a => a.severity === 'critical').length;
      const warn = alerts.filter(a => a.severity === 'warn').length;
      const b = (d && d.budget) || {};
      const counts = (b.state && b.state.counts) || {};
      const ceilings = (b.config && b.config.ceilings) || {};
      const lanes = Object.keys(ceilings).map(l => `${l}:${counts[l]||0}/${ceilings[l]!=null?ceilings[l]:'∞'}`).join(' ');
      summary = `${alerts.length} alerts${crit ? ' ('+crit+' critical)' : ''}${warn ? ' '+warn+'w' : ''} · ${lanes || 'budget —'}`;
      state = crit ? 'critical' : (warn || alerts.length ? 'warn' : (alerts.length === 0 ? 'ok' : 'stale'));
    } catch (e) { summary = null; state = 'stale'; }
    el.innerHTML = `<div class="mc-stat-tile">
      <div class="mc-stat-value">${summary ? esc(summary) : '<span class="mc-na">—</span>'}</div>
      <div class="mc-stat-foot"><span class="mc-stat-label">Watchdog · alerts + budget</span></div>
    </div>`;
    // tint border by state
    const meta = ladder(state);
    el.style.borderColor = meta.color;
  },
});

export async function refreshWatchdog() {
  // Find the active watchdog panel container and refresh it.
  const el = document.getElementById('panel-watchdog');
  if (el) { await refresh(el); }
}
Object.assign(window, { refreshWatchdog });
